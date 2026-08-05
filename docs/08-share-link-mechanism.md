# 08 - 抖音分享链接机制与完整下载访问链路

> 本文回答两类问题：
> 1. **为什么同一视频多次分享会得到不同短链，却解析为同一 `aweme_id`？**（含 302 响应全字段逐条解析、时间戳换算为可读时间）
> 2. **本工具下载一个视频，网络层面和 Python 包层面到底经历了什么？**（`短链 → 302 → ? → 服务器 → 下载` 的真实链路，纠正「短链直连 CDN」的常见误解）
>
> 链接解析的「怎么做」见 [03-development.md](03-development.md) §1；本文聚焦「为什么」与「端到端网络/包链路」。

---

## 0. 一句话结论（先建立正确心智模型）

**短链 ≠ 视频本身。** 整个下载分两条独立的链路：

```
链路 A（仅拿 ID）：  短链  --302-->  iesdouyin 网页(内含 aweme_id)   ← 只为了提取 aweme_id
链路 B（拿字节）：   aweme_id  --API(aweme/detail)-->  JSON(CDN 地址列表)  --GET 流式-->  CDN  -->  文件
```

> ⚠️ **常见误解纠正**：不是「短链 302 直接跳到 CDN 然后下载」。302 这一步**只用于解析出 `aweme_id`**；真正的视频字节来自一条**独立**的「API → CDN → 流式 GET」链路。两条链路的目的地完全不同。

---

## 1. 现象回顾

批量下载 36 条链接时，第 33、34 条：

```
[33/36] https://v.douyin.com/PFg1MB_kChk/    → aweme_id=7653736004347612450
[34/36] https://v.douyin.com/GQP7lTFztb4/    → aweme_id=7653736004347612450
```

两个**完全不同**的短码，却解析为**同一 `aweme_id`**。第 33 条下载成功，第 34 条因「文件已存在，跳过」被去重。

---

## 2. 实证：两个短链的完整 302 响应（保留全部字段）

### 复现命令

```bash
# 必须禁用代理，否则本地代理会干扰对抖音的请求
NO_PROXY='*' no_proxy='*' curl -s -D - -o /dev/null --max-time 20 \
  -A "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1" \
  "https://v.douyin.com/PFg1MB_kChk/"
```

> 说明：抖音短链对 UA 敏感，移动端 UA 才能拿到标准 302；桌面 UA 可能返回网页而非跳转。本工具 `resolve_share_url` 用的是 `config.user_agent`（与 f2 内部一致的移动端 UA）。

### 原始响应头（两个短码，已完整保留）

```
===== PFg1MB_kChk =====
HTTP/2 302
server: Tengine
location: https://www.iesdouyin.com/share/video/7653736004347612450/?region=CN&mid=7653736188986608410&u_code=2e1j763lag8i&did=MS4wLjABAAAAPTKQ0_oweKAJ7Cwjhl5ykEYjDFNxakKxkb7ahNcR1oIFuAA90WLTH5FT6kFenrxc&iid=MS4wLjABAAAA3kfrSLrzjBW1iPIbleLl6jt6LP_Mxr9kC_jK7ljxAYY7MT3dm9hR2MjG2JxBevyy&with_sec_did=1&video_share_track_ver=&titleType=title&share_sign=DojKrx5_uZk1ASQna5YOZ6RWT2_lJ1ZleSotQC3s4nE-&share_version=350000&ts=1785892876&from_aid=2329&from_ssr=1&share_track_info=%7B%22link_description_type%22%3A%22%22%7D&utm_source=copy&utm_campaign=client_share&utm_medium=android&app=aweme&activity_info=%7B%22social_author_id%22%3A%222815214799821495%22%2C%22social_share_id%22%3A%222488904481510231_1785893757304%22%2C%22social_share_time%22%3A%221785893757%22%2C%22social_share_user_id%22%3A%222488904481510231%22%7D&share_extra_params=%7B%22schema_type%22%3A%221%22%7D

===== GQP7lTFztb4 =====
HTTP/2 302
server: Tengine
location: https://www.iesdouyin.com/share/video/7653736004347612450/?region=CN&mid=7653736188986608410&u_code=2e1j763lag8i&did=MS4wLjABAAAAPTKQ0_oweKAJ7Cwjhl5ykEYjDFNxakKxkb7ahNcR1oIFuAA90WLTH5FT6kFenrxc&iid=MS4wLjABAAAA3kfrSLrzjBW1iPIbleLl6jt6LP_Mxr9kC_jK7ljxAYY7MT3dm9hR2MjG2JxBevyy&with_sec_did=1&video_share_track_ver=&titleType=title&share_sign=DojKrx5_uZk1ASQna5YOZ6RWT2_lJ1ZleSotQC3s4nE-&share_version=350000&ts=1785892876&from_aid=2329&from_ssr=1&share_track_info=%7B%22link_description_type%22%3A%22%22%7D&utm_source=copy&utm_campaign=client_share&utm_medium=android&app=aweme&activity_info=%7B%22social_author_id%22%3A%222815214799821495%22%2C%22social_share_id%22%3A%222488904481510231_1785894296105%22%2C%22social_share_time%22%3A%221785894296%22%2C%22social_share_user_id%22%3A%222488904481510231%22%7D&share_extra_params=%7B%22schema_type%22%3A%221%22%7D
```

**肉眼可辨的异同：**

| 位置 | `PFg1MB_kChk` | `GQP7lTFztb4` | 是否相同 |
|------|---------------|---------------|----------|
| 路径 `/share/video/{id}/` | `7653736004347612450` | `7653736004347612450` | ✅ 同（即 aweme_id） |
| `social_share_id` 后缀 | `…_1785893757304` | `…_1785894296105` | ❌ 不同 |
| `social_share_time` | `1785893757` | `1785894296` | ❌ 不同（晚 9 分钟） |
| 其余参数（did/iid/share_sign/ts…） | 完全相同 | 完全相同 | ✅ 同 |

→ **两个短码指向同一视频；差异全部来自「分享事件」的追踪参数。**

---

## 3. 302 查询参数逐字段解析

下表对 `location` 中的每一对 `key=value` 给出含义与值解析。所有 Unix 时间戳已换算为**北京时间（UTC+8）**，方便肉眼对照。

| 参数 | 示例值（节选） | 含义 | 解析 / 备注 |
|------|----------------|------|-------------|
| 路径 `video/{id}` | `7653736004347612450` | **视频唯一 ID（aweme_id）** | 🔑 本工具**唯一使用**的字段；其余全部忽略 |
| `region` | `CN` | 地区 | 中国 |
| `mid` | `7653736188986608410` | 分享所在「混合流/动态」ID | 分享时的上下文流 ID |
| `u_code` | `2e1j763lag8i` | 分享者标识码 | 分享用户的短码 |
| `did` | `MS4wLjABAAAA…xR1oIFuAA90WLTH5FT6kFenrxc` | **设备 ID（device_id）** | 一长串 base64url 编码的抖音设备令牌（opaque），每次同设备分享相同 |
| `iid` | `MS4wLjABAAAA…jK7ljxAYY7MT3dm9hR2MjG2JxBevyy` | **安装 ID（install_id）** | 同理，设备上的安装令牌；与 `did` 同源 |
| `with_sec_did` | `1` | 是否携带加密设备 ID | 安全增强标记 |
| `video_share_track_ver` | （空） | 视频分享追踪版本 | 未启用 |
| `titleType` | `title` | 标题类型 | 分享卡片展示样式 |
| `share_sign` | `DojKrx5_uZk1…s4nE-` | **分享签名** | 服务端下发的签名，防止分享链接被伪造/篡改 |
| `share_version` | `350000` | 分享协议版本 | App 分享功能版本号 |
| `ts` | `1785892876` | **短码生成时间戳** | → **2026-08-05 09:21:16 (UTC+8)**，即这次「复制链接」发生的时刻 |
| `from_aid` | `2329` | 来源 App ID | `2329` 是抖音（Douyin）客户端的固定 aid |
| `from_ssr` | `1` | 是否服务端渲染 | Server-Side Render 标记 |
| `share_track_info` | `{"link_description_type":""}` | 分享文案追踪 | URL 编码的 JSON；此处类型为空 |
| `utm_source` | `copy` | 渠道来源 | 通过「复制链接」分享 |
| `utm_campaign` | `client_share` | 推广活动 | 客户端分享 |
| `utm_medium` | `android` | 渠道媒介 | 来自安卓端 |
| `app` | `aweme` | App 名 | `aweme` 是抖音在字节内部的项目代号 |
| `social_author_id` | `2815214799821495` | 作者 ID | 视频发布者的用户 ID |
| `social_share_id` | `2488904481510231_1785893757304` | **分享事件 ID** | = `分享者ID_分享时间戳`；两次分享的后缀时间戳不同 |
| `social_share_time` | `1785893757` / `1785894296` | **本次分享发生时间** | → **第1次 2026-08-05 09:35:57**；**第2次 2026-08-05 09:44:56（晚约 9 分钟）** |
| `social_share_user_id` | `2488904481510231` | 分享者用户 ID | 谁点了这个「分享」 |
| `share_extra_params` | `{"schema_type":"1"}` | 额外分享参数 | URL 编码 JSON，schema 类型 |

**核心要点**：除路径里的 `aweme_id` 外，全部参数都服务于抖音后台的**分享归因统计**（谁、何时、从哪个平台分享了这条视频）。它们**不影响下载哪个视频**，所以本工具解析时只提取 `aweme_id`，其余一概丢弃——这正是「不同短链 → 同一视频」却被正确去重的根本原因。

---

## 4. 完整访问链路（网络层 + Python 包）

下面按时间顺序拆解本工具下载单个视频时，**网络层面**真实发生的请求，以及**每个环节由哪个 Python 包负责**。

### 总览（端到端时序）

```
┌─────────┐   ①  GET v.douyin.com/code  (302)        ┌──────────────┐
│         │ ───────────────────────────────────────▶ │  Tengine 短链 │
│  Python │   ②  ← Location: iesdouyin/.../video/ID/  │  分发服务     │
│  进程   │                                          └──────────────┘
│(douyindl│   ③  POST www.douyin.com/aweme/v1/web/aweme/detail/
│ + f2 +  │        ?aweme_id=ID&a_bogus=签名  (200 JSON) ┌──────────┐
│  httpx) │ ───────────────────────────────────────▶ │ 抖音 API  │
│         │   ④  ← aweme_detail: video.play_addr.url_list │ 网关     │
│         │                                            └──────────┘
│         │   ⑤  GET url_list[0]  (CDN, 可再 302 到边缘节点, 流式 200)
│         │ ───────────────────────────────────────▶ ┌──────────┐
│         │   ⑥  ← bytes（aiter_bytes 逐块）           │ douyinvod│
│         │                                            │  CDN     │
│         │   ⑦  f.write(chunk) 累积写盘                 └──────────┘
└─────────┘
```

### Phase A — 链接解析（只拿 `aweme_id`，不碰视频字节）

- **包/代码**：`httpx.AsyncClient`（`resolve_share_url()`，`downloader.py:188`）
- **网络动作**：
  - `GET https://v.douyin.com/{code}/`，`follow_redirects=True`
  - 抖音 `Tengine`（基于 Nginx 自研的 Web 服务器，见响应头 `server: Tengine`）返回 **HTTP/2 302**
  - `Location` 指向 `https://www.iesdouyin.com/share/video/{aweme_id}/?…`（即上面的完整参数）
- **提取**：读最终 URL，用正则 `/video/(\d+)`（或 `/share/video/(\d+)`）提取 `aweme_id`。**到此阶段结束，没有任何视频数据流经本机。**

### Phase B — 获取视频信息（API + `a_bogus` 签名，核心门槛）

- **包/代码**：`f2` 的 `DouyinCrawler.fetch_one_video(aweme_id)` → 端点常量 `POST_DETAIL`（`f2/apps/douyin/api.py:55`）：
  ```
  POST_DETAIL = f"{DOUYIN_DOMAIN}/aweme/v1/web/aweme/detail/"
              → https://www.douyin.com/aweme/v1/web/aweme/detail/
  ```
- **请求要素**：
  - **参数**：`aweme_id`，外加 `aid`（Web 端固定 `6383`）、`device_id`、`channel` 等；**末尾追加 `a_bogus` 签名**：
    ```python
    # f2/apps/douyin/utils.py:667
    final_endpoint = f"{base_endpoint}{separator}{param_str}&a_bogus={ab_value[1]}"
    ```
  - **请求头**：
    - `User-Agent`：**必须与 f2 内部 UA 完全一致**，否则 `a_bogus` 校验失败（这是匿名访问最易踩的坑，见 [CLAUDE.md](CLAUDE.md) 关键坑点）。
    - `Referer: https://www.douyin.com/`
    - `Cookie: ttwid=…; msToken=…` —— 由 `build_cookie()` 生成的**匿名**凭证（`ttwid` 是访客 ID，`msToken` 用伪值；详见 [03-development.md](03-development.md) §2）。
  - **`a_bogus` 是什么**：抖音 Web 端反爬签名算法。f2 用 `Abogus`（utils 内实现）对「请求参数 + User-Agent」计算签名，缺签名或签名错误 → API 返回 400/403。**它才是匿名免登录访问的真正门槛**，而不是登录态。
- **响应**：JSON `aweme_detail`，内含：
  - `video.play_addr.url_list` / `video_play_addr`（无水印地址列表）—— 即下一阶段要下载的 **CDN 候选 URL**
  - 全部元数据：文案、作者、时长、分辨率、`statistics`（点赞/评论/分享/收藏）等

### Phase C — 下载视频字节（CDN 流式写入）

- **包/代码**：`httpx.AsyncClient.stream()`（`download_video()`，`downloader.py:474`）
- **选地址**：取 `video_play_addr[0]`（f2 返回的候选列表**第一项**，即无水印、质量最优的 CDN 地址）。
  - **实测 CDN 域名**：`v26-webf.douyinvod.com` / `v26-web.douyinvod.com`（抖音自建 CDN，根域 `douyinvod.com`）。
  - **回退端点**：列表中还有 `https://www.douyin.com/aweme/v1/play/?video_id=…&sign=…`（带 `sign` 签名，自身会 **302 重定向**到某个 CDN 边缘节点，因此 `follow_redirects=True` 必须有）。
- **网络动作**：
  ```python
  async with httpx.AsyncClient(follow_redirects=True, headers=..., proxy=None, timeout=60) as client:
      async with client.stream("GET", video_url) as resp:
          resp.raise_for_status()                 # 4xx/5xx 直接抛异常
          total = int(resp.headers.get("content-length", 0))
          with open(save_path, "wb") as f:
              async for chunk in resp.aiter_bytes(chunk_size=config.chunk_size):  # 默认 65536=64KB
                  f.write(chunk)                   # 写入内存缓冲并落盘
                  downloaded += len(chunk)
                  _print_progress(downloaded, total)
                  # 限速：若实际用时 < 期望用时(下载太快)，sleep 差值以降低速度
  ```
  - `timeout=60` 为**硬编码**（注意：不读 `config.timeout`，详见 [CLAUDE.md](CLAUDE.md) 坑点）。
  - `follow_redirects=True`：应对 `aweme/v1/play/` 的二次 302，以及 CDN 边缘节点调度重定向。
  - `aiter_bytes(chunk_size=65536)`：按 **64KB** 为一块从网络流读取，立即 `f.write` 写入文件。这就是你已知的「逐块写入」——在网络层面表现为**分块传输（Transfer-Encoding: chunked / 范围读）**，而非一次性把整文件拉进内存。
- **CDN 地址有时效**：实测 `play_addr` 中 `dy_q=1785902797`、`l=20260805120636…` 均为 **2026-08-05 12:06:36 前后签发**的带签名限时地址。过期后需重新走 Phase B 拉新地址——这也是 `--retry-failed` 必须重新 `fetch_one_video` 的原因。

### Python 包职责一览

| 包 | 角色 | 在本工具中的用途 |
|----|------|------------------|
| **`httpx`** | 异步 HTTP 客户端（支持 HTTP/2、流式、`follow_redirects`） | Phase A 短链解析、Phase B 的 API 调用（经 f2 内部）、Phase C 视频字节流式下载——**三层都靠它** |
| **`f2`** | 抖音 SDK | `DouyinCrawler`（发请求）、`PostDetail`（参数模型）、`PostDetailFilter`（字段过滤）、`TokenManager`（生成 `ttwid`/`msToken`）、`Abogus`（计算 `a_bogus` 签名）、`AwemeIdFetcher`（备用 ID 提取） |
| **`asyncio`** | 异步事件循环 | 驱动全部 `async` 流程；`config.max_tasks`（默认 1）控制单视频/合集内的并发度 |
| **`re`** | 正则 | 从最终 URL 提取 `aweme_id`（`_AWEME_ID_PATTERN` 等） |
| **`sqlite3`** | 进度持久化 | `ProgressDB` 以 `aweme_id` 为主键记录下载状态（去重依据） |
| **`pathlib` / `time` / `json`** | 标准库 | 路径处理 / 限速 sleep / 解析 API JSON |

---

## 5. 由此引申的认知

- **短链是「钥匙」不是内容**：`v.douyin.com/{code}` 是抖音链接缩短服务，每次分享在服务端 KV 表里 mint 一个随机、不透明的短码映射到目标内容；短码与 `aweme_id` 无数学关系，故两次分享码不同。
- **`aweme_id` 是内容唯一稳定主键**：跨短链/合集/作者主页都不变，抖音各子系统均以它关联数据。
- **视频字节来自独立链路**：`短链 → 302` 只解析 ID；真正下载是 `API(aweme/detail, 带 a_bogus) → CDN(url_list) → 流式 GET`。不要以为 302 直接到了 CDN。
- **CDN 地址限时**：`play_addr` 带签名且会过期，重试必须重新拉取。
- **合集同理**：合集有稳定主键 `mix_id`，多次分享生成不同短链但同一 `mix_id`。

---

## 6. 去重（为什么第 34 条被跳过）

三层同时生效，确保同一视频不重复下载：

1. **解析层**：`resolve_share_url` 只取 `aweme_id`，丢弃所有追踪参数 → 两个短码得到同一 ID。
2. **进度层**：`DouyinDownloader.run` 调用 `progress_db.is_success_downloaded(aweme_id)`，`downloaded_videos` 表以 `aweme_id` 为 **PRIMARY KEY**。第 33 条已记录 → 第 34 条跳过。
3. **文件层**：文件名强制带 `_<aweme_id>` 后缀（如 `20260805_五六百的山地车究竟怎样选_7653736004347612450.mp4`）。即便进度库判断失效，同名文件也会因 `save_path.exists()` 被跳过——双保险。

> 进度库已存 `url` 字段（见 [05-progress.md](05-progress.md)），可回溯每条记录对应的**原始分享链接**（即「当初是从哪个短码下来的」）。

---

## 7. 使用建议

- 批量 `urls` 里出现**重复短链、或不同短链指向同一视频 → 完全正常**，工具自动去重，无需手动清理。
- 想快速判断两个链接是否同一视频：看解析出的 `aweme_id` 是否一致（日志 `检测到单视频链接, aweme_id=…` 一行即为准）。
- 短链本身通常长期有效（映射在服务端）；但 `share_sign` 等追踪签名可能有时效。只要视频未被删除或设私密，302 一般仍能跳到正确内容页，不影响下载。
- 下载中途失败要重试：直接重跑同一条链接即可，`--retry-failed` 会重新拉取（因 CDN 地址可能已过期）并以 `aweme_id` 续传/去重。

---

## 相关文档

- 链接解析「怎么做」：[03-development.md](03-development.md) §1 链接解析
- 匿名认证 / `a_bogus` 背景：[03-development.md](03-development.md) §2–§3
- 进度持久化与 `aweme_id` 主键：[05-progress.md](05-progress.md)
- 文件命名（含 aweme_id 后缀）：[03-development.md](03-development.md) §6
- 运行约定（NO_PROXY、UA 一致性、硬编码 timeout）：[CLAUDE.md](CLAUDE.md) 关键坑点
