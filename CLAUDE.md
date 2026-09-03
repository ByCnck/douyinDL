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
│   └── downloader.py                 # 核心逻辑（约 1400 行，唯一大文件）
├── pyproject.toml                    # 依赖与脚本入口
└── uv.lock
```

`downloader.py` 是唯一核心模块，建议改动前通读：关键类/函数见 `docs/07-faq.md` 附录代码索引。

## 架构要点（downloader.py）

- **`Config`**：从 `config.yaml` 加载，逐项带默认值兜底；`DEFAULT_CONFIG_PATH` 由 `__file__` 向上三级定位到项目根 `config/config.yaml`。
- **`resolve_share_url`**：支持短链 `v.douyin.com`、合集 `iesdouyin.com/share/mix/detail/{id}` 或 `douyin.com/collection/{id}`、单视频 `douyin.com/video/{id}`；短链跟随重定向后用正则 `_MIX_PATH_PATTERN` / `_AWEME_ID_PATTERN` 提取 `mix`/`one` 类型与 ID。
- **匿名认证**：`build_cookie(config)` 优先复用「会话预热」拿到的首页 cookie（`__ac_nonce` 等），再补真实 `ttwid`（`gen_ttwid()`，每次新鲜）+ 假 `msToken`（`gen_false_msToken()`）；无需登录。根因见「403 风控根因与应对」节。
- **视频列表**：`fetch_mix_videos`（分页循环，受 `page_counts`/`api_request_interval` 节流）与 `fetch_one_video`，分别用 f2 的 `UserMixFilter._to_list()` 与 `PostDetailFilter._to_dict()`。
- **API 请求重试层**：`fetch_one_video` 与 `fetch_mix_videos` 的接口调用统一经 `_request_with_retry` 包裹。原因：f2 的 `base_crawler.handle_http_status_error` 对 403/429/5xx 等 HTTP 状态错误是 `else` 分支**直接抛异常、不进入重试循环**（它只在响应为空时才重试）。抖音对 `aweme/detail` 等接口存在**间歇性风控（偶发 403 Forbidden）**，表现为批量下载时零星失败——而换新会话（换 PC 链接/重跑）往往就成功。本层补上重试：**每次重试都重新生成匿名 ttwid cookie（换会话）**，退避用 `_random_interval(max(api_request_interval, 2.0))`，仅对 `APIResponseError`/`APIRateLimitError`/`APIConnectionError`/`APITimeoutError` 重试，逻辑错误不重试。重试次数由 `config.api_max_retries`（默认 3）控制。另见「会话预热」：启动先 GET 抖音首页并入首页 cookie 进一步降低首打 403；`config.api_request_interval` 调大（默认 5.0s）亦可降首打概率（详见「403 风控根因与应对」节）。
- **`_extract_video_meta`**：f2 filter 会丢失 `video.*` 嵌套字段，此函数从原始 JSON 补提（duration/width/height/file_size/bit_rate/fps/ratio/video_format/is_h265/作者/统计/创建时间），用于入库。
- **下载**：`download_video` 用 httpx 流式下载，**注意其内部 `AsyncClient(timeout=60)` 是硬编码，并不读取 `config.timeout`**；限速由 `max_download_speed`（字节/秒，0=不限）按累计字节数节流。`_download_simple` 用于小文件（封面/原声）。
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

## 关键坑点（改动前必读）

- **代理**：前文已强调，运行与代码两层都要禁用代理，否则请求异常。
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
- `docs/08-share-link-mechanism.md` 分享链接机制：为何同视频多次分享得到不同短链、aweme_id 去重原理
