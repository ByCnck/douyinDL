# 02 - 配置文件详解

所有运行参数集中在 `config/config.yaml`，修改后无需改动代码。

## 配置文件位置

默认路径：`<项目根>/config/config.yaml`

CLI 可通过 `-c/--config` 参数覆盖：

```bash
uv run python -m douyindl "<链接>" -c /path/to/custom-config.yaml
```

## 参数详解

配置按功能分为 6 组，下面逐项说明。

### HTTP 请求相关

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `user_agent` | string | Chrome 130 Edg 130 UA | 必须与 f2 内部 UA 一致，否则 a_bogus 签名校验失败 |
| `timeout` | int | 15 | HTTP 请求超时（秒） |
| `max_retries` | int | 5 | f2 crawler 内部重试次数（仅当「响应内容为空」时重试，**对 403/429/5xx 不重试**） |
| `max_tasks` | int | 1 | f2 crawler 内部信号量上限，控制并发 |
| `max_connections` | int | 5 | httpx 连接池大小 |

> `user_agent` 是签名算法的入参之一，不能随意改成移动端 UA，否则 API 会返回 403。

### 抖音 API 调用相关

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `page_counts` | int | 10 | 合集 API 分页每页条数，不建议超过 20，可能触发风控 |
| `api_request_interval` | float | 5.0 | API 分页请求间隔（秒），避免抖音风控；**同时作为风控重试退避的基准**（实际退避 ≈ 此值 ±10%）。调大可降低「裸会话首打 403」概率，代价是请求更稀疏、整体更慢 |
| `api_max_retries` | int | 3 | **业务层 API 重试次数**：针对抖音对 `aweme/detail`/合集列表的间歇性风控（偶发 403/429/5xx）。f2 自身不重试这类错误，这里在业务层补一层带随机抖动退避的重试，每次重试都重新生成匿名 ttwid 会话（等价于「换 PC 链接重跑」能成功）。0 表示不重试 |

> 抖音会在批量下载时对单视频/合集接口偶发返回 403 Forbidden，这是风控而非接口故障（根因见 CLAUDE.md「403 风控根因与应对」节）。设置 `api_max_retries ≥ 1` 后，失败的请求会自动换新会话重试，通常随即成功。**会话预热**：启动时会先 GET 抖音首页并把首页 cookie（`__ac_nonce` 等）并入后续所有 API 请求，使请求比纯裸 ttwid 更像连续会话，进一步降低首打 403 概率（局限：`sid_tt`/`odin_tt` 需浏览器 JS 才下发，纯 HTTP 拿不到）。若仍失败，可运行 `-r/--retry-failed` 在进度库中重跑失败记录（同样会换新会话重试）；重试 3 次仍失败的链接会自动写入 `urls_fail`（`时间 原因 url` 格式），供手工重试。`api_request_interval` 同时作为重试退避的基准（实际退避 ≈ 此值 ±10%）。

合集视频数超过 `page_counts` 时会自动翻页，每页之间等待 `api_request_interval` 秒。

### 视频下载相关

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mix_download_interval` | int | 60 | 合集内视频下载间隔基准值（秒），实际等待在 `base*0.9` 到 `base` 之间随机取数，防封 IP；单视频无需等待 |
| `chunk_size` | int | 65536 | 流式下载 chunk 大小（字节），默认 64KB |
| `filename_max_len` | int | 60 | 下载文件名最大长度（字符），超长截断 |
| `download_max_retries` | int | 3 | 下载失败时的最大重试次数（不含首次下载），0 表示不重试 |
| `download_retry_interval` | float | 5.0 | 下载重试间隔（秒），失败后等待多久再重试 |
| `max_download_speed` | int | 0 | 最大下载速度（字节/秒），0 表示不限速；如 10MB/s = 10485760 |

> `mix_download_interval` 是间隔基准值，实际等待时间在 `base*0.9` 到 `base` 之间随机取数（如 60 秒基准值实际等待 54-60 秒），避免固定间隔被风控识别。该间隔用于合集内视频之间、多链接之间、失败重试记录之间。
> `download_max_retries` 控制单个视频下载失败后的自动重试次数，重试间隔由 `download_retry_interval` 控制。无论成功失败都会记录到数据库，失败记录可通过 `-r/--retry-failed` 重新下载。
> `max_download_speed` 控制下载速度上限，0 表示不限速。限速原理：每下载一个 chunk 后按累计字节数计算期望用时，实际用时不足则 sleep 差值。常用值：10MB/s=10485760，5MB/s=5242880，1MB/s=1048576。

### 元数据保存

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `save_metadata` | bool | false | 总开关：是否额外保存视频元数据 |
| `save_cover` | bool | true | 子开关：视频封面图 (.jpg) |
| `save_desc` | bool | true | 子开关：视频文案全文 (.txt) |
| `save_music` | bool | true | 子开关：原声 MP3 (.mp3) |
| `save_json` | bool | true | 子开关：完整视频信息 JSON (.json) |

子开关仅在 `save_metadata=true` 时生效。CLI 的 `-m` 参数会覆盖这些开关，详见 [04-metadata.md](04-metadata.md)。

### 进度持久化

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enable_progress` | bool | true | 是否启用 SQLite 进度数据库 |
| `progress_db_path` | string | `.douyindl/progress.db` | 数据库路径（相对路径基于 cwd） |

启用后，已下载视频会记录到数据库，再次执行同一合集时自动跳过。详细结构见 [05-progress.md](05-progress.md)。

### 输出目录相关

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `output_dir` | string | `./downloads` | 默认输出根目录，CLI `-o` 参数可覆盖 |

### 日志相关

日志使用 [loguru](https://github.com/Delgan/loguru)，统一双写：**控制台（stderr）+ 文件 `app.log`**。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `log_level` | string | `INFO` | **控制台**日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR`。默认 `INFO`，即控制台只显示结果/警告/错误，`[1/4]~[4/4]` 步骤细节被隐藏 |
| `log_file` | string | `app.log` | 日志文件路径（相对 cwd）。**文件级别固定 `DEBUG`**，始终记录全部细节（含步骤日志、时间戳、模块:函数:行号），便于事后排查偶发 403 / 下载失败 |

> 日志级别约定（代码内已按此区分）：
> - **DEBUG**：`[1/4]~[4/4]` 步骤脚手架、视频列表枚举、等待间隔等过程日志
> - **INFO**：单条结果信息（解析到的资源类型/ID、每条下载成功+元数据、跳过原因、`[4/4]` 完成汇总、多链接总结）
> - **WARNING**：偶发风控/下载的瞬时可恢复重试提示（区别于最终失败）
> - **ERROR**：真实失败（重试耗尽、异常、参数错误）
>
> 运行时加 `-v/--verbose` 可让控制台也降到 `DEBUG`，显示步骤细节（文件始终为 DEBUG）。

## 认证与前置检查

> 这一组配置用于应对「抖音服务端变更导致匿名接口系统性 403」的情况。
> 根因与整体策略见 CLAUDE.md「403 风控根因与应对」节的「系统性 403（服务端签名更新）」小节。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `login_cookie` | string | `""` | **真实登录态 Cookie（绕过匿名风控的核心手段）**。留空则用匿名会话（真实 ttwid + 假 msToken），抖音可能间歇性/系统性 403。填入后 `build_cookie()` 直接返回该字符串，不再走匿名生成。获取方式：抖音网页端登录后，F12 → Network → 任意请求 → Request Headers 的 `Cookie` 整串复制填入，**需含 `sid_tt`/`sess`/`uid_tt`/`odin_tt` 等登录字段**。注意登录 Cookie 有有效期，过期需重新复制；失效时接口仍会 403 |
| `preflight_check_deps` | bool | `true` | **依赖自检**：启动时查询 PyPI 的 f2 最新版，若比本地新则自动 `uv pip install` 升级并重启当前进程（即「每次下载前先更新依赖库」的落地）。网络不可达时静默跳过，不影响主流程 |
| `preflight_canary` | bool | `true` | **连通性探针（金丝雀）**：正式批量下载前，用若干已知公开视频探测 `aweme/detail` 接口是否可用。若全部返回 403（风控拦截），判定「下载方法当前整体失效」，提前中止并打印可操作建议，**避免对每个链接反复重试 3 次、把整批误写进 `urls_fail`**。除非你明确要无视探针强行尝试，否则保持 `true` |

> **何时需要 `login_cookie`**：当你确认 `preflight_canary` 触发、诊断信息指向「匿名 a_bogus 被服务端拒绝」时，最稳的绕过方式就是提供真实登录态 Cookie——它让请求带完整登录上下文，绝大多数情况下可彻底消除匿名 403。这是当前「一劳永逸」绕过风控的唯一可靠手段（等待 f2 适配新签名是另一条路径，工具已通过 `preflight_check_deps` 自动跟进）。

## 配置覆盖优先级

从高到低：

1. **CLI 参数**（`-o` / `-n` / `-m` / `-f` / `-r` 等）
2. **`-c` 指定的配置文件**（若未指定则用默认 `config/config.yaml`）
3. **代码内默认值**（`Config.__init__` 中的 `data.get(key, default)`）

## 完整配置示例

```yaml
# config/config.yaml 完整示例

# HTTP 请求相关
user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
timeout: 15
max_retries: 5
max_tasks: 1
max_connections: 5

# 抖音 API 调用相关
page_counts: 10
# API 分页间隔（秒）；同时作为风控重试退避基准。调大可降低首打 403 概率
api_request_interval: 5.0
api_max_retries: 3

# 视频下载相关
mix_download_interval: 60
chunk_size: 65536
filename_max_len: 60
download_max_retries: 3
download_retry_interval: 5.0
max_download_speed: 0

# 元数据保存
save_metadata: false
save_cover: true
save_desc: true
save_music: true
save_json: true

# 进度持久化
enable_progress: true
progress_db_path: ".douyindl/progress.db"

# 输出相关
output_dir: "./downloads"

# 日志相关
log_level: "INFO"
log_file: "app.log"

# 认证与前置检查
# 真实登录态 Cookie（可选）：主要缓解"偶发"风控 403。
# 注意：它**无法**绕过签名校验（签名是服务端硬要求，与是否登录无关）
login_cookie: ""
# 依赖自检：启动时检查 f2 最新版，有更新则自动升级并重启
preflight_check_deps: true
# 连通性探针（金丝雀）：批量下载前探测接口是否可用
# resolve_mode=auto 时探针失败会自动降级浏览器解析并继续，不再中止
preflight_canary: true

# 解析策略（v0.6.0）
# 抖音 Web 接口 aweme/detail 现要求 uifid + x-secsdk-web-signature（字节 BDMS
# 原生 SDK 产物），纯 HTTP 无法复现 → 默认 auto，自动降级到浏览器解析
resolve_mode: "auto"          # auto | f2 | browser
browser_wait_seconds: 25.0    # 等待播放器起播取直链的最长秒数
browser_port: 0               # CDP 调试端口，0=自动选空闲端口

# 并发分段下载（v0.6.0，移植自即存 jicun-dl 下载引擎）
concurrent_download: true     # false=退回单连接顺序流式下载
dl_lanes: 32                  # 每文件并发连接数（实际会被段数收敛）
dl_file_concurrency: 4        # 同时下载几个文件
dl_segment_bytes: 4194304     # 主体段 4MB
dl_tail_bytes: 33554432       # 尾部 32MB 内切小段
dl_tail_chunk_bytes: 1048576  # 尾段 1MB
dl_segment_from_bytes: 8388608  # 小于 8MB 不分段
dl_connection_budget: 10.0    # 慢连接轮换阈值（秒）
```

---

## 解析策略（`resolve_mode`）

抖音 Web 接口 `aweme/v1/web/aweme/detail/` 目前需要两样纯 HTTP 拿不到的东西：

| 要求 | 缺失时服务端返回 | 性质 |
| --- | --- | --- |
| `uifid` 查询参数 | `403 Blocked by ArgusSecurityPlugin Uifid Not Found` | 页面 JS 生成，可从浏览器取 |
| `x-secsdk-web-signature` 签名 | `403 Blocked by ArgusSecurityPlugin Signature Not Found` | 字节 BDMS 原生算法，纯 HTTP 无法复现 |

| 取值 | 行为 | 适用 |
| --- | --- | --- |
| `auto`（默认） | 先走 f2 接口；金丝雀探测或实际请求出现系统性 403 时，自动降级到浏览器解析，并记在 config 上（同批后续不再重复试 f2） | 绝大多数场景 |
| `f2` | 只用 f2 接口，不降级 | f2 恢复可用、且想追求速度时 |
| `browser` | 只用浏览器解析 | 不想等 f2 失败重试；或已确定 f2 不可用 |

**为什么需要浏览器回退**：第三方 App（如「即存」）能免登录下载，不是因为解析算法更先进，而是**它是原生 App，走 App 端协议而非 Web 签名协议**。我们无法复现 SecSDK 签名，于是改为**借真实浏览器生成签名**：脚本拉起 headless Chrome，用 CDP（Chrome DevTools Protocol）打开视频页，等播放器把 `<video>` 填上直链后取走。

**要求**：本机需安装 Chrome / Edge / Chromium（可用环境变量 `DOUYINDL_CHROME_PATH` 指定路径）。

**代价与限制**：
- 慢：单条约 5 秒（等播放器起播），远慢于纯 HTTP；合集逐条解析，N 条约 `5N` 秒。
- 拿不到 API 侧元数据：封面、原声、分辨率、时长等均为空。故 `save_metadata` 开启时只会产出 `.txt`/`.json`，不会有 `.jpg`/`.mp3`。
- 触发验证码时解析失败（会明确报错，不会静默产出坏文件）。

---

## 并发分段下载

原实现是单连接顺序流式下载，遇到抖音 CDN 对单连接限速时速度低，且断连即整段重来。v0.6.0 起默认走移植自即存 `jicun-dl` 的并发分段引擎。

### 工作原理

```
探针 GET Range: bytes=0-0        → 总长 / Content-Type / 是否认 Range
   ├─ 回 200（忽略 Range）→ 退化为整条顺序下载
   └─ 回 206 → 分段
分段计划                          → 主体 4MB；尾部 32MB 内切 1MB；段数 ≤64
共享游标 + N 路并发               → 谁空谁认领下一段（不是固定分片）
每段 Range 请求                   → os.pwrite 写到对应偏移（不落 .part、不拼接）
断连 / 慢连接                    → 从断点续传；10s 未收完则掐掉换连接
完整性校验                        → 字节总数 + 落地文件大小 + Content-Range 偏移
```

### 参数说明

| 参数 | 默认 | 作用 | 调大 / 调小的效果 |
| --- | --- | --- | --- |
| `concurrent_download` | `true` | 总开关 | `false` 退回单连接顺序流式（旧行为） |
| `dl_lanes` | `32` | 每文件并发连接数 | **会被段数自然收敛**：4MB 文件只分 1 段，就只开 1 条连接。小文件看不到并发收益是正常的 |
| `dl_file_concurrency` | `4` | 同时下载几个文件 | 批量下载时摊薄连接数，避免 4×32=128 条连接撞 CDN 并发上限 |
| `dl_segment_bytes` | `4194304`（4MB） | 主体段大小 | 段越小请求越多、调度越细，但握手开销上升 |
| `dl_tail_bytes` | `33554432`（32MB） | 尾部切小段的范围 | 收尾阶段慢连接的拖累范围 |
| `dl_tail_chunk_bytes` | `1048576`（1MB） | 尾段大小 | 原版实测：尾部用 4MB 段时慢连接拖 8.5s，切到 1/4 后拖累按 1/4 计 |
| `dl_segment_from_bytes` | `8388608`（8MB） | 小于此不分段 | 调小会让更多文件走分段（但小文件分段收益有限） |
| `dl_connection_budget` | `10.0` | 慢连接轮换阈值（秒） | **按 4MB 段设计**；改 `dl_segment_bytes` 时建议同步调整，否则大段会被误判"慢"而反复轮换 |

### 已实测验证

| 场景 | 结果 |
| --- | --- |
| 分段计划（1MB / 6MB / 20MB / 200MB / 2GB / 7GB） | 覆盖完整、区间连续、段数 ≤64 全部通过 |
| 64MB 文件，32/64 路并发 | SHA256 与源完全一致 |
| **35% 概率随机断连**（48MB / 36 段 / 16 路） | 18 次重试后收敛，SHA256 一致 |
| 4xx（404） | 立即失败不重试（403/404 再试也是白等） |
| 单连接模式 vs 并发模式 | 产物逐字节一致 |

## 下一步

- 元数据保存详解：[04-metadata.md](04-metadata.md)
- 进度持久化数据库结构：[05-progress.md](05-progress.md)
