# CLAUDE.md

本文件为在本项目（douyinDL）中工作的 AI 助手与人类协作者提供指引。请先读 `README.md` 与 `docs/` 了解全貌，本文件聚焦「约定、命令、架构、坑点」。

## 项目速览

- **douyinDL**：基于 [f2](https://github.com/Johnserf-Seed/f2) 库的**免登录**抖音视频/合集下载工具，支持合集与单视频。
- **核心能力**：短链解析、匿名 ttwid 认证、合集分页拉取、无水印 mp4 流式下载、元数据（封面/文案/原声/JSON）保存、SQLite 进度持久化（断点续传 + 增量下载）、失败自动重试与 `--retry-failed` 重跑。
- **技术栈**：Python `>=3.12,<3.14`（f2 的 pydantic-core 不支持 3.14）；`uv` 管理依赖；`f2==0.0.1.7` + `pyyaml`；SQLite 为标准库，无额外依赖。

## 必须遵守的约定

1. **运行必须禁用代理**：抖音是国内服务，环境代理变量会干扰请求。每次运行都要前置 `NO_PROXY='*' no_proxy='*'`。代码内部 `build_crawler_kwargs` 与 `download_video`/`_download_simple` 也已显式 `proxy=None`，但 shell 层面仍需禁用。
2. **一律用 `uv run` 运行**，禁止系统级 `pip install` 或裸 `python`。依赖锁定在 `uv.lock`。
3. **Git commit 信息用中文**（见 `.trae/rules/git-commit-message.md`，`alwaysApply: true`）。历史提交风格为 `feat: xxx` / `fix: xxx`。
4. **配置驱动**：所有运行参数集中在 `config/config.yaml`，改参数优先改配置而非改代码。修改配置后无需改动代码。
5. **UA 必须与 f2 一致**：`config.yaml` 的 `user_agent` 须与 f2 的 `conf/conf.yaml` 一致，否则 ABogus 签名校验失败导致 API 拉取失败。

## 常用命令

```bash
# 初始化环境（首次）
uv python pin 3.12
uv sync

# 下载合集（直接粘分享文本即可，脚本自动提取 URL）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "0- 6.10 teO:/ ... https://v.douyin.com/SlGTwuMq498/ 8@9.com" -o ./downloads

# 单视频
NO_PROXY='*' no_proxy='*' uv run python -m douyindl "https://v.douyin.com/LQVBJcukSyA/"

# 仅下载原声 MP3
NO_PROXY='*' no_proxy='*' uv run python -m douyindl "https://v.douyin.com/SlGTwuMq498/" -m music

# 从文件读取链接（多行/空格分隔，下载后自动清空文件）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl -i links.txt -o ./downloads

# 重试数据库中所有失败记录（无需 url/-i）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl -r
```

### CLI 参数速查
| 参数 | 说明 | 默认 |
|------|------|------|
| `url` | 分享链接/分享文本（可选，支持空格分隔多链接）；与 `-i` 至少其一 | - |
| `-i, --input-file` | 从文件读链接，下载后清空文件 | - |
| `-o, --output` | 输出根目录 | `./downloads` |
| `-n, --max-counts` | 最大下载数，0=不限 | `0` |
| `-c, --config` | 配置文件路径 | `config/config.yaml` |
| `-f, --force` | 强制重下，忽略进度库 | 关 |
| `-r, --retry-failed` | 重试失败记录 | 关 |
| `-m [TYPES]` | 元数据 `all`/`cover`/`desc`/`music`/`json`（逗号分隔）；仅 `-m`=all | 关 |

## 项目结构

```
douyinDL/
├── .trae/
│   ├── rules/git-commit-message.md   # 中文 commit 规则（alwaysApply）
│   └── skills/douyin-download/SKILL.md  # AI Skill 定义（触发条件/用法）
├── config/config.yaml                # 全部运行参数（核心可配点）
├── docs/                             # 按主题拆分文档（01~07）
├── src/douyindl/
│   ├── __init__.py                   # 导出 DouyinDownloader, main
│   ├── __main__.py                   # python -m douyindl 入口
│   ├── downloader.py                 # 核心逻辑（约 2000 行，唯一大文件）
│   ├── browser_fallback.py           # v0.6.0 浏览器回退解析（CDP）
│   ├── download_engine.py            # v0.6.0 并发分段下载引擎（jicun-dl 移植）
│   └── logger.py                     # loguru 双 sink 配置
├── pyproject.toml                    # 依赖与脚本入口
└── uv.lock
```

`downloader.py` 是唯一核心模块，建议改动前通读：关键类/函数见 `docs/07-faq.md` 附录代码索引。

> **v0.6.0 新增依赖说明**：`websockets`（CDP 通信）**未**写进 `pyproject.toml`——它是 f2 依赖链间接带入的。已在 f2 升级时验证可用。若将来 f2 换掉这条依赖链，需显式 `uv add websockets`，否则 `browser_fallback` 会 ImportError。

## 架构要点（downloader.py）

- **`Config`**：从 `config.yaml` 加载，逐项带默认值兜底；`DEFAULT_CONFIG_PATH` 由 `__file__` 向上三级定位到项目根 `config/config.yaml`。
- **`resolve_share_url`**：支持短链 `v.douyin.com`、合集 `iesdouyin.com/share/mix/detail/{id}` 或 `douyin.com/collection/{id}`、单视频 `douyin.com/video/{id}`；短链跟随重定向后用正则 `_MIX_PATH_PATTERN` / `_AWEME_ID_PATTERN` 提取 `mix`/`one` 类型与 ID。
- **匿名认证**：`build_cookie(config)` 优先复用「会话预热」拿到的首页 cookie（`__ac_nonce` 等），再补真实 `ttwid`（`gen_ttwid()`，每次新鲜）+ 假 `msToken`（`gen_false_msToken()`）；无需登录。若 `config.login_cookie` 非空则直接返回该真实 Cookie、跳过匿名生成（绕过匿名风控，详见同节「系统性 403」）。根因见「403 风控根因与应对」节。
- **视频列表**：`fetch_mix_videos`（分页循环，受 `page_counts`/`api_request_interval` 节流）与 `fetch_one_video`，分别用 f2 的 `UserMixFilter._to_list()` 与 `PostDetailFilter._to_dict()`。
- **API 请求重试层**：`fetch_one_video` 与 `fetch_mix_videos` 的接口调用统一经 `_request_with_retry` 包裹。原因：f2 的 `base_crawler.handle_http_status_error` 对 403/429/5xx 等 HTTP 状态错误是 `else` 分支**直接抛异常、不进入重试循环**（它只在响应为空时才重试）。抖音对 `aweme/detail` 等接口存在**间歇性风控（偶发 403 Forbidden）**，表现为批量下载时零星失败——而换新会话（换 PC 链接/重跑）往往就成功。本层补上重试：**每次重试都重新生成匿名 ttwid cookie（换会话）**，退避用 `_random_interval(max(api_request_interval, 2.0))`，仅对 `APIResponseError`/`APIRateLimitError`/`APIConnectionError`/`APITimeoutError` 重试，逻辑错误不重试。重试次数由 `config.api_max_retries`（默认 3）控制。另见「会话预热」：启动先 GET 抖音首页并入首页 cookie 进一步降低首打 403；`config.api_request_interval` 调大（默认 5.0s）亦可降首打概率（详见「403 风控根因与应对」节）。
- **`_extract_video_meta`**：f2 filter 会丢失 `video.*` 嵌套字段，此函数从原始 JSON 补提（duration/width/height/file_size/bit_rate/fps/ratio/video_format/is_h265/作者/统计/创建时间），用于入库。
- **下载**：`download_video` 默认走**并发分段下载**（`download_engine.download_concurrent`，移植 jicun-dl）；`config.concurrent_download=false` 时退回旧的单连接顺序流式实现。两条路径实测产物逐字节一致。限速由 `max_download_speed` 按累计字节节流（并发模式在进度回调里用 `time.sleep`，单连接模式在主循环里用 `asyncio.sleep`）。`_download_simple` 用于小文件（封面/原声）。
- **`ProgressDB`**：SQLite 表 `downloaded_videos`，主键 `aweme_id`；`_migrate_schema` 用 `PRAGMA table_info` 检测缺失列并 `ALTER TABLE ADD COLUMN` 做版本迁移（兼容 v0.3→v0.5 旧库）。`status` 字段记录 `success`/`failed`，失败不跳过、可重跑。
- **`DouyinDownloader`**：主流程 `run()`（解析→拉列表→下载→入库），`_download_with_retry`（受 `download_max_retries`）、`retry_failed()`（查失败记录→重新取地址→下载→更新库）。`main()` 为 CLI 入口，多链接串行、去重、下载后清空 `-i` 文件。
- **风控**：合集视频间隔、多链接间隔、失败重试间隔均通过 `_random_interval(base)` 在 `[base*0.9, base]` 随机取值，避免固定间隔被识别。
- **日志（loguru，双写）**：`src/douyindl/logger.py` 的 `setup_logger()` 配置两个 sink——**控制台（stderr）级别由 `config.log_level` 控制（默认 INFO）** + **文件 `app.log` 固定 DEBUG**。代码内已按级别区分：`DEBUG`=`[1/4]~[4/4]` 步骤脚手架/列表枚举/等待间隔（含 `[4/4] 完成` 步骤标记）、`保存目录` 路径、合集累计下载数等细节；`INFO`=单条结果（解析到的资源类型/ID、每条下载成功+元数据、跳过原因、完成汇总 `完成: 成功 X/N，跳过 Y`、多链接总结）；`WARNING`=偶发风控/下载的可恢复重试提示；`ERROR`=真实失败（重试耗尽/异常/参数错误）。原 `print()` 已全部替换为 `logger.xxx`。注意：`download_video` 内的实时进度条仍直接写 stdout（非日志）。CLI 加 `-v/--verbose` 让控制台也降到 DEBUG。
  - **视觉约定（避免「丑/乱」）**：① 单视频**不显示** `[1/1]` 序号（仅合集多视频才显示 `[i/N]`，避免误导）；② `完成` 汇总行在 INFO 层不带 `[4/4]` 前缀（`[4/4]` 步骤标记只在 DEBUG 成组出现，默认控制台不会看到孤立的 `[4/4]`）；③ **视频之间、链接之间用空行分隔**——通过发送空消息触发 loguru 纯空行（logger.py 的 `_console_format`/`_file_format` 对空消息返回 `"\n"`），不要用带 `INFO |` 前缀的空行。

## 403 风控根因与应对（重要）

批量下载时常见「第一次请求 403 Forbidden → 重试（换新 ttwid）→ 成功」的现象。根因与应对如下：

**请求身份是「合成匿名」的**：`build_cookie()` 的 `msToken` 永远是**假的**随机串（`gen_false_msToken()`）；`ttwid` 是抖音下发的真实值，但属于**「裸」会话**——只有 ttwid 一个 cookie，没有真实浏览器先逛一遍 douyin.com 会累积的 `sid_tt`/`odin_tt`/`passport_csrf_token` 等「已被访问」cookie。抖音 `aweme/detail` 接口会对这种冷启动匿名会话做**概率性挑战**，命中即 403。

**为什么「第一次 403、重试就成功」**：重试时 `_request_with_retry` 重新 `build_cookie()`——生成**全新 ttwid（= 新会话）+ ~2 秒带抖动退避**，相当于「换 PC 链接重跑」= 重新掷一次骰子，大概率放行。若真是 IP 硬封禁，重试也会失败；重试几乎总能成功，恰恰证明它是**按次概率放行**而非硬封。

**为什么「感觉现在 80% 都 403」**：大概率是**可见性变化、不是频率变高**——加重试前 f2 遇 403 直接失败（你看到「下载失败」或手动重跑），现在重试会显式打印 WARNING，于是本就存在的 403 第一次被看见。除非对比历史成功率确证下降，否则先按「可见性变了」理解；若确变高，通常是抖音收紧匿名接口风控、或本机因下载量大 IP 风险分升高。

**有影响吗**：没有任何影响。每条视频只多付一次 ~2 秒退避，结果完整正确，不缺文件、不损坏；403 日志是预期噪声（控制台已屏蔽 f2 的 403 大段 URL 噪声，仅 `app.log` 留存）。

**缓解措施（均已实施，可按需调参）**：
- `config.api_request_interval` 调大（默认 5.0s）：同时作为风控重试退避 base，请求更稀疏、首打 403 概率更低，代价是整体更慢。
- **会话预热**：启动（CLI `main()`）时先 GET 抖音首页，把首页下发的 cookie（`__ac_nonce` 等）并入后续所有 API 请求（`_warm_up_session()`，结果缓存一次）。作用：让请求比纯裸 ttwid 更像「连续会话」。
  - 局限：`sid_tt`/`odin_tt` 等需浏览器 JS 才下发，纯 HTTP 拿不到，故预热只能补充首页少量 cookie + 真实 ttwid，无法完全模拟登录态浏览器；要进一步降 403 需引入无头浏览器（playwright）取完整 cookie 罐——当前未做。
- 重试次数 `api_max_retries`（默认 3）兜底最终失败；仍失败则写入 `urls_fail`（`时间 原因 url`）供手工重试。

### 系统性 403（服务端签名更新）—— 与「间歇性概率 403」不同

上述是**间歇性、按次概率**的 403（重试换会话即可成功）。还存在另一种**系统性、必然**的 403：某天开始所有请求（含换新 ttwid 重试 3 次）**全部 403**，无论重试多少次都不成功。

**根因（2026-10 实测确认）**：抖音 Web 端 `aweme/v1/web/aweme/detail/` 现要求**两样东西**，纯 HTTP 都拿不到：

1. **`uifid` 查询参数**——缺失时服务端返回 `403 Blocked by ArgusSecurityPlugin Uifid Not Found`。该值由页面 JS 生成。
2. **`x-secsdk-web-signature` 签名**——字节 BDMS/Argus 安全 SDK 的**本地原生算法**产物（JS 混淆 + 浏览器指纹绑定）。补齐 uifid 后错误变为 `403 Blocked by ArgusSecurityPlugin Signature Not Found`。

证据链（全部为 2026-10-10 实测）：

1. **「依赖过期」假设不成立**：`uv pip install --dry-run "f2>0.0.1.7"` 结果 `Because only f2<=0.0.1.7 is available ... unsatisfiable` —— **f2 0.0.1.7 即 PyPI 最新版**。
2. **非本机 IP 单封**：在完全不同 IP/网络的干净环境实测同一接口，同样 403。
3. **补齐 uifid + fp + webid 仍不够**：错误从 `Uifid Not Found` 变为 `Signature Not Found`，证明签名是独立的第二道校验。
4. **f2 的 a_bogus 已过期**：补齐 uifid/fp/webid 后用 f2 `ABogus.generate_abogus()` 签名，仍 `Signature Not Found`。服务端已换成 SecSDK 系校验。
5. **SSR 分享页已无数据**：`www.douyin.com/video/{id}` 返回 JS 字节码挑战页（`_$jsvmprt`）；`www.iesdouyin.com/share/video/{id}/` 返回 `window._ROUTER_DATA` 但 `videoInfoRes` 为空——加完整浏览器 cookie 也一样（仍 302 跳回 www）。
6. **旧接口全废**：`web/api/v2/aweme/iteminfo/` 返回 `status_code:11110 encrypt_data_miss`；`aweme.snssdk.com/aweme/v1/aweme/detail/`（App 端）返回 200 但**响应体为空**。
7. **f2 内有未被使用的 `XBogus`**：`f2/utils/xbogus.py` 存在但无任何接口调用，实测也无法解锁上述端点。

**结论**：Web 接口在纯 HTTP 下已被彻底封死。**第三方 App（如「即存」）能用是因为它是原生 App，走 App 端协议而非 Web 签名协议**——不是它的"解析算法更先进"。

**应对（v0.6.0 起新增浏览器回退，见下节）**：

- **① 浏览器回退解析 `browser_fallback.py`（当前主力）**：不自己实现签名，而是**借真实浏览器生成签名**——脚本拉起 headless Chrome（`--remote-debugging-port`），用 CDP 导航到视频页，等播放器把 `<video>` 填上直链后取走。零新增第三方依赖（`websockets` 由 f2 依赖链带入），不需登录、不需用户 cookie。
- **② 连通性探针（金丝雀）`_canary_probe`**：批量下载前探测 f2 接口；`resolve_mode=auto` 时探针失败**自动降级浏览器并继续**，不再 `sys.exit(2)` 中止。
- **③ 真实登录态 Cookie `login_cookie`**：填入浏览器登录后的完整 Cookie。注意——登录态只能绕过**风控**，**无法绕过签名校验**（签名是服务端的硬要求，与是否登录无关）。
- **④ 依赖自检 `_check_deps_fresh`**：一旦 f2 发布新版自动升级并重启。当前仍是 no-op。

### v0.6.0：浏览器回退解析（`browser_fallback.py`）

**设计目标**：抖音 Web 接口签名被封死后，仍要能免登录下载。

**做法**：不自己实现签名，而是**借真实浏览器生成签名**。

| 环节 | 实现 |
| --- | --- |
| 拉起浏览器 | `ChromeLauncher`：自选空闲端口 + `--headless=new` + 独立临时 profile（不污染用户日常 profile），轮询 `/json/version` 判就绪 |
| 驱动 | `_CDPSession`：WebSocket JSON（标准库 + `websockets`）。**唯一读循环 + Future 分发**（见 v0.7.0 踩坑） |
| **覆盖 UA** | 必须 `Emulation.setUserAgentOverride`——默认 headless UA 含 `HeadlessChrome`，抖音 CDN 会识别该特征 |
| 取直链 | ~~`Runtime.evaluate` 轮询 `<video>.currentSrc`~~ **v0.7.0 已改为拦截 `aweme/detail` 响应体**，本项仅作兜底 |
| 直链筛选 | `_pick_best_src`（兜底路径用）：**① `__vid` 与目标 aweme_id 一致 → ② 抖音 VOD 域名 → ③ 码率高** |
| 元数据 | ~~文案取 `h1.innerText`~~ **v0.7.0 起从接口 JSON 取全**（封面/原声/时长/分辨率/码率/作者/统计） |
| 合集 | `_collect_mix_ids` 从合集页 `a[href*="/video/"]` 收集 aweme_id（滚动加载），再**逐条**走单视频解析 |
| 清理 | 标签页用完即关（成功/失败都关），浏览器 `terminate()` + 删临时 profile |

**`resolve_mode` 三态**（`config.yaml`）：

| 值 | 行为 |
| --- | --- |
| `auto`（默认） | 先 f2，探测到系统性 403 自动降级浏览器；降级后**记在 config 上**，同批后续不再重复试 f2 |
| `f2` | 只用 f2（快，但签名被封时整体失败） |
| `browser` | 只用浏览器（慢但稳，不依赖 f2） |

`_is_systemic_block()` 区分「系统性拦截」与「偶发风控」：含 `403`/`Forbidden`/`ArgusSecurityPlugin` 等特征才降级，偶发 403 仍按原逻辑重试（避免把正常抖动也拖去开浏览器）。

> **注意**：`login_cookie` 只挡风控，**挡不住签名校验**。v0.6.0 之前文档暗示"填 Cookie 是一劳永逸绕过手段"，在签名问题上这个说法不准确——签名是服务端硬要求。Cookie 仍有价值（减少偶发 403），但不是解法。

### v0.7.0：改用拦截 `aweme/detail` 响应体（修复"MSE 视频解析失败"）

**故障现象**：视频明明存在（页面标题/文案/播放均正常），却报
`页面已加载但未取到视频直链…可能触发了验证码或视频已删除`。

**根因（2026-10-10 实测定位）**：抖音 PC Web 播放器对**部分**视频启用 **MSE**
（Media Source Extensions）。此时 `<video>.currentSrc` 是 `blob:` 而非 http 直链，
真实地址在网络层且被拆成**两条独立流**：

| URL 片段 | 内容 | MP4 轨（实测 box 解析） |
| --- | --- | --- |
| `.../media-video-avc1/` | 仅画面 | `['vide']` |
| `.../media-audio-und-mp4a/` | 仅声音 | `['soun']` |

两条都导致故障：

1. **取不到直链 → 误报"视频已删除"**。v0.6.0 兜底 JS 用 `.mp4/.m4s/.m3u8` 扩展名正则过滤性能记录，而抖音 VOD 地址形如 `.../media-video-avc1/?a=6383&br=551...` —— **路径里没有扩展名**，连兜底也一并落空。
   故障样本 `7680879925243711922` 实测：`<video>` 的 `readyState=4`、`duration=855s`、`paused=false`、`error=null`（**播放完全正常**），却因上述规则一个 url 都没匹配到。**是代码问题，不是视频被删。**
2. **即便取到 MSE 流也是残缺文件**（无声或无画面），而非可播放 mp4。

而 `aweme/detail` 响应里的 `video.bit_rate[]` / `video.play_addr` 是**渐进式合流** MP4（实测 moov 内同时含 `vide` 与 `soun`），这才是正确来源。

**新做法**：`_DetailSniffer` 监听 `Network` 事件，**截下页面自己发出的 `aweme/detail` 响应体**并解析。既不自己实现签名，又拿到与 f2 接口**同构**的完整 JSON。

| 环节 | 实现 |
| --- | --- |
| 拦截 | `Network.requestWillBeSent` 记录 `aweme/detail` 的 requestId → `loadingFinished` 时 `getResponseBody` 取回 |
| 合流优选 | `_pick_best_muxed_url`：按 `bit_rate` 降序取**非 H.265** 最高档（兼容优先于画质） |
| 显式排除 | URL 含 `media-audio` 的分片流一律跳过，避免下到无声文件 |
| 元数据 | `_aweme_to_resolved` 一次拿全：直链/封面/原声/时长/分辨率/码率/作者/点赞评论数/创建时间 |
| 兜底 | 拦截失败才回落 `<video>` 扫描（可能是 MSE 残缺流，仅作最后手段） |

**实测收益**：单条约 **4~6 秒**（不必等播放器起播），且**封面/原声/时长等元数据从"全部缺失"变为完整可用**——`_from_browser_resolved` 复用 `_extract_video_meta`，与 f2 路径同一套提取逻辑，`save_metadata` 在浏览器路径下终于能产出 `.jpg`/`.mp3`/`.json`。

**踩坑（已修，勿回退）**：

1. **CDP 会话必须单读循环**。响应与事件走同一条 socket，原实现 `call()` 里直接 `recv()` 到匹配 id 就丢弃其余消息，无法监听事件。现改为唯一读循环 + Future 分发。
2. **读循环里不得 await 需要 CDP 回包的命令**（如 `getResponseBody`），否则读循环自己挂起、永远收不到自己的回包 → **死锁**（实测表现为响应体永远拿不到、静默超时）。必须 `asyncio.create_task` 派发出去。
3. **`Network.enable` 要开大缓冲**（`maxTotalBufferSize`/`maxResourceBufferSize`），否则 `aweme/detail`（可达数百 KB）响应体被回收、`getResponseBody` 取不到。
4. **`_collect_mix_ids` 的收敛判据不能在"从未采到 id"时触发**。页面加载需数秒，期间 DOM 里一个 `<a href="/video/">` 都没有；把加载期空结果计入「无新增」会在列表渲染前就收敛、返回空列表并误报「合集页未列出任何视频」。
   **但也不能改用 `document.readyState === "complete"` 作守卫**——`/collection/{id}` 会 302 到第一个视频页，`complete` 时侧栏已被**推荐流**灌满，采到的是与合集无关的视频（实测取到高考/ChatGPT/幼儿园等）。侧栏按渲染顺序出现、合集在前推荐在后，故只需保证"采到过 id 后才允许收敛"。

### v0.6.0：并发分段下载引擎（`download_engine.py`）

移植自第三方 App「即存」的原生下载引擎 `jicun-dl`（Kotlin `NativeDownloader`），源码分析见 `/Users/zhenxi/codes/Android/即存_jicun-dl下载引擎源码解析.md`。

**移植对照**（完整表见 `download_engine.py` 模块 docstring）：

| 原 Kotlin | 本模块 | 作用 |
| --- | --- | --- |
| `DEFAULT_LANES = 32` | `dl_lanes` | 每文件并发连接数 |
| `FILE_CONCURRENCY = 4` | `dl_file_concurrency` | 同时下几个文件 |
| 探针 `Range: bytes=0-0` | `_probe()` | 一次拿总长/Content-Type/是否认 Range |
| `AtomicLong` 共享游标 | `itertools.count()` | **谁空谁认领下一段** |
| `RandomAccessFile.seek(start)` | `os.pwrite(fd, data, offset)` | 直接写偏移，**不落 .part、不拼接** |
| `TransferInterrupted(written)` | `_TransferInterrupted` | 带已写入字节数，从断点续 |
| `shouldRotateConnection` | `_should_rotate()` | 按「用多久没收完」判慢连接 |
| `ChunkAttempts` | `_ChunkAttempts` | 按「有无进展」记账，非固定次数 |

**核心设计（值得记住的三点）**：

1. **共享游标而非固定分片**：固定切分的总耗时 = 最慢那条 lane。共享游标让先空出来的 lane 继续认领后面的段，**慢连接只拖住它手里那一段**。原版实测能快近一倍。
2. **尾部切小段**：文件尾部 32MB 内切成 1MB（而非 4MB）——实测收尾时慢连接拖住一个 4MB 段要 8.5s，切到 1/4 后拖累按 1/4 计。
3. **`Accept-Encoding: identity` 固定**：带 Range 请求要 gzip 自相矛盾，压缩会破坏字节偏移。引擎里若发现响应真的被压缩，**直接判失败**而非冒险写坏文件。

**实测验证（2026-10-10，本机）**：

| 场景 | 结果 |
| --- | --- |
| 分段计划（1MB / 6MB / 20MB / 200MB / 2GB / 7GB） | 覆盖完整、区间连续、段数 ≤64 全部通过 |
| 64MB 本地服务，32/64 路并发 | **SHA256 与源完全一致** |
| 目标视频分段 vs 单连接 | **逐字节一致** |
| **35% 概率随机断连**（48MB，36 段，16 路） | 18 次重试后**收敛，SHA256 一致** |
| 4xx（404） | 立即失败不重试 |
| 单连接模式 vs 并发模式 | 产物逐字节一致 |

**踩过的两个坑（已修，勿回退）**：

1. **`httpx.aiter_bytes` 会缓冲到凑满 `chunk_size` 才吐出** → 连接在半个 chunk 处被重置时，缓冲里已到达的字节被**静默丢弃**，表现为「每次重试都零进展、4 次后误判段失败」。**必须用 `aiter_raw`**，收到多少吐多少，断点续传才拿得到已收字节。
2. **`written` 必须提到 `try` 外层**：无论以何种异常中断（协议错误/超时/EOF），都要带着「本次已写入多少字节」去记账。放在 `try` 内会被重置成 0，退化成每次从段首重传。

**已知边界**：
- 服务端忽略 Range（回 200）→ 自动退化为整条顺序下载，此时并发无效。
- 段数会被 `MAX_SEGMENTS=64` 收敛，故大文件段长会被放大（7GB → 64 段 × 100MB）。
- `dl_lanes` 会被「段数」自然收敛：4MB 的视频只分 1 段，就只开 1 条连接。**小文件看不到并发收益是正常的**，收益在大文件上。
- `dl_connection_budget` 是按 4MB 段设计的；改 `dl_segment_bytes` 时建议同步调整（否则大段会被误判"慢"而反复轮换）。

## 关键坑点（改动前必读）

- **代理**：前文已强调，运行与代码两层都要禁用代理，否则请求异常。
- **浏览器路径需要本机 Chrome/Edge/Chromium**：没有则 `BrowserNotFoundError`。可用 `DOUYINDL_CHROME_PATH` 环境变量指定。headless 下必须覆盖 UA（默认含 `HeadlessChrome` 会被识别）。
- **浏览器路径的元数据（v0.7.0 起已可用）**：改为拦截 `aweme/detail` 响应体后，封面/原声/时长/分辨率/码率/统计等**全部可得**，`save_metadata` 能正常产出 `.jpg`/`.mp3`/`.json`（v0.6.0 时这些全部为空）。仅**兜底路径**（拦截失败回落 `<video>`）拿不到元数据。
- **不能只读 `<video>.currentSrc`**：抖音对部分视频走 MSE，`<video>` 上是 `blob:`，且网络层被拆成仅画面/仅声音两条流——既取不到直链，取到也是残缺文件。必须用 `aweme/detail` 里的 `bit_rate`/`play_addr` 合流地址。详见「v0.7.0」节。
- **浏览器路径慢**：单条约 4~6 秒。合集逐条解析，N 条视频约 `5N` 秒 —— 这是用稳定性换速度的取舍。
- **UA 一致性**：改 UA 必须同步 f2 的 conf.yaml，否则签名失败。
- **`timeout` 不一致**：`config.timeout`（默认 15s）用于 API/链接解析；视频下载用硬编码 60s。调超时注意区分。
- **`max_tasks=1`**：`config.yaml` 默认单任务，控制并发不要盲目调高以免触发风控。
- **`video_play_addr` 可能为空或列表**：代码已处理（取列表首项），新增下载逻辑时注意判空。
- **`aweme/detail` 偶发 403 是风控、不是 bug**：根因与缓解见「403 风控根因与应对」节。实操要点：重试层已用 `_request_with_retry`（每次换新 ttwid 会话 + 抖动退避，`api_max_retries` 默认 3 控制次数）；仍失败可用 `--retry-failed` 重跑进度库失败记录（再次换新会话），或调大 `api_max_retries`/`api_request_interval`。注意 `api_max_retries` 过高会增加被风控识别概率，慎用。
- **`_meta` 来自原始响应**：f2 filter 不提供完整元数据，任何依赖分辨率/码率/时长的地方都要走 `_extract_video_meta`。
- **进度库相对路径**：`ProgressDB` 路径相对当前工作目录解析（CLI 运行时 cwd=项目根）。非 CLI 调用时注意 cwd。
- **`downloads/` 与 `.douyindl/`** 是运行产物，已下载视频/进度库不要误删；`downloads/` 下 72 个 mp4 为既有成果。
- **不要引入系统级依赖**：新增第三方包必须加进 `pyproject.toml` 并 `uv lock` / `uv sync`，保持隔离。
- **文件命名规则（含 aweme_id，防碰撞）**：文件名由 `_build_video_name(desc, aweme_id)` 生成，格式 `<base>_<aweme_id>.mp4`。`base` 优先级：真实文案（去除 #话题 后的文本）> 话题文字拼接（如 `比亚迪_原创作品`）> `untitled`。合集内为 `001_<base>_<aweme_id>.mp4`，单视频为 `YYYYMMDD_<base>_<aweme_id>.mp4`。**末尾强制带 aweme_id**，确保多个无标题/同名视频不会因文件存在被误跳过。改动命名逻辑只看这个函数，别再用旧的 `_sanitize_filename` 生成视频文件名（它仍用于合集目录名）。
- **f2 `gen_real_msToken` 端点偶发 503 → 已在导入期打兜底补丁**：f2 在 `model.py` 的 `BaseRequestModel.msToken` 类属性里于「导入期」直接调用 `TokenManager.gen_real_msToken()`，该函数请求外部端点 `mssdk.bytedance.com/web/report`，该端点偶发 503/超时。f2 自带的 `try/except` 在 `except` 里又会二次调用同一个会失败的函数，导致**整个模块导入失败、程序起不来**。修复方式：在 `downloader.py` 顶部导入 `crawler/model` **之前**，monkeypatch `TokenManager.gen_real_msToken` 为 `_safe_gen_real_msToken`——真实生成失败时回退 `gen_false_msToken()`（随机虚假 token），契合 f2「出错返回虚假值」意图，**不改动 site-packages**。`uv sync` 后仍生效。若以后升级 f2 大版本，需确认该类名/方法未变。

## AI 技能（Skill）

本项目配套一个抖音下载 Skill，供 AI 助手在用户给出抖音分享链接时自动触发下载：

- **原始定义（Trae 格式）**：`.trae/skills/douyin-download/SKILL.md` —— 仅 Trae 可读，WorkBuddy 不可用。
- **WorkBuddy 可用版本（用户级）**：`~/.workbuddy/skills/douyin-download/SKILL.md` —— 由 `.trae` 版本移植而来，已通过安全审计（P2 安全，纯指令文件、无捆绑脚本，仅调用本地 `python -m douyindl`）。
  - 触发：用户提供含 `v.douyin.com` 的抖音分享链接/分享文本并表达下载意图。
  - 调用本质上就是执行 `NO_PROXY='*' no_proxy='*' uv run python -m douyindl "<链接或文本>" -o ./downloads`，详见该 SKILL.md 与上文「常用命令」。
  - 注意：该 user 级 skill 内硬编码了 `cd /Users/zhenxi/codes/python/douyinDL`，换机器/换路径需同步修改；运行**必须**前置 `NO_PROXY='*' no_proxy='*'`。

> 若需随仓库分发，可将其移到项目级目录 `项目根/.workbuddy/skills/douyin-download/SKILL.md`。

## 文档索引
- `docs/01-getting-started.md` 背景/选型/结构/环境
- `docs/02-config.md` 配置参数详解
- `docs/03-development.md` 开发流程与核心原理（链接解析/匿名认证/ABogus）
- `docs/04-metadata.md` 元数据保存
- `docs/05-progress.md` 进度持久化与数据库结构
- `docs/06-usage.md` 使用指南与 Skill 集成
- `docs/07-faq.md` 排查/扩展方向/代码索引/依赖清单/变更记录

---

## 变更记录

### v0.6.0（2026-10-10）—— 应对抖音签名封杀 + 并发下载引擎

用户实测：第三方 App（即存）在模拟器上能免登录下载同一视频，本工具却因系统性 403 完全不可用。排查结论与改造：

| 改动 | 文件 | 说明 |
| --- | --- | --- |
| 新增浏览器回退解析 | `browser_fallback.py` | 借 headless Chrome + CDP 取直链，绕开 Web 签名 |
| 新增并发分段下载引擎 | `download_engine.py` | 移植 jicun-dl，含探针/共享游标/续传/慢连接轮换 |
| 解析策略三态 + 自动降级 | `downloader.py` | `resolve_mode: auto/f2/browser`，`_is_systemic_block` 区分系统性 vs 偶发 |
| 金丝雀不再直接中止 | `downloader.py` | auto 模式下探针失败自动降级浏览器并继续 |
| `download_video` 接并发引擎 | `downloader.py` | `concurrent_download=false` 可退回单连接 |
| 新增 11 个配置项 | `config/config.yaml` | 解析策略 3 项 + 并发引擎 8 项 |

**实测结果**（目标链接 `https://v.douyin.com/ti6iWTa_muM/`）：单条与合集（3 条）均下载成功，QuickLook 能解码出缩略图，进度库正常入库。

**同时修正了文档里一处不准确表述**：v0.6.0 之前称「填 `login_cookie` 是唯一可靠的一劳永逸绕过手段」——实测登录态**无法**绕过签名校验（签名是服务端硬要求，与登录无关），该说法已在 CLAUDE.md 中更正。
- `docs/08-share-link-mechanism.md` 分享链接机制：为何同视频多次分享得到不同短链、aweme_id 去重原理

### v0.7.0（2026-10-10）—— 修复 MSE 视频「解析不到直链」误报

用户实测：部分视频报 `页面已加载但未取到视频直链…可能触发了验证码或视频已删除`，
但页面标题（文案）已正常取到，说明视频存在、即存 App 也能正常下载。

排查结论与改造：

| 改动 | 文件 | 说明 |
| --- | --- | --- |
| 主路径改为拦截接口响应体 | `browser_fallback.py` | 新增 `_DetailSniffer`，截下页面自己发出的 `aweme/detail` 响应体并解析 |
| 合流直链优选 | `browser_fallback.py` | 新增 `_pick_best_muxed_url`，按码率取非 H.265 最高档，显式排除 `media-audio` 分片 |
| CDP 会话重写 | `browser_fallback.py` | 单读循环 + Future 分发，支持事件监听；修掉读循环内 await 造成的死锁 |
| 元数据补全 | `browser_fallback.py` / `downloader.py` | `BrowserResolved` 增 `cover_url`/`music_play_url`/`raw_aweme`；`_from_browser_resolved` 复用 `_extract_video_meta` |
| 合集收敛判据修正 | `browser_fallback.py` | `_collect_mix_ids` 未采到过 id 时不判收敛（修掉加载期空结果导致的误报） |

**根因**：抖音 PC Web 播放器对部分视频启用 MSE，`<video>.currentSrc` 是 `blob:`，
真实地址在网络层且被拆成「仅画面 `media-video-*`」与「仅声音 `media-audio-*`」两条流；
而 v0.6.0 兜底 JS 用 `.mp4/.m4s/.m3u8` 扩展名正则过滤，抖音 VOD 地址路径里没有扩展名，
于是主路径与兜底同时落空 → 误报「视频已删除」。

**实测结果**：

| 验证项 | 结果 |
| --- | --- |
| 故障视频 `7680879925243711922` 端到端 | 下载成功，114,282,084 字节，与服务端 `Content-Range` 总长一致 |
| MP4 轨校验（box 解析） | `['soun','vide']` **音视频合流**；末尾字节非全 0，数据完整 |
| 6 条视频批量解析 | 6/6 成功，单条约 4~6 秒 |
| 合集解析 | 3/3 正确（合集本身，非推荐流），封面/原声齐全 |
| `-m all` 元数据 | `.mp4`/`.jpg`/`.mp3`/`.json`/`.txt` **五种产物齐全**（v0.6.0 只有 `.mp4`/`.txt`/`.json`） |

**副产物**：解析不再需要等播放器起播，单条耗时从「等 5 秒起播」降到「等约 3 秒接口响应」。
