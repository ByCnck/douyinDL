---
name: "douyin-download"
description: "Downloads Douyin (抖音) videos or video collections from share links. Invoke when user provides a Douyin share link/text containing v.douyin.com URL and wants to download videos."
---

# 抖音视频/合集下载

## 功能

接收抖音分享链接（短链、合集、单视频均可），自动解析真实 URL，提取合集内全部视频列表（序号 + 标题 + 下载地址），并下载无水印 MP4 文件。

- **合集**：创建子目录 `YYYYMMDD_合集名/`，视频间间隔 60 秒下载（防封 IP），文件名 `001_文案.mp4`
- **单视频**：直接下载到输出目录，文件名 `YYYYMMDD_文案.mp4`
- **配置化**：UA、chunk 大小、间隔时间等参数集中在 `config/config.yaml`
- **元数据保存**：可选下载封面/文案/原声/JSON（`-m` 或配置 `save_metadata: true`）
- **进度持久化**：基于 SQLite 记录已下载视频，支持断点续传与增量下载（防重复下载）

## 触发条件

当用户提供包含以下任一形式的抖音链接并表达下载意图时调用：

- 抖音分享文本（包含 `v.douyin.com/xxx` 短链）
- 合集页 URL（`iesdouyin.com/share/mix/detail/{id}/` 或 `douyin.com/collection/{id}`）
- 单视频 URL（`douyin.com/video/{id}`）

## 使用方法

### 基本命令

```bash
# 确保使用 uv 运行，并禁用代理（抖音是国内服务）
export PATH="$HOME/.local/bin:$PATH"
cd /Users/zhenxi/codes/python/douyinDL
NO_PROXY='*' no_proxy='*' uv run python -m douyindl "<分享链接或文本>" -o ./downloads
```

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `url` | 抖音分享链接或包含链接的分享文本（可选）；支持空格分隔多个链接。未指定时需用 `-i` | - |
| `-i, --input-file` | 从文件读取分享链接（多行/空格分隔），下载完成后清空文件，便于反复复用 | - |
| `-o, --output` | 视频保存根目录 | `./downloads`（config.yaml 可改） |
| `-n, --max-counts` | 最大下载视频数，0 表示不限 | `0` |
| `-c, --config` | 配置文件路径 | `config/config.yaml` |
| `-f, --force` | 强制重新下载，忽略进度数据库记录 | 关闭 |
| `-m [TYPES]`, `--metadata [TYPES]` | 保存元数据，可选 `all`/`cover`/`desc`/`music`/`json`（逗号分隔多个）；仅 `-m` 等同于 `all` | 关闭 |

> `url` 与 `-i` 至少指定一个，可同时使用（两处链接合并去重后串行下载）。

### 示例

```bash
# 1. 下载合集（直接粘贴分享文本即可，脚本会自动提取 URL）
#    合集会创建子目录 downloads/20260729_翟东升看百年大变局/
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "0- 6.10 teO:/ ... https://v.douyin.com/SlGTwuMq498/ 8@9.com" \
  -o ./downloads

# 2. 仅下载合集前 5 个视频
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -n 5

# 3. 下载单个视频（文件名 YYYYMMDD_文案.mp4）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/LQVBJcukSyA/"

# 4. 使用自定义配置文件
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -c /path/to/config.yaml

# 5. 下载合集并保存全部元数据（封面/文案/原声/JSON）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -m

# 6. 仅下载原声 MP3（精细控制元数据类型）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -m music

# 7. 下载封面 + 原声（逗号分隔多个类型）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -m music,cover

# 8. 强制重新下载（忽略进度记录，覆盖已有文件）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -f

# 9. 批量下载多个链接（空格分隔，串行执行）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/LusIAXyGX-I/ https://v.douyin.com/w531WJ7dzEw/" \
  -o ./downloads

# 10. 从文件读取链接（多个链接粘贴到文件，下载后自动清空）
#     文件支持多行/空格分隔，适合链接较多的场景
NO_PROXY='*' no_proxy='*' uv run python -m douyindl -i links.txt -o ./downloads
```

## 输出结构

```
downloads/
├── 20260729_翟东升看百年大变局/        # 合集目录（日期_合集名）
│   ├── 001_翟东升_既得利益者肤浅.mp4
│   ├── 001_翟东升_既得利益者肤浅.jpg    # 封面（-m 时生成）
│   ├── 001_翟东升_既得利益者肤浅.txt    # 文案（-m 时生成）
│   ├── 001_翟东升_既得利益者肤浅.mp3    # 原声（-m 时生成）
│   ├── 001_翟东升_既得利益者肤浅.json   # 视频信息（-m 时生成）
│   ├── 002_翟东升_美元体系其实是特例.mp4
│   └── ...
└── 20260729_用最好的动画讲解HBM原理.mp4  # 单视频（日期_文案）

.douyindl/
└── progress.db                          # SQLite 进度数据库（自动创建）
```

## 配置文件

路径：`config/config.yaml`，关键参数：

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `user_agent` | UA（必须与 f2 一致，否则签名失败） | Chrome 130 Edg 130 |
| `timeout` | HTTP 超时（秒） | 15 |
| `page_counts` | 合集 API 每页条数 | 20 |
| `api_request_interval` | API 分页请求间隔（秒） | 2.0 |
| `mix_download_interval` | 合集视频下载间隔（秒，防封 IP） | 60 |
| `chunk_size` | 流式下载 chunk 大小（字节） | 65536 |
| `filename_max_len` | 文件名最大长度 | 60 |
| `save_metadata` | 是否保存元数据（封面/文案/原声/JSON） | false |
| `save_cover` / `save_desc` / `save_music` / `save_json` | 元数据子开关 | true |
| `enable_progress` | 是否启用进度数据库 | true |
| `progress_db_path` | 进度数据库路径 | `.douyindl/progress.db` |
| `output_dir` | 默认输出目录 | `./downloads` |

## 技术原理

1. **链接解析**：短链 `v.douyin.com/xxx` 经 HTTP 重定向到 `iesdouyin.com/share/mix/detail/{id}/`，再重定向到 `douyin.com/collection/{id}`，从中提取 mix_id 或 aweme_id
2. **匿名认证**：通过 f2 的 `TokenManager.gen_ttwid()` 获取匿名 ttwid（无需登录），配合伪 msToken 组成 cookie
3. **API 调用**：使用 f2 的 `DouyinCrawler` + ABogus 签名算法调用 `https://www.douyin.com/aweme/v1/web/mix/aweme/` 接口，分页获取合集视频列表
4. **视频下载**：从 API 返回的 `video.bit_rate[0].play_addr.url_list` 提取无水印地址，用 httpx 流式下载

## 注意事项

- **无需登录**：使用匿名 ttwid 即可访问合集 API，无需提供登录 cookie
- **代理禁用**：运行时必须设置 `NO_PROXY='*'`，否则环境代理变量会干扰抖音请求
- **Python 版本**：项目固定 `requires-python = ">=3.12,<3.14"`（f2 的 pydantic-core 依赖不支持 3.14）
- **风控策略**：合集下载每视频间隔 60 秒（可在 config.yaml 调整），API 分页间隔 2 秒
- **文件命名**：合集内 `001_文案.mp4`，单视频 `YYYYMMDD_文案.mp4`，自动去除 #话题标签
- **断点续传**：已存在的文件会自动跳过
