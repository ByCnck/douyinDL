# 06 - 使用指南与 Skill 集成

## 运行前置

所有命令必须满足两个条件：

1. 在项目根目录执行：`cd /Users/zhenxi/codes/python/douyinDL`
2. 禁用代理（抖音是国内服务）：`NO_PROXY='*' no_proxy='*'`

```bash
export PATH="$HOME/.local/bin:$PATH"
cd /Users/zhenxi/codes/python/douyinDL
```

## CLI 参数

```bash
uv run python -m douyindl [url] [选项]
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `url` | 抖音分享链接或包含链接的分享文本（可选）；支持空格分隔多个链接。未指定时需用 `-i` | - |
| `-i, --input-file` | 从文件读取分享链接（多行/空格分隔），下载完成后清空文件，便于反复复用 | - |
| `-o, --output` | 视频保存根目录 | `./downloads`（config.yaml 可改） |
| `-n, --max-counts` | 最大下载视频数，0 表示不限 | `0` |
| `-c, --config` | 配置文件路径 | `config/config.yaml` |
| `-f, --force` | 强制重新下载，忽略进度数据库记录 | 关闭 |
| `-r, --retry-failed` | 重试数据库中所有下载失败的记录（无需提供 url/-i） | 关闭 |
| `-m [TYPES]` | 保存元数据，可选 `all`/`cover`/`desc`/`music`/`json`（逗号分隔）；仅 `-m` 等同于 `all` | 关闭 |

> `url` 与 `-i` 至少指定一个，可同时使用（两处链接合并去重后串行下载）。
> `-r` 单独使用，从数据库查询失败记录并重新下载，无需提供 url/-i。
> `url` 支持直接粘贴抖音 App 分享的完整文本（含乱码字符），脚本会用正则自动提取其中的 URL。
> 也支持空格分隔多个链接，串行下载（详见下方场景 8）。

## 使用场景

### 1. 下载合集

```bash
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "0- 6.10 teO:/ ... https://v.douyin.com/SlGTwuMq498/ 8@9.com" \
  -o ./downloads
```

- 自动创建子目录 `downloads/YYYYMMDD_合集名/`
- 视频间间隔 54-60 秒随机（`mix_download_interval` 基准值 60，实际等待 base*0.9 到 base 之间）
- 文件名 `001_文案.mp4`、`002_文案.mp4`...

### 2. 下载单视频

```bash
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/LQVBJcukSyA/"
```

- 直接下载到输出目录
- 文件名 `YYYYMMDD_文案.mp4`

### 3. 限制下载数量

```bash
# 仅下载合集前 5 个视频
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -n 5
```

### 4. 保存元数据

```bash
# 保存全部元数据（封面/文案/原声/JSON）
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -m

# 仅下载原声 MP3
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -m music

# 封面 + 原声
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -m music,cover
```

详见 [04-metadata.md](04-metadata.md)。

### 5. 强制重新下载

```bash
# 忽略进度记录，覆盖已有文件
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -f
```

### 6. 重试下载失败的记录

下载失败的视频会记录到数据库（status='failed'），不会跳过。网络恢复后可用 `-r/--retry-failed` 一键重试所有失败记录，无需重新提供链接：

```bash
# 重试数据库中所有失败记录
NO_PROXY='*' no_proxy='*' uv run python -m douyindl -r
```

流程：
- 从数据库查询所有 `status='failed'` 的记录
- 对每条记录用 aweme_id 重新获取下载地址（旧地址可能已失效）
- 下载到原路径（带重试，复用 `download_max_retries` 配置）
- 成功则更新为 `status='success'`，失败则刷新 error_msg 和 retry_count
- 失败记录之间等待 `mix_download_interval` 秒，避免风控

> 下载失败时会自动重试 `download_max_retries` 次（默认 3 次），重试间隔 `download_retry_interval` 秒（默认 5 秒），均可在 config.yaml 配置。

### 7. 自定义配置文件

```bash
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/" -c /path/to/custom-config.yaml
```

### 8. 从文件读取链接（推荐用于多链接）

当链接较多时，命令行会变得冗长。可将分享链接粘贴到文件中，通过 `-i/--input-file` 参数读取。

```bash
# 1. 把分享链接粘贴到文件（每行一个，或同一行空格分隔多个）
#    文件内容示例 links.txt：
#    https://v.douyin.com/SlGTwuMq498/
#    https://v.douyin.com/LQVBJcukSyA/ https://v.douyin.com/LusIAXyGX-I/

# 2. 执行下载
NO_PROXY='*' no_proxy='*' uv run python -m douyindl -i links.txt -o ./downloads
```

特点：
- 文件支持多行，每行也可包含多个空格分隔的链接，脚本自动提取所有 URL
- **下载完成后自动清空文件内容**（保留文件本身），便于反复粘贴新链接复用
- 即使某些链接已下载过被跳过，文件仍会被清空（视为已处理）
- 可与 `url` 参数同时使用，两处链接合并去重后串行下载
- 文件不存在或内容为空时报错退出，不会静默跳过

### 9. 批量下载多个链接

`url` 参数支持空格分隔多个链接，脚本自动提取所有 URL 并串行下载。

```bash
# 两个单视频链接
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/LusIAXyGX-I/ https://v.douyin.com/w531WJ7dzEw/" \
  -o ./downloads

# 混合合集与单视频
NO_PROXY='*' no_proxy='*' uv run python -m douyindl \
  "https://v.douyin.com/SlGTwuMq498/ https://v.douyin.com/LQVBJcukSyA/" \
  -o ./downloads
```

每个链接会打印分隔线 `========== [1/2] <url> ==========`，串行执行：
- 合集链接内部按 54-60 秒随机间隔下载（`mix_download_interval` 基准值 60）
- 链接之间复用 `mix_download_interval`（基准值 60，实际 54-60 秒随机）间隔等待，防止风控
- 最后一个链接下载完不等待
- 单个链接失败不中断后续链接
- 全部链接下载完成后打印总结，含每个链接的状态和合计统计
- 进度数据库记录不受影响，断点续传/增量下载正常工作

总结输出示例：

```
========== 多链接下载总结 ==========
  [1/2] 单视频 7324XXXXX - 成功 1/1，跳过 0
  [2/2] 合集 翟东升看百年大变局 - 成功 19/19，跳过 0
  合计: 成功 20/20，跳过 0
```

## 输出结构

```
downloads/
├── 20260729_翟东升看百年大变局/        # 合集目录（日期_合集名）
│   ├── 001_翟东升_既得利益者肤浅.mp4
│   ├── 001_翟东升_既得利益者肤浅.jpg    # 封面（-m 时生成）
│   ├── 001_翟东升_既得利益者肤浅.txt    # 文案（-m 时生成）
│   ├── 001_翟东升_既得利益者肤浅.mp3    # 原声（-m 时生成）
│   ├── 001_翟东升_既得利益者肤浅.json   # 信息（-m 时生成）
│   ├── 002_翟东升_美元体系其实是特例.mp4
│   └── ...
└── 20260729_用最好的动画讲解HBM原理.mp4  # 单视频（日期_文案）

.douyindl/
└── progress.db                          # SQLite 进度数据库（自动创建）
```

## 控制台输出示例

```
[1/4] 解析分享链接: https://v.douyin.com/SlGTwuMq498/
      检测到合集链接, mix_id=7324XXXXX
      合集名称: 翟东升看百年大变局
[2/4] 共获取 19 个视频:
    1. [7324XXXXX] 翟东升_既得利益者肤浅
    2. [7324XXXXX] 翟东升_美元体系其实是特例
    ...
[3/4] 开始下载到 downloads/20260729_翟东升看百年大变局/ ...
  [1/19] 下载 001_翟东升_既得利益者肤浅.mp4
  [================--------------] 56%
      元数据: 时长 1004.7s, 分辨率 3840x2160, 大小 152.4MB, 2160p, H.265
      等待 57.3 秒后继续下载下一个...
  [2/19] 下载 002_翟东升_美元体系其实是特例.mp4
  ...

[4/4] 完成: 成功 19/19，跳过 0
      保存目录: downloads/20260729_翟东升看百年大变局/
      合集 7324XXXXX 累计已下载 19 个视频（含历史记录）
```

## Skill 集成

项目内置 Trae IDE Skill 定义：[.trae/skills/douyin-download/SKILL.md](../.trae/skills/douyin-download/SKILL.md)

### 触发方式

在 Trae IDE 中，当用户提供包含 `v.douyin.com` 链接的抖音分享文本并表达下载意图时，Skill 自动触发。

### Skill 工作流程

1. 从用户消息中提取抖音链接
2. 执行 `uv run python -m douyindl "<链接>" -o ./downloads`
3. 返回下载结果（成功/失败/跳过统计）

### 手动调用 Skill

也可通过 Trae IDE 的 Skill 系统显式调用 `douyin-download`。

## 下一步

- 常见问题排查：[07-faq.md](07-faq.md)
- 配置参数详解：[02-config.md](02-config.md)
