# 01 - 项目入门

## 项目背景

`douyinDL` 是一个基于 [f2](https://github.com/Johnserf-Seed/f2) 库的抖音视频/合集下载工具。

核心诉求：

- **免登录**：不依赖账号 cookie，通过匿名 ttwid 访问抖音 API
- **合集友好**：自动识别合集链接，按序下载全部视频，间隔 60 秒防封 IP
- **断点续传**：基于 SQLite 记录下载历史，重复执行不会重复下载
- **元数据保存**：可选下载封面/文案/原声/JSON，并支持 `-m` 参数精细控制类型
- **配置化**：UA、chunk 大小、间隔时间等参数集中在 `config/config.yaml`

## 技术栈选型

| 技术 | 用途 | 选型理由 |
|------|------|----------|
| Python 3.12+ | 运行时 | f2 的 pydantic-core 依赖要求 3.12-3.13 |
| uv | 包管理 | 依赖隔离，不污染系统 Python |
| f2 >= 0.0.1.7 | 抖音 API 封装 | 提供 DouyinCrawler + ABogus 签名算法 |
| httpx | HTTP 下载 | f2 自带 0.27.2 版本，流式下载无水印视频 |
| pyyaml | 配置解析 | 读取 `config/config.yaml` |
| sqlite3 | 进度持久化 | Python 标准库，无需额外依赖 |

> 注意：项目不直接依赖 `httpx` 和 `rich`，由 f2 统一管理其版本，避免版本冲突。

## 项目结构

```
douyinDL/
├── .trae/skills/douyin-download/SKILL.md   # Skill 定义（Trae IDE 集成）
├── config/config.yaml                      # 配置文件（UA/间隔/开关等）
├── docs/                                   # 项目文档（按主题拆分）
│   ├── 01-getting-started.md               # 本文
│   ├── 02-config.md                        # 配置文件详解
│   ├── 03-development.md                   # 开发流程与核心技术原理
│   ├── 04-metadata.md                      # 元数据保存功能
│   ├── 05-progress.md                      # 进度持久化与数据库结构
│   ├── 06-usage.md                         # 使用指南与 Skill 集成
│   └── 07-faq.md                           # 常见问题与扩展方向
├── src/douyindl/
│   ├── __init__.py                         # 包入口，导出核心类
│   ├── __main__.py                         # python -m douyindl 入口
│   └── downloader.py                       # 核心下载逻辑（链接解析/API/下载/DB）
├── .gitignore
├── pyproject.toml                          # 项目依赖与 Python 版本约束
├── uv.lock                                 # uv 锁定的依赖版本
└── README.md                               # 项目概述与文档索引
```

运行时还会生成以下目录（已在 `.gitignore` 中忽略）：

```
downloads/                                  # 视频输出根目录（CLI -o 可覆盖）
.douyindl/progress.db                       # SQLite 进度数据库
.venv/                                      # uv 创建的虚拟环境
```

## 环境搭建

### 1. 安装 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

### 2. 克隆项目

```bash
git clone git@github.com:ByCnck/douyinDL.git
cd douyinDL
```

### 3. 初始化环境

```bash
# 固定 Python 版本（f2 要求 3.12-3.13）
uv python pin 3.12

# 创建虚拟环境并安装依赖（读取 pyproject.toml + uv.lock）
uv sync
```

### 4. 验证安装

```bash
# 查看帮助
NO_PROXY='*' no_proxy='*' uv run python -m douyindl -h
```

> 必须设置 `NO_PROXY='*'`，否则本机的 http_proxy 环境变量会干扰抖音请求。

### 5. 首次下载测试

```bash
# 下载单视频（最简验证）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -o ./downloads
```

成功后会在 `downloads/` 下生成 `YYYYMMDD_文案.mp4` 文件，并在 `.douyindl/progress.db` 写入下载记录。

## 下一步

- 配置参数详解：[02-config.md](02-config.md)
- 核心技术原理：[03-development.md](03-development.md)
- 使用指南：[06-usage.md](06-usage.md)
