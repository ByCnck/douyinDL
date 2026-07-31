# 07 - 常见问题与扩展方向

## 常见问题排查

### 1. 请求报错：连接超时 / 无法连接

**原因**：本机存在 `http_proxy` / `https_proxy` 环境变量，代理干扰了对抖音的请求。

**解决**：所有命令前加 `NO_PROXY='*' no_proxy='*'`：

```bash
NO_PROXY='*' no_proxy='*' uv run python -m douyindl "https://v.douyin.com/xxx/"
```

### 2. API 返回 403 / 签名校验失败

**原因**：`user_agent` 与 f2 内部 UA 不一致，导致 a_bogus 签名校验失败。

**解决**：保持 `config/config.yaml` 中的 `user_agent` 与 f2 的 `conf/conf.yaml` 一致，不要改成移动端 UA。

### 3. `No module named douyindl.__main__`

**原因**：缺少 `__main__.py` 入口文件，或未在项目根目录执行。

**解决**：

1. 确认在项目根目录执行（`cd /Users/zhenxi/codes/python/douyinDL`）
2. 确认 `src/douyindl/__main__.py` 存在
3. 重新 `uv sync` 安装依赖

### 4. Python 版本冲突

**原因**：f2 的 pydantic-core 依赖要求 Python 3.12-3.13，不支持 3.14+。

**解决**：

```bash
uv python pin 3.12
uv sync
```

### 5. 合集下载被封 IP

**原因**：合集视频数多，频繁请求触发抖音风控。

**解决**：

- 合集内视频间默认间隔 60 秒（`mix_download_interval`），不要调低
- API 分页间隔默认 2 秒（`api_request_interval`），不要调低
- 被封后等待一段时间再试，或更换网络环境

### 6. f2 与 httpx / rich 版本冲突

**原因**：直接在 `pyproject.toml` 中添加 `httpx` 或 `rich` 依赖，与 f2 内置版本冲突。

**解决**：项目不直接依赖 `httpx` 和 `rich`，由 f2 统一管理。移除 `pyproject.toml` 中的直接依赖，执行 `uv sync`。

### 7. 已下载视频被重复下载

**原因**：进度数据库未启用，或数据库路径错误。

**解决**：

1. 确认 `config/config.yaml` 中 `enable_progress: true`
2. 确认 `.douyindl/progress.db` 存在且有写权限
3. 使用 `-f` 参数可强制重新下载（忽略进度记录）

### 8. 元数据未生成

**原因**：`save_metadata` 总开关未开启，且未使用 `-m` 参数。

**解决**：

```bash
# 方式1：CLI 参数（推荐）
uv run python -m douyindl "<链接>" -m

# 方式2：配置文件
# config.yaml 中设置 save_metadata: true
```

## 后续扩展方向

| 方向 | 说明 |
|------|------|
| 用户主页下载 | 复用 `DouyinCrawler` 的 `fetch_user_post` 接口，支持下载某用户的全部作品 |
| 直播流录制 | f2 支持直播流解析，可扩展直播录制功能 |
| 并发下载 | 当前 `max_tasks=1` 串行下载，可调大实现并发（注意风控） |
| 进度数据库迁移 | 从 SQLite 迁移到 PostgreSQL/MySQL，支持多机共享进度 |
| Web UI | 提供 Web 界面提交链接、查看下载进度 |
| 视频去重 | 基于内容哈希去重，而非仅 aweme_id |

## 附录

### 代码索引

| 文件 | 职责 |
|------|------|
| [src/douyindl/downloader.py](../src/douyindl/downloader.py) | 核心下载逻辑（链接解析/API/下载/元数据/进度DB） |
| [src/douyindl/__init__.py](../src/douyindl/__init__.py) | 包入口，导出核心类 |
| [src/douyindl/__main__.py](../src/douyindl/__main__.py) | `python -m douyindl` 入口 |
| [config/config.yaml](../config/config.yaml) | 配置文件 |
| [.trae/skills/douyin-download/SKILL.md](../.trae/skills/douyin-download/SKILL.md) | Trae IDE Skill 定义 |
| [pyproject.toml](../pyproject.toml) | 项目依赖与 Python 版本约束 |

### 依赖清单

| 依赖 | 版本约束 | 用途 |
|------|----------|------|
| f2 | >=0.0.1.7 | 抖音 API 封装 + ABogus 签名 |
| pyyaml | >=6.0 | 配置文件解析 |
| Python | >=3.12,<3.14 | 运行时（f2 的 pydantic-core 要求） |

> `httpx`、`rich`、`pydantic` 等由 f2 间接引入，不在 `pyproject.toml` 中直接声明。

### 变更记录

> 以下为功能里程碑（非 pyproject.toml 中的版本号，pyproject.toml 当前为 `0.1.0`）。

| 里程碑 | 变更 |
|--------|------|
| 初始版本 | 合集/单视频下载，匿名认证，合集间隔 60 秒 |
| 配置化 | UA/chunk/间隔参数集中到 config.yaml |
| 进度持久化 | SQLite 记录下载历史，支持断点续传与增量下载 |
| 元数据扩展 | 元数据保存（`-m` 精细控制）+ 视频元数据入库（15 个字段从 API 提取） |
| 文档拆分 | 单文件拆为 7 个主题文档 + README 索引 |
| 文件读取链接 | 新增 `-i/--input-file` 参数，从文件读取链接，下载后清空文件 |
| 失败重试 | 下载失败自动重试 N 次；无论成功失败都记录到 db；新增 `-r/--retry-failed` 重试失败记录 |

## 文档导航

- [01-getting-started.md](01-getting-started.md) - 项目入门
- [02-config.md](02-config.md) - 配置文件详解
- [03-development.md](03-development.md) - 开发流程与核心技术原理
- [04-metadata.md](04-metadata.md) - 元数据保存
- [05-progress.md](05-progress.md) - 进度持久化与数据库结构
- [06-usage.md](06-usage.md) - 使用指南与 Skill 集成
- [07-faq.md](07-faq.md) - 本文（常见问题与扩展方向）
