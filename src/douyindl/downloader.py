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
import random
import re
import sqlite3
import sys
import time
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
    download_max_retries: int
    download_retry_interval: float
    max_download_speed: int

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
        # 下载失败时的最大重试次数（不含首次下载），0 表示不重试
        self.download_max_retries = int(data.get("download_max_retries", 3))
        # 下载重试间隔（秒），失败后等待多久再重试
        self.download_retry_interval = float(data.get("download_retry_interval", 5.0))
        # 最大下载速度（字节/秒），0 表示不限速；如 10MB/s = 10485760
        self.max_download_speed = int(data.get("max_download_speed", 0))

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
        每个字典除 filter 返回的字段外，额外补充 _meta 子字典（duration/width/height/
        file_size/bit_rate/fps/ratio/video_format/is_h265 等视频元数据）
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

        # 从原始响应提取每个视频的元数据，补充到 filter 返回的字典中
        aweme_list = response.get("aweme_list", []) if isinstance(response, dict) else []
        for item, aweme in zip(page_items, aweme_list):
            item["_meta"] = _extract_video_meta(aweme)

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

    返回的字典额外包含 _meta 子字典（duration/width/height/file_size/
    bit_rate/fps/ratio/video_format/is_h265 等视频元数据）。
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

    # 从原始响应提取视频元数据
    aweme_detail = response.get("aweme_detail", {}) if isinstance(response, dict) else {}
    item["_meta"] = _extract_video_meta(aweme_detail)
    return item


def _extract_video_meta(aweme: Dict[str, Any]) -> Dict[str, Any]:
    """从 API 原始响应的单个 aweme 节点提取视频元数据。

    f2 的 filter 会丢失 video 对象下的 width/height/data_size 等嵌套字段，
    此函数从原始 JSON 中补充提取，用于写入进度数据库。

    Args:
        aweme: 原始响应中的 aweme_detail 或 aweme_list[i] 节点

    Returns:
        包含视频元数据的字典，字段缺失时为 None
    """
    if not aweme or not isinstance(aweme, dict):
        return {}

    video = aweme.get("video") or {}
    # bit_rate 是列表，取第一个清晰度档位
    bit_rate_list = video.get("bit_rate") or []
    br0 = bit_rate_list[0] if bit_rate_list and len(bit_rate_list) > 0 else {}
    play_addr = br0.get("play_addr") or {}

    # 统计字段在 aweme 根节点下的 statistics
    stats = aweme.get("statistics") or {}

    # 作者昵称在 author 字段下
    author = aweme.get("author") or {}

    # create_time 是 UNIX 时间戳（秒），转字符串
    create_ts = aweme.get("create_time")
    create_time_str = ""
    if create_ts and isinstance(create_ts, (int, float)):
        try:
            create_time_str = datetime.fromtimestamp(
                int(create_ts), tz=datetime.now().astimezone().tzinfo
            ).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError):
            create_time_str = ""

    return {
        "duration": video.get("duration"),          # 毫秒
        "width": video.get("width"),                # 像素
        "height": video.get("height"),              # 像素
        "file_size": play_addr.get("data_size"),    # 字节
        "bit_rate": br0.get("bit_rate"),            # bps
        "fps": br0.get("FPS"),                      # 帧率
        "ratio": video.get("ratio"),                # 分辨率标识，如 "2160p"
        "video_format": video.get("format"),        # 格式，如 "mp4"
        "is_h265": 1 if br0.get("is_h265") else 0,  # 0/1
        "nickname": author.get("nickname"),         # 作者昵称
        "digg_count": stats.get("digg_count"),      # 点赞数
        "comment_count": stats.get("comment_count"),# 评论数
        "share_count": stats.get("share_count"),    # 分享数
        "collect_count": stats.get("collect_count"),# 收藏数
        "create_time": create_time_str,             # 创建时间字符串
    }


# ── 视频下载 ────────────────────────────────────────────────────

def _random_interval(base: float) -> float:
    """在 base*0.9 到 base 之间随机取数，避免固定间隔被风控识别。

    用于合集视频下载间隔、多链接间隔、失败重试间隔等场景，
    将固定间隔改为随机区间，降低被抖音风控识别的概率。

    Args:
        base: 基础间隔秒数（如 config.mix_download_interval）

    Returns:
        随机间隔秒数，范围 [base*0.9, base]；base<=0 时返回 0
    """
    if base <= 0:
        return 0
    return random.uniform(base * 0.9, base)


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


def _extract_topics(desc: str) -> List[str]:
    """从文案中提取 #话题 标签文字（不含分隔符 _/#/空白）。

    例: '#比亚迪_#原创作品' -> ['比亚迪', '原创作品']
    """
    return re.findall(r"#([^\s#_]+)", desc or "")


def _build_video_name(desc: str, aweme_id: str, max_len: int = 60) -> str:
    """生成视频文件名（不含日期前缀与扩展名）。

    规则：
    1. 优先使用去除 #话题 后的真实文案；
    2. 若真实文案为空（整条文案都是话题标签），则拼接话题文字作为名称；
    3. 仍为空则回退 'untitled'；
    4. 末尾追加 _<aweme_id> 保证每个视频文件名唯一，
       避免多个无标题/同名视频被「文件已存在」误判跳过。

    例: '20260804_<base>_<aweme_id>.mp4'
    """
    # 1) 去除 #话题 标签，得到真实文案
    text = re.sub(r"#[^\s#]+", "", desc or "")
    text = re.sub(r'[\\/:*?"<>|\n\r\t]', "", text).strip()
    text = re.sub(r"[_\s]+", "_", text).strip("_")
    # 2) 无真实文案时，用话题文字拼接（如 '比亚迪_原创作品'）
    if not text:
        text = "_".join(_extract_topics(desc)).strip("_")
    # 3) 兜底
    if not text:
        text = "untitled"
    # 4) 拼接 aweme_id（为后缀预留长度预算）
    suffix = f"_{aweme_id}"
    budget = max_len - len(suffix)
    if budget > 0 and len(text) > budget:
        text = text[:budget].strip("_")
    return f"{text}{suffix}"


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
        config: 配置对象（使用 chunk_size / timeout / user_agent / max_download_speed）
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

    # 限速配置：max_download_speed 字节/秒，0 表示不限速
    # 限速原理：每下载一个 chunk 后，按累计字节数计算"期望用时"，
    # 若实际用时小于期望用时（下载太快），sleep 差值以降低速度
    max_speed = config.max_download_speed
    speed_limit_start = time.monotonic() if max_speed > 0 else 0

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

                    # 限速：实际用时小于期望用时则 sleep 差值
                    if max_speed > 0:
                        expected_time = downloaded / max_speed
                        actual_time = time.monotonic() - speed_limit_start
                        if actual_time < expected_time:
                            await asyncio.sleep(expected_time - actual_time)

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
            -- 视频元数据（从 API 原始响应提取，v0.4.0 新增）
            duration        INTEGER,           -- 视频时长（毫秒）
            width           INTEGER,           -- 视频宽度（像素）
            height          INTEGER,           -- 视频高度（像素）
            file_size       INTEGER,           -- 文件大小（字节，来自 API）
            bit_rate        INTEGER,           -- 视频码率（bps）
            fps             REAL,              -- 帧率
            ratio           TEXT,              -- 分辨率标识（如 2160p）
            video_format    TEXT,             -- 视频格式（如 mp4）
            is_h265         INTEGER,           -- 是否 H.265 编码（0/1）
            -- 作者与统计信息（v0.4.0 新增）
            nickname        TEXT,              -- 作者昵称
            digg_count      INTEGER,           -- 点赞数
            comment_count   INTEGER,           -- 评论数
            share_count     INTEGER,           -- 分享数
            collect_count   INTEGER,           -- 收藏数
            create_time     TEXT,              -- 视频创建时间
            -- 下载状态（v0.5.0 新增，无论成功失败都记录）
            status          TEXT DEFAULT 'success',  -- 下载状态：success / failed
            error_msg       TEXT,                    -- 失败时的错误信息（成功时为 NULL）
            retry_count     INTEGER DEFAULT 0,       -- 已重试次数（不含首次）
            downloaded_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )

    用法:
        with ProgressDB(Path(".douyindl/progress.db")) as db:
            if db.is_success_downloaded("123456"):
                print("已下载成功")
            db.record("123456", "mix", "mix_id_xxx", "合集名", "文案", "/path/to.mp4",
                      status="success", meta={...})
            for row in db.query_failed():
                ...
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> "ProgressDB":
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._migrate_schema()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _migrate_schema(self) -> None:
        """建表并对旧版数据库做列迁移（ALTER TABLE ADD COLUMN）。"""
        if not self._conn:
            return
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
        # v0.4.0 新增字段：通过 PRAGMA 检测列是否存在，缺失则 ALTER TABLE 补齐
        # 旧数据库（v0.3.0）只有 7 个字段，此处自动补齐新字段，保留历史记录
        existing_cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(downloaded_videos)")
        }
        new_cols = {
            "duration": "INTEGER",
            "width": "INTEGER",
            "height": "INTEGER",
            "file_size": "INTEGER",
            "bit_rate": "INTEGER",
            "fps": "REAL",
            "ratio": "TEXT",
            "video_format": "TEXT",
            "is_h265": "INTEGER",
            "nickname": "TEXT",
            "digg_count": "INTEGER",
            "comment_count": "INTEGER",
            "share_count": "INTEGER",
            "collect_count": "INTEGER",
            "create_time": "TEXT",
            # v0.5.0 新增：下载状态字段，无论成功失败都记录
            # 旧数据库中已有记录视为成功（status='success'），error_msg=NULL，retry_count=0
            "status": "TEXT DEFAULT 'success'",
            "error_msg": "TEXT",
            "retry_count": "INTEGER DEFAULT 0",
        }
        for col, col_type in new_cols.items():
            if col not in existing_cols:
                self._conn.execute(
                    f"ALTER TABLE downloaded_videos ADD COLUMN {col} {col_type}"
                )
        # 为 resource_id 建索引，加速合集场景下查询已下载视频
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_resource_id ON downloaded_videos(resource_id)"
        )
        # 为 status 建索引，加速 --retry-failed 查询失败记录
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_status ON downloaded_videos(status)"
        )
        self._conn.commit()

    def is_success_downloaded(self, aweme_id: str) -> bool:
        """查询某视频是否已记录为下载成功。

        仅当 status='success' 时返回 True，失败记录不跳过会重新下载。
        注意：不验证文件是否还在磁盘上，文件存在性由调用方额外判断。
        """
        if not self._conn:
            return False
        cur = self._conn.execute(
            "SELECT 1 FROM downloaded_videos WHERE aweme_id = ? AND status = 'success'",
            (aweme_id,),
        )
        return cur.fetchone() is not None

    # 兼容旧调用方的别名（内部已改用 is_success_downloaded）
    is_downloaded = is_success_downloaded

    def record(
        self,
        aweme_id: str,
        resource_type: str,
        resource_id: str,
        mix_name: str,
        desc: str,
        file_path: str,
        meta: Optional[Dict[str, Any]] = None,
        status: str = "success",
        error_msg: Optional[str] = None,
        retry_count: int = 0,
    ) -> None:
        """记录一条下载记录（已存在则更新），无论成功失败都记录。

        Args:
            meta: 视频元数据字典，可包含 duration/width/height/file_size/
                  bit_rate/fps/ratio/video_format/is_h265/nickname/
                  digg_count/comment_count/share_count/collect_count/create_time
                  缺失字段写 NULL
            status: 下载状态，'success' 或 'failed'
            error_msg: 失败时的错误信息（成功时传 None）
            retry_count: 已重试次数（不含首次下载，0 表示首次即成功/失败）
        """
        if not self._conn:
            return
        meta = meta or {}
        self._conn.execute(
            """INSERT INTO downloaded_videos
               (aweme_id, resource_type, resource_id, mix_name, desc, file_path,
                duration, width, height, file_size, bit_rate, fps, ratio,
                video_format, is_h265, nickname, digg_count, comment_count,
                share_count, collect_count, create_time,
                status, error_msg, retry_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(aweme_id) DO UPDATE SET
                   resource_type=excluded.resource_type,
                   resource_id=excluded.resource_id,
                   mix_name=excluded.mix_name,
                   desc=excluded.desc,
                   file_path=excluded.file_path,
                   duration=excluded.duration,
                   width=excluded.width,
                   height=excluded.height,
                   file_size=excluded.file_size,
                   bit_rate=excluded.bit_rate,
                   fps=excluded.fps,
                   ratio=excluded.ratio,
                   video_format=excluded.video_format,
                   is_h265=excluded.is_h265,
                   nickname=excluded.nickname,
                   digg_count=excluded.digg_count,
                   comment_count=excluded.comment_count,
                   share_count=excluded.share_count,
                   collect_count=excluded.collect_count,
                   create_time=excluded.create_time,
                   status=excluded.status,
                   error_msg=excluded.error_msg,
                   retry_count=excluded.retry_count,
                   downloaded_at=CURRENT_TIMESTAMP
            """,
            (
                aweme_id, resource_type, resource_id, mix_name, desc, file_path,
                meta.get("duration"),
                meta.get("width"),
                meta.get("height"),
                meta.get("file_size"),
                meta.get("bit_rate"),
                meta.get("fps"),
                meta.get("ratio"),
                meta.get("video_format"),
                meta.get("is_h265"),
                meta.get("nickname"),
                meta.get("digg_count"),
                meta.get("comment_count"),
                meta.get("share_count"),
                meta.get("collect_count"),
                meta.get("create_time"),
                status,
                error_msg,
                retry_count,
            ),
        )
        self._conn.commit()

    def query_failed(self) -> List[Dict[str, Any]]:
        """查询所有下载失败的记录（status='failed'），用于 --retry-failed 重试。

        返回字典列表，每个字典包含 aweme_id/resource_type/resource_id/mix_name/
        desc/file_path/error_msg/retry_count 等字段。
        """
        if not self._conn:
            return []
        cur = self._conn.execute(
            """SELECT aweme_id, resource_type, resource_id, mix_name, desc,
                      file_path, error_msg, retry_count
               FROM downloaded_videos WHERE status = 'failed'
               ORDER BY downloaded_at"""
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def count_by_resource(self, resource_id: str) -> int:
        """统计某合集/单视频已下载成功的视频数（仅含 status='success'）。"""
        if not self._conn:
            return 0
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM downloaded_videos WHERE resource_id = ? AND status = 'success'",
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

    async def _download_with_retry(
        self,
        play_addr: str,
        save_path: Path,
    ) -> Tuple[bool, str, int]:
        """带重试的下载，返回 (是否成功, 错误信息, 重试次数)。

        重试次数由 config.download_max_retries 控制（0 表示不重试），
        重试间隔由 config.download_retry_interval 控制。
        retry_count 语义：0=首次即成功/失败，N=重试 N 次后成功/失败。
        """
        cfg = self.config
        attempt = 0
        last_error = ""
        max_attempts = 1 + cfg.download_max_retries  # 首次 + 重试次数
        for attempt in range(max_attempts):
            try:
                await download_video(play_addr, save_path, cfg)
                return True, "", attempt
            except Exception as e:
                last_error = str(e)
                if attempt < cfg.download_max_retries:
                    print(f"      下载失败（第{attempt+1}次尝试），"
                          f"{cfg.download_retry_interval}秒后重试: {e}")
                    await asyncio.sleep(cfg.download_retry_interval)
                else:
                    print(f"      下载失败（共尝试{max_attempts}次，"
                          f"已用尽重试次数）: {e}")
        return False, last_error, attempt

    async def retry_failed(self) -> Dict[str, Any]:
        """重试数据库中所有下载失败的记录（status='failed'）。

        流程：
        1. 从 db 查询所有失败记录
        2. 对每条记录用 aweme_id 调用 fetch_one_video 获取最新下载地址
        3. 下载到原 file_path（带重试）
        4. 更新 db 记录（成功则 status='success'，失败则刷新 error_msg/retry_count）

        Returns:
            统计字典，含 total/success/still_failed
        """
        cfg = self.config
        print("[retry-failed] 开始重试数据库中的失败记录...")

        db = self._get_progress_db()
        if db is None:
            print("错误: 进度数据库未启用，无法查询失败记录", file=sys.stderr)
            return {"total": 0, "success": 0, "still_failed": 0}

        with db as progress_db:
            failed_list = progress_db.query_failed()

        if not failed_list:
            print("没有失败记录需要重试")
            return {"total": 0, "success": 0, "still_failed": 0}

        total = len(failed_list)
        print(f"共 {total} 条失败记录需要重试:")
        for i, r in enumerate(failed_list, 1):
            desc_short = (r.get("desc") or "").replace("\n", " ")[:40]
            print(f"  {i:>3d}. [{r['aweme_id']}] {desc_short}")
            if r.get("error_msg"):
                print(f"       上次错误: {r['error_msg'][:80]}")

        success = 0
        still_failed = 0
        with db as progress_db:
            for i, r in enumerate(failed_list, 1):
                aweme_id = r["aweme_id"]
                save_path = Path(r["file_path"]) if r.get("file_path") else None
                desc = r.get("desc") or ""
                mix_name = r.get("mix_name") or ""
                resource_id = r.get("resource_id") or aweme_id
                resource_type = r.get("resource_type") or "one"

                print(f"\n  [{i}/{total}] 重试 {aweme_id}")

                # 重新获取视频下载地址（旧地址可能已失效）
                try:
                    video = await fetch_one_video(str(aweme_id), cfg)
                except Exception as e:
                    print(f"      获取视频信息失败: {e}")
                    # API 失败也记录到 db，更新 error_msg（meta 无法获取，置 None）
                    progress_db.record(
                        aweme_id=str(aweme_id),
                        resource_type=resource_type,
                        resource_id=resource_id,
                        mix_name=mix_name,
                        desc=desc[:200],
                        file_path=str(save_path) if save_path else "",
                        meta=None,
                        status="failed",
                        error_msg=f"获取视频信息失败: {str(e)[:400]}",
                        retry_count=r.get("retry_count", 0) + cfg.download_max_retries,
                    )
                    still_failed += 1
                    continue

                play_addr = video.get("video_play_addr")
                if isinstance(play_addr, list):
                    play_addr = play_addr[0] if play_addr else None
                if not play_addr:
                    print(f"      无视频地址，跳过")
                    progress_db.record(
                        aweme_id=str(aweme_id),
                        resource_type=resource_type,
                        resource_id=resource_id,
                        mix_name=mix_name,
                        desc=desc[:200],
                        file_path=str(save_path) if save_path else "",
                        meta=video.get("_meta"),
                        status="failed",
                        error_msg="无视频下载地址",
                        retry_count=r.get("retry_count", 0) + cfg.download_max_retries,
                    )
                    still_failed += 1
                    continue

                # 复用原 file_path，保持目录结构一致
                if not save_path:
                    name = _build_video_name(desc, str(aweme_id), cfg.filename_max_len)
                    date_str = datetime.now().strftime("%Y%m%d")
                    save_path = self.output_dir / f"{date_str}_{name}.mp4"
                save_path.parent.mkdir(parents=True, exist_ok=True)

                download_ok, last_error, attempt = await self._download_with_retry(
                    play_addr, save_path
                )

                if download_ok:
                    success += 1
                    # 下载元数据（封面/文案/原声/JSON）
                    if cfg.save_metadata:
                        base_path = save_path.with_suffix("")
                        await download_metadata(video, base_path, cfg)
                    # 更新 db 记录为成功
                    progress_db.record(
                        aweme_id=str(aweme_id),
                        resource_type=resource_type,
                        resource_id=resource_id,
                        mix_name=mix_name,
                        desc=desc[:200],
                        file_path=str(save_path),
                        meta=video.get("_meta"),
                        status="success",
                        error_msg=None,
                        retry_count=attempt,
                    )
                else:
                    still_failed += 1
                    # 更新 db 记录为失败（累加重试次数）
                    progress_db.record(
                        aweme_id=str(aweme_id),
                        resource_type=resource_type,
                        resource_id=resource_id,
                        mix_name=mix_name,
                        desc=desc[:200],
                        file_path=str(save_path),
                        meta=video.get("_meta"),
                        status="failed",
                        error_msg=last_error[:500],
                        retry_count=r.get("retry_count", 0) + attempt + 1,
                    )

                # 失败记录之间也等待间隔，避免风控（最后一个不等待）
                # 间隔时间在 base*0.9 到 base 之间随机取数
                if i < total and cfg.mix_download_interval > 0:
                    actual_interval = _random_interval(cfg.mix_download_interval)
                    print(f"      等待 {actual_interval:.1f} 秒后继续...")
                    await asyncio.sleep(actual_interval)

        print(f"\n[retry-failed] 完成: 成功 {success}/{total}，仍失败 {still_failed}")
        return {
            "total": total,
            "success": success,
            "still_failed": still_failed,
        }

    async def run(self, share_url: str) -> Dict[str, Any]:
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
            统计字典，含 url/kind/resource_id/mix_name/total/success/skipped/target_dir
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

                name = _build_video_name(desc, str(aweme_id), cfg.filename_max_len)
                # 合集内文件：001_文案_awemeid.mp4（目录已含日期，无需重复）
                # 单视频文件：YYYYMMDD_文案_awemeid.mp4
                if kind == "mix":
                    save_path = target_dir / f"{i:03d}_{name}.mp4"
                else:
                    save_path = target_dir / f"{date_str}_{name}.mp4"

                # 跳过判断：文件已存在 或 进度数据库已记录为成功
                # force=True 时跳过此检查，强制重新下载
                # 注意：失败记录（status='failed'）不跳过，会重新下载
                if not self.force:
                    if save_path.exists():
                        print(f"  [{i}/{len(videos)}] {save_path.name} 文件已存在，跳过")
                        success += 1
                        skipped += 1
                        continue
                    if progress_db and progress_db.is_success_downloaded(str(aweme_id)):
                        print(f"  [{i}/{len(videos)}] {save_path.name} 进度记录已存在，跳过")
                        skipped += 1
                        continue

                print(f"  [{i}/{len(videos)}] 下载 {save_path.name}")
                # 下载重试：失败时自动重试 download_max_retries 次（0 表示不重试）
                download_ok, last_error, attempt = await self._download_with_retry(
                    play_addr, save_path
                )

                meta = v.get("_meta") or {}

                if download_ok:
                    success += 1
                    # 打印视频元数据（从 API 响应提取）
                    if meta:
                        dur = meta.get("duration") or 0
                        w = meta.get("width") or 0
                        h = meta.get("height") or 0
                        size = meta.get("file_size") or 0
                        # 时长转秒，文件大小转 MB
                        info_parts = []
                        if dur:
                            info_parts.append(f"时长 {dur/1000:.1f}s")
                        if w and h:
                            info_parts.append(f"分辨率 {w}x{h}")
                        if size:
                            info_parts.append(f"大小 {size/1024/1024:.1f}MB")
                        if meta.get("ratio"):
                            info_parts.append(meta["ratio"])
                        if meta.get("is_h265"):
                            info_parts.append("H.265")
                        if info_parts:
                            print(f"      元数据: {', '.join(info_parts)}")

                    # 下载元数据（封面/文案/原声/JSON）
                    if cfg.save_metadata:
                        # base_path 不含扩展名，用于生成 .jpg/.txt/.mp3/.json
                        base_path = save_path.with_suffix("")
                        await download_metadata(v, base_path, cfg)

                    # 记录到进度数据库（成功，含视频元数据与重试次数）
                    if progress_db:
                        progress_db.record(
                            aweme_id=str(aweme_id),
                            resource_type=kind,
                            resource_id=resource_id,
                            mix_name=mix_name,
                            desc=desc[:200],
                            file_path=str(save_path),
                            meta=meta,
                            status="success",
                            error_msg=None,
                            retry_count=attempt,
                        )
                else:
                    # 下载失败也记录到数据库，便于后续 --retry-failed 重试
                    if progress_db:
                        progress_db.record(
                            aweme_id=str(aweme_id),
                            resource_type=kind,
                            resource_id=resource_id,
                            mix_name=mix_name,
                            desc=desc[:200],
                            file_path=str(save_path),
                            meta=meta,
                            status="failed",
                            error_msg=last_error[:500],
                            retry_count=attempt,
                        )

                # 合集场景：视频间间隔等待（最后一个不需要）
                # 间隔时间在 base*0.9 到 base 之间随机取数，避免固定间隔被风控识别
                if download_interval and i < len(videos):
                    actual_interval = _random_interval(download_interval)
                    print(f"      等待 {actual_interval:.1f} 秒后继续下载下一个...")
                    await asyncio.sleep(actual_interval)

        # 统计输出
        print(f"\n[4/4] 完成: 成功 {success}/{len(videos)}，跳过 {skipped}")
        print(f"      保存目录: {target_dir}/")
        if progress_db and kind == "mix":
            total = progress_db.count_by_resource(resource_id)
            print(f"      合集 {resource_id} 累计已下载 {total} 个视频（含历史记录）")
        return {
            "url": share_url,
            "kind": kind,
            "resource_id": resource_id,
            "mix_name": mix_name,
            "total": len(videos),
            "success": success,
            "skipped": skipped,
            "target_dir": str(target_dir),
        }


class _NullContext:
    """空上下文管理器，用于进度数据库未启用时保持 with 语法统一。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass


def main():
    """CLI 入口：python -m douyindl <分享链接> [输出目录]

    链接来源（二选一或组合）：
    - url 位置参数：直接传入分享文本（支持空格分隔多个链接）
    - -i/--input-file：从文件读取分享文本（支持多行/空格分隔），下载完成后清空文件
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="抖音视频/合集下载工具（支持短链分享文本）"
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="抖音分享链接（短链或完整URL，可包含分享文本）；支持空格分隔多个链接。"
             "若未指定，需通过 -i/--input-file 提供链接文件",
    )
    parser.add_argument(
        "-i", "--input-file",
        default=None,
        help="从文件读取分享链接（每行一个或空格分隔多个），下载完成后清空文件内容，"
             "便于反复粘贴新链接复用该文件",
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
        "-r", "--retry-failed",
        action="store_true",
        help="重试数据库中所有下载失败的记录（status='failed'），无需提供 url/-i",
    )
    parser.add_argument(
        "-m", "--metadata",
        nargs="?", const="all", default=None,
        help="保存元数据，可选类型: all/cover/desc/music/json（逗号分隔多个）；"
             "仅 -m 等同于 all；未指定时使用 config.yaml 中的 save_metadata 设置",
    )
    args = parser.parse_args()

    # 加载配置
    config_path = Path(args.config) if args.config else None
    config = Config(config_path)

    # CLI -m 参数精细控制元数据子开关
    # - 未指定 -m：沿用 config.yaml 设置
    # - -m 或 -m all：开启全部元数据
    # - -m music,cover：仅开启指定类型，其余关闭
    if args.metadata is not None:
        meta_types = args.metadata.lower()
        meta_all = meta_types == "all"
        # 解析逗号分隔的类型列表
        wanted = set(t.strip() for t in meta_types.split(",") if t.strip())
        valid = {"cover", "desc", "music", "json", "all"}
        invalid = wanted - valid
        if invalid:
            print(f"错误: 未知的元数据类型 {invalid}，可选: {valid}", file=sys.stderr)
            sys.exit(1)
        config.save_metadata = True
        config.save_cover = meta_all or "cover" in wanted
        config.save_desc = meta_all or "desc" in wanted
        config.save_music = meta_all or "music" in wanted
        config.save_json = meta_all or "json" in wanted

    dl = DouyinDownloader(
        output_dir=args.output,
        max_counts=args.max_counts,
        config=config,
        force=args.force,
    )

    # ── --retry-failed 模式：重试 db 中的失败记录，不需要 url/-i ──
    if args.retry_failed:
        asyncio.run(dl.retry_failed())
        return

    # ── 收集所有 URL 来源（url 参数 + 输入文件） ──
    raw_texts: List[str] = []
    if args.url:
        raw_texts.append(args.url)
    input_path: Optional[Path] = None
    if args.input_file:
        input_path = Path(args.input_file)
        if not input_path.exists():
            print(f"错误: 输入文件不存在: {input_path}", file=sys.stderr)
            sys.exit(1)
        file_content = input_path.read_text(encoding="utf-8")
        if not file_content.strip():
            print(f"错误: 输入文件内容为空: {input_path}", file=sys.stderr)
            sys.exit(1)
        raw_texts.append(file_content)

    if not raw_texts:
        print("错误: 未提供分享链接，请通过 url 参数或 -i/--input-file 指定，"
              "或使用 -r/--retry-failed 重试失败记录", file=sys.stderr)
        sys.exit(1)

    # 从所有文本来源中提取 URL（支持空格/换行分隔多链接）
    # 用户可能用反引号包裹 URL（如 markdown 示例），strip 掉反引号
    urls: List[str] = []
    for text in raw_texts:
        found = re.findall(r'https?://[^\s，,]+', text)
        urls.extend(u.strip("`") for u in found if u.strip("`"))
    # 去重并保序（同一链接在文件和参数中同时出现时只下载一次）
    seen = set()
    dedup_urls: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            dedup_urls.append(u)
    urls = dedup_urls

    if not urls:
        print("错误: 未在输入中找到有效的 URL", file=sys.stderr)
        sys.exit(1)

    # 多链接串行下载（asyncio.run 只能调用一次，包一层协程循环）
    async def run_all() -> None:
        total = len(urls)
        results: List[Dict[str, Any]] = []
        for i, url in enumerate(urls, 1):
            if total > 1:
                print(f"\n========== [{i}/{total}] {url} ==========")
            try:
                stat = await dl.run(url)
                results.append(stat)
            except Exception as e:
                # 单个链接失败不中断后续链接
                print(f"      下载失败: {e}", file=sys.stderr)
                results.append({
                    "url": url, "kind": "", "resource_id": "", "mix_name": "",
                    "total": 0, "success": 0, "skipped": 0, "target_dir": "",
                    "error": str(e),
                })
            # 多链接之间等待间隔，防止风控（复用 mix_download_interval，最后一个不等待）
            # 间隔时间在 base*0.9 到 base 之间随机取数，避免固定间隔被风控识别
            if i < total and config.mix_download_interval > 0:
                actual_interval = _random_interval(config.mix_download_interval)
                print(f"\n等待 {actual_interval:.1f} 秒后继续下载下一个链接...")
                await asyncio.sleep(actual_interval)

        # 多链接下载总结
        if total > 1:
            sum_success = sum(r.get("success", 0) for r in results)
            sum_skipped = sum(r.get("skipped", 0) for r in results)
            sum_total = sum(r.get("total", 0) for r in results)
            failed_links = [r["url"] for r in results if r.get("error")]
            print("\n========== 多链接下载总结 ==========")
            for i, r in enumerate(results, 1):
                kind_label = "合集" if r.get("kind") == "mix" else (
                    "单视频" if r.get("kind") == "one" else "失败"
                )
                name = r.get("mix_name") or r.get("resource_id") or r.get("error", "")
                status = f"成功 {r.get('success', 0)}/{r.get('total', 0)}，跳过 {r.get('skipped', 0)}"
                if r.get("error"):
                    status = f"失败: {r['error']}"
                print(f"  [{i}/{total}] {kind_label} {name} - {status}")
            print(f"  合计: 成功 {sum_success}/{sum_total}，跳过 {sum_skipped}", end="")
            if failed_links:
                print(f"，失败 {len(failed_links)} 个链接")
            else:
                print()

    asyncio.run(run_all())

    # 下载完成后清空输入文件内容（保留文件本身），便于反复粘贴新链接复用
    if input_path and input_path.exists():
        input_path.write_text("", encoding="utf-8")
        print(f"\n已清空输入文件: {input_path}")


if __name__ == "__main__":
    main()
