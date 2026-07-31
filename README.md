# douyinDL - 抖音视频/合集下载工具

基于 [f2](https://github.com/Johnserf-Seed/f2) 库的免登录抖音视频下载器，支持合集与单视频，使用 uv 管理依赖。

## 功能特性

- **免登录下载**：通过匿名 ttwid 访问抖音 API，无需提供账号 cookie
- **合集/单视频**：自动识别链接类型，合集创建子目录 `YYYYMMDD_合集名/`
- **风控友好**：合集视频间间隔 60 秒下载，API 分页间隔 2 秒
- **文件读取链接**：`-i` 参数从文件读取多个链接，下载后自动清空文件
- **失败自动重试**：下载失败自动重试 N 次（可配置），无论成功失败都记录到数据库
- **失败重试命令**：`-r/--retry-failed` 一键重试数据库中的失败记录
- **元数据保存**：可选保存封面/文案/原声/JSON（`-m` 参数精细控制）
- **进度持久化**：基于 SQLite 记录下载历史（含成功/失败状态），支持断点续传与增量下载
- **视频元数据入库**：时长/分辨率/文件大小/码率/帧率/作者/统计等写入数据库
- **配置化**：UA、chunk 大小、间隔时间、重试次数等参数集中在 `config/config.yaml`
- **依赖隔离**：使用 uv 创建独立虚拟环境，不污染系统 Python

## 快速开始

```bash
# 1. 安装 uv（如未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# 2. 克隆并进入项目
git clone git@github.com:ByCnck/douyinDL.git
cd douyinDL

# 3. 初始化环境（自动创建 .venv + 安装依赖）
uv python pin 3.12
uv sync

# 4. 下载合集（直接粘贴分享文本即可）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "0- 6.10 teO:/ ... https://v.douyin.com/SlGTwuMq498/ 8@9.com" -o ./downloads

# 5. 仅下载原声 MP3
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -m music

# 6. 从文件读取链接（多个链接粘贴到文件，下载后自动清空）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl -i links.txt -o ./downloads
```

## CLI 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `url` | 抖音分享链接或包含链接的分享文本（可选）；支持空格分隔多个链接。未指定时需用 `-i` | - |
| `-i, --input-file` | 从文件读取分享链接（多行/空格分隔），下载完成后清空文件，便于反复复用 | - |
| `-o, --output` | 视频保存根目录 | `./downloads` |
| `-n, --max-counts` | 最大下载视频数，0 表示不限 | `0` |
| `-c, --config` | 配置文件路径 | `config/config.yaml` |
| `-f, --force` | 强制重新下载，忽略进度数据库记录 | 关闭 |
| `-r, --retry-failed` | 重试数据库中所有下载失败的记录（无需提供 url/-i） | 关闭 |
| `-m [TYPES]` | 保存元数据，可选 `all`/`cover`/`desc`/`music`/`json`（逗号分隔） | 关闭 |

> `url` 与 `-i` 至少指定一个，可同时使用（两处链接合并去重后串行下载）。
> `-r` 单独使用，从数据库查询失败记录并重新下载。

## 输出结构

```
downloads/
├── 20260729_翟东升看百年大变局/        # 合集目录（日期_合集名）
│   ├── 001_翟东升_xxx.mp4
│   ├── 001_翟东升_xxx.jpg              # 封面（-m 时生成）
│   ├── 001_翟东升_xxx.mp3              # 原声（-m music 时生成）
│   └── ...
└── 20260729_单视频文案.mp4              # 单视频（日期_文案）

.douyindl/
└── progress.db                          # SQLite 进度数据库（自动创建）
```

## 文档索引

详细文档按主题拆分，位于 `docs/` 目录：

| 文档 | 内容 |
|------|------|
| [01-getting-started.md](docs/01-getting-started.md) | 项目背景、技术栈选型、项目结构、环境搭建 |
| [02-config.md](docs/02-config.md) | 配置文件 `config.yaml` 全部参数详解 |
| [03-development.md](docs/03-development.md) | 开发流程、核心技术原理（链接解析/匿名认证/ABogus签名） |
| [04-metadata.md](docs/04-metadata.md) | 元数据保存（封面/文案/原声/JSON，`-m` 参数精细控制） |
| [05-progress.md](docs/05-progress.md) | 进度持久化（SQLite 数据库结构、断点续传、增量下载、视频元数据入库） |
| [06-usage.md](docs/06-usage.md) | 使用指南（CLI 参数、示例、输出结构）与 Skill 集成 |
| [07-faq.md](docs/07-faq.md) | 常见问题排查、后续扩展方向、附录（代码索引/依赖清单/变更记录） |

## 技术栈

- **Python 3.12+**（f2 的 pydantic-core 依赖要求 3.12-3.13）
- **uv** 包管理，禁止系统级别安装依赖
- **f2 >= 0.0.1.7** 抖音 API 封装 + ABogus 签名算法
- **pyyaml** 配置文件解析
- **sqlite3**（Python 标准库）进度持久化，无需额外依赖

## 项目结构

```
douyinDL/
├── .trae/skills/douyin-download/SKILL.md   # Skill 定义
├── config/config.yaml                      # 配置文件
├── docs/                                   # 文档（按主题拆分）
├── src/douyindl/
│   ├── __init__.py                         # 包入口
│   ├── __main__.py                         # python -m douyindl 入口
│   └── downloader.py                       # 核心下载逻辑
├── pyproject.toml                          # 项目依赖
└── uv.lock                                 # uv 锁定的依赖版本
```

## License

MIT
