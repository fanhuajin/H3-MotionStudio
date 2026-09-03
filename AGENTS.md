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
- The completed state must display the final video and preserve access to the original video.
- Douyin downloads save to `D:\EV` by default (env `H3_DOUYIN_OUTPUT` overrides). The H3 backend starts the downloader service with `DOUYIN_PATH` set to the same directory so both sides agree.
- The Douyin downloader service (separate Python process) must be memory-lean for ComfyUI: start it only on explicit user actions (submitting a download / login window), never from page reads; stop it automatically after 60 s of download inactivity (`IDLE_STOP_SECONDS`). An on-disk job mirror (`data/douyin-jobs.json`) keeps finished jobs listed and playable while the service is offline.
- The upload-card singing-video preview must survive HEVC: picking a file stores it via `/api/uploads/preview` and serves an H.264 copy when the codec isn't browser-safe (`data/uploads/`, 24 h cleanup); the pipeline still receives the original file unchanged at submit.
- Every completed Douyin download must be converted in place to a browser-playable H.264/AAC MP4; after a successful atomic replacement, no HEVC original or duplicate source file is retained.
- Closing or refreshing the workspace must preserve the latest character image, singing video, action/camera instructions, and job state; reopening the page must restore the draft and resubscribe to persisted backend progress until the job ends.
- After each completed implementation batch, commit and push the changes to `origin/main`.
