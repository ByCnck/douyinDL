"""loguru 日志配置（统一出口）

设计：
- 控制台（sink=sys.stderr）：级别由调用方指定，默认 INFO。
  运行时「默认 info」即指控制台默认只显示 INFO 及以上（结果 / 警告 / 错误），
  步骤细节（[1/4]~[4/4]）需在 DEBUG 级别才显示（或加 -v/--verbose）。
- 文件（app.log）：级别固定 DEBUG，记录全部细节（含步骤日志与时间/位置），
  便于事后排查偶发 403 / 下载失败等问题。

关于 f2 的 403 噪声：
  f2 在收到 403 时会用「同一个 loguru 单例」打两行 ERROR：
    - “HTTP状态错误：Client error '403 Forbidden' for url ...”（含完整请求 URL）
    - “请前往QA文档 https://f2.wiki/faq 查看相关帮助”
  这两行是 f2 底层噪声，且 URL 极长、刷屏。控制台（INFO）通过 _console_filter
  将其屏蔽，只保留我们自己的「遇风控/异常（第N次）重试」WARNING 与最终结果；
  文件（DEBUG）仍完整保留这些 403 详情，便于排查。即：403 详情仅出现在 DEBUG。

模块内统一 `from .logger import logger` 使用；`logger` 是 loguru 的全局单例，
未调用 setup_logger() 时仍可用（使用 loguru 默认配置）。
"""
import sys
from pathlib import Path

from loguru import logger as _logger

# 标记是否已初始化，setup_logger 重复调用时不会叠加多个 handler
_initialized = False

# f2 底层 403 噪声标记：命中即仅在 DEBUG（文件）留存，控制台（INFO）屏蔽
_F2_NOISE_TAGS = ("请前往QA文档", "HTTP状态错误")


def _console_format(record) -> str:
    """控制台格式：空消息渲染为纯空行（用于视频/链接之间的视觉分隔）。
    非空消息带时间戳 + 级别 + 正文。"""
    if not record["message"]:
        return "\n"
    return "{time:YYYY-MM-DD HH:mm:ss} | <level>{level: <8}</level> | {message}\n"


def _file_format(record) -> str:
    """文件格式：空消息渲染为纯空行；非空时带时间/级别/位置，便于排查。"""
    if not record["message"]:
        return "\n"
    return (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
        "{name}:{function}:{line} | {message}\n"
    )


def _console_filter(record) -> bool:
    """控制台过滤器：屏蔽 f2 底层 403 噪声（HTTP状态错误 / 请前往QA文档），
    仅保留业务日志（我们的重试 WARNING、最终结果 ERROR 等）。"""
    msg = record["message"]
    return not any(tag in msg for tag in _F2_NOISE_TAGS)


def setup_logger(level: str = "INFO", log_file: str = "app.log") -> None:
    """配置全局 logger。

    Args:
        level: 控制台日志级别（默认 "INFO"）。可选 DEBUG/INFO/WARNING/ERROR。
        log_file: 日志文件路径（默认项目根目录 app.log）；文件级别固定 DEBUG。
            相对路径相对于当前工作目录解析。
    """
    global _initialized
    # 清除默认 handler，避免与 loguru 内置重复输出
    _logger.remove()

    # ── 控制台：默认 INFO 及以上，简洁格式（带时间戳/级别名，方便区分） ──
    # 通过 filter 屏蔽 f2 的 403 噪声；403 详情仅写入文件（DEBUG）
    _logger.add(
        sys.stderr,
        level=level,
        colorize=True,
        format=_console_format,
        filter=_console_filter,
    )

    # ── 文件：固定 DEBUG，完整格式（时间/级别/模块:函数:行号），自动轮转 ──
    _logger.add(
        str(log_file),
        level="DEBUG",
        encoding="utf-8",
        rotation="10 MB",
        retention=5,
        enqueue=True,
        format=_file_format,
    )
    _initialized = True


# 对外暴露的 logger 实例（与 _logger 同一单例）
logger = _logger
