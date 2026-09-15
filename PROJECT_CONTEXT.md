# H3 MotionStudio 项目速查

> 这是一份给后续开发/维护使用的“快速理解项目”文档。产品硬性约束与原始设计决策以根目录 `AGENTS.md` 为准；本文只提炼最常用的架构、入口和修改落点。

## 1. 项目定位

H3 MotionStudio（H3 影动高清工作台）是一个运行在 Windows 本机上的桌面式深色网页工作台，把本地 ComfyUI 视频生成、RealESRGAN 二采放大、RVC 音色转换、歌词字幕和抖音下载整合到一个界面中。

核心特点：

- 所有任务在本机执行，不是云端营销站点。
- ComfyUI 与 RVC 严格单链路运行，避免 8GB 显存争用。
- 任务状态持久化，刷新/重新打开页面后可以恢复最近任务和进度。
- 页面只展示“最终成片”和“原版成片”；其它中间视频仍写入磁盘和状态，供链路或历史兼容使用。
- 当前默认 RVC 音色为 `kikiV1`（v2 / 40k / RMVPE）；所有音频一律用 kikiV1。歌曲生成链路的音色转换基于**源视频的原唱音轨**，不是 H3 生成的音频。

## 2. 运行结构

```text
浏览器
  └─ React + Vite（src/）
       └─ /api 代理或 FastAPI 静态服务（backend/app.py）
            ├─ SQLite 任务库（data/motionstudio.db）
            ├─ ComfyUI HTTP/WS（D:\Comfyui\ComfyUI）
            ├─ RVC 便携转换器（D:\Comfyui\RVC）
            ├─ 本地歌词脚本（scripts/lyrics_*.py）
            └─ 独立抖音下载服务（D:\project\douyin-downloader）
```

前端开发时由 Vite 提供页面，并把 `/api` 和 WebSocket 代理到 `127.0.0.1:8111`。生产/本地预览时，FastAPI 会直接挂载 `dist/client`，并为各个前端路由返回同一份 `index.html`。

## 3. 页面路由

| 路由 | 页面 | 默认行为 | 主要输入/输出 |
| --- | --- | --- | --- |
| `/` | 歌曲生成 | 4:3；二采 4× 开；RVC 开 | 一张人物图 + 一段演唱视频 + 动作/运镜提示词 → 原版/最终成片 |
| `/migrate` | 动作迁移 | 9:16；动作迁移；二采 4× 开 | 动作视频 + 可选人物图 + 三类提示词 → 保留源音频/帧率的成片 |
| `/upscale` | 独立二采放大 | 固定 4× RealESRGAN | 上传视频或选择最近任务成片 → 1080p 档最终成片 |
| `/rvc` | 独立音色转换 | `kikiV1` | 上传视频或选择最近原版/最终成片 → RVC 最终成片 |
| `/lyrics` | 歌词字幕 | 多语言；中文最终走 FunASR 强制对齐 | 上传/选择成片 + 网易云歌词编辑 → 剪映手书风格烧录成片 |
| `/douyin` | 抖音下载 | 下载器按需启动 | 抖音链接 → 本地 H.264/AAC MP4 |
| `/batch` | 批量制作 | 页面粘贴链接后逐条跑 | 抖音链接列表 → 每条一个发布成品文件夹 |
| `/portrait` | 人物定妆 | 当前从侧边栏隐藏并关闭前端路由 | API、组件和素材保留，方便后续恢复 |

所有页面共用 `App.tsx` 中的固定左侧导航、系统资源监控、任务队列入口和深靛色视觉壳。当前 `/migrate` 页面已实际打开验证，导航和任务恢复状态可见。

## 4. 七条任务链
### 4.1 歌曲生成 `/`

1. 接收一张人物参考图和一段带歌声视频，限制约 40 秒。
2. 根据画布比例动态修改唱歌工作流节点 `15/29/400/420/440` 的宽高，以及节点 `269` 的参考图缩放档。
3. ComfyUI 按 H3 分段节点生成并同步当前节点、采样进度和 `H3 分段 X/N`。
4. 防闪拼接，裁切到源视频时长，写入“原版成片”。
5. 如果 `use_upscale=true`，先执行 RealESRGAN 4× 二采并收 1080p 档；此步骤必须在关闭 ComfyUI 和 RVC 之前完成。
6. 如果 `use_rvc=true`，彻底关闭 ComfyUI，运行 Demucs + RVC，再替换音频并标记 `{job_id}_最终版.mp4`。
7. 关闭 RVC 时直接把可用成片收为最终输出，保留原声。

H3 分段估算常量在 `backend/settings.py`：24fps、首段 362 帧、后续每段增加 340 帧、最多 5 段。运行中的段位依据实际 CLIP 锚点推进，不能用前端假进度代替。

### 4.2 动作迁移 `/migrate`

1. 先选 9:16 或 4:3，再上传动作视频；人物参考图可选，缺省使用 ComfyUI input 中的 `singing_portrait_4x3_1440x1080.png`。
2. 可选先做去字幕：自动检测持续字幕条，按比例映射遮罩；长视频按显存保护阈值分批运行 ProPainter，重叠裁剪后按帧拼回并回灌原音频。
3. 执行动作迁移/人物替换工作流：节点 `563` 驱动视频、`30` 参考图、`353` 模式开关、`545/509/510` 三类提示词、`456` 输出前缀。
4. 输出保留源视频音频和帧率。
5. 默认执行二采 4×；最终结果由 `mark_final_version()` 标记。此路由不经过 RVC。

去字幕分批参数只改 `CANVAS_PARAMS`，不要改工作流 JSON：9:16 默认每批 300 帧，4:3 默认每批 900 帧，重叠 24 帧。批内进度通过 `cleanBatch`、`cleanBatches` 和 `cleanBatchEstSecs` 投影。

### 4.3 独立二采 `/upscale`

- 输入可以是本地上传，也可以是最近任务的“原版成片/最终成片”。
- 放大倍数固定为 4×，模型固定 `RealESRGAN_x4plus.pth`；页面和 `POST /api/jobs/upscale` 都不接受新的倍数字段。
- 使用工作流：`视频-成片输入-独立二采-RealESRGAN4x转1080P-8GB高清加强版.json`。
- VHS meta-batch 按每批 8 帧推进 `currentSegment/estimatedSegments`，所有批次完成后才关闭 ComfyUI。
- 结果文件为 `{job_id}_upscale_最终版.mp4`；同前缀 PNG 和无音频 MP4 是中间文件。

### 4.4 独立 RVC `/rvc`

- 输入可以是本地视频，或最近任务的原版/最终成片；`source_key=rvc` 的任务不列入可选来源，防止自循环。
- 先 `resources.stop_comfy()`，再跑 `D:\Comfyui\RVC\convert_video_to_my_voice.py`。
- 里程碑为：关闭 ComfyUI → Demucs 分离人声/伴奏 → 加载并转换 `kikiV1` → mux 音频。
- 无音轨时必须在开始转换前明确失败；成功后标记 `{job_id}_最终版.mp4`。

### 4.5 歌词字幕 `/lyrics`

1. 上传视频或选择最近成片。
2. 通过 `/api/lyrics/search`、`/api/lyrics/lyric` 查询网易云歌词，自动识别原语种与中文翻译，用户可编辑后确认。
3. 任务只走本地歌词链，不经过 ComfyUI/RVC：读取 → Demucs 人声分离 → faster-whisper 识别实际唱到的片段 → 对齐 → 字幕烧录。
4. 中文最终时间轴必须用 `scripts/lyrics_force_align.py` 的 FunASR `fa-zh` 强制对齐；不能退回歌词库时间、等距铺字幕或手写插值。
5. 输出烧录 MP4；剪映精修用 SRT 放在 `data/lyrics/{job_id}/`，不放在成片目录旁，避免播放器自动叠加两份字幕。

### 4.6 抖音下载 `/douyin`

- 只有用户提交下载或打开登录窗口时才启动独立下载服务；读取状态不会拉起服务。
- 服务空闲 60 秒自动停止；`data/douyin-jobs.json` 镜像保证服务离线时已完成任务仍可见。
- 默认落盘 `D:\EV`，环境变量 `H3_DOUYIN_OUTPUT` 可覆盖；后端会以同一路径设置子进程 `DOUYIN_PATH`。
- 下载成功后原地转换为浏览器可播放的 H.264/AAC MP4，并原子替换，不保留 HEVC 原文件或重复源文件。

### 4.7 批量制作 `/batch`

页面分别向「歌曲视频」「跳舞视频」两个多行输入区粘贴抖音链接（表格导入/导出只是可选的批次备份方式，不是启动前提），批次严格逐条串行，复用全局 `pipeline_lock`。

每个条目的 **4 个里程碑**（下载 / 备料 / 审核 / 出片，2026-09-13 起定型）：

1. **download**：抖音下载 → 浏览器兼容化 → 从 `D:\EV\download_manifest.jsonl` 取原作品 desc/tags；**本地已有同作品就直接复用**（`cached_download_path()`，不提交下载任务）；结束后立刻 `douyin_service.stop()` 给 ComfyUI 让内存。
2. **prepare**：本地 ffmpeg 抽 6 帧联系表 + 抽一帧全分辨率「场景帧」（`scene-frame.jpg`）→ 直连文本模型一次调用拿到造型来源判断与标题/简介/标签，以及唱歌的动作/运镜时间轴（跳舞则是迁移提示词）→ **生成候选人物图**。模型失败退回源作品文案、出图失败退回源视频取帧，只标 warning 不阻断整批。
3. **review**：停在审核点（只做「看图 + 改比例/去字幕/迁移模式 + 换图 + 确认」，无教学式提示）。
4. **video**：唱歌提交 `POST /api/jobs`（按条目比例、RVC 开、二采开）；跳舞提交 `POST /api/jobs/migrate`（按条目比例、动作迁移/人物替换、按需去字幕、二采开）。出片在后台跑，runner 继续给后面的条目备料。

交付（`_deliver`）：`E:\AI_Exports\H3-MotionStudio\发布成品\{编号}_{标题}_{作品号}\` 内的
`最终成片.mp4` + `人物图.png` + `发布文案.txt`；封面由 ChatGPT 生成（见下）。

#### 图片流程（候选人物图 + 封面）——2026-09-15 固化

**这是本路由唯一需要「外部程序」的环节**，其余（下载/备料/文案/确认/出片）都是本地自动。

- **执行方式**：`backend/chatgpt_cdp.py` 用 **CDP 静默驱动 ChatGPT 桌面端**
  （专用 profile `~/.codex-automation/profile` + 端口 9444；登录态来自 `~/.codex/auth.json`，
  所以专用 profile 自动已登录）。**不抢前台、不动鼠标键盘**，窗口可最小化。
- **提示词**：按类型取**用户桌面文件** —— 唱歌 `C:\Users\admin\Desktop\4比3图片.txt`、
  跳舞 `C:\Users\admin\Desktop\9比16图片.txt`（**不用**项目自动拼的 `出图提示词.txt`）。
- **两张输入图**：图一 = 该条 `scene-frame.jpg`；图二 = `E:\AI_Assets\PortraitIdentity\本人固定参考.png`。
- **取图**：生成图在 DOM 里是 blob，不落盘；`fetch(blob:)` 被 CSP 拦，用 **canvas 导出**。
  落 `E:\AI_Exports\H3-MotionStudio\ChatGPT生成图\`，再 `POST /api/batches/{b}/items/{i}/image` 上传回条目。
- **封面**：`人物图.png` + `发布文案.txt` → **同一对话内串行**生成
  `封面_B站4x3.png`（4:3）→ `封面_抖音3x4.png`（3:4）；只有第一条开新对话。
- **单链路**：`backend/chatgpt_image.py` 的 `_IMAGE_LOCK` 是独立于 `pipeline_lock` 的第二把锁，
  所有图片操作共用它，严格一条一条。
- **开关**：`H3_AUTO_CHATGPT_IMAGE=1` / `H3_AUTO_CHATGPT_COVER=1`（默认关闭）；
  页面上的「AI 换一张 / 写意见再换」走 `POST /api/batches/{b}/items/{i}/regen-image`。

> 详细的踩坑清单（DPI / 长提示词发不出 / 附件堆叠 / 取图取错 / 窗口错乱…）
> 见 `docs/自动化流程说明.md`，改这条链路前必读。

**仍然保留的降级路径**：`H3_BATCH_IMAGE_PROVIDER` 决定「项目内部」的出图来源 ——
`manual`（默认，只备料等你上传）、`api`（用户自备国内中转站）、`local`（本地 Krea2 双图编辑，
8GB 卡上单图 74.6 秒且画质不达标，仅备用）、`frame`（直接用源视频取帧）。
官方 API 的 `gpt-image-*` 全报 `credit_balance_exhausted`，不能走官方图片接口。
造型提示词收在 `backend/prompts/`（`H3_BATCH_*_PROMPT` 可覆盖）。

批次状态存在 SQLite `batches` 表，页面链接草稿存 localStorage；`POST /api/batches/{id}/cancel` 用于整批取消。跳过/删除单条通过 `cancel_item_work` 取消正在跑的预审子任务。

## 5. 任务状态与实时进度

### 状态来源

- `backend/store.py`：SQLite `jobs` 表，整份任务状态 JSON 存在 `state_json`。
- `JobStore.update/mutate/set_milestone`：更新数据库后向 WebSocket 订阅者发布最新状态。
- 前端打开任务时先取 `GET /api/jobs/{job_id}`，再连接 `/api/jobs/{job_id}/ws`；刷新页面会从 `GET /api/jobs/latest?kind=...` 恢复。
- 后端启动时若发现未结束任务，会标记为 `interrupted`，不会假装继续运行。

### 常用 `JobState` 字段

| 字段 | 含义 |
| --- | --- |
| `kind` | `singing` / `migrate` / `upscale` / `rvc` / `lyrics` |
| `status` | `queued` / `running` / `completed` / `failed` / `cancelled` / `interrupted` |
| `stage` | 当前阶段，如 `upload`、`h3`、`migrate`、`upscale`、`handoff`、`lyrics`、`completed` |
| `milestones` | 阶段列表；单项状态为 pending/running/completed/skipped/error |
| `currentNodeId/title` | 当前 ComfyUI 节点和可读标题 |
| `progress/value/max` | 当前采样或阶段进度 |
| `currentSegment/estimatedSegments` | H3、迁移或独立放大的分段状态 |
| `cleanBatch/cleanBatches` | 去字幕分批状态，仅 cleaning 阶段使用 |
| `originalOutput` | 原版/迁移草稿等链路的原始可用成片 |
| `enhancedOutput` | 二采后的中间高清成片 |
| `finalOutput` | 标记后的最终 MP4 |
| `originalReady/finalReady` | UI 是否显示对应结果入口 |
| `errorSummary/errorDetail` | 面向用户的摘要和完整错误详情 |

### 单任务互斥

`backend/pipeline.py` 的全局 `pipeline_lock` 保证同一后端进程只有一条生成/迁移/放大/RVC/歌词任务链运行。ComfyUI 与 RVC 的资源切换必须通过 `ResourceManager` 完成，不能在新链路里直接并行启动。

## 6. 后端 API 速查

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/config` | ComfyUI、依赖、默认提示词、最大时长和运行模式 |
| GET | `/api/system/stats` | CPU、内存、磁盘、网络、GPU/显存/温度 |
| GET | `/api/jobs/latest?kind=...` | 恢复某类最近任务 |
| GET | `/api/jobs/recent` | 最近任务及可公开的原版/最终成片 |
| GET | `/api/jobs/{id}` | 单任务完整状态 |
| WS | `/api/jobs/{id}/ws` | 任务状态实时推送 |
| POST | `/api/jobs` | 创建歌曲生成任务 |
| POST | `/api/jobs/migrate` | 创建动作迁移任务 |
| POST | `/api/jobs/upscale` | 创建固定 4× 独立放大任务 |
| POST | `/api/jobs/rvc` | 创建独立音色转换任务 |
| POST | `/api/jobs/lyrics` | 创建歌词字幕任务 |
| POST | `/api/jobs/{id}/retry-voice` | 歌曲任务重试 RVC |
| POST | `/api/jobs/{id}/cancel` | 取消任务 |
| GET/POST | `/api/comfy/queue` | 查看/清理 ComfyUI 队列 |
| GET | `/api/jobs/{id}/media/{kind}` | 读取 `final` 或 `original` 成片 |
| GET | `/api/jobs/{id}/input/{kind}` | 读取原始输入或兼容历史来源 |
| POST/GET | `/api/uploads/preview...` | 上传卡片预览及 HEVC 转码状态 |
| GET | `/api/lyrics/search`、`/api/lyrics/lyric` | 网易云搜索与歌词详情 |
| GET/POST | `/api/douyin/...` | 下载器状态、下载、登录、任务和媒体 |
| GET/POST | `/api/portrait/...` | 人物定妆保留 API，不代表当前导航开放 |

## 7. 重要文件地图

### 前端

- `src/main.tsx`：React 入口。
- `src/App.tsx`：应用壳、`/` 歌曲生成路由、任务状态恢复、结果区、侧边栏。
- `src/MigrateRoute.tsx`：动作迁移页面与草稿恢复。
- `src/UpScaleRoute.tsx`：独立放大页面。
- `src/RvcRoute.tsx`：独立音色转换页面。
- `src/LyricRoute.tsx`：歌词搜索、编辑、任务页面。
- `src/DouyinRoute.tsx`：抖音下载与登录状态页面。
- `src/QueuePanel.tsx`：ComfyUI 队列和取消/清理操作。
- `src/SystemMonitor.tsx`：系统资源轮询显示。
- `src/TaskTabStatus.tsx`：浏览器标签页标题/图标随任务状态变化。
- `src/types.ts`：前端共享的 `JobState`、`Milestone`、`AppConfig` 类型。
- `src/styles.css`：全局壳、路由布局和深靛/青色视觉规范。

### 后端

- `backend/app.py`：FastAPI 路由、文件上传、任务创建、媒体响应、生命周期和抖音 housekeeping。
- `backend/pipeline.py`：资源管理、ComfyUI 提交/监听、各条 pipeline、取消、分批、输出标记。
- `backend/workflows.py`：工作流 JSON 读取、节点参数替换、图转 API prompt、运行时低显存补丁。
- `backend/settings.py`：所有外部路径、模型、画布参数、帧数、分批阈值和环境变量入口。
- `backend/store.py`：SQLite 任务库、里程碑模板、WebSocket 订阅发布。
- `backend/lyrics_worker.py`：网易云取词、识别结果筛选、歌词任务编排。
- `backend/subtitle_detect.py`：持续字幕条检测。
- `backend/input_preview.py`：上传视频预览保存、HEVC/H.264 预览转换和过期清理。
- `backend/douyin_service.py`：独立抖音服务生命周期与 HTTP 客户端。
- `backend/douyin_mirror.py`：抖音任务的磁盘镜像。
- `backend/douyin_preview.py`：下载视频的浏览器兼容化与原子替换。
- `backend/portrait_studio.py`：人物定妆 API 后端实现，当前保留但前端隐藏。
- `backend/chatgpt_cdp.py`：**静默驱动 ChatGPT 桌面端**（专用 profile + CDP 9444 + canvas 取 blob 图）。图片流程的执行层。
- `backend/chatgpt_image.py`：图片流程编排（按类型取桌面提示词、上传成图、封面同对话串行）；持有 `_IMAGE_LOCK`。
- `backend/chatgpt_desktop.py`：坐标点击版驱动（备用，会抢前台），含 `force_foreground` / `capture_window` 等工具。
- `backend/batch_worker.py` / `backend/batch_store.py` / `backend/batch_ai.py` / `backend/batch_image.py` / `backend/batch_portrait.py`：批量制作的 runner、状态库、文案、内部出图、定妆。
- `docs/自动化流程说明.md`：批量图片流程的**权威说明 + 踩坑清单**，改这条链路前必读。

### 外部资源与脚本

- `D:\Comfyui\ComfyUI\user\default\workflows\video\`：唱歌、迁移、去字幕、独立放大工作流 JSON。
- `D:\Comfyui\RVC\convert_video_to_my_voice.py`：便携音色转换器。
- `scripts/lyrics_stage.py`：Demucs + faster-whisper 识别实际演唱片段。
- `scripts/lyrics_force_align.py`：中文 FunASR `fa-zh` 逐字强制对齐。
- `design/reference-ui.png`、`design/douzy-shell-reference.png`：当前视觉参考源。
- `worker/index.js`、`scripts/prepare-sites-build.mjs`、`.openai/hosting.json`：Sites 构建/托管边界，保持不动。

## 8. 关键配置与环境变量

默认根路径见 `backend/settings.py`，常用覆盖项：

| 环境变量 | 默认值/作用 |
| --- | --- |
| `H3_COMFY_HOME` | `D:\Comfyui` |
| `H3_COMFY_URL` | `http://127.0.0.1:8188` |
| `H3_DOUYIN_DOWNLOADER_ROOT` | `D:\project\douyin-downloader` |
| `H3_DOUYIN_DOWNLOADER_URL` | `http://127.0.0.1:9000` |
| `H3_DOUYIN_OUTPUT` | `D:\EV` |
| `H3_WHISPER_MODEL` | `D:\tmp\fw-small` |
| `H3_LYRICS_ALIGN_PY` | `D:\Comfyui\FunASR\.venv\Scripts\python.exe` |
| `H3_LYRICS_ALIGN_MODEL` | `D:\tmp\funasr-fa-zh` |
| `H3_SUBTITLE_DETECT=0` | 关闭自动字幕定位，回退固定底部遮罩 |
| `H3_FFMPEG_BIN_DIR` | 指定 ffmpeg 依赖 DLL 目录 |
| `H3_BATCH_IMAGE_PROVIDER` | 候选人物图来源：`auto`（默认）/ `api` / `local` / `frame` |
| `H3_BATCH_IMAGE_BASE_URL` | 中转站图片接口地址（配了它才认为出图可用，不走 `OPENAI_BASE_URL`）|
| `H3_BATCH_IMAGE_API_KEY` | 中转站 key；缺省回落到 `OPENAI_API_KEY` |
| `H3_BATCH_IMAGE_MODEL` | 中转站出图模型，默认 `gpt-image-2.5-sunburst` |
| `H3_BATCH_IMAGE_MODE` | 请求方案：`auto`（默认，多图 `image[]`→`image`→拼合）/ `multi` / `composite` |
| `H3_BATCH_IMAGE_FIELD` | 钉死 multipart 图片字段名（中转站只认单数 `image` 时用）|
| `H3_BATCH_IMAGE_QUALITY` / `H3_BATCH_IMAGE_FIDELITY` | 默认 `high`；后者对应编辑接口的 `input_fidelity` |
| `H3_BATCH_TEXT_BASE_URL` / `H3_BATCH_TEXT_API_KEY` | 预审文本分析端点与凭据（默认官方 `https://api.openai.com/v1`）|
| `H3_BATCH_LUNA_MODEL` | 预审文本模型，默认 `gpt-5.6-luna` |
| `H3_AUTO_CHATGPT_IMAGE=1` | 打开「备料后自动用 ChatGPT 生成候选人物图」（默认关闭）|
| `H3_AUTO_CHATGPT_COVER=1` | 打开「人物图+文案落盘后自动生成两张封面」（默认关闭）|
| `H3_CHATGPT_PROMPT_43` | 唱歌出图提示词，默认桌面 `4比3图片.txt` |
| `H3_CHATGPT_PROMPT_916` | 跳舞出图提示词，默认桌面 `9比16图片.txt` |
| `H3_CHATGPT_CDP_PORT` | 专用实例调试端口，默认 **9444**（**不要用 9222**，会被主实例占用）|
| `H3_CHATGPT_AUTOMATION_PROFILE` | 专用 profile，默认 `%USERPROFILE%\.codex-automation\profile`（自动已登录）|
| `H3_CHATGPT_IMAGE_DIR` | 生成图落盘目录，默认 `E:\AI_Exports\H3-MotionStudio\ChatGPT生成图` |

人物定妆 API 的身份图和输出目录另见 `backend/portrait_studio.py`：默认身份图在 `E:\AI_Assets\PortraitIdentity\本人固定参考.png`，输出在 `E:\AI_Exports\PortraitStudio\4x3` 或 `9x16`。OpenAI 凭据只能从进程环境读取。

## 9. 开发、构建与验证

### 启动

最完整的本地启动方式是双击根目录的 `启动H3影动高清工作台.bat`，它会准备依赖、构建前端、启动 `127.0.0.1:8111` 并打开浏览器。已运行时会复用服务。

常用开发命令：

```powershell
npm install
npm run dev
npm run dev:backend
npm run typecheck
npm run build
npm run test:backend
npm run test:sites
```

`npm run build` 应留下：

- `dist/client/index.html`
- `dist/server/index.js`
- `dist/.openai/hosting.json`

### 修改后的验证顺序

1. 前端改动：`npm run typecheck`，需要交付时再 `npm run build`。
2. 后端/工作流改动：`npm run test:backend`，必要时先重启 uvicorn。
3. Sites 边界相关改动：`npm run build` + `npm run test:sites`。
4. 用浏览器打开实际路由，确认页面、恢复状态、结果入口和错误提示。

后端代码只有在 uvicorn 重启后生效；前端产物只有在重新构建后才更新。用户有任务运行时不要重启服务或重建前端。

## 10. 修改时最容易踩的约束

- 新的 UI 放在 `src/`；不要破坏 `.openai/hosting.json`、`worker/index.js`、`scripts/prepare-sites-build.mjs`、`tests/sites-worker.test.mjs`。
- 画布尺寸、遮罩、分批阈值改 `backend/settings.py`，不要直接改工作流 JSON。
- 工作流节点替换和图转 API prompt 集中放在 `backend/workflows.py`；ComfyUI 外部补丁不属于本仓库。
- 不要让 ComfyUI 和 RVC 并行；复用 `pipeline_lock` 和 `ResourceManager`。
- 不能用等距假进度冒充 H3/迁移/歌词实际进度；状态要来自真实节点、批次或子进程阶段。
- 结果区只暴露 `final` 与 `original`；中间字段可写入状态，但不要重新加入 `_job_media_entries()`。
- 不要把 OpenAI、RVC 或下载器凭据写进前端、数据库或仓库。
- HEVC 预览与下载兼容化必须保留原始提交文件不变（上传卡片预览）或按下载规则原地原子替换（抖音结果）。
- **改「ChatGPT 出图」这条链路前必读 `docs/自动化流程说明.md` 的踩坑清单**，其中每条都是实测踩出来的：
  进程必须 `SetProcessDpiAwareness(2)`（否则 125% 缩放下坐标整体错位）；
  长提示词会把输入框撑出视口、聚焦要点**靠近底部**而不是中心；
  发送后要**校验输入框已清空**；每次生成前要**清空输入框含附件**（移除按钮 `pointer-events-none`，只能 JS 触发）；
  取图基线要在**贴图之后**记；新对话后要**校验页面为 0 张图**（blob URL 会随重绘变化，残留旧图会被当成新图）；
  用 JS 操作 DOM 而不是 Playwright 的 `click()`（后者会滚动页面，用户看得到「滑来滑去」）。
- **专用 ChatGPT 实例与用户自己的窗口是两回事**：走专用 profile + 端口 9444，**不要用 9222**
  （主实例命令行可能带着它，端口不生效）；也不要改动用户正在用的那个窗口。
- **长任务要用 `启动H3影动高清工作台.bat` 起后端**：从临时会话 `Start-Process` 起的后端可能随会话结束被杀，
  会把生成中的图片/视频任务打断。
- 每次已验证的修改都必须立即 `git add` → `git commit` → `git push origin main`，不要把已完成改动留到回合末尾。

## 11. 当前工作区注意事项

当前 Git 分支为 `main`，与 `origin/main` 同步。工作区里存在若干未跟踪的视频分析临时目录和 `qrcode.png`；它们属于已有工作区产物，本次文档没有触碰，也不应在无明确要求时清理或纳入提交。

