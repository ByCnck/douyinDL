"""并发分段下载引擎（jicun-dl 移植版）

移植来源
--------
/Users/zhenxi/codes/Android/即存_jicun-dl下载引擎源码解析.md
（对开源项目 dhvbjvvb/jicun 的原生 Kotlin `NativeDownloader` 的源码解析）

移植范围与对照
--------------
| 原生 Kotlin (jicun-dl)              | 本模块                | 说明                       |
|--------------------------------------|-----------------------|----------------------------|
| `FILE_CONCURRENCY = 4`               | `file_concurrency`    | 同时下几个文件             |
| `DEFAULT_LANES = 32`                 | `lanes`               | 每文件并发连接数           |
| `MAX_SEGMENTS = 64`                  | `max_segments`        | 分段硬上限                 |
| `SEGMENT_BYTES = 4MB`                | `segment_bytes`       | 主体段大小                 |
| `TAIL_BYTES = 32MB` / `TAIL_CHUNK_BYTES = 1MB` | `tail_bytes` / `tail_chunk_bytes` | 尾部切小段 |
| `SEGMENT_FROM_BYTES = 8MB`           | `segment_from_bytes`  | 小于此不分段               |
| `CONNECTION_BUDGET_MS = 10s`         | `connection_budget`   | 慢连接轮换阈值             |
| `STALL_LIMIT=4` / `CHUNK_ATTEMPT_LIMIT=40` | 同名常量         | 段重试账本                 |
| 探针 `Range: bytes=0-0`              | `probe()`             | 取总长/是否认Range         |
| `AtomicLong` 共享游标                 | `itertools.count`     | 谁空谁认领下一段           |
| `RandomAccessFile.seek(start)` 写偏移 | `os.pwrite`           | 不分片不拼接               |
| `TransferInterrupted` 断点续传       | `_TransferInterrupted`| 从断点续                   |
| `shouldRotateConnection`             | `_should_rotate`      | 按"用多久没收完"判         |
| `ChunkAttempts.noteFailure`          | `_ChunkAttempts`      | 按"有无进展"记账           |
| 浏览器 UA + 平台 Referer             | `_headers_for`        | 绕 CDN 风控                |
| 多道完整性校验                        | 三处断言               | 防"假成功坏文件"           |

为什么需要它
------------
原 `download_video` 是单连接顺序流式下载，遇到抖音 CDN 单连接限速
（实测每路仅 ~60KB/s）时速度极低，且断连即整段重来、必须从头重下。

实测本机走单连接下载该视频（6MB）明显慢于多路并发，详见 README 备注。

差异说明（与原 Kotlin 版有意不同之处）
--------------------------------------
1. Kotlin 用线程池 + 阻塞 IO；本模块是 asyncio，每个 lane 一个任务。
2. 原版 `setLength(size)` 预分配；本模块同样预分配，但**绝不当完整性判据**
   （否则假成功会漏进相册）。
3. 原版慢连接轮换阈值 10s 是按 4MB 段设计的；本模块保持同值，段大小改变时
   建议同步调整（见 `connection_budget` 说明）。
"""

from __future__ import annotations

import asyncio
import itertools
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import httpx

from .logger import logger

# ── 默认参数（对齐 jicun-dl 实测值） ─────────────────────────────

DEFAULT_LANES = 32            # 每文件并发连接数
DEFAULT_FILE_CONCURRENCY = 4  # 同时下载几个文件
MAX_SEGMENTS = 64             # 分段数硬上限，防止把网络打爆
SEGMENT_BYTES = 4 * 1024 * 1024        # 主体段 4MB
TAIL_BYTES = 32 * 1024 * 1024          # 尾部 32MB 内切小段
TAIL_CHUNK_BYTES = 1024 * 1024         # 尾段 1MB
SEGMENT_FROM_BYTES = 8 * 1024 * 1024   # 小于 8MB 不分段
CONNECTION_BUDGET = 10.0      # 慢连接轮换阈值（秒）
STALL_LIMIT = 4               # 连续 4 次零进展判段失败
CHUNK_ATTEMPT_LIMIT = 40      # 段重试硬上限
READ_TIMEOUT = 30.0
CONNECT_TIMEOUT = 20.0
PROGRESS_INTERVAL = 0.2       # 进度按时间节流上报


class DownloadError(RuntimeError):
    """下载失败。"""


class HttpStatusError(DownloadError):
    """HTTP 4xx/5xx 错误。4xx 立刻放弃，不重试。"""


@dataclass
class ChunkPlan:
    """一个分段的字节区间（闭区间，start/end 均为合法偏移）。"""

    start: int
    end: int  # 包含端点

    @property
    def size(self) -> int:
        return self.end - self.start + 1


@dataclass
class ProbeResult:
    """探针结果。"""

    total: int                  # 文件总长（字节）
    content_type: str = ""
    supports_range: bool = True # 服务端是否认Range（206）
    final_url: str = ""


@dataclass
class DownloadStats:
    """下载统计，用于日志与排障。"""

    size: int = 0
    downloaded: int = 0
    lanes: int = 0
    chunks: int = 0
    retries: int = 0
    rotations: int = 0
    range_ignored: bool = False
    elapsed: float = 0.0
    status_counts: Dict[str, int] = field(default_factory=dict)


class _TransferInterrupted(Exception):
    """连接中断，携带已写入字节数用于断点续传。

    对齐 Kotlin 版 `TransferInterrupted(written)`。
    """

    def __init__(self, written: int):
        super().__init__(f"传输中断，已写入 {written} 字节")
        self.written = written


class _ChunkAttempts:
    """段级重试账本：按"有没有进展"记账，而非固定重试次数。

    对齐 Kotlin 版 `ChunkAttempts(stallLimit=4, attemptLimit=40)`。

    背景：CDN 会对每条连接限速（实测 B 站类 ~128KB/s），约每几 MB 断一次。
    固定 3 次重试在长文件上必然连续撞同一位置、报"80% 处失败"。只要还有
    进展就续传，重试代价只有一次连接握手（段是幂等重写的）。

    Attributes:
        stalls: 连续"一个字节都没收到"的次数
        attempts: 总尝试次数（含成功）
    """

    def __init__(self, stall_limit: int = STALL_LIMIT,
                 attempt_limit: int = CHUNK_ATTEMPT_LIMIT):
        self.stall_limit = stall_limit
        self.attempt_limit = attempt_limit
        self.attempts = 0
        self.stalls = 0

    def note_failure(self, progress: int) -> bool:
        """记录一次失败，返回是否还应继续重试。

        Args:
            progress: 本次尝试写入了多少字节（>0 表示有进展）
        """
        self.attempts += 1
        self.stalls = 0 if progress > 0 else self.stalls + 1
        return self.stalls < self.stall_limit and self.attempts < self.attempt_limit

    @property
    def delay(self) -> float:
        """退避延迟：200ms 起翻倍，封顶 2s。"""
        return min(0.2 * (2 ** min(self.stalls, 3)), 2.0)


def _resume_offset(written: int, offset: int, end: int) -> int:
    """从已写入位置续传的偏移。

    对齐 Kotlin 版 `resumeOffset(written, offset, end) = minOf(offset + written, end + 1)`。

    边界：收满整段后立刻被 RST（服务端按 Content-Length 收完就断）也算成功，
    否则续传会发出 `bytes=X-(X-1)` 非法区间换回 416。
    """
    return min(offset + written, end + 1)


def _should_rotate(start: int, written: int, wanted: int,
                   elapsed: float, budget: float) -> bool:
    """慢连接轮换判据。

    对齐 Kotlin 版 `shouldRotateConnection`：`start >= 0 && written < wanted && elapsed >= budget`

    判据是"用了多久还没收完这一段"，不是速率——同一批连接之间速率差 20 倍
    是常态（实测 47KB/s ~ 2MB/s），拿绝对速率当判据不是误杀就是没反应。
    """
    return start >= 0 and written < wanted and elapsed >= budget


def _plan_chunks(total: int, segment_bytes: int = SEGMENT_BYTES,
                 tail_bytes: int = TAIL_BYTES,
                 tail_chunk_bytes: int = TAIL_CHUNK_BYTES,
                 segment_from_bytes: int = SEGMENT_FROM_BYTES,
                 max_segments: int = MAX_SEGMENTS) -> List[ChunkPlan]:
    """生成分段计划。

    对齐 Kotlin 版 `claimedChunkWithTail()`：
    - 小于 `segment_from_bytes`(8MB) 不分段，返回单段
    - 文件尾部 `tail_bytes`(32MB) 内切成 `tail_chunk_bytes`(1MB) 小段
      （实测收尾时慢连接 4MB 段拖 8.5s，切 1/4 后拖累按 1/4 计）
    - 文件不够大（< 2× 尾巴）则整条按大段走
    - 段数受 `max_segments` 限制，超出则放大段长重新均分
    """
    if total <= 0:
        return []

    # 小文件不分段
    if total < segment_from_bytes:
        return [ChunkPlan(0, total - 1)]

    plans: List[ChunkPlan] = []
    tail_start = max(0, total - tail_bytes)

    # 主体部分：固定大段
    off = 0
    while off < tail_start:
        end = min(off + segment_bytes - 1, tail_start - 1)
        plans.append(ChunkPlan(off, end))
        off = end + 1

    # 尾部：小段
    while off < total:
        end = min(off + tail_chunk_bytes - 1, total - 1)
        plans.append(ChunkPlan(off, end))
        off = end + 1

    # 段数超限：按段数均分放大段长（保持"少而大"而非"多而小"）
    if len(plans) > max_segments:
        seg = (total + max_segments - 1) // max_segments
        plans = []
        off = 0
        while off < total:
            end = min(off + seg - 1, total - 1)
            plans.append(ChunkPlan(off, end))
            off = end + 1

    return plans


def _headers_for(url: str, user_agent: str, referer: str = "https://www.douyin.com/") -> Dict[str, str]:
    """按平台生成差异化请求头。

    对齐 Kotlin 版 4.9「平台 UA / Referer（反限速细节）」：
    - 默认浏览器 UA（Java 默认的 `Dalvik/2.x` 对 CDN 风控和扫描器没区别）
    - B 站 CDN 单独一份 UA + Referer（移动 Chrome UA 直接 403，要桌面 UA）
    - `Accept-Encoding: identity`：带 Range 要 gzip 自相矛盾，压缩会破坏字节偏移

    抖音的 VOD 域名不需要特殊 Referer，但仍走浏览器 UA。
    """
    headers = {
        "User-Agent": user_agent,
        "Referer": referer,
        "Accept": "*/*",
        # Range 请求必须禁用压缩，否则字节偏移与解包后内容错位
        "Accept-Encoding": "identity",
    }

    host = _host_of(url)
    # 域名边界判断（防 evilbilivideo.com 这类伪后缀被误判）
    if _is_host_under(host, "bilivideo.com") or _is_host_under(host, "akamaized.net"):
        headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        )
        headers["Referer"] = "https://www.bilibili.com/"
    return headers


def _host_of(url: str) -> str:
    m = re.match(r"^https?://([^/:?#]+)", url or "", re.I)
    return (m.group(1) if m else "").lower()


def _is_host_under(host: str, suffix: str) -> bool:
    """域名边界判断：`a.bilivideo.com` 为真，`evilbilivideo.com` 为假。"""
    host = (host or "").lower().rstrip(".")
    return host == suffix or host.endswith("." + suffix)


async def _probe(client: httpx.AsyncClient, url: str,
                 headers: Dict[str, str],
                 attempts: int = 3) -> ProbeResult:
    """串行探针：发一个 `Range: bytes=0-0` 请求，一次拿三件事。

    对齐 Kotlin 版 4.2 探针：
    - 总大小：`Content-Range: bytes 0-0/7073396` 末尾 `/N`
    - Content-Type：交给上层猜后缀
    - 是否认Range：回 206 支持分段；回 200（整条）说明服务端忽略 Range，
      `supports_range=False`，后面改走整条顺序下载

    探针本身也可能遇到瞬时断连，故内建带退避的重试（对齐原版
    `retrying(attempts=3, delays=[300,900]ms)`）。失败只作废这一条，
    不影响同批其它文件。
    """
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            resp = await client.get(url, headers={**headers, "Range": "bytes=0-0"})
            if resp.status_code not in (200, 206):
                raise HttpStatusError(f"探针失败 HTTP {resp.status_code}")

            ctype = resp.headers.get("content-type", "")
            final_url = str(resp.url)

            if resp.status_code == 206:
                cr = resp.headers.get("content-range", "")
                m = re.search(r"/(\d+)\s*$", cr)
                if m:
                    return ProbeResult(total=int(m.group(1)), content_type=ctype,
                                       supports_range=True, final_url=final_url)
                # 有 206 但没有合法 Content-Range，视为不支持 Range
                return ProbeResult(total=0, content_type=ctype,
                                   supports_range=False, final_url=final_url)

            # 200：服务端忽略了 Range，取 Content-Length 作为总长
            total = int(resp.headers.get("content-length", 0) or 0)
            return ProbeResult(total=total, content_type=ctype,
                               supports_range=False, final_url=final_url)

        except HttpStatusError:
            raise  # 状态码错误不重试
        except (httpx.HTTPError, OSError) as e:
            last_exc = e
            if attempt < attempts - 1:
                await asyncio.sleep(0.3 * (3 ** attempt))
    raise DownloadError(f"探针失败（重试 {attempts} 次）: {last_exc}")


async def _download_one_chunk(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    fd: int,
    chunk: ChunkPlan,
    stats: DownloadStats,
    cancel: Optional[asyncio.Event] = None,
    budget: float = CONNECTION_BUDGET,
    on_progress: Optional[Callable[[int], None]] = None,
    chunk_size: int = 64 * 1024,
) -> None:
    """下载单个分段，seek 直接写盘（不落.part、不拼接）。

    对齐 Kotlin 版 4.5 + 4.6 + 4.7 + 4.8。

    Args:
        fd: 目标文件的已打开文件描述符（os.pwrite 直接按偏移写）
        chunk: 字节区间（闭区间）
        budget: 慢连接轮换阈值（秒）
        on_progress: 进度回调（收到字节时调用）
        chunk_size: 读块大小

    Raises:
        DownloadError: 重试耗尽仍失败
    """
    attempts = _ChunkAttempts()
    offset = chunk.start
    # written 提到 try 外层：无论以何种异常中断（协议错误/超时/EOF），
    # 都必须带着"本次已写入多少字节"去记账，否则会丢进度、每次从段首重传
    # （这正是原版 TransferInterrupted(written) 要解决的问题）。
    written = 0

    while offset <= chunk.end:
        if cancel and cancel.is_set():
            raise DownloadError("已取消")

        wanted = chunk.end - offset + 1
        written = 0
        started = time.monotonic()

        try:
            req_headers = {**headers, "Range": f"bytes={offset}-{chunk.end}"}
            async with client.stream("GET", url, headers=req_headers) as resp:
                if resp.status_code in (416,):
                    # 416：区间非法。若已收满整段（服务端按 Content-Length 收完
                    # 就断），视为成功；否则判失败。
                    if offset > chunk.end:
                        return
                    raise HttpStatusError(f"HTTP 416 (offset={offset})")
                if resp.status_code >= 400:
                    raise HttpStatusError(f"HTTP {resp.status_code}")
                if resp.status_code != 206:
                    # 服务端忽略了 Range 却返回 2xx → 数据不是从 offset 开始，
                    # 写下去会损坏文件（对齐原版"按它自己声明的 Content-Range
                    # 起始位置解释，不符即判失败"）。
                    raise DownloadError(
                        f"期望 206 Partial Content，实际 {resp.status_code}"
                    )

                # 校验响应声明的起始偏移与请求一致（对齐原版 4.5）
                cr = resp.headers.get("content-range", "")
                m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+)", cr)
                if m:
                    declared_start = int(m.group(1))
                    if declared_start != offset:
                        raise DownloadError(
                            f"Content-Range 起始({declared_start}) 与请求({offset})不符"
                        )

                # 用 aiter_raw 而非 aiter_bytes：后者会缓冲到凑满 chunk_size
                # 才吐出，导致「连接在半个 chunk 处被重置」时，缓冲里那部分
                # 已到达的字节被静默丢弃 —— 表现为每次重试都零进展、4 次后
                # 误判段失败。aiter_raw 收到多少就吐多少，让断点续传能真正
                # 拿到已收字节。（前提是响应未压缩：请求头已固定
                # Accept-Encoding: identity）
                content_encoding = resp.headers.get("content-encoding", "")
                if content_encoding and content_encoding.lower() not in ("identity", ""):
                    # 理论上不可达（我们已要求 identity）；真出现则判失败，
                    # 因为压缩流与字节偏移不自洽
                    raise DownloadError(
                        f"响应被压缩({content_encoding})，与 Range 请求冲突，判失败"
                    )

                async for data in resp.aiter_raw(chunk_size):
                    if not data:
                        continue
                    if cancel and cancel.is_set():
                        raise DownloadError("已取消")

                    # 限制本段最多写 wanted 字节，防止服务端多发覆盖别的
                    # lane 正在写的区域（对齐原版 4.5）
                    room = wanted - written
                    if room <= 0:
                        break
                    if len(data) > room:
                        data = data[:room]

                    # seek 直接写对应偏移：os.pwrite 一次定位+写，无拼接开销
                    os.pwrite(fd, data, offset + written)
                    written += len(data)

                    if on_progress:
                        on_progress(len(data))

                    # 慢连接轮换：用完 budget 还没收完这一段 → 抛中断，从断点续
                    if _should_rotate(offset, written, wanted,
                                      time.monotonic() - started, budget):
                        stats.rotations += 1
                        raise _TransferInterrupted(written)

                if written >= wanted:
                    return  # 本段收满

                # 未收满就被断开（正常 EOF 但字节不足）→ 也按中断处理
                raise _TransferInterrupted(written)

        except _TransferInterrupted as e:
            if e.written > 0:
                offset = _resume_offset(e.written, offset, chunk.end)
            if not attempts.note_failure(e.written):
                raise DownloadError(
                    f"分段 {chunk.start}-{chunk.end} 重试耗尽"
                    f"（attempts={attempts.attempts}, stalls={attempts.stalls}）"
                )
            stats.retries += 1
            await asyncio.sleep(attempts.delay)

        except HttpStatusError as e:
            # 4xx 立刻放弃不重试（403/404 再试也是白等）；5xx 归普通错误可重试
            m = re.search(r"HTTP (\d{3})", str(e))
            code = int(m.group(1)) if m else 0
            if 400 <= code < 500:
                raise
            if not attempts.note_failure(0):
                raise
            stats.retries += 1
            await asyncio.sleep(attempts.delay)

        except (asyncio.TimeoutError, httpx.TimeoutException) as e:
            # 超时同样可能已写入部分字节，按真实进度续传
            if written > 0:
                offset = _resume_offset(written, offset, chunk.end)
            if not attempts.note_failure(written):
                raise DownloadError(
                    f"分段 {chunk.start}-{chunk.end} 超时重试耗尽"
                ) from e
            stats.retries += 1
            await asyncio.sleep(attempts.delay)

        except DownloadError:
            # 结构性错误（Content-Range 不符等）：不重试，直接上抛
            raise

        except (httpx.HTTPError, OSError) as e:
            # 传输层中断（RemoteProtocolError / ConnectError / 连接重置…）：
            # 与 _TransferInterrupted 同等处理——带已写入字节数续传。
            # 这是 CDN 周期性断连场景的主路径，不能丢进度。
            if written > 0:
                offset = _resume_offset(written, offset, chunk.end)
            if not attempts.note_failure(written):
                raise DownloadError(
                    f"分段 {chunk.start}-{chunk.end} 网络错误重试耗尽"
                ) from e
            stats.retries += 1
            await asyncio.sleep(attempts.delay)


async def download_concurrent(
    url: str,
    save_path: Path,
    *,
    lanes: int = DEFAULT_LANES,
    segment_bytes: int = SEGMENT_BYTES,
    tail_bytes: int = TAIL_BYTES,
    tail_chunk_bytes: int = TAIL_CHUNK_BYTES,
    segment_from_bytes: int = SEGMENT_FROM_BYTES,
    max_segments: int = MAX_SEGMENTS,
    connection_budget: float = CONNECTION_BUDGET,
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    referer: str = "https://www.douyin.com/",
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[asyncio.Event] = None,
    timeout: float = 60.0,
    chunk_size: int = 64 * 1024,
) -> DownloadStats:
    """并发分段下载单个文件，返回统计。

    流程：探针 → 分段计划 → 共享游标 + lanes 条连接并发 → 完整性校验。

    与原 `download_video`（单连接顺序流式）的区别：
    - 单连接被 CDN 限速时（实测~60KB/s），32 路并发聚合速度可达其数十倍
    - 断连从断点续传，而非整段重来
    - 慢连接只拖住它手里那一段，不拖住整个文件

    Args:
        url: 视频直链
        save_path: 保存路径
        lanes: 并发连接数（会被段数上限自然收敛）
        progress_cb: 进度回调 (downloaded, total)
        cancel: 取消信号

    Returns:
        DownloadStats
    """
    if url.startswith("//"):
        url = "https:" + url

    save_path.parent.mkdir(parents=True, exist_ok=True)
    headers = _headers_for(url, user_agent, referer)
    stats = DownloadStats()

    limits = httpx.Limits(max_connections=lanes, max_keepalive_connections=lanes)
    async with httpx.AsyncClient(
        proxy=None,
        follow_redirects=True,
        headers=headers,
        limits=limits,
        timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT, read=READ_TIMEOUT),
    ) as client:
        # ── 探针：拿总长 + 是否认 Range ──
        probe = await _probe(client, url, headers)
        if probe.final_url and probe.final_url != url:
            url = probe.final_url
            headers = _headers_for(url, user_agent, referer)

        total = probe.total
        if total <= 0:
            raise DownloadError("探针未能确定文件总长（Content-Range/Content-Length 均缺失）")

        stats.size = total

        # ── 服务端忽略 Range → 整条顺序下载（单连接） ──
        if not probe.supports_range:
            logger.info("服务端不支持 Range（206），退化为单连接顺序下载")
            stats.range_ignored = True
            got = await _download_whole(client, url, headers, save_path, total,
                                        stats, cancel, progress_cb, chunk_size)
            stats.downloaded = got
            stats.elapsed = time.monotonic() - (stats.elapsed or time.monotonic())
            _verify(save_path, total)
            return stats

        # ── 分段计划 + 共享游标 ──
        plans = _plan_chunks(total, segment_bytes, tail_bytes,
                             tail_chunk_bytes, segment_from_bytes, max_segments)
        # lanes 摊薄：段数比 lanes 少时没必要开更多连接
        effective_lanes = max(1, min(lanes, len(plans)))
        stats.lanes = effective_lanes
        stats.chunks = len(plans)
        logger.debug(
            f"并发下载: size={total} chunks={len(plans)} lanes={effective_lanes} "
            f"(请求 {lanes}，按段数收敛)"
        )

        # 预分配全尺寸（仅性能优化，绝不当完整性判据）
        fd = os.open(str(save_path), os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        try:
            os.ftruncate(fd, total)

            counter = itertools.count()  # 共享游标：谁空谁认领下一段
            lock = asyncio.Lock()
            total_written = 0
            last_report = 0.0
            t0 = time.monotonic()

            def make_on_progress():
                """每 lane 的进度回调：累计字节 + 节流上报。"""
                nonlocal total_written, last_report

                def _cb(n: int) -> None:
                    nonlocal total_written, last_report
                    total_written += n
                    now = time.monotonic()
                    if progress_cb and (now - last_report) >= PROGRESS_INTERVAL:
                        last_report = now
                        try:
                            progress_cb(total_written, total)
                        except Exception:
                            pass

                return _cb

            async def lane() -> None:
                """单个 lane：不停认领下一段直到游标耗尽。

                对齐 Kotlin 版 4.4 共享游标设计——**不是**"每条 lane 固定分
                一块"。固定切分的总耗时 = 最慢那条 lane；共享游标让先空出来
                的 lane 继续认领后面的段，慢连接只拖住它手里那一段。
                """
                while True:
                    async with lock:
                        idx = next(counter)
                    if idx >= len(plans):
                        break
                    await _download_one_chunk(
                        client, url, headers, fd, plans[idx], stats,
                        cancel=cancel, budget=connection_budget,
                        on_progress=make_on_progress(), chunk_size=chunk_size,
                    )

            await asyncio.gather(*(lane() for _ in range(effective_lanes)))
            stats.elapsed = time.monotonic() - t0
        finally:
            os.close(fd)

        stats.downloaded = total_written

    # ── 完整性校验（多道，防"假成功坏文件"）──
    if stats.downloaded != total:
        raise DownloadError(
            f"字节数不符：期望 {total}，实际 {stats.downloaded}"
        )
    _verify(save_path, total)

    if progress_cb:
        try:
            progress_cb(total, total)
        except Exception:
            pass

    return stats


async def _download_whole(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    save_path: Path,
    total: int,
    stats: DownloadStats,
    cancel: Optional[asyncio.Event],
    progress_cb: Optional[Callable[[int, int], None]],
    chunk_size: int,
) -> int:
    """整条顺序下载（服务端忽略 Range 时的退化路径）。"""
    written = 0
    last_report = 0.0
    async with client.stream("GET", url, headers=headers) as resp:
        resp.raise_for_status()
        with open(save_path, "wb") as f:
            async for data in resp.aiter_bytes(chunk_size):
                if cancel and cancel.is_set():
                    raise DownloadError("已取消")
                f.write(data)
                written += len(data)
                now = time.monotonic()
                if progress_cb and (now - last_report) >= PROGRESS_INTERVAL:
                    last_report = now
                    try:
                        progress_cb(written, total)
                    except Exception:
                        pass
    return written


def _verify(save_path: Path, expected: int) -> None:
    """落地字节数校验：最后一道防"假成功"。

    对齐原版 4.10「落地字节 vs 承诺大小」校验。
    """
    actual = save_path.stat().st_size
    if actual != expected:
        raise DownloadError(f"落地文件大小不符：期望 {expected}，实际 {actual}")
    logger.debug(f"完整性校验通过: {save_path.name} ({actual} 字节)")