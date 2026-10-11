"""浏览器回退解析器（browser_fallback.py）

背景与根因（2026-10 实测确认）
--------------------------------
抖音 Web 端接口 `aweme/v1/web/aweme/detail/` 目前存在**双重校验**：

1. **`uifid` 参数**：缺失时服务端直接返回
   `403 Blocked by ArgusSecurityPlugin Uifid Not Found`。
   该值由页面 JS 生成，纯 HTTP 无法构造，但可以「抄」浏览器已生成的。
2. **`x-secsdk-web-signature` 签名**：这是字节 BDMS/Argus 安全 SDK 的
   **本地原生算法**产物（JS 混淆 + 浏览器指纹绑定），开源实现
   （f2 的 a_bogus 2024 版）已被服务端拒绝。补齐 uifid 后错误会变成
   `403 Blocked by ArgusSecurityPlugin Signature Not Found`。

实测排除项（避免重复踩坑）：

| 尝试路径                                        | 结果                                   |
| ----------------------------------------------- | -------------------------------------- |
| `www.douyin.com/video/{id}` SSR 页面            | 返回 JS 字节码挑战页 `_$jsvmprt`，无数据 |
| `www.iesdouyin.com/share/video/{id}/`          | 300 字节级同壳页，`videoInfoRes` 为空   |
| 同上 + 完整浏览器 cookie                        | 仍 302 跳回 www，无视频数据             |
| `web/api/v2/aweme/iteminfo/`（旧接口）          | `status_code:11110 encrypt_data_miss`   |
| `aweme.snssdk.com/aweme/v1/aweme/detail/`（App） | 200 但响应体为空                        |
| Web detail + f2 `a_bogus` + 完整 uifid/fp/webid  | `403 Signature Not Found`               |

**结论**：Web 接口在纯 HTTP 下已被彻底封死，第三方 App（即存等）能用是因为
它是**原生 App**，走的是 App 端协议而非 Web 签名协议。

本模块的解法
------------
不自己实现签名，而是**借真实浏览器生成签名**：脚本自己拉起一个
headless Chrome（带 `--remote-debugging-port`），通过 CDP(Chrome DevTools
Protocol) 导航到视频页。

v0.6.0 的做法是「等播放器把 `<video>` 填上直链后取走」，**v0.7.0 已改为主路径：
拦截页面自己发出的 `aweme/detail` 响应体**（见 `_DetailSniffer`）。

为什么不能只读 `<video>`（2026-10-10 实测定位的根因）
---------------------------------------------------
抖音 PC Web 的播放器对**部分**视频启用 MSE（Media Source Extensions），
此时 `<video>.currentSrc` 是 `blob:` 而非 http 直链，真实地址在网络层，
且被拆成**两条独立流**：

| URL 片段 | 内容 | MP4 轨（实测 box 解析） |
| --- | --- | --- |
| `.../media-video-avc1/` | 仅画面 | `['vide']` |
| `.../media-audio-und-mp4a/` | 仅声音 | `['soun']` |

后果有两条，都曾导致实际故障：

1. **取不到直链 → 误报"视频已删除/验证码"**。v0.6.0 的兜底 JS 用
   `.mp4/.m4s/.m3u8` 扩展名正则过滤性能记录，而抖音 VOD 地址形如
   `.../media-video-avc1/?a=6383&br=551...` —— **路径里没有扩展名**，
   于是连兜底也一并落空。实测故障视频 `7680879925243711922`：
   页面 `<video>` 的 `readyState=4`、`duration=855s`、`paused=false`、
   `error=null`（**播放完全正常**），却因上述两条规则一个 url 都没匹配到，
   最终抛出「页面已加载但未取到视频直链…可能触发了验证码或视频已删除」。
   页面标题、文案均正常读取，进一步证明视频存在——**是代码问题，不是视频问题**。
2. **即便取到 MSE 流，也是残缺文件**（无声或无画面），而非可播放的 mp4。

而 `aweme/detail` 响应里的 `video.bit_rate[]` / `video.play_addr` 是
**渐进式合流** MP4（实测 moov 内同时含 `vide` 与 `soun`），这才是正确来源。

好处：
- 零新增第三方依赖（`websockets` 由 f2 依赖链带入，CDP 用标准 HTTP+WS 实现）
- 不需要登录、不需要用户 cookie，用独立的临时 profile
- 拿到与 f2 接口**同构**的完整 JSON：直链、封面、原声、时长、分辨率、
  码率、作者、点赞/评论数、创建时间全部可用（v0.6.0 这些全部缺失）
- 解析更快：不必等播放器起播，接口响应通常 2~4 秒即到

代价：
- 需要本机安装 Chrome/Edge/Chromium
- 每个视频仍需数秒（等接口响应），比纯 HTTP 慢
- 故定位为**回退方案**：默认仍走 f2 接口，仅在接口被封时启用
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import httpx
import websockets

from .logger import logger

# macOS / Linux / Windows 上常见浏览器的可执行文件路径
_BROWSER_CANDIDATES = [
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/microsoft-edge",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

# 桌面 UA：与 config.user_agent 保持同代（Chrome 130），避免 headless 特征
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# 页面内取直链的 JS：播放器会把真实地址写进 <video>.currentSrc
# 注意 must-be 条件：必须是抖音/字节的 VOD 域名，排除 douyinstatic.com 下的
# 站点静态资源（首页引导视频之类），否则会误取到与 aweme 无关的 mp4。
_JS_GET_VIDEO_SRC = """
(() => {
  const HOST_OK = /douyinvod\\.com|bytev\\.com|ibytedtos\\.com|tos-cn-ve|aweme/.test(location.hostname) ? false : true;
  const ok = (u) => {
    if (!u || !u.startsWith('http')) return false;
    if (u.includes('douyinstatic.com') || u.includes('douyinpic.com')) return false;
    if (u.includes('douyinvod.com') || u.includes('bytev.com')) return true;
    return /\\.(mp4|m4s|m3u8)(\\?|$)/.test(u);
  };
  const out = [];
  for (const v of document.querySelectorAll('video')) {
    const u = v.currentSrc || v.src || '';
    if (ok(u)) out.push(u);
  }
  return out;
})()
"""

# 兜底：有时 video.src 是 blob:，真实地址在 <source> 或性能记录里
_JS_GET_FALLBACK_SRC = """
(() => {
  const out = [];
  const ok = (u) => u && u.startsWith('http')
    && !u.includes('douyinstatic.com') && !u.includes('douyinpic.com')
    && (u.includes('douyinvod.com') || u.includes('bytev.com')
        || /\\.(mp4|m4s)(\\?|$)/.test(u));
  document.querySelectorAll('video source').forEach(s => { if (ok(s.src)) out.push(s.src); });
  try {
    performance.getEntriesByType('resource').forEach(e => {
      const n = e.name || '';
      if (ok(n) && /\\.(mp4|m4s|m3u8)(\\?|$)/.test(n)) out.push(n);
    });
  } catch (e) {}
  return Array.from(new Set(out));
})()
"""

_JS_GET_TITLE = """
(() => {
  // 优先 h1 标题（页面 title 常被 SEO 后缀污染，如 "xxx - 抖音"）
  const h1 = document.querySelector('h1');
  return ((h1 && h1.innerText) || document.title || '').trim();
})()
"""

# 提取视频文案 / 作者 / 合集名（用于文件名与元数据）
# 实测选择器（2026-10，PC Web 视频页）：
#   文案 h1 的 innerText（已含 #话题 文本）
#   合集名 [data-e2e="video-detail"] 区块内的「合集 · xxx」文字
_JS_GET_DESC = """
(() => {
  const h1 = document.querySelector('h1');
  const desc = ((h1 && h1.innerText) || '').trim();
  let mixName = '';
  try {
    const detail = document.querySelector('[data-e2e="video-detail"]');
    if (detail) {
      const m = detail.innerText.match(/合集\\s*·\\s*([^\\n]+)/);
      if (m) mixName = m[1].trim();
    }
  } catch (e) {}
  let nickname = '';
  try {
    // 作者昵称在头像链接旁。注意排除「关注/粉丝/获赞」等按钮文字，
    // 它们同样是指向 /user/ 的链接，故只取 span 文案且过滤按钮词。
    const links = Array.from(document.querySelectorAll('a[href*="/user/"] span span'));
    const BAD = ['关注', '粉丝', '获赞', '我的', '登录', '头像'];
    for (const el of links) {
      const t = (el.innerText || '').trim();
      if (t && t.length <= 30 && !BAD.includes(t)) { nickname = t; break; }
    }
  } catch (e) {}
  return { desc, mix_name: mixName, nickname };
})()
"""


class BrowserNotFoundError(RuntimeError):
    """本机未找到可用的 Chrome/Edge/Chromium。"""


class BrowserResolveError(RuntimeError):
    """浏览器成功打开页面但未能取到视频直链。"""


@dataclass
class BrowserResolved:
    """浏览器解析结果。"""

    aweme_id: str
    play_url: str
    title: str = ""
    page_url: str = ""
    desc: str = ""
    mix_name: str = ""
    nickname: str = ""
    # v0.7.0：来自 aweme/detail 响应体的 API 侧元数据
    # （旧版只从 DOM 取标题，故封面/原声/时长等全部缺失）
    cover_url: str = ""
    music_play_url: str = ""
    raw_aweme: Optional[dict] = None


def find_browser() -> Optional[str]:
    """查找本机可用的 Chrome 系浏览器可执行文件路径。"""
    # 优先环境变量（允许用户覆盖，如 CHROME_PATH=/path/to/chrome）
    env = os.environ.get("DOUYINDL_CHROME_PATH")
    if env and Path(env).exists():
        return env
    for p in _BROWSER_CANDIDATES:
        if Path(p).exists():
            return p
    # 兜底：在 PATH 里找
    for name in ("google-chrome", "chrome", "chromium", "microsoft-edge", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


class ChromeLauncher:
    """以 CDP 模式管理一个无头 Chrome 实例的生命周期。

    职责：
    - 选定空闲调试端口并拉起浏览器（独立临时 profile，不污染用户日常 profile）
    - 等待 /json/version 就绪
    - 进程退出/关闭时清理临时目录
    """

    def __init__(self, port: int = 0, startup_timeout: float = 25.0):
        self.port = port
        self.startup_timeout = startup_timeout
        self.proc: Optional[subprocess.Popen] = None
        self.profile_dir: Optional[str] = None
        self.browser_path: Optional[str] = None

    @property
    def cdp_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> str:
        """拉起浏览器，返回 CDP 基址。失败抛 BrowserNotFoundError。"""
        exe = find_browser()
        if not exe:
            raise BrowserNotFoundError(
                "未找到 Chrome/Edge/Chromium，无法启用浏览器回退解析。"
                "请安装 Chrome，或设置环境变量 DOUYINDL_CHROME_PATH 指向浏览器可执行文件。"
            )
        self.browser_path = exe
        self.port = self.port or self._pick_port()
        self.profile_dir = tempfile.mkdtemp(prefix="douyindl-chrome-")

        cmd = [
            exe,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-features=Translate,BackForwardCache",
            "--disable-blink-features=AutomationControlled",
            "--mute-audio",
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--window-size=1280,900",
            "about:blank",
        ]
        logger.debug(f"启动浏览器解析: {exe} (CDP 端口 {self.port})")
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise BrowserNotFoundError(
                    f"浏览器进程启动即退出（returncode={self.proc.returncode}）"
                )
            try:
                r = httpx.get(f"{self.cdp_base}/json/version", timeout=2)
                if r.status_code == 200:
                    logger.debug("浏览器 CDP 就绪")
                    return self.cdp_base
            except Exception:
                pass
            time.sleep(0.4)
        self.close()
        raise BrowserNotFoundError(
            f"等待浏览器 CDP 就绪超时（{self.startup_timeout}s，端口 {self.port}）"
        )

    @staticmethod
    def _pick_port() -> int:
        """用系统分配的空闲端口，避免与既有调试端口冲突。"""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def close(self) -> None:
        """关闭浏览器并清理临时 profile。"""
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
            except Exception as e:
                logger.debug(f"关闭浏览器失败（忽略）: {e}")
        self.proc = None
        if self.profile_dir:
            shutil.rmtree(self.profile_dir, ignore_errors=True)
            self.profile_dir = None


# 合集页取视频列表的 JS。
# 实测（2026-10）：访问 /collection/{id} 会 302 到该合集的**第一个视频**页
# （标题即"第1课"），视频列表以 <a href="/video/{id}"> 形式出现在侧栏/底部推荐位。
# 因此合集解析 = 从列表取全部 aweme_id，再逐条走单视频解析取直链。
_JS_GET_MIX_LINKS = """
(() => {
  const out = [];
  document.querySelectorAll('a[href*="/video/"]').forEach(a => {
    const m = (a.getAttribute('href') || '').match(/\\/video\\/(\\d+)/);
    if (m) out.push(m[1]);
  });
  return Array.from(new Set(out));
})()
"""


class _CDPSession:
    """单个标签页的 CDP 会话（WebSocket JSON 协议）。

    只实现本模块需要的命令：Page/Runtime/Network 域。

    **并发模型（重要，勿改回"边等响应边丢事件"的写法）**
    CDP 的响应与事件走同一条 socket，必须由**唯一**的读循环统一收包：
    - 带 `id` 的消息 → 交给对应的 Future；
    - 其余为事件消息 → 分发给注册的回调（用于拦截 aweme/detail 响应体）。

    早期实现用 `call()` 里直接 `recv()` 直到匹配 id、把事件消息直接丢弃，
    那样无法监听事件。改成读循环后还有一条铁律：**读循环里不得 await 需要
    CDP 回包的命令**（如 `Network.getResponseBody`），否则读循环自己被挂起、
    永远收不到自己的回包 → 死锁。必须用 `asyncio.create_task` 派发出去。
    """

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._ws = None
        self._msg_id = 0
        self._pending: dict = {}
        self._reader_task = None
        self._event_handler = None
        self._closed = False

    async def __aenter__(self) -> "_CDPSession":
        # CDP 返回的帧可能较大（aweme/detail 响应体可达数百 KB），放宽 max_size
        self._ws = await websockets.connect(
            self.ws_url, max_size=256 * 1024 * 1024, ping_interval=None
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *exc) -> None:
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._reader_task = None
        # 唤醒仍在等待的调用，避免调用方永久挂起
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("CDP 会话已关闭"))
        self._pending.clear()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    def on_event(self, handler) -> None:
        """注册事件回调 `handler(method: str, params: dict)`（在读循环内同步调用）。"""
        self._event_handler = handler

    async def _read_loop(self) -> None:
        """唯一的 socket 读循环：带 id 的走 Future，其余走事件回调。"""
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mid = msg.get("id")
                if mid is not None and mid in self._pending:
                    fut = self._pending.pop(mid)
                    if not fut.done():
                        if "error" in msg:
                            fut.set_exception(
                                RuntimeError(f"CDP 错误: {msg['error'].get('message')}")
                            )
                        else:
                            fut.set_result(msg.get("result", {}))
                    continue
                if self._event_handler and msg.get("method"):
                    try:
                        self._event_handler(msg["method"], msg.get("params") or {})
                    except Exception:
                        # 事件回调异常不得影响读循环
                        pass
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def call(self, method: str, params: Optional[dict] = None,
                   timeout: float = 30.0) -> dict:
        """发送一条 CDP 命令并等待对应 id 的响应。"""
        if self._ws is None or self._closed:
            raise ConnectionError("CDP 会话未打开")
        self._msg_id += 1
        mid = self._msg_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await self._ws.send(
                json.dumps({"id": mid, "method": method, "params": params or {}})
            )
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(mid, None)

    async def eval(self, expression: str, timeout: float = 15.0):
        """在页面里执行 JS 并取回可序列化结果。"""
        res = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
                "timeout": int(timeout * 1000),
            },
        )
        if res.get("exceptionDetails"):
            raise RuntimeError(
                f"页面 JS 执行异常: {res['exceptionDetails'].get('text')}"
            )
        return (res.get("result") or {}).get("value")

    async def setup(self, user_agent: str) -> None:
        """启用 Page/Runtime 域并设置 UA。

        UA 必须覆盖：默认 headless UA 里含 "HeadlessChrome"，抖音 CDN 与
        风控会识别该特征，导致返回的直链被降级或拒绝。

        Network 域需开大缓冲：aweme/detail 响应体可达数百 KB，默认缓冲会被
        回收，导致 `getResponseBody` 取不到。
        """
        await self.call("Page.enable")
        await self.call("Runtime.enable")
        await self.call("Network.enable", {
            "maxTotalBufferSize": 64 * 1024 * 1024,
            "maxResourceBufferSize": 64 * 1024 * 1024,
        })
        await self.call("Emulation.setUserAgentOverride", {
            "userAgent": user_agent,
            "acceptLanguage": "zh-CN,zh;q=0.9",
            "platform": "Win32",
        })


class _DetailSniffer:
    """拦截页面自身发出的 `aweme/detail` 响应体（v0.7.0 核心）。

    不自己实现签名，而是让真实浏览器把带签名的请求发出去，再把**响应体**
    截下来解析。这样既绕开签名，又拿到与 f2 接口同构的完整 JSON。
    """

    def __init__(self, session: "_CDPSession", aweme_id: str = ""):
        self._session = session
        self.aweme_id = aweme_id
        self.aweme: Optional[dict] = None
        self._req_ids: set = set()
        self._tasks: set = set()

    def handle(self, method: str, params: dict) -> None:
        """事件回调（由 _CDPSession 读循环同步调用）。"""
        if method == "Network.requestWillBeSent":
            req_id = params.get("requestId")
            url = ((params.get("request") or {}).get("url")) or ""
            if "/aweme/v1/web/aweme/detail/" in url:
                self._req_ids.add(req_id)
        elif method == "Network.loadingFinished":
            req_id = params.get("requestId")
            if req_id in self._req_ids:
                # 必须派发独立 task：读循环内 await CDP 回包会死锁
                t = asyncio.create_task(self._grab(req_id))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)

    async def _grab(self, req_id: str) -> None:
        try:
            res = await self._session.call("Network.getResponseBody", {"requestId": req_id})
        except Exception:
            return
        body = res.get("body") or ""
        if not body:
            return
        if res.get("base64Encoded"):
            try:
                body = base64.b64decode(body).decode("utf-8", "ignore")
            except Exception:
                return
        try:
            data = json.loads(body)
        except Exception:
            return
        detail = data.get("aweme_detail")
        if not isinstance(detail, dict):
            return
        # 页面可能重复发该请求；只接受与目标 id 一致的响应
        if self.aweme_id and str(detail.get("aweme_id")) != str(self.aweme_id):
            return
        if not ((detail.get("video") or {}).get("play_addr")):
            return
        self.aweme = detail


def _pick_best_muxed_url(aweme: dict) -> str:
    """从 aweme_detail 挑一条**音视频合流**的最佳 MP4 直链。

    关键约束（2026-10-10 实测）：抖音播放器对部分视频走 MSE 分片流，
    `<video>` 上是 `blob:` 地址，网络层被拆成 `media-video-*`（仅画面）与
    `media-audio-*`（仅声音）两条，**任取其一都是残缺文件**。

    而 `video.bit_rate[]` / `video.play_addr` 给的是**渐进式合流** MP4
    （实测 moov 内同时含 vide 与 soun 轨），必须优先用它们。

    选取顺序：
    1. `bit_rate[]` 中码率最高且非 H.265 的档位（H.265 兼容性差，优先 H.264）
    2. `play_addr`（默认档）
    3. `play_addr_h264` / `play_addr_265` 兜底

    Returns:
        直链；无可用地址时返回空字符串
    """
    video = aweme.get("video") or {}

    def first_url(node: dict) -> str:
        for u in ((node or {}).get("url_list") or []):
            # 显式排除 audio 分片，即便混入也只取视频合流地址
            if u and u.startswith("http") and "media-audio" not in u:
                return u
        return ""

    cands = []
    for br in video.get("bit_rate") or []:
        u = first_url(br.get("play_addr"))
        if u:
            # 排序键：非 H.265 优先，其次码率高
            cands.append((0 if br.get("is_h265") else 1, br.get("bit_rate") or 0, u))
    if cands:
        cands.sort(key=lambda x: (-x[0], -x[1]))
        return cands[0][2]

    for key in ("play_addr", "play_addr_h264", "play_addr_265"):
        u = first_url(video.get(key))
        if u:
            return u
    return ""


def _aweme_to_resolved(aweme: dict, aweme_id: str) -> BrowserResolved:
    """aweme_detail → BrowserResolved（含封面/原声等 API 侧元数据）。"""
    video = aweme.get("video") or {}
    author = aweme.get("author") or {}
    music = aweme.get("music") or {}
    desc = (aweme.get("desc") or "").strip()

    cover = ""
    for key in ("cover", "origin_cover", "dynamic_cover"):
        urls = (video.get(key) or {}).get("url_list") or []
        if urls:
            cover = urls[0]
            break

    music_url = ((music.get("play_url") or {}).get("url_list") or [""])[0]

    return BrowserResolved(
        aweme_id=str(aweme.get("aweme_id") or aweme_id),
        play_url=_pick_best_muxed_url(aweme),
        title=desc,
        page_url=f"https://www.douyin.com/video/{aweme.get('aweme_id') or aweme_id}",
        desc=desc,
        mix_name="",
        nickname=author.get("nickname") or "",
        cover_url=cover,
        music_play_url=music_url,
        raw_aweme=aweme,
    )


def _pick_best_src(urls: List[str], aweme_id: str = "") -> Optional[str]:
    """从候选直链里挑最优的一条。

    抖音播放器会挂多个 <video>（主视频 + 预加载/广告），选取优先级：

    1. URL 的 `__vid` 与目标 aweme_id 一致 —— **最关键**，否则可能取到
       页面上其它播放器（如站点引导视频）的地址
    2. 是抖音 VOD 域名（douyinvod / bytev）
    3. 码率（br/bt）越大画质越好

    规则保持简单稳定，不引入难以调试的启发式。
    """
    cands = [u for u in urls if u and u.startswith("http")]
    if not cands:
        return None

    def score(u: str) -> tuple:
        vid_match = 1 if (aweme_id and f"__vid={aweme_id}" in u) else 0
        is_vod = 1 if ("douyinvod" in u or "bytev" in u) else 0
        br = 0
        for key in ("br=", "bt="):
            idx = u.find(key)
            if idx >= 0:
                tail = u[idx + len(key):].split("&")[0]
                try:
                    br = max(br, int(tail))
                except ValueError:
                    pass
        return (vid_match, is_vod, br)

    return max(cands, key=score)


async def _resolve_one(
    cdp_base: str, aweme_id: str, user_agent: str, wait_seconds: float
) -> BrowserResolved:
    """打开单个视频页并提取直链（供单个 aweme 使用）。

    主路径（v0.7.0）：**拦截 `aweme/detail` 响应体**。
    页面自己会带签名去请求该接口，我们把响应体截下来解析，得到与 f2 同构的
    `aweme_detail` —— 既有合流直链，又有封面/原声/时长等完整元数据。

    兜底路径：读 `<video>.currentSrc`。仅在拦截失败时启用（见 `_pick_best_src`
    关于 blob: 与 MSE 分片的说明，兜底路径拿到的可能不完整）。
    """
    async with httpx.AsyncClient(proxy=None, timeout=15) as client:
        tab = (await client.put(f"{cdp_base}/json/new?about:blank")).json()
    tab_id, ws_url = tab.get("id"), tab.get("webSocketDebuggerUrl")
    if not ws_url:
        raise BrowserResolveError("创建浏览器标签页失败")

    try:
        async with _CDPSession(ws_url) as s:
            await s.setup(user_agent)
            sniffer = _DetailSniffer(s, aweme_id)
            s.on_event(sniffer.handle)
            await s.call("Page.navigate", {"url": f"https://www.douyin.com/video/{aweme_id}"})

            deadline = time.monotonic() + wait_seconds
            srcs: List[str] = []
            while time.monotonic() < deadline:
                # 拦截到即返回（通常 2~4 秒，不必等播放器起播）
                if sniffer.aweme:
                    got = _aweme_to_resolved(sniffer.aweme, aweme_id)
                    if got.play_url:
                        logger.debug(
                            f"拦截 aweme/detail 成功: {aweme_id} "
                            f"desc={len(got.desc)}字"
                        )
                        return got
                await asyncio.sleep(0.4)
                # 兜底轮询：<video> 元素
                if not srcs:
                    try:
                        got = await s.eval(_JS_GET_VIDEO_SRC)
                        if got:
                            srcs = list(got)
                    except Exception:
                        pass

            if not _pick_best_src(srcs, aweme_id):
                # 兜底：查 <source> 与性能记录（player 用 blob: 时会命中）
                try:
                    got = await s.eval(_JS_GET_FALLBACK_SRC)
                    if got:
                        srcs = list(got)
                except Exception:
                    pass

            try:
                title = await s.eval(_JS_GET_TITLE) or ""
            except Exception:
                title = ""
            meta = {}
            try:
                meta = await s.eval(_JS_GET_DESC) or {}
            except Exception:
                meta = {}

        best = _pick_best_src(srcs, aweme_id)
        if not best:
            raise BrowserResolveError(
                f"页面已加载但未取到视频直链（aweme_id={aweme_id}）。"
                f"已拦截 aweme/detail 但未获得可用 play_addr，"
                f"可能触发了验证码或视频已删除，页面标题：{title!r}"
            )
        logger.debug(f"aweme/detail 拦截失败，回退 <video> 取直链: {aweme_id}")
        return BrowserResolved(
            aweme_id=aweme_id,
            play_url=best,
            title=title,
            page_url=f"https://www.douyin.com/video/{aweme_id}",
            desc=(meta.get("desc") or title or "").strip(),
            mix_name=(meta.get("mix_name") or "").strip(),
            nickname=(meta.get("nickname") or "").strip(),
        )
    finally:
        # 无论成功失败都关掉标签页，避免标签堆积
        try:
            async with httpx.AsyncClient(proxy=None, timeout=8) as client:
                await client.get(f"{cdp_base}/json/close/{tab_id}")
        except Exception:
            pass


async def _collect_mix_ids(cdp_base: str, mix_id: str, user_agent: str,
                           wait_seconds: float) -> List[str]:
    """打开合集页，收集其中列出的全部 aweme_id。

    实测：/collection/{id} 会跳到合集第一个视频页，视频列表以
    `<a href="/video/{id}">` 形式出现在侧栏与"合集"推荐位。通过滚动促使
    懒加载更多条目，直到连续数轮无新增或超时。

    **收敛判据：只在「已经采到过 id」之后才允许判定完成**
    （2026-10-10 实测踩坑）：页面加载需要数秒，期间 DOM 里一个
    `<a href="/video/">` 都没有。若把加载期的空结果计入「无新增」轮次，
    会在列表渲染前就收敛、返回空列表，最终误报「合集页未列出任何视频…」。

    但**不能**改用 `document.readyState === "complete"` 作为守卫：
    实测 `/collection/{id}` 会 302 到合集第一个视频页，`complete` 时侧栏已
    被「推荐流」灌满，采到的是**与合集无关的视频**（实测取到高考/ChatGPT/
    幼儿园等推荐内容）。侧栏条目按渲染顺序出现，合集在前、推荐在后，
    故只要在列表成型前就开始采，拿到的就是合集本身。
    """
    async with httpx.AsyncClient(proxy=None, timeout=15) as client:
        tab = (await client.put(f"{cdp_base}/json/new?about:blank")).json()
    tab_id, ws_url = tab.get("id"), tab.get("webSocketDebuggerUrl")
    if not ws_url:
        raise BrowserResolveError("创建浏览器标签页失败")

    seen: List[str] = []
    seen_set = set()
    try:
        async with _CDPSession(ws_url) as s:
            await s.setup(user_agent)
            await s.call("Page.navigate", {
                "url": f"https://www.douyin.com/collection/{mix_id}"
            })
            deadline = time.monotonic() + wait_seconds
            stable = 0
            while time.monotonic() < deadline:
                await asyncio.sleep(1.0)
                try:
                    ids = await s.eval(_JS_GET_MIX_LINKS) or []
                except Exception:
                    continue
                new = [i for i in ids if i not in seen_set]
                for i in new:
                    seen_set.add(i)
                    seen.append(i)
                stable = stable + 1 if not new else 0
                # 连续 3 轮无新增视为收集完成。
                # 必须已有 id 才允许收敛：否则加载期的空结果会被误判为"收集完毕"
                if seen and stable >= 3:
                    break
                try:
                    await s.eval(
                        "() => { window.scrollBy(0, window.innerHeight); return 1; }"
                    )
                except Exception:
                    pass
        return seen
    finally:
        try:
            async with httpx.AsyncClient(proxy=None, timeout=8) as client:
                await client.get(f"{cdp_base}/json/close/{tab_id}")
        except Exception:
            pass


async def _resolve_mix(
    cdp_base: str, mix_id: str, user_agent: str, wait_seconds: float, max_items: int
) -> List[BrowserResolved]:
    """解析合集：先收集 aweme_id 列表，再逐条取直链。

    合集接口（mix/list）同样走 Web 签名被封死，故只能借页面拿列表。
    逐条解析在**同一个浏览器实例**内完成，避免反复启动 Chrome。
    """
    ids = await _collect_mix_ids(cdp_base, mix_id, user_agent, wait_seconds)
    if not ids:
        raise BrowserResolveError(
            f"合集页未列出任何视频（mix_id={mix_id}），可能触发验证码或合集已删除"
        )

    if max_items and len(ids) > max_items:
        ids = ids[:max_items]

    logger.info(f"合集 {mix_id} 列出 {len(ids)} 个视频，逐条解析直链")
    results: List[BrowserResolved] = []
    for idx, aid in enumerate(ids, 1):
        try:
            got = await _resolve_one(cdp_base, aid, user_agent,
                                     wait_seconds=min(wait_seconds, 25.0))
            results.append(got)
            logger.debug(f"合集解析进度 {idx}/{len(ids)}: {aid}")
        except Exception as e:
            # 单条失败不影响同批其它条目（对齐原版 4.2「探针失败只作废这一条」）
            logger.warning(f"合集内第 {idx}/{len(ids)} 条解析失败（{aid}）: {e}")

    if not results:
        raise BrowserResolveError(f"合集 {mix_id} 内所有条目解析均失败")
    return results


async def resolve_via_browser(
    kind: str,
    resource_id: str,
    *,
    user_agent: str = _BROWSER_UA,
    port: int = 0,
    wait_seconds: float = 25.0,
    max_items: int = 0,
) -> List[BrowserResolved]:
    """浏览器回退解析入口。

    Args:
        kind: 'mix'（合集）或 'one'（单视频）
        resource_id: mix_id 或 aweme_id
        user_agent: 覆盖的 UA（须非 headless）
        port: CDP 端口，0 表示自动选空闲端口
        wait_seconds: 等待播放器起播的最长秒数
        max_items: 合集最多收集多少条，0 表示不限

    Returns:
        解析结果列表（单视频也返回长度 1 的列表）

    Raises:
        BrowserNotFoundError: 本机无可用浏览器
        BrowserResolveError: 浏览器可用但未取到直链
    """
    launcher = ChromeLauncher(port=port)
    launcher.start()
    try:
        if kind == "mix":
            return await _resolve_mix(
                launcher.cdp_base, resource_id, user_agent, wait_seconds, max_items
            )
        got = await _resolve_one(launcher.cdp_base, resource_id, user_agent, wait_seconds)
        return [got]
    finally:
        launcher.close()