# 05 - 进度持久化与数据库结构

本工具基于 SQLite 记录下载历史，支持断点续传、增量下载，并在 v0.4.0 扩展了视频元数据字段（时长、分辨率、文件大小等，全部从 API 响应提取，不依赖本地分析视频文件）。

## 功能概述

- **断点续传**：已下载的视频自动跳过，避免重复下载
- **增量下载**：合集新增视频后，再次执行只下载新视频
- **元数据入库**：从 API 响应提取时长/分辨率/文件大小/码率/帧率/作者/统计等 15 个字段
- **自动迁移**：旧版数据库（v0.3.0）启动时自动补齐新字段，保留历史记录

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

### 索引

| 索引名 | 字段 | 用途 |
|--------|------|------|
| `idx_resource_id` | `resource_id` | 加速合集场景下查询已下载视频 |

## 表结构迁移

实现：[_migrate_schema()](../src/douyindl/downloader.py)

为兼容旧版数据库（v0.3.0 只有 7 个基础字段），启动时自动执行迁移：

```
1. CREATE TABLE IF NOT EXISTS downloaded_videos (... 基础字段 ...)
2. PRAGMA table_info(downloaded_videos)  → 获取现有列名集合
3. 对 15 个新字段逐个检查：
   若列不存在 → ALTER TABLE downloaded_videos ADD COLUMN {col} {type}
4. CREATE INDEX IF NOT EXISTS idx_resource_id ON downloaded_videos(resource_id)
5. COMMIT
```

迁移是幂等的：新数据库会跳过 ALTER，旧数据库补齐字段后保留历史记录。

## 断点续传与增量下载

### 跳过逻辑

每个视频下载前检查（`force=False` 时）：

```
if 文件已存在于磁盘:
    跳过，计入 success
elif progress_db.is_downloaded(aweme_id):
    跳过，计入 skipped
else:
    执行下载
```

> 注意：`is_downloaded()` 只检查数据库记录，不验证文件是否还在磁盘上。文件存在性由调用方额外判断。

### 强制重新下载

CLI `-f/--force` 参数忽略进度记录，强制重新下载：

```bash
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -f
```

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
# 查看所有已下载视频
sqlite3 .douyindl/progress.db \
  "SELECT aweme_id, mix_name, desc, duration, width||'x'||height, file_size FROM downloaded_videos"

# 查看某合集的下载情况
sqlite3 .douyindl/progress.db \
  "SELECT aweme_id, desc, file_size, downloaded_at FROM downloaded_videos WHERE resource_id='<mix_id>'"

# 统计各合集下载数量
sqlite3 .douyindl/progress.db \
  "SELECT resource_id, mix_name, COUNT(*) as cnt FROM downloaded_videos WHERE resource_type='mix' GROUP BY resource_id"

# 查看大文件（>100MB）
sqlite3 .douyindl/progress.db \
  "SELECT aweme_id, desc, file_size/1024/1024 as mb FROM downloaded_videos WHERE file_size > 104857600"
```

代码内查询：

| 方法 | 用途 |
|------|------|
| `is_downloaded(aweme_id)` | 查询某视频是否已记录 |
| `count_by_resource(resource_id)` | 统计某合集已下载视频数 |

## 下一步

- 完整使用指南：[06-usage.md](06-usage.md)
- 常见问题排查：[07-faq.md](07-faq.md)
