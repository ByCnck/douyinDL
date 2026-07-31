# 05 - 进度持久化与数据库结构

本工具基于 SQLite 记录下载历史，支持断点续传、增量下载，并在 v0.4.0 扩展了视频元数据字段（时长、分辨率、文件大小等，全部从 API 响应提取，不依赖本地分析视频文件）。v0.5.0 新增下载状态字段（success/failed），无论成功失败都记录，并支持 `-r/--retry-failed` 重试失败记录。

## 功能概述

- **断点续传**：已下载成功的视频自动跳过，避免重复下载
- **增量下载**：合集新增视频后，再次执行只下载新视频
- **失败记录**：下载失败也记录到数据库（status='failed'），不跳过，下次会重新下载
- **自动重试**：下载失败时自动重试 N 次（`download_max_retries` 配置）
- **失败重试命令**：`-r/--retry-failed` 一键重试数据库中所有失败记录
- **元数据入库**：从 API 响应提取时长/分辨率/文件大小/码率/帧率/作者/统计等 15 个字段
- **自动迁移**：旧版数据库启动时自动补齐新字段，保留历史记录

## 数据库位置

默认路径：`.douyindl/progress.db`（相对当前工作目录）

可在 `config/config.yaml` 中通过 `progress_db_path` 修改，或设置 `enable_progress: false` 关闭。

## 表结构

表名：`downloaded_videos`

### 基础字段（v0.3.0）

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `aweme_id` | TEXT | PRIMARY KEY | 视频 ID（抖音 aweme_id） |
| `resource_type` | TEXT | NOT NULL | 资源类型：`mix`（合集）或 `one`（单视频） |
| `resource_id` | TEXT | NOT NULL | mix_id 或 aweme_id |
| `mix_name` | TEXT | | 合集名（单视频为 NULL） |
| `desc` | TEXT | | 视频文案（截断 200 字符） |
| `file_path` | TEXT | | 视频保存路径 |
| `downloaded_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 下载时间 |

### 视频元数据字段（v0.4.0 新增）

这些字段全部从 API 原始响应提取，不依赖本地分析视频文件。提取逻辑见 `_extract_video_meta()`。

**视频技术参数：**

| 字段 | 类型 | 说明 | API 来源 |
|------|------|------|----------|
| `duration` | INTEGER | 视频时长（毫秒） | `video.duration` |
| `width` | INTEGER | 视频宽度（像素） | `video.width` |
| `height` | INTEGER | 视频高度（像素） | `video.height` |
| `file_size` | INTEGER | 文件大小（字节） | `video.bit_rate[0].play_addr.data_size` |
| `bit_rate` | INTEGER | 视频码率（bps） | `video.bit_rate[0].bit_rate` |
| `fps` | REAL | 帧率 | `video.bit_rate[0].FPS` |
| `ratio` | TEXT | 分辨率标识，如 `2160p` | `video.ratio` |
| `video_format` | TEXT | 视频格式，如 `mp4` | `video.format` |
| `is_h265` | INTEGER | 是否 H.265 编码（0/1） | `video.bit_rate[0].is_h265` |

**作者与统计信息：**

| 字段 | 类型 | 说明 | API 来源 |
|------|------|------|----------|
| `nickname` | TEXT | 作者昵称 | `author.nickname` |
| `digg_count` | INTEGER | 点赞数 | `statistics.digg_count` |
| `comment_count` | INTEGER | 评论数 | `statistics.comment_count` |
| `share_count` | INTEGER | 分享数 | `statistics.share_count` |
| `collect_count` | INTEGER | 收藏数 | `statistics.collect_count` |
| `create_time` | TEXT | 视频创建时间（字符串） | `aweme.create_time`（时间戳转字符串） |

### 下载状态字段（v0.5.0 新增）

无论下载成功还是失败，都会记录到数据库。失败记录不跳过，下次执行会重新下载，也可通过 `-r/--retry-failed` 手动重试。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `status` | TEXT | `'success'` | 下载状态：`success`（成功）或 `failed`（失败） |
| `error_msg` | TEXT | NULL | 失败时的错误信息（成功时为 NULL），截断 500 字符 |
| `retry_count` | INTEGER | 0 | 已重试次数（不含首次下载），0 表示首次即成功/失败 |

> 旧数据库迁移时，已有记录的 `status` 默认为 `'success'`，`error_msg` 为 NULL，`retry_count` 为 0。

### 索引

| 索引名 | 字段 | 用途 |
|--------|------|------|
| `idx_resource_id` | `resource_id` | 加速合集场景下查询已下载视频 |
| `idx_status` | `status` | 加速 `-r/--retry-failed` 查询失败记录 |

## 表结构迁移

实现：[_migrate_schema()](../src/douyindl/downloader.py)

为兼容旧版数据库（v0.3.0 只有 7 个基础字段），启动时自动执行迁移：

```
1. CREATE TABLE IF NOT EXISTS downloaded_videos (... 基础字段 ...)
2. PRAGMA table_info(downloaded_videos)  → 获取现有列名集合
3. 对 18 个新字段逐个检查（15 个元数据字段 + 3 个状态字段）：
   若列不存在 → ALTER TABLE downloaded_videos ADD COLUMN {col} {type}
4. CREATE INDEX IF NOT EXISTS idx_resource_id ON downloaded_videos(resource_id)
5. CREATE INDEX IF NOT EXISTS idx_status ON downloaded_videos(status)
6. COMMIT
```

迁移是幂等的：新数据库会跳过 ALTER，旧数据库补齐字段后保留历史记录。
旧记录的 `status` 默认为 `'success'`（由 DEFAULT 约束自动填充）。

## 断点续传与增量下载

### 跳过逻辑

每个视频下载前检查（`force=False` 时）：

```
if 文件已存在于磁盘:
    跳过，计入 success
elif progress_db.is_success_downloaded(aweme_id):   # 仅查 status='success'
    跳过，计入 skipped
else:
    执行下载（带重试）
```

> 注意：失败记录（status='failed'）不会跳过，下次执行会重新下载。
> `is_success_downloaded()` 只检查 status='success' 的记录，不验证文件是否还在磁盘上。

### 下载重试

下载失败时自动重试 `download_max_retries` 次（默认 3 次，0 表示不重试），重试间隔由 `download_retry_interval` 控制（默认 5 秒）。

```
for attempt in range(1 + download_max_retries):   # 首次 + 重试次数
    try:
        await download_video(...)
        记录 status='success', retry_count=attempt
        break
    except:
        if 还有重试机会:
            等待 download_retry_interval 秒
        else:
            记录 status='failed', error_msg=错误信息, retry_count=attempt
```

### 强制重新下载

CLI `-f/--force` 参数忽略进度记录，强制重新下载：

```bash
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -f
```

## 失败记录重试（--retry-failed）

CLI `-r/--retry-failed` 参数从数据库查询所有 `status='failed'` 的记录，重新下载：

```bash
NO_PROXY='*' no_proxy='*' uv run python -m douyindl -r
```

流程：

```
1. 查询 db 中所有 status='failed' 的记录
2. 对每条记录：
   a. 用 aweme_id 调用 fetch_one_video 获取最新下载地址（旧地址可能已失效）
   b. 下载到原 file_path（带重试，复用 download_max_retries 配置）
   c. 成功 → 更新 status='success'；失败 → 更新 error_msg 和 retry_count
3. 失败记录之间等待 mix_download_interval 秒，避免风控
```

> 适用场景：某些视频因网络波动临时失败，网络恢复后用 `-r` 一键重试，无需重新提供链接。

## 视频元数据入库流程

```
[1] API 返回原始响应
      │  aweme_list[i] 或 aweme_detail
      ▼
[2] _extract_video_meta(aweme)
      │  提取 15 个字段 → _meta 字典
      │  挂到 video_data["_meta"]
      ▼
[3] 视频下载成功后
      │  progress_db.record(aweme_id, ..., meta=video_data["_meta"])
      ▼
[4] INSERT ... ON CONFLICT(aweme_id) DO UPDATE
      │  新记录插入，已存在则更新（含 downloaded_at 刷新）
      ▼
[5] 控制台打印元数据摘要
      │  如：时长 1004.7s, 分辨率 3840x2160, 大小 152.4MB, 2160p, H.265
```

## 查询示例

使用 `sqlite3` 命令行直接查询：

```bash
# 查看所有已下载视频（含状态）
sqlite3 .douyindl/progress.db \
  "SELECT aweme_id, status, mix_name, desc, duration, width||'x'||height, file_size FROM downloaded_videos"

# 查看所有失败记录
sqlite3 .douyindl/progress.db \
  "SELECT aweme_id, desc, error_msg, retry_count, downloaded_at FROM downloaded_videos WHERE status='failed'"

# 查看某合集的下载情况（含成功/失败）
sqlite3 .douyindl/progress.db \
  "SELECT aweme_id, status, desc, file_size, downloaded_at FROM downloaded_videos WHERE resource_id='<mix_id>'"

# 统计各合集下载成功数量
sqlite3 .douyindl/progress.db \
  "SELECT resource_id, mix_name, COUNT(*) as cnt FROM downloaded_videos WHERE resource_type='mix' AND status='success' GROUP BY resource_id"

# 查看大文件（>100MB）
sqlite3 .douyindl/progress.db \
  "SELECT aweme_id, desc, file_size/1024/1024 as mb FROM downloaded_videos WHERE file_size > 104857600"
```

代码内查询：

| 方法 | 用途 |
|------|------|
| `is_success_downloaded(aweme_id)` | 查询某视频是否已记录为成功（仅 status='success'） |
| `query_failed()` | 查询所有失败记录（status='failed'），用于 `-r/--retry-failed` |
| `count_by_resource(resource_id)` | 统计某合集已下载成功的视频数（仅 status='success'） |

## 下一步

- 完整使用指南：[06-usage.md](06-usage.md)
- 常见问题排查：[07-faq.md](07-faq.md)
