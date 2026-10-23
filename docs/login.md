# 私人工作台登录

首次更新并启动后端后，在本机打开工作台，创建管理员账号及至少 12 位密码。没有默认密码，不提供公开注册。账号数据存于 `data/auth.db`，与任务数据库分开，备份时一并保存。

勾选「记住登录」后会话保留 30 天；否则 Cookie 随浏览器会话结束，服务端最长有效 12 小时。退出登录只注销当前会话，不停止正在运行的生成任务。登录保护覆盖 `/api/` 的业务接口、下载与 WebSocket；接口写操作检查请求来源。连续五次密码错误后，同一来源五分钟内不可再登录。

密码使用带随机盐的 scrypt 保存，数据库不存明文密码；会话 Cookie 为 HttpOnly、SameSite=Strict，HTTPS 下启用 Secure。远程部署需要由 HTTPS 代理提供连接，并保留原始 Host、正确配置协议转发。此功能不把本机服务发布到公网。

忘记密码时，在项目目录运行 `.venv/Scripts/python.exe -m backend.auth_admin`，在终端交互输入新密码。重置会撤销全部登录会话，不改任务或作品。

登录页按选定玻璃 H 参考设计实现，背景资产位于 `public/assets/login-prism.png`。Three.js 按需加载，在装饰图上渲染三维轨道、沿轨道运动的光点和高光着色器；离开登录页释放资源，后台标签暂停渲染，减少动态效果时使用静态画面，WebGL 不可用时保留原图。

界面结构参考 MIT 项目 Shadcn Admin（https://github.com/satnaing/shadcn-admin），动效方向参考 Magic UI（https://github.com/magicuidesign/magicui）；本次没有复制其源码或整体替换现有框架。
