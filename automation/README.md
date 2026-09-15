# H3 MotionStudio 自动化流水线

把「人工在 H3 网页点批量制作 + 人工去 ChatGPT 桌面端聊天传图生成 + 人工等发布成品目录」的
重复劳动自动化。全部 Python，直接调 H3 后端 API（不识别屏幕）+ Playwright/CDP 接管
ChatGPT 桌面端（不点坐标）。

## 两条路线

**首选（已接入后端）**：图片流程自动化直接做进了后端，点「准备任务」自动跑。
见 `backend/chatgpt_image.py` 与 `docs/自动化流水线设计方案.md` 第 9 节。
开关（Windows 用户级环境变量）：`H3_AUTO_CHATGPT_IMAGE=1`、`H3_AUTO_CHATGPT_COVER=1`。
所有图片操作共用 `_IMAGE_LOCK`，**严格一条一条完成，绝不并发**。

**备用（独立脚本）**：本目录的 `orch.py` CLI 可在不侵入后端的情况下手动触发各环节，
供调试/验证用。完整设计见 `docs/自动化流水线设计方案.md`。

## 模块

| 文件 | 作用 |
|---|---|
| `config.json` | 所有路径/开关配置 |
| `settings.py` | 读配置 + 环境变量覆盖 |
| `h3_client.py` | H3 后端 API：建批次/追加/轮询/确认/上传图 |
| `prompts.py` | 读桌面提示词 txt（4:3 / 9:16）|
| `frame_picker.py` | ffmpeg+OpenCV 抽帧挑「人物最完整」帧 |
| `codex_automation.py` | Playwright+CDP 接管 ChatGPT 桌面端聊天 |
| `cover_flow.py` | 封面生成（回同一聊天，先 B站4:3 后 抖音3:4）|
| `publish_watcher.py` | watchdog 监听发布成品目录 |
| `orch.py` | 主编排 CLI 入口 |

## 依赖

```powershell
# 使用项目已有 .venv
.venv\Scripts\python.exe -m pip install httpx playwright watchdog psutil
# opencv 用于抽帧挑帧（可选，缺省会降级取中间帧）
.venv\Scripts\python.exe -m pip install opencv-python
```

## 用法（独立脚本路线）

```powershell
# 1) 添加链接（唱歌/跳舞标注）；无批次则新建，有则追加到同一批次
.venv\Scripts\python.exe -m automation.orch add --mixed "唱歌: https://v.douyin.com/xxx" "跳舞: https://..."
.venv\Scripts\python.exe -m automation.orch add --singing "https://..." --dance "https://..."

# 2) 查看批次状态（待确认检测）
.venv\Scripts\python.exe -m automation.orch status

# 3) 确认所有「待确认且已有候选图」的条目出片
.venv\Scripts\python.exe -m automation.orch confirm

# 4) 桌面端 CDP 引导（会把桌面端退出再用调试端口重启；登录态在 profile 不丢）
.venv\Scripts\python.exe -m automation.orch cdp-boot

# 5) 对某个发布目录生成封面（先 B站4:3 后 抖音3:4，串行）
.venv\Scripts\python.exe -m automation.orch cover --folder "E:\AI_Exports\H3-MotionStudio\发布成品\004_xxx_123"

# 6) 监听发布成品目录
.venv\Scripts\python.exe -m automation.orch watch
```

## 关键约束

- **H3 操作走 API**，不是 pywinauto。`h3_client.py` 直调 `/api/batches/*`。
- **桌面端要开 CDP** 才能自动化：`cdp-boot` 会退出并带端口重启，需人工确认一次。
- **图片严格单链路**：所有图片操作（人物图 + 封面）共用全局锁，一条一条完成，绝不并发。
- **封面严格先 4:3 后 3:4**，不做成可并行。
- 多次提供的链接**合并追加到同一批次**（`orch add` 自动判断）。
