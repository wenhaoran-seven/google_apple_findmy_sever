# 公司内网部署（2026-11-17）

生产入口迁移到 `https://192.168.168.162/`，只读入口 `/view`。原阿里云业务采集进程已停止并禁用，避免同一Google推送凭据在两台服务器竞争。原数据保留用于回退；回退前必须同步新服务器新增数据。

Ubuntu22.04使用独立Python3.12重新安装原环境冻结依赖；保留`/opt`运行路径、systemd账号隔离和原数据加密密钥。最终切换在停止所有写入后增量同步；比较数据SHA256并逐库运行SQLite quick_check，再启用目标服务。部署备份和身份凭据不得提交Git。

Caddy使用内网CA和`tls internal`。全局采用`auto_https disable_redirects`而非`off`，由Caddy管理内网证书；HTTP跳转按原路由处理。systemd需`RuntimeDirectory=caddy`，重载地址为`unix//run/caddy/admin.sock`。每台访问电脑需要信任新CA，浏览器串口依赖可信HTTPS。根私钥留在服务器；仅分发root.crt。地图子站18447端口也需要可达。

变更入口时需要同时调整console的FINDMY_PUBLIC_ORIGINS、FINDMY_MAP_ORIGIN、Apple地图FINDMY_MAP_PARENTS、各账号public-web环境、Caddy站点、授权helper的target.json默认入口以及Windows打印桥Origin白名单。公开/view不能暴露授权浏览器和管理写入API。

Google服务使用本机`127.0.0.1:18780`中继，上游配置在`/etc/findmy-upstream-proxy.env`；Apple代理配置在`/etc/findmy-proxy.env`。本机在这里指公司服务器，不是用户电脑。上游HTTP代理需要允许CONNECT到Google443及FCM5228。不应把HTTP代理的scheme错误改成https。

百度服务端AK在`/etc/findmy-address/baidu-ak`，服务端白名单随公网出口改变而调整。浏览器AK与服务端AK独立，不应互换。已有100米缓存和OSM回退继续生效。

迁移验收应包括：实际账号位置返回、共享历史一致性、登录保护、只读列表/轨迹、云端授权浏览器启动与清理、打印跨域检查、地址服务与第三方上报连通、开机自启检查，而不只检查进程active。
