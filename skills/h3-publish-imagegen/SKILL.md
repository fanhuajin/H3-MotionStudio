---
name: h3-publish-imagegen
description: "Run the local H3 publish-asset image pipeline. Use when the user invokes this skill to signal they have a batch of publish folders needing images: it drives the desktop ChatGPT chat over CDP to create 人物图.png and then B站4x3.png and 抖音3x4.png for every 发布成品 subfolder, strictly one work at a time, skipping existing files, locking identity to the reference portrait and locking the aspect ratio. Never uses Codex quota. NOTE: keep this description ASCII-only; Codex ignores skills whose description contains non-ASCII characters."
---

# H3 发布素材出图（执行型 skill）

**调用本 skill = 用户有一条出图任务。**

执行方式是**运行本机已经跑通的脚本**（聊天模式 CDP，走 ChatGPT 订阅额度）。
不要用"自己手工调内置 `image_gen` 一张张出图"的方式代替它 —— 那正是重复劳动，
而且没有身份判据与断点续跑。

## 一条命令就是全部

```powershell
cd D:\project\H3-MotionStudio
.\.venv\Scripts\python.exe scripts\chat_imagegen.py --all
```

常见变形：

| 目的 | 命令 |
|---|---|
| 先看缺什么（不出图、不耗额度） | `.\.venv\Scripts\python.exe scripts\chat_imagegen.py --list` |
| 只补人物图 | `... --all --steps person` |
| 只补两张封面 | `... --all --steps bili,douyin` |
| 只做某一个作品 | `... --dir "009_跳舞_跟着节奏随性摇摆，松弛感拉满_7686568985073396197"` |
| 用户明确要求重做某张 | 追加 `--force`（**用户没让重做就绝不要加**） |
| 身份门槛调整（默认 60） | 追加 `--min-similarity 60`（用户：60 以上就可以） |
| 只要体检不重做 | 追加 `--max-repairs 0` |
| 跳过身份体检 | 追加 `--no-identity-check` |
| 关掉身份护栏（糊脸+身份块，仅对比测试用） | 追加 `--no-identity-guard` |
| 比例更宽容地重试 | 追加 `--max-ratio-repairs 2`（默认 1） |
| **按需模式**（用户 2026-09-19 的默认流程） | `.\scripts\run-chat-imagegen-need.ps1`（起 / `-Stop` 停 / `-Status` 看） |

## 按需模式：批量没得跑时，只补「那一条」

用户原话：「**当批量任务中没有可以运行的任务 全部都是需要人物图的时候再去跑**」+
「**出图也是只需要运行的时候的那条出图**」。所以默认入口是常驻小循环：

```powershell
cd D:\project\H3-MotionStudio
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run-chat-imagegen-need.ps1   # 默认每 300 秒判断一次
```

它每轮只做一件事：问后端 `GET /api/batches/latest` ——

- 批量自己还有事干（`pending` / `confirmed` / `running` / `revising`）→ **什么都不做**；
- **后端读不出来也什么都不做**（要照跑得显式加 `--need-assume-idle`）；
- 批量空档时，**只补批量里真实存在的那一条**还缺的步骤（按 `awemeId` / `outputs.folder`
  认发布目录，`deleted` / `skipped` 绝不碰；后端地址按 8111 → 15111 → 15222 → 18333 探活）。

两条**必须保留**的顺序约束：

1. **人物图没落盘 → 当前作品立即停止**，不许跳过人物图去做封面（原有规则）。
2. **人物图落盘后先等后端把发布文案重写完，再出封面**（`--copy-wait`，默认 180 秒）：
   `backend/publish_watcher` 每 5 秒扫一次发布目录，看到新的 `人物图.png` 就调上传接口，
   那个接口会让 `write_copy()` **按最终画面重写标题**；封面上的大字取自 `发布文案.txt`
   的标题，先出封面就会把**旧标题**画进图里（001~017 就是这么对不上的）。

## 脚本自己会做的事（不要重复实现、不要中途干预）

- **严格顺序**：`人物图.png` → `B站4x3.png` → `抖音3x4.png`，绝不并发；
  人物图没落盘 → **当前作品立即停止**，不许跳过人物图直接做封面
- **已有不重做**：产物 ≥50KB 就跳过；抖音**沿用 B站那一个封面对话**，不重传抽帧
- **身份锁图二（只针对人物图）**：图一（抽帧）先糊脸（视觉模型定位人脸后高斯模糊）+ 消息末尾
  追加身份优先级块 + 出图后做一次身份体检；**门槛 60 分**（用户 2026-09-19 规格：
  「只有人物图要像，60 以上就可以了，封面图不需要管」）→ 低于 60 才回一句「五官不对」重做
  （默认最多 1 次），并且**始终保留分数最高的那张**（重做更差就丢弃，不会覆盖掉更好的）
- **封面两步不做身份体检**，只校验画幅比例（B站 4:3 / 抖音 3:4）
- **比例锁死**：消息末尾写死比例硬要求；出图后比例不对**自动回一句让它重做**
- **额度**：只走**聊天模式**（ChatGPT 订阅），**完全不碰 Codex / Work 额度**
- **静默**：自动起独立实例（端口 9444、独立 profile），**不影响用户自己开着的窗口**，
  可最小化运行；不抢前台、不动鼠标

## 你要做的（agent）

1. 先跑 `--list`，把「缺什么」向用户讲清楚。
2. 按用户给的范围跑 `--all`（或指定 `--dir` / `--steps`）；**用后台方式跑**
   （单个作品约 3 分钟，24 个约 1 小时），不要卡着前台等。
3. 期间按需汇报进度（已完成到第几个、当前在做什么）。
4. 跑完汇总：每个作品的**绝对路径 + 像素尺寸 + 身份分 + 新生成 / 跳过 / 失败**。
5. 失败项必须说明**真实原因**（比例不对 / 身份判据不通过 / 发送未确认 / 缺素材），
   不要粉饰，也不要自作主张换模型或换方案。

## 硬约定（别改）

- 发布根：`E:\AI_Exports\H3-MotionStudio\发布成品\<三位编号>_<唱歌|跳舞>_<标题>_<作品号>\`
- 产物文件名**逐字一致**：`人物图.png`、`B站4x3.png`(1448×1086)、`抖音3x4.png`(1086×1448)
- 人物图比例：**唱歌 4:3；跳舞 9:16**
- 身份锚点：`E:\AI_Exports\H3-MotionStudio\发布成品\晚柠.png`（唯一人物身份来源）
- 规则文件：唱歌 `4比3图片.txt`；跳舞 `9比16图片.txt`
  —— **作为 .txt 文档附件上传，绝不把上万字贴进输入框**（会触发风控）
- 图一 = 本作品的 `抽帧.jpg`（场景/灯光/镜头/造型参考，**不提供脸**）
- 不删除、不覆盖用户已有成品（`最终成片.mp4`、`最终成片_字幕版.mp4` 一律不碰）
- 封面上的大字取自 `发布文案.txt` 的标题 → **封面只许在发布文案定稿之后出**。
  往发布目录里放/换 `人物图.png` 会让后端按新图重写标题，所以顺序永远是
  「人物图 → 等文案重写 → 封面」，绝不反过来

## 日志与排错

- 运行日志：`data\codex_imagegen_logs\`（另：每次运行的完整控制台输出在后台任务日志里）
- 糊脸缓存：`data\identity_guard\<作品名>\抽帧-noface.png`
- 身份体检（给所有已完成的作品出分数表）：
  `data\judge_existing.py`（临时工具，可让 agent 代为运行）
- 常见失败与处理：
  - **宽高比不合格** → 看 `ratio_line()` 的硬要求是否写进消息末尾；可提高 `--max-ratio-repairs`
  - **人物图身份分低于 60** → 看它列出的 differences 判断是不是真的换人了；
    脚本会自动回「五官不对」重做一次并保留更好的那张。想只体检不重做就加 `--max-repairs 0`
  - **发送未确认** → 页面可能还有上一轮未结束，脚本会自动重试；持续失败属 App 状态问题
  - **取到的是参考图而不是生成图** → 生成结果在页面上是 `data:image/png;base64` 内联图，
    参考图是 `blob:`；判据已按这个事实实现
