# Google 授权自动恢复

位置查询继续使用每账号独立推送通道。断连时重建通道，只读查询可有限重试；注册、删除等写操作不自动重放。

短期令牌到期或位置接口返回 401/403 时，使用保存的长期凭据重新申请令牌，不需要保存 Google 密码。令牌请求网络错误有限重试，持续异常按 30、60、120、240、480、900 秒退避；定时任务在冷却结束后继续。

仅 Google 令牌接口明确返回 BadAuthentication、NeedsBrowser、CaptchaRequired、AccountDeleted、AccountDisabled 时，将 auth_recovery 标记为 reauthorization_required，暂停自动任务并在账号管理提示人工授权。普通超时、未知响应和单纯位置接口 403 不判定账号失效。

成功导入新授权会清除暂停状态；现有设备定时任务配置保留。状态保存在账号加密数据库，网页通过后台快照读取，最多约一分钟同步。错误记录不含令牌、Cookie、密码或响应原文。

测试：tests/test_auth_recovery.py（使用采集服务 Python 环境运行）。部署时三个代码目录均需包含 auth_recovery.py；动态账号复用 google-findmy-2 代码。

## 实时页面状态与空闲检查

采集器在没有运行/排队任务且最近 5 分钟未校验令牌时，排入内部 auth-check 任务。它复用已保存的 Android ID，不创建额外 FCM 连接，不改设备配置。未授权或已要求人工验证的账号不自动登录。

控制 worker 每 5 秒读取轻量 /api/auth/health 并写入独立 account_health 表。网页 SSE /api/account-health/events 每 2 秒检查数据库变化，15 秒心跳，不直接访问采集器或 Google。接口要求管理令牌，只读公开页不包含账号状态。

状态码：HEALTHY 正常，RETRYING 自动重试，REAUTH_REQUIRED 需人工验证，UNAUTHORIZED 未授权，PENDING_CHECK 待检查，UNAVAILABLE 采集服务不可用，STALE 状态采集超过 30 秒未更新。

页面首次收到每次 REAUTH_REQUIRED 故障时弹窗，并在顶部保留提示。浏览器 localStorage 记录本次故障时间，刷新不重复弹窗；健康恢复后解除。页面关闭时不发送系统、邮件或企业微信通知。故障发生到被发现仍受请求/检查周期影响，不能承诺 Google 撤销授权后立即发现。
