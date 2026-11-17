# Google 授权自动恢复

位置查询继续使用每账号独立推送通道。断连时重建通道，只读查询可有限重试；注册、删除等写操作不自动重放。

短期令牌到期或位置接口返回 401/403 时，使用保存的长期凭据重新申请令牌，不需要保存 Google 密码。令牌请求网络错误有限重试，持续异常按 30、60、120、240、480、900 秒退避；定时任务在冷却结束后继续。

仅 Google 令牌接口明确返回 BadAuthentication、NeedsBrowser、CaptchaRequired、AccountDeleted、AccountDisabled 时，将 auth_recovery 标记为 reauthorization_required，暂停自动任务并在账号管理提示人工授权。普通超时、未知响应和单纯位置接口 403 不判定账号失效。

成功导入新授权会清除暂停状态；现有设备定时任务配置保留。状态保存在账号加密数据库，网页通过后台快照读取，最多约一分钟同步。错误记录不含令牌、Cookie、密码或响应原文。

测试：tests/test_auth_recovery.py（使用采集服务 Python 环境运行）。部署时三个代码目录均需包含 auth_recovery.py；动态账号复用 google-findmy-2 代码。
