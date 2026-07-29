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
"""

import asyncio
import re
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
    ):
        self.config = config or Config()
        # output_dir 优先用参数，其次用配置文件
        self.output_dir = Path(output_dir) if output_dir else Path(self.config.output_dir)
        self.max_counts = max_counts

    async def run(self, share_url: str) -> List[Dict[str, Any]]:
        """主入口：解析链接 → 获取视频列表 → 下载。

        命名规则：
        - 合集：创建子目录 YYYYMMDD_合集名，内部文件 001_文案.mp4
        - 单视频：直接下载到 output_dir，文件名 YYYYMMDD_文案.mp4
        - 合集内视频间间隔 mix_download_interval 秒，单视频无需等待

        Returns:
            已下载视频的信息列表
        """
        cfg = self.config
        # 下载当日日期，用于目录名 / 单视频文件名前缀
        date_str = datetime.now().strftime("%Y%m%d")

        print(f"[1/3] 解析分享链接: {share_url}")
        kind, resource_id = await resolve_share_url(share_url, cfg)

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

        print(f"[2/3] 共获取 {len(videos)} 个视频:")
        for i, v in enumerate(videos, 1):
            desc = (v.get("desc") or "").replace("\n", " ")[:40]
            print(f"  {i:>3d}. [{v.get('aweme_id')}] {desc}")

        print(f"[3/3] 开始下载到 {target_dir}/ ...")
        target_dir.mkdir(parents=True, exist_ok=True)
        success = 0
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
            if save_path.exists():
                print(f"  [{i}/{len(videos)}] {save_path.name} 已存在，跳过")
                success += 1
                continue

            print(f"  [{i}/{len(videos)}] 下载 {save_path.name}")
            try:
                await download_video(play_addr, save_path, cfg)
                success += 1
            except Exception as e:
                print(f"      下载失败: {e}")

            # 合集场景：视频间间隔等待（最后一个不需要）
            if download_interval and i < len(videos):
                print(f"      等待 {download_interval} 秒后继续下载下一个...")
                await asyncio.sleep(download_interval)

        print(f"\n完成: {success}/{len(videos)} 个视频已下载到 {target_dir}/")
        return videos


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

    dl = DouyinDownloader(
        output_dir=args.output,
        max_counts=args.max_counts,
        config=config,
    )
    asyncio.run(dl.run(url))


if __name__ == "__main__":
    main()
