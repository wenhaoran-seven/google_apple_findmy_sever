本机 Chrome 授权工具

需要 Windows、Python 3.11 或更高版本、官方 Google Chrome，以及能访问 Google 的网络。
首次运行会创建独立 Python 环境并安装依赖，可能需要几分钟。

Authorize.cmd：本机完成 Google 登录和加密解锁，然后自动上传到 target.json 指定服务器账号。
Authorize-And-Export.cmd：完成授权后仅导出 JSON，不连接公司服务器。文件保存在 exports 文件夹。
Import.cmd：把已导出的 JSON 导入 target.json 指定服务器账号，不重新登录 Google。

也可在网页：账号管理 → 对应 Google 账号 → 授权/同步 → 本机授权备用方式 → 选择 JSON → 导入授权。
Google 密码、验证码、手机锁屏 PIN 只在 Google 页面输入。
命令行要求的“平台网页登录用户名/密码”是 FindMy 管理网页的登录信息，不是 Google 密码。

使用前检查 target.json 中服务器地址和账号编号。公司服务器需要连接公司内网或 VPN。
工具附带公司服务器公开根证书，仅供工具验证 HTTPS，不修改系统证书、不关闭证书校验。
不同 Google 邮箱使用独立解压目录。导出文件包含长期访问凭据、解密密钥和 FCM 凭据，务必妥善保管，勿发聊天或提交 Git。
本机 auth-local 文件夹为加密授权缓存，请与电脑账号一起妥善保护。
若提示提交未确认，不要重复提交；先在网页查看结果。last-import-task.json 记录后台任务编号，不含密钥。
Google 显示浏览器不安全或要求额外验证时，请按官方提示处理，本工具不绕过验证。
