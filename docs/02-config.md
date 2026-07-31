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
| `max_retries` | int | 5 | API 请求最大重试次数 |
| `max_tasks` | int | 1 | f2 crawler 内部信号量上限，控制并发 |
| `max_connections` | int | 5 | httpx 连接池大小 |

> `user_agent` 是签名算法的入参之一，不能随意改成移动端 UA，否则 API 会返回 403。

### 抖音 API 调用相关

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `page_counts` | int | 10 | 合集 API 分页每页条数，不建议超过 20，可能触发风控 |
| `api_request_interval` | float | 2.0 | API 分页请求间隔（秒），避免抖音风控 |

合集视频数超过 `page_counts` 时会自动翻页，每页之间等待 `api_request_interval` 秒。

### 视频下载相关

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mix_download_interval` | int | 60 | 合集内视频下载间隔基准值（秒），实际等待在 `base*0.9` 到 `base` 之间随机取数，防封 IP；单视频无需等待 |
| `chunk_size` | int | 65536 | 流式下载 chunk 大小（字节），默认 64KB |
| `filename_max_len` | int | 60 | 下载文件名最大长度（字符），超长截断 |
| `download_max_retries` | int | 3 | 下载失败时的最大重试次数（不含首次下载），0 表示不重试 |
| `download_retry_interval` | float | 5.0 | 下载重试间隔（秒），失败后等待多久再重试 |

> `mix_download_interval` 是间隔基准值，实际等待时间在 `base*0.9` 到 `base` 之间随机取数（如 60 秒基准值实际等待 54-60 秒），避免固定间隔被风控识别。该间隔用于合集内视频之间、多链接之间、失败重试记录之间。
> `download_max_retries` 控制单个视频下载失败后的自动重试次数，重试间隔由 `download_retry_interval` 控制。无论成功失败都会记录到数据库，失败记录可通过 `-r/--retry-failed` 重新下载。

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
api_request_interval: 2.0

# 视频下载相关
mix_download_interval: 60
chunk_size: 65536
filename_max_len: 60
download_max_retries: 3
download_retry_interval: 5.0

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
```

## 下一步

- 元数据保存详解：[04-metadata.md](04-metadata.md)
- 进度持久化数据库结构：[05-progress.md](05-progress.md)
