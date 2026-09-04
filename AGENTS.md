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
- Backend code changes only take effect after the local uvicorn service restarts and `npm run build` refreshes `dist`; while the user has a job running, do not restart the service or rebuild the frontend.
- After each completed implementation batch, commit and push the changes to `origin/main`.
