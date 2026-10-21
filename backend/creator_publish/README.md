# 三端发布执行引擎

从本机 creator-publish-helper 移入的 Windows 原生输入引擎。通过 worker.py 的独立进程调用，避免旧模块的顶层导入、DPI 和 UIA 设置影响视频后端。原平台校验、日历滚动、提交前检查、输入助手和检查点逻辑保留。

网页队列由 backend/publish_service.py 管理，状态和控制文件在 data/publisher/，不读取或覆盖原项目队列。旧记录通过页面显式导入。引擎同目录的 JSON 为本机诊断和按键协作文件，不提交。运行期间共用原项目的 Windows 互斥锁，防止两个三端队列同时操作桌面。

不需 ffpyplayer：网页使用项目已有的视频播放方式。发布依赖 Windows MCP 0.8.5 和 pyperclip，见 backend/requirements-publish.txt。
