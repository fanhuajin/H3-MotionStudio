# H3 MotionStudio 项目规则

## 使用与维护

- 本文件记录当前有效的项目约束，作为维护时的默认行为，不禁止用户明确授权的产品变更；通用沟通、研究复用、改动范围、编码及 Git 授权沿用公用规则，不在此重复。
- 修改前先读本文件，再按任务阅读 [当前业务约定](docs/project-rules-current.md) 的相关章节。实现入口可查 `PROJECT_CONTEXT.md`，但其中架构快照和旧流程须与当前代码、配置核对，不把旧默认值当作现行要求。
- [历史存档](docs/project-rules-history-2026-10-24.md) 仅用于追溯，不作为默认任务指令。尚未提炼的历史细节应结合后续决定、相关文档及实现核查，不因归档而认定所有旧约束失效，也不据旧记录恢复已取消功能。根文件维护关键边界，业务细节维护在当前业务约定，专题文档补充实现说明；发生矛盾时先查用户最新决定，无法确定再确认，不能机械认为代码或文件日期较新就代表业务要求已改变。
- 用户给出持久项目决定时，更新对应现行条款并删除已被覆盖的表述；实施过程、单条作品操作和测试结果放进历史或专题文档，不继续向本文件堆叠日志。业务变化同时检查直接相关文档，避免出现两份相反的“当前规则”。

## 项目与定位入口

- 本项目是 Windows 本机视频工作台：React + TypeScript + Vite 前端（`src/`），FastAPI 后端（`backend/`），SQLite 持久化（`data/`）；本地 ComfyUI、RVC、ffmpeg 和剪映分别承担生成、换声与字幕处理。
- 默认首页为批量制作。主要维护入口：`src/BatchRoute.tsx` / `backend/batch_worker.py`、`backend/batch_store.py`；图片与封面：`src/ImageWorkbenchRoute.tsx` / `backend/image_workbench.py`、`backend/batch_image.py`、`backend/batch_ai.py`、`backend/cover_review.py`；资源与任务：`backend/pipeline.py`、`backend/settings.py`、`backend/store.py`；字幕：`src/SubtitleRoute.tsx` / `backend/subtitle_step.py`。
- 路径、端口、模型、渠道及环境变量优先核对 `backend/settings.py`、实际配置和相关实现，不复制 README 或旧记录中的过时值，不读取或输出无关凭据。

## 不可破坏的运行与数据约束

- 维护或诊断时不擅自重启、停止或接管正在运行的后端、ComfyUI、RVC 或生成任务；这不限制用户已启动任务中由程序正常执行的资源切换与收尾。后端代码修改后由用户自行重启；用户明确要求启动或重启时遵循下方运行约定，前端或文档变更不机械要求后端重启。
- 视频生成、迁移、放大和换声沿用全局 `pipeline_lock` 与资源管理器，避免 GPU 资源争用。中转站封面等待可在当前条目后台运行；字幕是否占锁以其独立链路实现为准，不把所有任务机械塞进同一把锁。
- 不用真实付费生成、真实作品目录、用户数据库或剪映草稿做常规测试；测试必须隔离数据目录、发布根、任务库和外部调用。未经用户明确要求，不重做已有图片、封面或成片，不恢复暂停的生成和修手试验。
- 已付费或结果未知的请求必须按冻结输入和任务检查点恢复。超时、查询或下载失败不等于生成终态失败，不得自动重新提交计费请求；已有结果只补保存。已确认终态失败的封面仅按当前业务约定允许一次备用请求，不能把这一例外推广为未知请求重发。
- 作品文件与任务记录分开保护：删除条目或清理批次历史不等于授权删除发布文件。诊断和只读检查不得调用会改名、搬移、创建作品目录或修改业务状态的函数；必要的诊断输出使用隔离临时目录。
- 所有人物生图入口保持既有整脸高斯糊法，源抽帧保留、处理图另存；定位失败停止提交。不得调整范围、强度或改为只糊五官，不重新引入 `sourceFaceBlurred` 特殊流程。封面使用原人物图且不糊脸。

## 当前流程边界

- 备料就绪只要求人物图与发布文案，不要求封面。备料完成留在等待区，视频仅通过逐条 `renderQueued` 手动安排；旧 `autoRender` 不能绕过。
- 自动封面只针对当前条目：取得视频单链路并成功提交视频任务后后台生成，备料、旧 `coverOnlyPrep`、等待资源和提交失败阶段均不生成。交付后补缺，图片页手动入口不受成片限制；收尾与关机仍等待后台任务。
- 封面只参考原 `人物图.png`，生成前检查手部及伸臂出画，返回后直接原图保存；不恢复成片选帧、双参考身份图、生成后视觉复查、16:9 扩图或二次放大。
- 跳舞源视频最长 40 秒，40 秒整允许；在付费生图前及相关替换、上传和出片入口验证真实时长。唱歌规则独立，不能套用此限制。
- 字幕由 `/subtitle` 手动启动（含用户主动的一键批量），不在批量唱歌交付后自动触发。已移除的人物定妆、旧歌词字幕、音色训练页面不得自行恢复；独立抖音下载页不重新加入导航。

## 运行与验证

- 后端常用地址 `127.0.0.1:15111`，ComfyUI 常用地址 `127.0.0.1:15188`；启动前检查已有健康服务并复用。启动脚本及实际环境可覆盖端口，不擅自改动运行配置。当前 Vite 开发代理仍指向 8111，不能把旧 `npm run dev:backend` 或 `npm run dev` 默认设置当作当前服务已可用的证据。
- 用户要求启动时，使用项目 `.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 15111` 的受管理终端会话，按需设置 `H3_SELF_URL` / `H3_COMFY_URL`，保留会话；不默认隐藏后台启动、不绕过执行策略。明确要求重启时先核实无活动任务，只操作确认属于本项目的进程。
- 前端改动按范围运行 `npm run typecheck`、`npm run build`；托管相关改动另运行 `npm run test:sites`。后端使用项目虚拟环境运行对应 `unittest` 模块；`npm run test:backend` 仅对应 `tests.test_backend`，不代表全部后端测试。
- 不以历史测试通过数代替本次验证。普通文档整理核对内容、链接、编码与差异即可，不启动服务或触发生成；浏览器验收沿用公用规则，不要求每次打开预览。
- 保留 `.openai/hosting.json`、`worker/index.js`、`scripts/prepare-sites-build.mjs` 及 `tests/sites-worker.test.mjs` 的托管能力。Sites 交付前构建和托管测试须通过，检查 `dist/client/index.html`、`dist/server/index.js`、`dist/.openai/hosting.json`。
- 提交与推送、暂存范围沿用公用规则。提交信息沿用 `fix(范围): 中文说明`、`feat(范围): …`、`docs: …`；不写死 `origin main` 或将仓库外文件误报为已同步远端。

## 专题阅读

| 任务 | 按需阅读 |
| --- | --- |
| 批量、发布目录、图片、封面、字幕、界面行为 | `docs/project-rules-current.md` 对应章节 |
| 人物图提示词与造型来源 | `docs/person-image-prompts.md`、`docs/source-appearance.md` |
| 中转站协议与恢复 | `docs/yeelo-image-relay.md`，核对现行模型与尺寸约定 |
| 发布文案 | `docs/publish-copy-safety.md` |
| 登录、管理与平台发布 | `docs/login.md`、`docs/prism-admin.md`、`docs/three-platform-publishing.md` |
| 关闭项目、素材补缺与手动抽帧 | `docs/close-project.md`、`docs/deferred-publish-materials.md`、`docs/manual-frame-replacement.md` |
| 剪映或旧桌面自动化排障 | `docs/手工剪映歌词字幕流程.md`、`docs/自动化流程说明.md`；旧出图方式仅作历史参考，不替换当前接口流程 |
