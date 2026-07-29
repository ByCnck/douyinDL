# 抖音视频/合集下载工具 - 完整流程文档

> 项目路径：`/Users/zhenxi/codes/python/douyinDL`
> 文档创建日期：2026-07-29
> 版本：v0.2.0

---

## 目录

1. [项目背景与目标](#1-项目背景与目标)
2. [技术栈选型](#2-技术栈选型)
3. [项目结构](#3-项目结构)
4. [环境搭建（uv 依赖管理）](#4-环境搭建uv-依赖管理)
5. [配置文件说明](#5-配置文件说明)
6. [开发流程](#6-开发流程)
7. [核心技术原理](#7-核心技术原理)
8. [使用指南](#8-使用指南)
9. [Skill 集成](#9-skill-集成)
10. [常见问题与排查](#10-常见问题与排查)
11. [后续扩展方向](#11-后续扩展方向)

---

## 1. 项目背景与目标

### 1.1 需求来源

用户在抖音 App 中通过"分享"功能复制了一段合集分享文本：

```
0- 6.10 teO:/ 03/21 a@A.GI 我正在看【翟东升看百年大变局】
长按复制此条消息，打开抖音搜索，一起看合集~
https://v.douyin.com/SlGTwuMq498/ 8@9.com :1pm
```

需要：

1. 从该分享文本中提取合集的真实 URL；
2. 解析合集内每个视频的序号与下载地址；
3. 使用 Python 批量下载无水印 MP4 文件；
4. 封装为可复用的 skill，后续接收合集/单视频链接即可自动下载；
5. 使用 uv 管理 Python 依赖，**严禁系统级别安装依赖**；
6. 输出完整流程文档。

### 1.2 设计目标

- **零登录**：用户无需提供抖音账号 cookie，使用匿名 token 即可访问公开合集/视频接口
- **多场景兼容**：同时支持合集、单视频、短链、长链、分享文本
- **隔离环境**：基于 uv 创建独立虚拟环境，不污染系统 Python
- **配置化**：UA、chunk 大小、下载间隔等参数集中在 `config/config.yaml`
- **风控友好**：合集视频间间隔 60 秒下载，API 分页间隔 2 秒，避免被抖音封 IP
- **可复用**：以 skill 形式沉淀，后续直接调用

---

## 2. 技术栈选型

| 组件 | 选型 | 说明 |
| ---- | ---- | ---- |
| 语言 | Python 3.12+ | f2 的 pydantic-core 依赖要求 3.12-3.13 |
| 包管理 | uv | 用户强制要求，禁止系统级安装 |
| 核心库 | [f2](https://github.com/Johnserf-Seed/f2) `>=0.0.1.7` | 抖音 API 封装 + ABogus 签名算法 |
| 配置解析 | pyyaml `>=6.0` | 读取 `config/config.yaml` |
| HTTP 客户端 | httpx（f2 自带，锁定 0.27.2） | 异步流式下载 |
| Skill 框架 | Trae Skills | 团队内部 skill 体系 |

### 2.1 为何选用 f2

抖音 Web API 自 2024 年起强制要求 `a_bogus` 签名参数，该签名算法基于 JS 混淆，纯 Python 实现非常复杂。f2 库已经：

- 完整实现 ABogus 签名算法（基于 Node.js 子进程调用）
- 封装合集、单视频、用户主页等多种 API
- 提供 `TokenManager` 生成匿名 ttwid 的能力
- 内置 httpx 客户端与重试机制

直接复用 f2 的底层 crawler 即可获得稳定的 API 访问能力，无需重新实现签名逻辑。

### 2.2 为何不用 f2 的 CLI / handler 层

f2 的命令行入口（`f2 dy`）和 handler 层强制要求登录 cookie（用于查询用户信息、记录下载进度到 SQLite），但**合集 API 本身只需匿名 ttwid 即可访问**。因此本项目绕过 handler，直接调用底层 `DouyinCrawler`，实现免登录下载。

---

## 3. 项目结构

```
douyinDL/
├── .trae/
│   └── skills/
│       └── douyin-download/
│           └── SKILL.md              # Skill 定义文件
├── config/
│   └── config.yaml                  # 配置文件（UA/间隔/chunk_size 等）
├── docs/
│   └── DOUYIN_DOWNLOAD_FLOW.md      # 本文档
├── downloads/                       # 视频下载目录（运行时自动创建，.gitignore 忽略）
│   ├── 20260729_翟东升看百年大变局/  # 合集子目录（日期_合集名）
│   │   ├── 001_xxx.mp4
│   │   └── ...
│   └── 20260729_单视频文案.mp4       # 单视频（日期_文案）
├── logs/                            # f2 运行日志（.gitignore 忽略）
├── src/
│   └── douyindl/
│       ├── __init__.py              # 包入口，导出 DouyinDownloader / main
│       ├── __main__.py              # 支持 python -m douyindl 运行
│       └── downloader.py            # 核心下载逻辑
├── .gitignore
├── pyproject.toml                   # 项目依赖与构建配置
└── uv.lock                          # uv 锁定的依赖版本
```

### 3.1 核心文件说明

#### `src/douyindl/downloader.py`

项目核心模块，包含以下组件：

| 函数/类 | 职责 |
| ---- | ---- |
| `Config` | 从 `config.yaml` 加载配置（UA、间隔、chunk_size 等） |
| `resolve_share_url(share_url, config)` | 解析分享短链，返回 `(kind, resource_id)` |
| `build_cookie()` | 生成匿名 cookie（ttwid + 伪 msToken） |
| `fetch_mix_videos(mix_id, config, ...)` | 分页获取合集全部视频列表，返回 `(合集名, 视频列表)` |
| `fetch_one_video(aweme_id, config)` | 获取单个视频详情（使用 `_to_dict()` 而非 `_to_list()`） |
| `download_video(url, path, config, ...)` | httpx 流式下载无水印 MP4 |
| `DouyinDownloader` | 主流程类，串联解析→获取→下载 |

#### `pyproject.toml`

```toml
[project]
name = "douyindl"
version = "0.1.0"
requires-python = ">=3.12,<3.14"
dependencies = [
    "f2>=0.0.1.7",
    "pyyaml>=6.0",
]

[project.scripts]
douyindl = "douyindl:main"

[build-system]
requires = ["uv_build>=0.12.0,<0.13.0"]
build-backend = "uv_build"
```

关键约束：

- **Python 版本固定为 `>=3.12,<3.14`**：f2 依赖的 pydantic-core 在 3.14 上尚不支持
- **不直接依赖 httpx / rich**：让 f2 统一管理其版本（httpx==0.27.2, rich==13.9.3），避免版本冲突
- **显式依赖 pyyaml**：用于读取配置文件
- **不依赖 beautifulsoup4**：链接解析用正则即可，无需 HTML 解析库

---

## 4. 环境搭建（uv 依赖管理）

### 4.1 安装 uv

如果系统未安装 uv，执行以下命令（仅安装 uv 本身，不涉及任何 Python 包）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# 安装完成后，uv 二进制位于 ~/.local/bin/uv
export PATH="$HOME/.local/bin:$PATH"
```

### 4.2 初始化项目环境

```bash
cd /Users/zhenxi/codes/python/douyinDL

# 固定使用 Python 3.12（如系统未安装，uv 会自动下载）
uv python pin 3.12

# 同步依赖（生成 uv.lock + .venv）
uv sync
```

### 4.3 验证环境

```bash
# 查看虚拟环境 Python 版本
uv run python --version
# 应输出: Python 3.12.x

# 检查 f2 与配置加载是否正常
uv run python -c "from douyindl.downloader import Config; c=Config(); print(c.user_agent[:30], c.mix_download_interval)"
```

### 4.4 依赖隔离说明

- 所有 Python 依赖安装在项目 `.venv/` 目录下，**不污染系统 Python**
- `uv.lock` 锁定所有传递依赖的精确版本，保证跨机器可复现
- 后续新增依赖：编辑 `pyproject.toml` 的 `dependencies` 后执行 `uv sync`

### 4.5 重新初始化（如需）

```bash
# 清理旧环境
rm -rf .venv uv.lock

# 重新创建
uv python pin 3.12
uv sync
```

---

## 5. 配置文件说明

### 5.1 配置文件路径

```
config/config.yaml
```

### 5.2 配置项详解

```yaml
# ── HTTP 请求相关 ──────────────────────────────────────────────

# User-Agent：必须与 f2/conf/conf.yaml 中的 UA 保持一致，否则 a_bogus 签名校验失败
user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ..."

# HTTP 请求超时（秒）
timeout: 15

# API 请求最大重试次数
max_retries: 5

# 最大并发任务数（f2 crawler 内部信号量）
max_tasks: 5

# 最大连接数（httpx 连接池）
max_connections: 5


# ── 抖音 API 调用相关 ─────────────────────────────────────────

# 合集 API 分页每页条数（不建议超过 20，可能触发风控）
page_counts: 20

# API 分页请求间隔（秒），避免抖音风控
api_request_interval: 2.0


# ── 视频下载相关 ──────────────────────────────────────────────

# 合集内视频下载间隔（秒），合集场景使用，单视频无需等待
# 防止频繁下载被抖音封 IP，默认 60 秒
mix_download_interval: 60

# 流式下载 chunk 大小（字节），默认 64KB
chunk_size: 65536

# 下载文件名最大长度（字符）
filename_max_len: 60


# ── 输出目录相关 ──────────────────────────────────────────────

# 默认输出根目录（CLI -o 参数可覆盖）
output_dir: "./downloads"
```

### 5.3 配置加载机制

`Config` 类（`src/douyindl/downloader.py`）在初始化时：

1. 优先读取 CLI `-c` 指定的配置文件
2. 否则读取默认路径 `config/config.yaml`
3. 文件不存在或字段缺失时使用代码内默认值兜底

---

## 6. 开发流程

### 6.1 步骤一：链接解析

**目标**：从分享文本中提取 URL，并识别资源类型（合集/单视频）。

抖音分享文本形如：

```
0- 6.10 teO:/ 03/21 a@A.GI 我正在看【翟东升看百年大变局】... https://v.douyin.com/SlGTwuMq498/ 8@9.com :1pm
```

实现要点：

1. 用正则 `https?://[^\s，,]+` 从文本中提取 URL
2. 短链 `v.douyin.com/xxx` 会 302 重定向，需用 `httpx` 跟随重定向获取最终 URL
3. 最终 URL 可能是以下任一形态：
   - 合集：`https://www.iesdouyin.com/share/mix/detail/{mix_id}/`
   - 合集：`https://www.douyin.com/collection/{mix_id}`
   - 单视频：`https://www.douyin.com/video/{aweme_id}`
4. 用正则匹配最终 URL，返回 `(kind, resource_id)`

### 6.2 步骤二：匿名认证

抖音 API 要求请求头携带 `ttwid` 和 `msToken`。通过 f2 的 `TokenManager`：

```python
from f2.apps.douyin.utils import TokenManager

ttwid = TokenManager.gen_ttwid()           # 生成匿名 ttwid
ms_token = TokenManager.gen_false_msToken() # 生成伪 msToken
cookie = f"ttwid={ttwid}; msToken={ms_token}"
```

**关键点**：

- `ttwid` 是抖音对未登录用户的设备标识，通过特定接口可匿名获取
- `msToken` 在合集接口的校验较松，使用伪 token 即可通过
- **无需用户提供登录 cookie**，这是本项目区别于 f2 CLI 的核心差异

### 6.3 步骤三：调用合集 API

使用 f2 的 `DouyinCrawler.fetch_user_mix` 接口：

```python
async with DouyinCrawler(kwargs) as crawler:
    params = UserMix(cursor=cursor, count=20, mix_id=mix_id)
    response = await crawler.fetch_user_mix(params)
    mix = UserMixFilter(response)

page_items = mix._to_list()      # 当前页视频列表
cursor = mix.max_cursor          # 下一页游标
has_more = mix.has_more          # 是否还有更多
```

**API 端点**：`https://www.douyin.com/aweme/v1/web/mix/aweme/`

**合集名提取**：从响应的 `aweme_list[0].mix_info.mix_name` 字段获取（用于子目录命名）

**分页策略**：

- 每页 20 条（不建议超过 20，否则可能触发风控）
- 通过 `cursor` 翻页，`has_more=False` 时停止
- 每次请求间隔 2 秒（`api_request_interval`），避免限流

### 6.4 步骤四：单视频 API（重点修复）

单视频使用 `PostDetail` 模型与 `PostDetailFilter` 过滤器：

```python
async with DouyinCrawler(kwargs) as crawler:
    params = PostDetail(aweme_id=aweme_id)
    response = await crawler.fetch_post_detail(params)
    video = PostDetailFilter(response)

# 重要：PostDetailFilter 只有 _to_dict() 方法（返回字典）
# 没有 _to_list()，与 UserMixFilter 不同！
item = video._to_dict()
```

**踩坑记录**：早期代码错误地调用 `PostDetailFilter._to_list()`，导致 `AttributeError`。f2 中：

- `UserMixFilter` 继承自 `UserPostFilter`，有 `_to_list()`（返回 list）
- `PostDetailFilter` 直接继承 `JSONModel`，只有 `_to_dict()`（返回 dict）

### 6.5 步骤五：视频下载与命名

从 API 返回的数据中提取无水印视频地址：

```python
play_addr = video.get("video_play_addr")
if isinstance(play_addr, list):
    play_addr = play_addr[0]  # 多个清晰度，取第一个
```

下载实现要点：

- 使用 `httpx.AsyncClient.stream` 流式下载，避免大文件占内存
- chunk_size 从配置文件读取（默认 64KB）
- 实时打印进度条
- 文件已存在则跳过（断点续传效果）

**命名规则**（v0.2.0 新增）：

| 场景 | 文件路径 | 说明 |
| ---- | ---- | ---- |
| 合集 | `downloads/YYYYMMDD_合集名/001_文案.mp4` | 目录含日期，文件用序号 |
| 单视频 | `downloads/YYYYMMDD_文案.mp4` | 文件名含日期前缀 |

**合集下载间隔**（v0.2.0 新增）：

```python
# 合集场景：视频间间隔等待（最后一个不需要）
if download_interval and i < len(videos):
    print(f"      等待 {download_interval} 秒后继续下载下一个...")
    await asyncio.sleep(download_interval)
```

默认 60 秒，防止频繁下载被抖音封 IP。

### 6.6 步骤六：文件名规范化

视频文案中常含 `#话题` 标签和特殊字符，需清理：

```python
def _sanitize_filename(name: str, max_len: int = 60) -> str:
    # 1. 去除 #话题 标签
    name = re.sub(r"#[^\s#]+", "", name)
    # 2. 去除文件名非法字符
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "", name).strip()
    # 3. 合并多余下划线/空格
    name = re.sub(r"[_\s]+", "_", name).strip("_")
    # 4. 限制长度
    if len(name) > max_len:
        name = name[:max_len].strip("_")
    return name or "untitled"
```

---

## 7. 核心技术原理

### 7.1 抖音链接解析流程

```
分享文本
   │
   ▼
正则提取 https://v.douyin.com/SlGTwuMq498/
   │
   ▼ (httpx 302 重定向)
https://www.iesdouyin.com/share/mix/detail/{mix_id}/?...
   │
   ▼ (再次 302)
https://www.douyin.com/collection/{mix_id}
   │
   ▼
正则匹配 → ("mix", mix_id)
```

### 7.2 匿名认证机制

| Token | 获取方式 | 用途 |
| ---- | ---- | ---- |
| `ttwid` | `TokenManager.gen_ttwid()` | 设备标识，未登录用户的唯一身份 |
| `msToken` | `TokenManager.gen_false_msToken()` | 请求签名校验（合集接口宽松） |
| `a_bogus` | f2 内部 ABogus 算法自动生成 | API 参数签名，防止请求伪造 |

**请求头示例**：

```http
GET /aweme/v1/web/mix/aweme/?mix_id=xxx&cursor=0&count=20&a_bogus=xxx HTTP/1.1
Host: www.douyin.com
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0
Referer: https://www.douyin.com/
Cookie: ttwid=xxx; msToken=xxx
```

### 7.3 ABogus 签名算法

抖音 Web API 的 `a_bogus` 参数基于以下要素生成：

- 请求 URL 与参数
- User-Agent
- 浏览器环境指纹（Canvas、WebGL、字体等，f2 使用预设值）
- 时间戳

f2 通过 Node.js 子进程执行混淆后的 JS 代码生成签名，Python 侧调用 `f2.apps.douyin.utils.ABogus` 即可。

**重要**：User-Agent 必须与 f2 配置文件中的预设值一致，否则签名校验失败。本项目的 `config.yaml` 中 `user_agent` 字段与 `f2/conf/conf.yaml` 中的 `User-Agent` 保持同步。

### 7.4 视频下载地址结构

API 返回的合集视频数据经 `UserMixFilter` 过滤后，关键字段：

| 字段 | 说明 |
| ---- | ---- |
| `aweme_id` | 视频 ID |
| `desc` | 视频文案 |
| `nickname` | 作者昵称 |
| `video_play_addr` | 无水印视频地址（可能是 list） |
| `cover` | 封面图地址 |
| `create_time` | 创建时间字符串 |

`video_play_addr` 形如 `https://v3-web.douyinvapi.com/xxx?...`，可直接 GET 下载。

---

## 8. 使用指南

### 8.1 基本用法

```bash
cd /Users/zhenxi/codes/python/douyinDL
export PATH="$HOME/.local/bin:$PATH"

# 禁用代理（抖音是国内服务，环境中的 http_proxy 会干扰请求）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl "<分享链接或文本>" -o ./downloads
```

### 8.2 参数说明

| 参数 | 说明 | 默认值 |
| ---- | ---- | ---- |
| `url` | 抖音分享链接或包含链接的分享文本（必填） | - |
| `-o, --output` | 视频保存根目录 | `./downloads`（config.yaml 可改） |
| `-n, --max-counts` | 最大下载视频数，0 表示不限 | `0` |
| `-c, --config` | 配置文件路径 | `config/config.yaml` |

### 8.3 使用示例

#### 示例 1：下载整个合集（直接粘贴分享文本）

```bash
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "0- 6.10 teO:/ ... 我正在看【翟东升看百年大变局】... https://v.douyin.com/SlGTwuMq498/ 8@9.com" \
  -o ./downloads
```

脚本会自动从分享文本中提取 URL，合集会创建子目录 `downloads/20260729_翟东升看百年大变局/`。

#### 示例 2：仅下载合集前 5 个视频

```bash
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -n 5
```

#### 示例 3：下载单个视频

```bash
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/LQVBJcukSyA/"
```

单视频文件名格式：`downloads/20260729_视频文案.mp4`

#### 示例 4：使用自定义配置文件

```bash
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -c /path/to/config.yaml
```

#### 示例 5：在 Python 代码中调用

```python
import asyncio
from douyindl import DouyinDownloader

dl = DouyinDownloader()  # 自动读取 config/config.yaml
asyncio.run(dl.run("https://v.douyin.com/SlGTwuMq498/"))
```

### 8.4 输出结构示例

```
downloads/
├── 20260729_翟东升看百年大变局/            # 合集目录（日期_合集名）
│   ├── 001_翟东升_既得利益者肤浅_新时代年轻人当君临天下.mp4
│   ├── 002_翟东升_美元体系其实是_特例_不是常态.mp4
│   ├── 003_翟东升_逆全球化会持续多久_这轮核时代不能开战.mp4
│   └── ...
└── 20260729_用最好的动画讲解HBM原理.mp4      # 单视频（日期_文案）
```

### 8.5 运行输出示例

```
[1/3] 解析分享链接: https://v.douyin.com/SlGTwuMq498/
      检测到合集链接, mix_id=7658437054228858923
      合集名称: 翟东升看百年大变局
[2/3] 共获取 19 个视频:
    1. [7653413470104210724] 翟东升：既得利益者肤浅...
    2. [7653413292609637670] 翟东升：美元体系其实是"特例"...
    ...
   19. [7400000000000019] 白左到底从哪来？翟东升给了一个反常识的答案...
[3/3] 开始下载到 downloads/20260729_翟东升看百年大变局/ ...
  [1/19] 下载 001_翟东升_既得利益者肤浅_新时代年轻人当君临天下.mp4
  [==============================] 100%
      等待 60 秒后继续下载下一个...
  [2/19] 下载 002_翟东升_美元体系其实是_特例_不是常态.mp4
  [==============================] 100%
      等待 60 秒后继续下载下一个...
  ...

完成: 19/19 个视频已下载到 downloads/20260729_翟东升看百年大变局/
```

---

## 9. Skill 集成

### 9.1 Skill 文件位置

```
.trae/skills/douyin-download/SKILL.md
```

### 9.2 Skill 定义要点

```markdown
---
name: "douyin-download"
description: "Downloads Douyin (抖音) videos or video collections from share links. Invoke when user provides a Douyin share link/text containing v.douyin.com URL and wants to download videos."
---
```

**description 字段必须包含触发条件**：使用 "Invoke when..." 句式，便于模型识别。

### 9.3 Skill 调用流程

1. 用户发送包含抖音链接的消息
2. Trae IDE 匹配到 `douyin-download` skill 的触发条件
3. Skill 加载，执行预设的 `uv run python -m douyindl` 命令
4. 脚本输出解析结果与下载进度
5. 完成后视频保存在 `downloads/` 目录

---

## 10. 常见问题与排查

### 10.1 `uv: command not found`

**原因**：uv 未安装或未加入 PATH。

**解决**：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

### 10.2 `'PostDetailFilter' object has no attribute '_to_list'`

**原因**：f2 中 `PostDetailFilter` 只有 `_to_dict()` 方法（返回字典），没有 `_to_list()`。

**解决**：单视频场景使用 `video._to_dict()` 而非 `video._to_list()`。本项目 v0.2.0 已修复。

### 10.3 API 请求返回空数据或 401

**可能原因**：

1. **代理干扰**：本地存在 `http_proxy` / `https_proxy` 环境变量
   - **解决**：命令前加 `NO_PROXY='*' no_proxy='*'`
2. **User-Agent 不匹配**：a_bogus 签名依赖特定 UA
   - **解决**：使用 `config.yaml` 中的 `user_agent`，与 f2 配置保持一致
3. **ttwid 失效**：极少发生，匿名 ttwid 通常长期有效
   - **解决**：重新运行即可，`build_cookie()` 每次生成新 token

### 10.4 依赖版本冲突（httpx / rich）

**原因**：f2 锁定了 `httpx==0.27.2` 和 `rich==13.9.3`，若 `pyproject.toml` 中显式声明其他版本会冲突。

**解决**：从 `pyproject.toml` 的 `dependencies` 中移除 `httpx` 和 `rich`，让 f2 统一管理。

### 10.5 Python 版本不兼容

**原因**：f2 依赖的 `pydantic-core` 在 Python 3.14 上尚未提供预编译 wheel。

**解决**：`pyproject.toml` 中固定 `requires-python = ">=3.12,<3.14"`，并用 `uv python pin 3.12` 锁定版本。

### 10.6 视频文案含特殊字符导致文件名异常

**原因**：抖音文案中可能包含 `#话题`、`@用户`、换行符等。

**解决**：`_sanitize_filename()` 函数已处理：

- 去除 `#话题` 标签
- 过滤 `\/:*?"<>|` 等非法字符
- 合并多余下划线/空格
- 限制长度为 60 字符（可配置）

### 10.7 合集下载被抖音封 IP

**原因**：频繁下载触发风控。

**解决**：

- 合集视频间间隔已默认设为 60 秒（`config.yaml` 的 `mix_download_interval`）
- 可适当增大该值（如 90 或 120 秒）
- 也可分批下载，用 `-n` 参数限制单次下载数量

### 10.8 合集视频数量超过 20 个

**原因**：单次 API 请求最多返回 20 条。

**解决**：`fetch_mix_videos()` 已实现自动分页，通过 `cursor` 翻页直到 `has_more=False`。

---

## 11. 后续扩展方向

### 11.1 支持更多资源类型

当前支持合集与单视频，可扩展支持：

- 用户主页全部视频（`sec_user_id`）
- 用户喜欢列表
- 直播回放
- 图集（图文作品）

### 11.2 下载元数据

可额外保存：

- 视频封面（`cover` 字段）
- 视频文案全文（`desc.txt`）
- 原声 MP3（`music_play_url`）
- 视频信息 JSON（aweme_id、create_time、stats 等）

### 11.3 并发下载

当前为串行下载，可改为 `asyncio.gather` 并发下载（建议并发数 3-5，避免触发风控）。

### 11.4 进度持久化

将已下载视频的 `aweme_id` 记录到本地数据库，支持：

- 跨会话断点续传
- 增量下载（仅下载合集新增视频）

---

## 附录 A：关键代码索引

| 功能 | 文件 | 函数/类 |
| ---- | ---- | ---- |
| 配置加载 | `src/douyindl/downloader.py` | `Config` |
| 链接解析 | `src/douyindl/downloader.py` | `resolve_share_url()` |
| 匿名 Token | `src/douyindl/downloader.py` | `build_cookie()` |
| 合集 API | `src/douyindl/downloader.py` | `fetch_mix_videos()` |
| 单视频 API | `src/douyindl/downloader.py` | `fetch_one_video()` |
| 视频下载 | `src/douyindl/downloader.py` | `download_video()` |
| 文件名清理 | `src/douyindl/downloader.py` | `_sanitize_filename()` |
| 主流程 | `src/douyindl/downloader.py` | `DouyinDownloader.run()` |
| CLI 入口 | `src/douyindl/downloader.py` | `main()` |

## 附录 B：依赖版本清单（uv.lock 关键项）

| 包 | 版本 | 说明 |
| ---- | ---- | ---- |
| f2 | >=0.0.1.7 | 抖音 API 封装 |
| pyyaml | >=6.0 | 配置文件解析 |
| httpx | 0.27.2 | f2 锁定，异步 HTTP |
| rich | 13.9.3 | f2 锁定，终端输出 |
| pydantic | 2.9.2 | f2 依赖 |
| pydantic-core | 2.23.4 | 要求 Python 3.12-3.13 |
| lxml | 最新 | f2 依赖 |

## 附录 C：版本变更记录

### v0.2.0（2026-07-29）

- 新增 `config/config.yaml` 配置文件，集中管理 UA / chunk_size / 间隔时间等参数
- 合集下载创建子目录 `YYYYMMDD_合集名/`
- 单视频文件名改为 `YYYYMMDD_文案.mp4`
- 合集视频间下载间隔默认 60 秒（防封 IP）
- 修复单视频下载报错（`PostDetailFilter._to_list()` → `_to_dict()`）
- 移除未使用的 `beautifulsoup4` 依赖
- 移除未使用的 `MixIdFetcher` / `AwemeIdFetcher` 导入
- 新增 `-c/--config` CLI 参数支持自定义配置文件

### v0.1.0（2026-07-29）

- 初始版本
- 支持抖音合集/单视频链接解析与下载
- 基于 f2 库实现匿名认证与 API 调用
- 集成 Trae Skill 框架

---

**文档结束**
