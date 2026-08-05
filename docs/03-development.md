# 03 - 开发流程与核心技术原理

本文梳理 `douyinDL` 的端到端下载流程，并解释关键技术的实现原理。

## 端到端流程

```
分享链接
   │
   ▼
[1] 链接解析        resolve_share_url()      → (kind, resource_id)
   │  kind = 'mix' (合集) 或 'one' (单视频)
   ▼
[2] 匿名认证        build_cookie()           → ttwid + 伪 msToken
   │
   ▼
[3] API 调用        fetch_mix_videos()       → 合集视频列表
   │                fetch_one_video()        → 单视频信息
   │  每项额外补充 _meta 子字典（时长/分辨率/文件大小等）
   ▼
[4] 逐个下载        download_video()         → 流式写入 mp4
   │  合集间隔 mix_download_interval 秒
   ▼
[5] 元数据保存      download_metadata()      → .jpg/.txt/.mp3/.json（可选）
   │
   ▼
[6] 进度记录        ProgressDB.record()      → SQLite，含视频元数据
   │
   ▼
完成
```

主入口在 [downloader.py](../src/douyindl/downloader.py) 的 `DouyinDownloader.run()` 方法。

## 核心技术原理

### 1. 链接解析

实现：[resolve_share_url()](../src/douyindl/downloader.py)

抖音分享链接有多种形态：

| 输入 | 重定向后 | 提取的资源 |
|------|----------|------------|
| `https://v.douyin.com/SlGTwuMq498/`（短链） | `douyin.com/collection/{id}` 或 `douyin.com/video/{id}` | mix_id / aweme_id |
| `https://www.iesdouyin.com/share/mix/detail/{id}/` | 同上 | mix_id |
| `https://www.douyin.com/collection/{id}` | 无重定向 | mix_id |
| `https://www.douyin.com/video/{id}` | 无重定向 | aweme_id |

解析步骤：

1. 先用正则 `_MIX_PATH_PATTERN` / `_AWEME_ID_PATTERN` 直接匹配长链接
2. 匹配失败则用 httpx 跟随重定向，拿到最终 URL
3. 再对最终 URL 做正则匹配
4. 全部失败时抛 `ValueError`

返回二元组 `(kind, resource_id)`：`kind` 为 `'mix'`（合集）或 `'one'`（单视频）。

### 2. 匿名认证

实现：[build_cookie()](../src/douyindl/downloader.py)

抖音 API 需要 cookie 鉴权，本工具采用**匿名认证**，无需登录账号：

```python
def build_cookie() -> str:
    ttwid = TokenManager.gen_ttwid()        # f2 生成匿名 ttwid
    ms_token = TokenManager.gen_false_msToken()  # 伪 msToken
    return f"ttwid={ttwid}; msToken={ms_token}"
```

- `ttwid`：抖音访客 ID，f2 通过特定算法生成，可免登录访问公开内容
- `msToken`：请求签名令牌，伪 token 即可通过校验

配合 `User-Agent`（必须与 f2 内部 UA 一致）和 `Referer: https://www.douyin.com/` 组成完整请求头。

### 3. API 调用与 ABogus 签名

实现：[fetch_mix_videos()](../src/douyindl/downloader.py)、[fetch_one_video()](../src/douyindl/downloader.py)

抖音 web API 使用 **a_bogus** 签名算法对请求参数签名，f2 的 `DouyinCrawler` 封装了这一过程。

> 关键点：本工具**绕过 f2 的 handler 层**，直接调用底层 crawler，因为 handler 层要求登录 cookie，而匿名 ttwid 无法通过 handler 校验。

合集 API：`https://www.douyin.com/aweme/v1/web/mix/aweme/`

- 分页参数：`cursor` + `count`
- 响应中 `aweme_list[0].mix_info.mix_name` 即合集名
- 通过 `UserMixFilter` 过滤出标准化视频字段

单视频 API：`https://www.douyin.com/aweme/v1/web/aweme/detail/`

- 参数：`aweme_id`
- 通过 `PostDetailFilter._to_dict()` 过滤（注意：单视频用 `_to_dict()`，合集用 `_to_list()`）

### 4. 视频元数据提取

实现：[_extract_video_meta()](../src/douyindl/downloader.py)

**问题**：f2 的 filter 在过滤时会丢失 `video` 对象下的嵌套字段（width/height/data_size 等），这些字段正是用户需要的"文件大小、时长、分辨率"。

**方案**：从 API 原始响应中补充提取，不依赖本地分析视频文件。

从原始 `aweme` 节点提取 15 个字段：

| 字段 | 来源 | 说明 |
|------|------|------|
| `duration` | `video.duration` | 视频时长（毫秒） |
| `width` | `video.width` | 视频宽度（像素） |
| `height` | `video.height` | 视频高度（像素） |
| `file_size` | `video.bit_rate[0].play_addr.data_size` | 文件大小（字节，来自 API） |
| `bit_rate` | `video.bit_rate[0].bit_rate` | 视频码率（bps） |
| `fps` | `video.bit_rate[0].FPS` | 帧率 |
| `ratio` | `video.ratio` | 分辨率标识，如 `2160p` |
| `video_format` | `video.format` | 格式，如 `mp4` |
| `is_h265` | `video.bit_rate[0].is_h265` | 是否 H.265 编码（0/1） |
| `nickname` | `author.nickname` | 作者昵称 |
| `digg_count` | `statistics.digg_count` | 点赞数 |
| `comment_count` | `statistics.comment_count` | 评论数 |
| `share_count` | `statistics.share_count` | 分享数 |
| `collect_count` | `statistics.collect_count` | 收藏数 |
| `create_time` | `aweme.create_time` | 视频创建时间（时间戳转字符串） |

提取后的元数据挂在视频字典的 `_meta` 键下，下载成功后随进度记录一起写入 SQLite。

### 5. 视频下载

实现：[download_video()](../src/douyindl/downloader.py)

- 下载地址来自 `video_play_addr`（无水印），可能是列表（多清晰度），取第一个
- 抖音 CDN 地址可能缺 `https:` 前缀，自动补齐
- 使用 httpx 流式下载，按 `chunk_size` 写入文件
- 下载过程打印进度条 `[=========---------] 45%`

合集场景：每个视频下载后等待 `mix_download_interval` 秒（默认 60 秒）再下载下一个，最后一个不等待。

### 6. 文件命名规则

实现：[_sanitize_filename()](../src/douyindl/downloader.py)

| 场景 | 命名格式 | 示例 |
|------|----------|------|
| 合集目录 | `YYYYMMDD_合集名` | `20260729_翟东升看百年大变局` |
| 合集内文件 | `NNN_文案.mp4` | `001_翟东升_既得利益者肤浅.mp4` |
| 单视频文件 | `YYYYMMDD_文案.mp4` | `20260729_用最好的动画讲解HBM原理.mp4` |

文案清理规则：

1. 去除 `#话题` 标签
2. 去除文件名非法字符 `\/:*?"<>|\n\r\t`
3. 合并多余下划线/空格
4. 截断到 `filename_max_len`（默认 60 字符）

## 关键代码索引

| 功能 | 位置 |
|------|------|
| 配置加载 | [Config 类](../src/douyindl/downloader.py) |
| 链接解析 | `resolve_share_url()` |
| 匿名认证 | `build_cookie()` / `build_crawler_kwargs()` |
| 合集视频列表 | `fetch_mix_videos()` / `_extract_mix_name()` |
| 单视频信息 | `fetch_one_video()` |
| 视频元数据提取 | `_extract_video_meta()` |
| 文件名清理 | `_sanitize_filename()` |
| 视频下载 | `download_video()` / `_print_progress()` |
| 元数据保存 | `download_metadata()` / `_download_simple()` |
| 进度持久化 | `ProgressDB` 类 |
| 主流程 | `DouyinDownloader.run()` |
| CLI 入口 | `main()` |

## 下一步

- 元数据保存详解：[04-metadata.md](04-metadata.md)
- 进度持久化数据库结构：[05-progress.md](05-progress.md)
- 分享链接机制（不同短链为何指向同一视频、aweme_id 去重）：[08-share-link-mechanism.md](08-share-link-mechanism.md)
