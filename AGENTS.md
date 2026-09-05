# Prototype Instructions

Run the local server yourself and open the preview in the browser available to this environment. Do not give the user server-start instructions when you can run it.

Before making substantial visual changes, use the Product Design plugin's `get-context` skill when the visual source is unclear or no longer matches the current goal. When the user gives durable prototype-specific design feedback, preferences, or decisions, record them in `AGENTS.md`.

When implementing from a selected generated mock, treat that image as the source of truth for layout, component anatomy, density, spacing, color, typography, visible content, and hierarchy.

Build app UI in `src/`. Keep `.openai/hosting.json`, `worker/index.js`, `scripts/prepare-sites-build.mjs`, and `tests/sites-worker.test.mjs` intact so the same local prototype can be handed to Sites. Before a Sites handoff, run `npm run build` and `npm run test:sites`; the build must leave `dist/client/index.html`, `dist/server/index.js`, and `dist/.openai/hosting.json`.

## Product decisions

- The selected visual source is `design/reference-ui.png`.
- The global shell and all route styling must follow `design/douzy-shell-reference.png`: fixed left navigation, deep indigo canvas, cyan active accents, low-contrast violet borders, restrained density, and generous empty space.
- The H3 generation workspace remains the `/` route; Douyin download is a separate `/douyin` route inside the same shell.
- This is a local desktop-style workspace, not a marketing site.
- The only editable generation inputs are one character reference image, one singing video, character action instructions, and camera instructions.
- Progress must reflect real ComfyUI nodes and RVC subprocess stages, including exact errors.
- The pipeline is strictly single-chain: ComfyUI generation and RealESRGAN finish first, ComfyUI then closes completely, RVC converts the voice, and the converted audio is muxed into the final MP4.
- The 1080P second pass must use the ComfyUI workflow `视频-成片输入-独立二采-RealESRGAN4x转1080P-8GB高清加强版.json` and must wait for every VHS meta-batch requeue to finish before ComfyUI is closed.
- The completed state must display the final video and preserve access to the original video.
- Douyin downloads save to `D:\EV` by default (env `H3_DOUYIN_OUTPUT` overrides). The H3 backend starts the downloader service with `DOUYIN_PATH` set to the same directory so both sides agree.
- The Douyin downloader service (separate Python process) must be memory-lean for ComfyUI: start it only on explicit user actions (submitting a download / login window), never from page reads; stop it automatically after 60 s of download inactivity (`IDLE_STOP_SECONDS`). An on-disk job mirror (`data/douyin-jobs.json`) keeps finished jobs listed and playable while the service is offline.
- The upload-card singing-video preview must survive HEVC: picking a file stores it via `/api/uploads/preview` and serves an H.264 copy when the codec isn't browser-safe (`data/uploads/`, 24 h cleanup); the pipeline still receives the original file unchanged at submit.
- Every completed Douyin download must be converted in place to a browser-playable H.264/AAC MP4; after a successful atomic replacement, no HEVC original or duplicate source file is retained.
- Closing or refreshing the workspace must preserve the latest character image, singing video, action/camera instructions, and job state; reopening the page must restore the draft and resubscribe to persisted backend progress until the job ends.
- The action-migration workspace is the `/migrate` route inside the same shell (sidebar third entry 动作迁移). Page order: canvas ratio first (default 9:16 竖版) → upload action video (required) + optional portrait reference (any aspect; fallback `singing_portrait_4x3_1440x1080.png`) → migration mode 动作迁移 (default) / 人物替换 → prompts (content/video person/image person) → toggles 去除字幕 (default off) and 1080P 高清加强 (default off).
- The migrate chain runs the workflow pair in one strict job: optional 视频-去字幕-ProPainter-固定底部 (its output is copied into ComfyUI input and becomes the drive video) → 视频-长视频替换-4x3加速版-ProPainter输入 (#563 drive, #30 reference, #353 false=动作迁移/true=人物替换, #545/#509/#510 prompts, #456 output) → optional RealESRGAN 成片 HD pass. Output keeps the source audio and frame rate; no RVC on this route; single-job mutual exclusion with the singing route.
- Canvas parameter groups live in `backend/settings.py` `CANVAS_PARAMS` (4:3 = 512×384, mask 430×58 at (256,344), HD 1440×1080; 9:16 = 512×896 per SCAIL2-Easy 512p rule, mask 430×135 at (256,803), HD 1080×1920). Tune numbers there, never in the JSONs.
- 歌曲生成路由同样先选画布比例（默认 4:3，可选 9:16 竖版，全链路按此输出）：参数组在 `backend/settings.py` `SINGING_CANVAS_PARAMS`（4:3 = 640×480、参考图缩放档 0.31MP；9:16 = 480×864、0.41MP），运行时替换唱歌工作流五个 H3 分段节点 15/29/400/420/440 的宽高与节点 269 的缩放档。分段构图提示词的比例文案由 ComfyUI 歌词节点 `H3AutoLyricsFromAudio5StyleSafeCamera` 的 `canvas_ratio` 输入控制（4:3 为节点默认、逐字节不变；9:16 改写为竖版 480×864 文案）；该补丁位于 `D:\Comfyui\ComfyUI\custom_nodes\h3_media_duration_router\__init__.py`（ComfyUI 环境，被其仓库 .gitignore 排除、不在本仓库版本控制内）——旧版节点缺少该输入时，9:16 唱歌任务会在提交 ComfyUI 前明确报错提示重启，4:3 不受影响。
- 歌曲生成运行中的「H3 分段 X/N」徽章（执行流程面板右上，样式与动作迁移分段徽章一致）：预估段数在任务创建时按源时长写入 `estimatedSegments`（`backend/pipeline.py` `estimate_singing_segments`：第 1 段 362 帧 @24fps ≈15.08s、每增一段多 340 帧，段间 22 帧续接，容量 362/702/1042/1382/1722，上限 5 段；边界常数在 `backend/settings.py` `H3_FPS`/`H3_CLIP_FRAMES`/`H3_CONTEXT_FRAMES`）；运行时按 CLIP 锚点节点推进 `currentSegment`（15/18→段1，29/32→段2，400/403→段3，420/423→段4，440/443→段5；H3 懒加载只跑所需前 N 段、节点只执行一次且按段顺序，段位只推进不回退）。
- Backend code changes only take effect after the local uvicorn service restarts and `npm run build` refreshes `dist`; while the user has a job running, do not restart the service or rebuild the frontend.
- After each completed implementation batch, commit and push the changes to `origin/main`.
- The final deliverable of an independent upscale job is stored on disk under the marked name `{job_id}_upscale_最终版.mp4` (the workflow's audio-muxed `*_00001-audio.mp4` is atomically renamed and the job state updated at job end); the same-prefix `*_00001.png` (first-frame preview) and the audio-less `*_00001.mp4` are intermediates. The upscale result panel and history picker always point at the marked final file only.

## Git rule（用户硬性规则：每次修改完成必须提交 GitHub）

- 每一次修改（不论大小：前端、后端、样式、文案、文档、配置、工作流参数）完成后，只要已验证通过，就必须立即 `git add` → `git commit` → `git push origin main`，并在同一次回复中告知用户提交哈希。
- 不允许在一个回合结束时留下已完成但未提交的改动；提交动作不得拖延到"批量攒齐"再执行。
