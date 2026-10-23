# 私人工作台登录

首次更新并启动后端后，在本机打开工作台，创建管理员账号及至少 6 位密码。没有默认密码，不提供公开注册。账号数据存于 `data/auth.db`，与任务数据库分开，备份时一并保存。

勾选「记住登录」后会话保留 30 天；否则 Cookie 随浏览器会话结束，服务端最长有效 12 小时。退出登录只注销当前会话，不停止正在运行的生成任务。登录保护覆盖 `/api/` 的业务接口、下载与 WebSocket；接口写操作检查请求来源。连续五次密码错误后，同一来源五分钟内不可再登录。

密码使用带随机盐的 scrypt 保存，数据库不存明文密码；会话 Cookie 为 HttpOnly、SameSite=Strict，HTTPS 下启用 Secure。远程部署需要由 HTTPS 代理提供连接，并保留原始 Host、正确配置协议转发。此功能不把本机服务发布到公网。

忘记密码时，在项目目录运行 `.venv/Scripts/python.exe -m backend.auth_admin`，在终端交互输入新密码。重置会撤销全部登录会话，不改任务或作品。

登录页恢复黑紫玻璃 H 视觉，图片位于 `public/assets/login-prism.png`。左上角紫色立体 H 通过 CSS 图片窗口直接呈现用户参考图的对应区域，保留原形与渐变；参考图保存在 `public/assets/login-brand-reference.png`。Three.js 只计算流动的照明：光带高光沿上、下光轨穿行，玻璃内部高光流转。原图坐标完全不随时间改变，没有形变、纹理位移、旋转或缩放动画。几何角色、星空背景与流体探索源码已移除，历史提交可恢复。

背景标签暂停渲染，离开登录页释放纹理、几何、材质和监听器；减少动态效果时显示静态画面，WebGL 不可用时直接显示原始图片。登录、密码与会话规则不变。

界面结构参考 MIT 项目 Shadcn Admin（https://github.com/satnaing/shadcn-admin），动效方向参考 Magic UI（https://github.com/magicuidesign/magicui）；本次没有复制其源码或整体替换现有框架。
