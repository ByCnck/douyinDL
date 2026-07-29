# 04 - 元数据保存

除视频 MP4 外，本工具可选保存四类元数据：封面图、文案全文、原声 MP3、完整信息 JSON。

## 功能开关

元数据保存受两层开关控制：

### 配置文件开关

在 `config/config.yaml` 中：

```yaml
save_metadata: false   # 总开关（默认关闭）
save_cover: true       # 子开关：封面 .jpg
save_desc: true        # 子开关：文案 .txt
save_music: true       # 子开关：原声 .mp3
save_json: true        # 子开关：信息 .json
```

子开关仅在 `save_metadata=true` 时生效。

### CLI `-m` 参数（精细控制）

CLI 的 `-m` 参数会**覆盖**配置文件开关，支持按类型精细控制：

| 用法 | 效果 |
|------|------|
| 不指定 `-m` | 沿用 `config.yaml` 的 `save_metadata` 设置 |
| `-m` 或 `-m all` | 开启全部四类元数据 |
| `-m music` | 仅下载原声 MP3，其余关闭 |
| `-m cover,desc` | 仅下载封面和文案，其余关闭 |
| `-m music,json` | 仅下载原声和信息 JSON |

合法类型：`all` / `cover` / `desc` / `music` / `json`（逗号分隔多个）。

## 输出文件

开启元数据保存后，每个视频旁会生成同名但扩展名不同的文件：

```
downloads/20260729_翟东升看百年大变局/
├── 001_翟东升_既得利益者肤浅.mp4      # 视频本体
├── 001_翟东升_既得利益者肤浅.jpg      # 封面（save_cover）
├── 001_翟东升_既得利益者肤浅.txt      # 文案（save_desc）
├── 001_翟东升_既得利益者肤浅.mp3      # 原声（save_music）
└── 001_翟东升_既得利益者肤浅.json     # 信息（save_json）
```

### 各类元数据说明

| 类型 | 扩展名 | 内容 | 数据来源 |
|------|--------|------|----------|
| 封面 | `.jpg` | 视频封面图 | `video_data["cover"]` URL 下载 |
| 文案 | `.txt` | 视频描述全文 | `video_data["desc"]` 原文写入 |
| 原声 | `.mp3` | 视频原声 MP3 | `video_data["music_play_url"]`（需 `music_status==1`） |
| 信息 | `.json` | f2 filter 过滤后的完整视频信息 | `json.dumps(video_data)` |

## 使用示例

```bash
# 1. 保存全部元数据（最常用）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -m

# 2. 仅下载原声 MP3（不想要封面/文案/JSON）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -m music

# 3. 下载封面 + 原声
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -m music,cover

# 4. 仅保存文案和 JSON（用于归档元信息）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -m desc,json
```

## 实现原理

实现：[download_metadata()](../src/douyindl/downloader.py)

```
对每个视频：
├── save_cover=True?  → _download_simple(cover_url, base.jpg)
├── save_desc=True?   → base.txt.write_text(desc)
├── save_music=True?  → 检查 music_status==1 → _download_simple(music_url, base.mp3)
└── save_json=True?   → base.json.write_text(json.dumps(video_data))
```

- 封面和原声通过 `_download_simple()` 下载（小文件，一次性写入，无进度条）
- 文案直接写入文本文件
- JSON 由 f2 filter 过滤后的 `video_data` 序列化生成（`default=str` 兜底不可序列化对象）
- 任一类型下载失败会打印警告但不中断主流程

> 注意：元数据中的 JSON 是 f2 filter 过滤后的字段，不包含 `video.width`/`height`/`data_size` 等嵌套字段。这些字段通过 `_extract_video_meta()` 单独提取后写入进度数据库，详见 [05-progress.md](05-progress.md)。

## 下一步

- 进度持久化与数据库结构：[05-progress.md](05-progress.md)
- 完整使用指南：[06-usage.md](06-usage.md)
