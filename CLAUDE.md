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
- **匿名认证**：`build_cookie()` 用 `TokenManager.gen_ttwid()` + `gen_false_msToken()` 拼 cookie，无需登录。
- **视频列表**：`fetch_mix_videos`（分页循环，受 `page_counts`/`api_request_interval` 节流）与 `fetch_one_video`，分别用 f2 的 `UserMixFilter._to_list()` 与 `PostDetailFilter._to_dict()`。
- **API 请求重试层**：`fetch_one_video` 与 `fetch_mix_videos` 的接口调用统一经 `_request_with_retry` 包裹。原因：f2 的 `base_crawler.handle_http_status_error` 对 403/429/5xx 等 HTTP 状态错误是 `else` 分支**直接抛异常、不进入重试循环**（它只在响应为空时才重试）。抖音对 `aweme/detail` 等接口存在**间歇性风控（偶发 403 Forbidden）**，表现为批量下载时零星失败——而换新会话（换 PC 链接/重跑）往往就成功。本层补上重试：**每次重试都重新生成匿名 ttwid cookie（换会话）**，退避用 `_random_interval(max(api_request_interval, 2.0))`，仅对 `APIResponseError`/`APIRateLimitError`/`APIConnectionError`/`APITimeoutError` 重试，逻辑错误不重试。重试次数由 `config.api_max_retries`（默认 3）控制。
- **`_extract_video_meta`**：f2 filter 会丢失 `video.*` 嵌套字段，此函数从原始 JSON 补提（duration/width/height/file_size/bit_rate/fps/ratio/video_format/is_h265/作者/统计/创建时间），用于入库。
- **下载**：`download_video` 用 httpx 流式下载，**注意其内部 `AsyncClient(timeout=60)` 是硬编码，并不读取 `config.timeout`**；限速由 `max_download_speed`（字节/秒，0=不限）按累计字节数节流。`_download_simple` 用于小文件（封面/原声）。
- **`ProgressDB`**：SQLite 表 `downloaded_videos`，主键 `aweme_id`；`_migrate_schema` 用 `PRAGMA table_info` 检测缺失列并 `ALTER TABLE ADD COLUMN` 做版本迁移（兼容 v0.3→v0.5 旧库）。`status` 字段记录 `success`/`failed`，失败不跳过、可重跑。
- **`DouyinDownloader`**：主流程 `run()`（解析→拉列表→下载→入库），`_download_with_retry`（受 `download_max_retries`）、`retry_failed()`（查失败记录→重新取地址→下载→更新库）。`main()` 为 CLI 入口，多链接串行、去重、下载后清空 `-i` 文件。
- **风控**：合集视频间隔、多链接间隔、失败重试间隔均通过 `_random_interval(base)` 在 `[base*0.9, base]` 随机取值，避免固定间隔被识别。

## 关键坑点（改动前必读）

- **代理**：前文已强调，运行与代码两层都要禁用代理，否则请求异常。
- **UA 一致性**：改 UA 必须同步 f2 的 conf.yaml，否则签名失败。
- **`timeout` 不一致**：`config.timeout`（默认 15s）用于 API/链接解析；视频下载用硬编码 60s。调超时注意区分。
- **`max_tasks=1`**：`config.yaml` 默认单任务，控制并发不要盲目调高以免触发风控。
- **`video_play_addr` 可能为空或列表**：代码已处理（取列表首项），新增下载逻辑时注意判空。
- **`aweme/detail` 偶发 403 是风控、不是 bug**：抖音对单视频详情/合集列表接口有间歇性风控，批量下载时零星返回 `403 Forbidden`。f2 自身对该类 HTTP 状态错误**不重试**（直接抛 `APIResponseError`），所以此前会直接失败。`fetch_one_video`/`fetch_mix_videos` 已用 `_request_with_retry` 补上带抖动退避的重试（每次换新 ttwid 会话），重现「换 PC 链接重跑就成功」的效果。若仍失败，可用 `--retry-failed` 在进度库里重跑失败记录（会再次换新会话重试）。调大 `config.api_max_retries` 可提高成功率，但过高会增加被风控识别的概率，慎用。
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
