"""抖音视频/合集下载核心模块

流程：
1. 解析分享短链接 → 获取真实 URL（合集 / 单视频）
2. 读取 config/config.yaml 配置
3. 通过 f2 的 TokenManager 获取匿名 ttwid（无需登录）
4. 调用 f2 的 DouyinCrawler + a_bogus 签名请求合集 / 视频接口
5. 提取视频列表（序号、标题、下载地址）
6. 用 httpx 流式下载无水印 mp4 文件
   - 合集：创建子目录 YYYYMMDD_合集名，视频间间隔 mix_download_interval 秒
   - 单视频：直接下载到输出目录
7. 可选：保存元数据（封面/文案/原声/JSON）
8. 可选：记录下载进度到 SQLite，支持断点续传与增量下载
"""

import asyncio
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml

# f2 底层组件：crawler / model / filter / token
from f2.apps.douyin.crawler import DouyinCrawler
from f2.apps.douyin.model import UserMix, PostDetail
from f2.apps.douyin.filter import UserMixFilter, PostDetailFilter
from f2.apps.douyin.utils import TokenManager

# ── 配置加载 ────────────────────────────────────────────────────

# 配置文件默认路径（项目根目录下 config/config.yaml）
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "config.yaml"


class Config:
    """从 config.yaml 加载的配置对象。

    属性与 config.yaml 中的字段一一对应，提供默认值兜底。
    """

    # HTTP 相关
    user_agent: str
    timeout: int
    max_retries: int
    max_tasks: int
    max_connections: int

    # API 相关
    page_counts: int
    api_request_interval: float

    # 下载相关
    mix_download_interval: int
    chunk_size: int
    filename_max_len: int

    # 元数据保存
    save_metadata: bool
    save_cover: bool
    save_desc: bool
    save_music: bool
    save_json: bool

    # 进度持久化
    enable_progress: bool
    progress_db_path: str

    # 输出目录
    output_dir: str

    def __init__(self, config_path: Optional[Path] = None):
        path = config_path or DEFAULT_CONFIG_PATH
        data = {}
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

        # 逐项加载，缺失时使用默认值
        self.user_agent = data.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        )
        self.timeout = int(data.get("timeout", 15))
        self.max_retries = int(data.get("max_retries", 5))
        self.max_tasks = int(data.get("max_tasks", 5))
        self.max_connections = int(data.get("max_connections", 5))

        self.page_counts = int(data.get("page_counts", 20))
        self.api_request_interval = float(data.get("api_request_interval", 2.0))

        self.mix_download_interval = int(data.get("mix_download_interval", 60))
        self.chunk_size = int(data.get("chunk_size", 65536))
        self.filename_max_len = int(data.get("filename_max_len", 60))

        # 元数据保存：总开关默认关闭，子开关默认开启
        self.save_metadata = bool(data.get("save_metadata", False))
        self.save_cover = bool(data.get("save_cover", True))
        self.save_desc = bool(data.get("save_desc", True))
        self.save_music = bool(data.get("save_music", True))
        self.save_json = bool(data.get("save_json", True))

        # 进度持久化：默认启用
        self.enable_progress = bool(data.get("enable_progress", True))
        self.progress_db_path = data.get("progress_db_path", ".douyindl/progress.db")

        self.output_dir = data.get("output_dir", "./downloads")

    @property
    def default_headers(self) -> Dict[str, str]:
        """构造默认请求头（含 UA 与 Referer）。"""
        return {
            "User-Agent": self.user_agent,
            "Referer": "https://www.douyin.com/",
        }


# ── 合集 / 单视频 URL 特征 ─────────────────────────────────────

_MIX_PATH_PATTERN = re.compile(r"/(?:share/mix/detail|collection)/(\d+)")
_AWEME_ID_PATTERN = re.compile(r"/video/(\d+)")


# ── 链接解析 ────────────────────────────────────────────────────

async def resolve_share_url(share_url: str, config: Config) -> Tuple[str, str]:
    """解析抖音分享短链接，返回 (资源类型, 资源ID)。

    资源类型: 'mix'（合集）或 'one'（单视频）。

    支持的输入：
      - 短链 https://v.douyin.com/SlGTwuMq498/
      - 合集页 https://www.iesdouyin.com/share/mix/detail/{id}/
      - 合集页 https://www.douyin.com/collection/{id}
      - 单视频 https://www.douyin.com/video/{id}
    """
    url = share_url.strip()

    # 先尝试从 URL 本身直接匹配（已是长链接的情况）
    m = _MIX_PATH_PATTERN.search(url)
    if m:
        return "mix", m.group(1)
    m = _AWEME_ID_PATTERN.search(url)
    if m:
        return "one", m.group(1)

    # 短链需要跟随重定向
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": config.user_agent},
        proxy=None,
        timeout=config.timeout,
    ) as client:
        resp = await client.get(url)
        final_url = str(resp.url)

    # iesdouyin 合集页重定向到 douyin.com/collection/{id}
    m = _MIX_PATH_PATTERN.search(final_url)
    if m:
        return "mix", m.group(1)
    m = _AWEME_ID_PATTERN.search(final_url)
    if m:
        return "one", m.group(1)

    # iesdouyin 合集页路径格式 /share/mix/detail/{id}/
    m = re.search(r"/share/mix/detail/(\d+)", final_url)
    if m:
        return "mix", m.group(1)
    # iesdouyin 单视频 /share/video/{id}/
    m = re.search(r"/share/video/(\d+)", final_url)
    if m:
        return "one", m.group(1)

    raise ValueError(f"无法从链接解析资源类型: {share_url} → {final_url}")


# ── Token 获取 ──────────────────────────────────────────────────

def build_cookie() -> str:
    """生成匿名 cookie（ttwid + 伪 msToken），无需登录。"""
    ttwid = TokenManager.gen_ttwid()
    ms_token = TokenManager.gen_false_msToken()
    return f"ttwid={ttwid}; msToken={ms_token}"


def build_crawler_kwargs(cookie: str, config: Config) -> Dict[str, Any]:
    """构造 DouyinCrawler 所需的 kwargs。"""
    return {
        "headers": config.default_headers,
        "cookie": cookie,
        "proxies": {"http://": None, "https://": None},
        "max_tasks": config.max_tasks,
        "max_connections": config.max_connections,
        "max_retries": config.max_retries,
        "timeout": config.timeout,
    }


# ── 合集视频列表获取 ────────────────────────────────────────────

def _extract_mix_name(response: Any) -> str:
    """从合集 API 响应中提取合集名称。

    合集 API 返回结构：response.aweme_list[0].mix_info.mix_name
    """
    try:
        data = response.json() if hasattr(response, "json") else response
        if isinstance(data, dict):
            aweme_list = data.get("aweme_list") or []
            if aweme_list:
                mix_info = aweme_list[0].get("mix_info") or {}
                return mix_info.get("mix_name") or ""
    except Exception:
        pass
    return ""


async def fetch_mix_videos(
    mix_id: str,
    config: Config,
    max_counts: int = 0,
) -> Tuple[str, List[Dict[str, Any]]]:
    """获取合集内全部视频列表。

    Args:
        mix_id: 合集 ID
        config: 配置对象
        max_counts: 最大获取数量，0 表示不限

    Returns:
        (合集名, 视频信息字典列表)
    """
    cookie = build_cookie()
    kwargs = build_crawler_kwargs(cookie, config)

    limit = max_counts if max_counts > 0 else float("inf")
    cursor = 0
    collected: List[Dict[str, Any]] = []
    mix_name = ""

    while len(collected) < limit:
        current_size = min(config.page_counts, limit - len(collected))
        async with DouyinCrawler(kwargs) as crawler:
            params = UserMix(
                cursor=cursor, count=current_size, mix_id=mix_id
            )
            response = await crawler.fetch_user_mix(params)

        # 从第一页响应中提取合集名
        if not mix_name:
            mix_name = _extract_mix_name(response)

        mix = UserMixFilter(response)
        page_items = mix._to_list()
        if not page_items:
            break

        collected.extend(page_items)
        cursor = mix.max_cursor or 0

        if not mix.has_more:
            break

        # 避免请求过于频繁
        await asyncio.sleep(config.api_request_interval)

    return mix_name, collected


# ── 单视频信息获取 ──────────────────────────────────────────────

async def fetch_one_video(aweme_id: str, config: Config) -> Dict[str, Any]:
    """获取单个视频的详细信息（含无水印下载地址）。

    注意：PostDetailFilter 只有 _to_dict() 方法（返回字典），
    没有 _to_list()，与 UserMixFilter 不同。
    """
    cookie = build_cookie()
    kwargs = build_crawler_kwargs(cookie, config)

    async with DouyinCrawler(kwargs) as crawler:
        params = PostDetail(aweme_id=aweme_id)
        response = await crawler.fetch_post_detail(params)
        video = PostDetailFilter(response)

    # PostDetailFilter._to_dict() 返回单个视频字典
    item = video._to_dict()
    if not item:
        raise ValueError(f"未获取到视频信息，aweme_id={aweme_id}")
    return item


# ── 视频下载 ────────────────────────────────────────────────────

def _sanitize_filename(name: str, max_len: int = 60) -> str:
    """清理文件名中的非法字符，去除 #话题 标签。"""
    # 去除 #话题 标签（#后跟非空白字符）
    name = re.sub(r"#[^\s#]+", "", name)
    # 去除文件名非法字符
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "", name).strip()
    # 合并多余下划线/空格
    name = re.sub(r"[_\s]+", "_", name).strip("_")
    if len(name) > max_len:
        name = name[:max_len].strip("_")
    return name or "untitled"


async def download_video(
    video_url: str,
    save_path: Path,
    config: Config,
    headers: Optional[Dict[str, str]] = None,
) -> int:
    """流式下载视频文件。

    Args:
        video_url: 无水印视频下载地址
        save_path: 保存路径（含文件名）
        config: 配置对象（使用 chunk_size / timeout / user_agent）
        headers: 额外请求头

    Returns:
        下载的字节数
    """
    if not video_url:
        raise ValueError("视频下载地址为空")

    # 抖音视频地址可能缺 https: 前缀
    if video_url.startswith("//"):
        video_url = "https:" + video_url

    save_path.parent.mkdir(parents=True, exist_ok=True)
    req_headers = config.default_headers | (headers or {})

    downloaded = 0
    total = 0
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers=req_headers,
        proxy=None,
        timeout=60,
    ) as client:
        async with client.stream("GET", video_url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            with open(save_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=config.chunk_size):
                    f.write(chunk)
                    downloaded += len(chunk)
                    _print_progress(downloaded, total)

    # 进度条换行
    if total:
        print()
    return downloaded


def _print_progress(downloaded: int, total: int) -> None:
    """在终端打印简单的下载进度条。"""
    if total <= 0:
        return
    pct = min(downloaded * 100 // total, 100)
    bar_len = 30
    filled = bar_len * pct // 100
    bar = "=" * filled + "-" * (bar_len - filled)
    sys.stdout.write(f"\r  [{bar}] {pct:3d}% ")
    sys.stdout.flush()


# ── 元数据下载 ──────────────────────────────────────────────────

async def _download_simple(
    url: str,
    save_path: Path,
    config: Config,
) -> int:
    """下载小文件（封面/音乐），无进度条，一次性写入。

    与 download_video 的区别：不打印进度条，适用于体积较小的元数据文件。
    """
    if not url:
        return 0
    # 抖音 CDN 地址可能缺 https: 前缀
    if url.startswith("//"):
        url = "https:" + url

    save_path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers=config.default_headers,
        proxy=None,
        timeout=60,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        save_path.write_bytes(resp.content)
    return len(resp.content)


async def download_metadata(
    video_data: Dict[str, Any],
    base_path: Path,
    config: Config,
) -> None:
    """保存视频元数据（封面/文案/原声/JSON）。

    仅当 config.save_metadata=True 时执行，文件与视频同名但扩展名不同：
      - 封面: base_path.jpg
      - 文案: base_path.txt
      - 原声: base_path.mp3
      - 信息: base_path.json

    Args:
        video_data: 经 f2 filter 过滤后的视频信息字典
        base_path: 不含扩展名的完整路径（如 .../001_文案）
        config: 配置对象
    """
    if not config.save_metadata:
        return

    # 封面图
    if config.save_cover:
        cover_url = video_data.get("cover")
        if cover_url:
            try:
                await _download_simple(cover_url, base_path.with_suffix(".jpg"), config)
            except Exception as e:
                print(f"      封面下载失败: {e}")

    # 文案全文
    if config.save_desc:
        desc = video_data.get("desc") or ""
        if desc:
            base_path.with_suffix(".txt").write_text(desc, encoding="utf-8")

    # 原声 MP3
    if config.save_music:
        # music_status=1 表示原声可用
        if video_data.get("music_status") == 1:
            music_url = video_data.get("music_play_url")
            if music_url:
                try:
                    await _download_simple(music_url, base_path.with_suffix(".mp3"), config)
                except Exception as e:
                    print(f"      原声下载失败: {e}")

    # 完整视频信息 JSON
    if config.save_json:
        json_path = base_path.with_suffix(".json")
        # default=str 兜底处理不可序列化对象（如时间）
        json_path.write_text(
            json.dumps(video_data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )


# ── 进度持久化 ──────────────────────────────────────────────────

class ProgressDB:
    """基于 SQLite 的下载进度数据库，支持断点续传与增量下载。

    表结构：
        downloaded_videos(
            aweme_id        TEXT PRIMARY KEY,  -- 视频 ID
            resource_type   TEXT NOT NULL,     -- 'mix' 或 'one'
            resource_id     TEXT NOT NULL,     -- mix_id 或 aweme_id
            mix_name        TEXT,              -- 合集名（单视频为 NULL）
            desc            TEXT,              -- 视频文案
            file_path       TEXT,              -- 保存路径
            downloaded_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )

    用法:
        with ProgressDB(Path(".douyindl/progress.db")) as db:
            if db.is_downloaded("123456"):
                print("已下载")
            db.record("123456", "mix", "mix_id_xxx", "合集名", "文案", "/path/to.mp4")
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> "ProgressDB":
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS downloaded_videos (
                aweme_id        TEXT PRIMARY KEY,
                resource_type   TEXT NOT NULL,
                resource_id     TEXT NOT NULL,
                mix_name        TEXT,
                desc            TEXT,
                file_path       TEXT,
                downloaded_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 为 resource_id 建索引，加速合集场景下查询已下载视频
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_resource_id ON downloaded_videos(resource_id)"
        )
        self._conn.commit()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def is_downloaded(self, aweme_id: str) -> bool:
        """查询某视频是否已记录为下载成功。

        注意：仅检查数据库记录是否存在，不验证文件是否还在磁盘上。
        文件存在性由调用方在跳过逻辑中额外判断。
        """
        if not self._conn:
            return False
        cur = self._conn.execute(
            "SELECT 1 FROM downloaded_videos WHERE aweme_id = ?", (aweme_id,)
        )
        return cur.fetchone() is not None

    def record(
        self,
        aweme_id: str,
        resource_type: str,
        resource_id: str,
        mix_name: str,
        desc: str,
        file_path: str,
    ) -> None:
        """记录一条下载成功记录（已存在则更新）。"""
        if not self._conn:
            return
        self._conn.execute(
            """INSERT INTO downloaded_videos
               (aweme_id, resource_type, resource_id, mix_name, desc, file_path)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(aweme_id) DO UPDATE SET
                   resource_type=excluded.resource_type,
                   resource_id=excluded.resource_id,
                   mix_name=excluded.mix_name,
                   desc=excluded.desc,
                   file_path=excluded.file_path,
                   downloaded_at=CURRENT_TIMESTAMP
            """,
            (aweme_id, resource_type, resource_id, mix_name, desc, file_path),
        )
        self._conn.commit()

    def count_by_resource(self, resource_id: str) -> int:
        """统计某合集/单视频已下载的视频数。"""
        if not self._conn:
            return 0
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM downloaded_videos WHERE resource_id = ?",
            (resource_id,),
        )
        return cur.fetchone()[0]


# ── 主流程 ──────────────────────────────────────────────────────

class DouyinDownloader:
    """抖音视频/合集下载器。

    用法:
        dl = DouyinDownloader()
        asyncio.run(dl.run("https://v.douyin.com/SlGTwuMq498/"))
    """

    def __init__(
        self,
        output_dir: Optional[str] = None,
        max_counts: int = 0,
        config: Optional[Config] = None,
        force: bool = False,
    ):
        self.config = config or Config()
        # output_dir 优先用参数，其次用配置文件
        self.output_dir = Path(output_dir) if output_dir else Path(self.config.output_dir)
        self.max_counts = max_counts
        # force=True 时忽略进度数据库记录，强制重新下载
        self.force = force

    def _get_progress_db(self) -> Optional[ProgressDB]:
        """根据配置获取进度数据库实例（未启用时返回 None）。

        数据库路径相对于当前工作目录解析（CLI 运行时 cwd 即项目根）。
        """
        if not self.config.enable_progress:
            return None
        db_path = Path(self.config.progress_db_path)
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        return ProgressDB(db_path)

    async def run(self, share_url: str) -> List[Dict[str, Any]]:
        """主入口：解析链接 → 获取视频列表 → 下载。

        命名规则：
        - 合集：创建子目录 YYYYMMDD_合集名，内部文件 001_文案.mp4
        - 单视频：直接下载到 output_dir，文件名 YYYYMMDD_文案.mp4
        - 合集内视频间间隔 mix_download_interval 秒，单视频无需等待

        进度持久化（启用时）：
        - 下载前查询 aweme_id 是否已记录，已记录且文件存在则跳过（断点续传）
        - 下载成功后写入记录，支持下次增量下载

        元数据保存（save_metadata=True 时）：
        - 每个视频旁额外生成 .jpg/.txt/.mp3/.json

        Returns:
            已下载视频的信息列表
        """
        cfg = self.config
        # 下载当日日期，用于目录名 / 单视频文件名前缀
        date_str = datetime.now().strftime("%Y%m%d")

        print(f"[1/4] 解析分享链接: {share_url}")
        kind, resource_id = await resolve_share_url(share_url, cfg)

        # mix_name 在两种场景下都需初始化，用于进度记录
        mix_name = ""
        if kind == "mix":
            print(f"      检测到合集链接, mix_id={resource_id}")
            mix_name, videos = await fetch_mix_videos(
                resource_id,
                config=cfg,
                max_counts=self.max_counts,
            )
            if mix_name:
                print(f"      合集名称: {mix_name}")

            # 合集：创建子目录 YYYYMMDD_合集名
            safe_name = _sanitize_filename(mix_name, cfg.filename_max_len) or resource_id
            target_dir = self.output_dir / f"{date_str}_{safe_name}"
            download_interval = cfg.mix_download_interval
        else:
            print(f"      检测到单视频链接, aweme_id={resource_id}")
            videos = [await fetch_one_video(resource_id, cfg)]
            # 单视频直接下载到 output_dir
            target_dir = self.output_dir
            download_interval = 0

        print(f"[2/4] 共获取 {len(videos)} 个视频:")
        for i, v in enumerate(videos, 1):
            desc = (v.get("desc") or "").replace("\n", " ")[:40]
            print(f"  {i:>3d}. [{v.get('aweme_id')}] {desc}")

        # 打开进度数据库（上下文管理器确保连接关闭）
        db = self._get_progress_db()
        db_ctx = db if db is not None else _NullContext()

        print(f"[3/4] 开始下载到 {target_dir}/ ...")
        target_dir.mkdir(parents=True, exist_ok=True)
        success = 0
        skipped = 0
        with db_ctx as progress_db:
            for i, v in enumerate(videos, 1):
                aweme_id = v.get("aweme_id", f"unknown_{i}")
                desc = v.get("desc") or ""
                play_addr = v.get("video_play_addr")
                # video_play_addr 可能是列表（多个清晰度地址）
                if isinstance(play_addr, list):
                    play_addr = play_addr[0] if play_addr else None

                if not play_addr:
                    print(f"  [{i}/{len(videos)}] {aweme_id} 无视频地址，跳过")
                    continue

                name = _sanitize_filename(desc, cfg.filename_max_len) or aweme_id
                # 合集内文件：001_文案.mp4（目录已含日期，无需重复）
                # 单视频文件：YYYYMMDD_文案.mp4
                if kind == "mix":
                    save_path = target_dir / f"{i:03d}_{name}.mp4"
                else:
                    save_path = target_dir / f"{date_str}_{name}.mp4"

                # 跳过判断：文件已存在 或 进度数据库已记录
                # force=True 时跳过此检查，强制重新下载
                if not self.force:
                    if save_path.exists():
                        print(f"  [{i}/{len(videos)}] {save_path.name} 文件已存在，跳过")
                        success += 1
                        skipped += 1
                        continue
                    if progress_db and progress_db.is_downloaded(aweme_id):
                        print(f"  [{i}/{len(videos)}] {save_path.name} 进度记录已存在，跳过")
                        skipped += 1
                        continue

                print(f"  [{i}/{len(videos)}] 下载 {save_path.name}")
                try:
                    await download_video(play_addr, save_path, cfg)
                    success += 1

                    # 下载元数据（封面/文案/原声/JSON）
                    if cfg.save_metadata:
                        # base_path 不含扩展名，用于生成 .jpg/.txt/.mp3/.json
                        base_path = save_path.with_suffix("")
                        await download_metadata(v, base_path, cfg)

                    # 记录到进度数据库
                    if progress_db:
                        progress_db.record(
                            aweme_id=str(aweme_id),
                            resource_type=kind,
                            resource_id=resource_id,
                            mix_name=mix_name,
                            desc=desc[:200],
                            file_path=str(save_path),
                        )
                except Exception as e:
                    print(f"      下载失败: {e}")

                # 合集场景：视频间间隔等待（最后一个不需要）
                if download_interval and i < len(videos):
                    print(f"      等待 {download_interval} 秒后继续下载下一个...")
                    await asyncio.sleep(download_interval)

        # 统计输出
        print(f"\n[4/4] 完成: 成功 {success}/{len(videos)}，跳过 {skipped}")
        print(f"      保存目录: {target_dir}/")
        if progress_db and kind == "mix":
            total = progress_db.count_by_resource(resource_id)
            print(f"      合集 {resource_id} 累计已下载 {total} 个视频（含历史记录）")
        return videos


class _NullContext:
    """空上下文管理器，用于进度数据库未启用时保持 with 语法统一。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass


def main():
    """CLI 入口：python -m douyindl <分享链接> [输出目录]"""
    import argparse

    parser = argparse.ArgumentParser(
        description="抖音视频/合集下载工具（支持短链分享文本）"
    )
    parser.add_argument(
        "url",
        help="抖音分享链接（短链或完整URL，可包含分享文本）",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="视频保存目录（默认使用 config.yaml 中的 output_dir）",
    )
    parser.add_argument(
        "-n", "--max-counts",
        type=int, default=0,
        help="最大下载视频数，0 表示不限",
    )
    parser.add_argument(
        "-c", "--config",
        default=None,
        help="配置文件路径（默认 config/config.yaml）",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="强制重新下载，忽略进度数据库记录",
    )
    parser.add_argument(
        "-m", "--metadata",
        action="store_true",
        help="保存元数据（封面/文案/原声/JSON），覆盖 config.yaml 中的 save_metadata 设置",
    )
    args = parser.parse_args()

    # 从分享文本中提取 URL
    url_match = re.search(r'https?://[^\s，,]+', args.url)
    if not url_match:
        print("错误: 未在输入中找到有效的 URL", file=sys.stderr)
        sys.exit(1)
    url = url_match.group(0)

    # 加载配置
    config_path = Path(args.config) if args.config else None
    config = Config(config_path)

    # CLI 参数覆盖配置文件
    if args.metadata:
        config.save_metadata = True

    dl = DouyinDownloader(
        output_dir=args.output,
        max_counts=args.max_counts,
        config=config,
        force=args.force,
    )
    asyncio.run(dl.run(url))


if __name__ == "__main__":
    main()
