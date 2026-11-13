# Google / Apple FindMy 服务端

本仓库是 2026-11-13 从阿里云运行实例提取的源码快照，包含统一控制台后端、Apple / Google 采集器、共享位置库、地址补全和第三方位置上报。它不是账号、设备或历史数据库备份。前端与 Windows 打印助手在 [findmy_web](https://github.com/wenhaoran-seven/findmy_web)。

## 新人先读

1. 阅读本文件的架构、配置和权限说明。
2. 克隆两个仓库，安装开发依赖，运行测试。
3. 使用测试账号与空数据库开发，不连接生产账号和位置库。
4. 前后端配套发布：网页源码不在本仓库，需执行前端仓库的资源安装脚本。
5. 当前不是通用的一键安装产品：原生产路径、端口和部分来源标识仍保留。部署到新环境前必须调整配置，并自行完成账号授权。

## 目录与进程

|目录|职责|原部署路径 / 监听端口|
|---|---|---|
|services/findmy-console|FastAPI 统一 API、账号管理、设备导入、轨迹、公开只读接口|/opt/findmy-console / 127.0.0.1:8870|
|services/findmy|Apple FindMy、会话保存、调度、CoreADI 适配|/opt/findmy / 127.0.0.1:8765|
|services/google-findmy|Google 账号1采集器、FCM 接收、注册/续报|/opt/google-findmy / 127.0.0.1:8767|
|services/google-findmy-2|Google 账号2部署版本；新 Google 账号模板来源|/opt/google-findmy-2 / 127.0.0.1:8769|
|services/google-findmy-3|Google 账号3部署版本|/opt/google-findmy-3 / 127.0.0.1:8771|
|services/findmy-address-worker|共享地址缓存、百度优先及回退|/opt/findmy-address-worker|
|services/startag-google-reporter|读取已有位置，上报 app.startag.cc，持久化待发送队列|/opt/startag-google-reporter|
|tests|统一控制台的自动化测试|临时数据库|
|deploy|无凭据的配置示例|须按环境替换|
|SOURCE_SNAPSHOT.json|提取时源文件 SHA256 清单|不含运行数据|

三个 Google 目录是实际部署副本，可能存在来源标识和实现差异，不能不经比较就合并。Apple 新账号复用 Apple 代码，但独立运行进程和数据目录。源码提取清单是提取时校验信息，后续整理、修复和测试路径调整会改变个别文件哈希。

## 数据流程

```mermaid
flowchart LR
 B[浏览器] --> C[Caddy HTTPS / 管理认证]
 C --> U[统一控制台 8870]
 U --> A[Apple 独立账号服务]
 U --> G[Google 独立账号服务]
 A --> D[(共享 SQLite)]
 G --> D
 W[地址补全 worker] --> D
 U --> D
 G --> F[Google 请求 + FCM 响应]
 R[位置上报 worker] --> S[app.startag.cc]
```

- 设备按 SN 合并展示；各采集器仍保存各自账号状态、密钥与历史缓存。
- 共享库默认生产位置为 `/var/lib/findmy-locations/locations.sqlite3`，包括 locations、location_versions、地址缓存和控制台管理表。
- 原始坐标保留 WGS84；地图显示转换和逆地址编码是另外的处理层，不应覆盖原始经纬度。
- 地址缓存跨设备/平台复用，附近 100 米可复用，旧记录由后台逐步补全。百度额度和限速由独立 worker 控制。
- Google 的请求触发与 FCM 返回是两个阶段；Uploading、ASSIGN_LOCATION、请求被接受均不等于位置上传成功。
- `unknown_11` 的含义尚未确认。deviceMetadata.field12 暂按服务端响应生成时间理解；目前只是分析结论，未替换位置时间或新增生产解析字段。

## 本地开发

建议 Python 3.12+、Git、Node.js 20+（运行另一个仓库的前端测试）。以下命令在仓库根目录运行：

```bash
git clone https://github.com/wenhaoran-seven/google_apple_findmy_sever.git
git clone https://github.com/wenhaoran-seven/findmy_web.git
cd google_apple_findmy_sever
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell 使用 .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python ../findmy_web/scripts/install_assets.py --target services
python -m unittest discover -s tests -p 'test_*.py'
```

启动控制台示例（Linux）：

```bash
export FINDMY_LOCATION_DB="$PWD/.local/locations.sqlite3"
export FINDMY_PUBLIC_ORIGINS='["http://127.0.0.1:8870"]'
export FINDMY_MAP_ORIGIN='http://127.0.0.1:8765'
python -m uvicorn app:app --app-dir services/findmy-console --host 127.0.0.1 --port 8870
```

控制台初始化会建立表和默认账号记录；采集器没有运行时，账号不可用属于预期现象。不要为了消除提示导入生产凭据。没有地图 AK / 配套地图服务时，百度图层可能不可用，列表/API测试不依赖百度。

采集器必须分别安装依赖并运行，不能只安装控制台依赖：

```bash
# 各采集器目录独立虚拟环境
cd services/google-findmy
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
# 如需复现原依赖，可检查 requirements.lock.txt；以目标系统兼容性为准
GOOGLEFINDMY_DATA=/absolute/test/google1 FINDMY_SOURCE=google1 \
 FINDMY_LOCATION_DB=/absolute/test/locations.sqlite3 \
 .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8767
```

Apple 在 `services/findmy` 安装 requirements.txt 后运行 `python -m uvicorn localfindmy.app:app --host 127.0.0.1 --port 8765`。实际位置查询还需要 Apple 登录/二次认证、可用的 CoreADI 运行资源；CoreADI 二进制没有上传，需要合法取得并通过 FINDMY_ADI_BINARY 配置。Google 需要完成该账号的登录和加密数据授权；授权缓存不在仓库中。

## 配置

|变量|用途|
|---|---|
|FINDMY_LOCATION_DB|所有服务共用 SQLite 绝对路径|
|FINDMY_SOURCE|来源 ID，例如 apple、apple2、google1；必须按账号唯一|
|FINDMY_DATA_DIR|Apple 独立私有数据目录|
|GOOGLEFINDMY_DATA|Google 独立私有数据目录|
|FINDMY_VAULT_KEY|Apple 数据加密密钥，切勿随意更换已有环境的值|
|FINDMY_ADI_BINARY|CoreADI 运行文件路径|
|FINDMY_HTTPS_PROXY / GOOGLEFINDMY_PROXY|各采集器出站代理；HTTPS 目标也可以使用 http:// 代理的 CONNECT 隧道|
|FINDMY_CONSOLE_TOKEN_FILE|控制台本地接口防误用令牌文件，生产应固定并限制权限|
|FINDMY_PUBLIC_ORIGINS|JSON数组，允许的网页 Origin|
|FINDMY_MAP_ORIGIN / FINDMY_MAP_PARENTS|地图 iframe 地址 / 允许的父页面|
|BAIDU_MAP_AK_FILE|百度服务端 AK 文件路径，不提交 AK|
|BAIDU_REQUEST_INTERVAL / BAIDU_REQUESTS_PER_HOUR|地址请求间隔/小时限额，须符合购买配额|

更多开关请检索对应服务 `os.environ`；`deploy/console.env.example` 仅是示例，不是完整生产环境文件。当前代码仍存在旧公网地址等非秘密默认值，新域名需同时调整 API Origin、地图父页面、Caddy、前端 iframe 和打印助手 Origin 白名单。

## 账号隔离与生产部署

- 每个账号独立进程、数据目录、授权会话；共享的是位置库，不是账号凭据。
- `provisioner.py` 会创建系统用户、systemd unit，按 `/opt` 固定模板复制运行配置；它需要受控特权，不能以普通开发测试方式随意启动。
- 新账号数据默认 `/var/lib/findmy-accounts/<account-id>`；动态端口19000–19999，仅绑定loopback。
- 生产需要 `findmylocations` 共享组和库文件权限，服务的 UMask=0007；不要设置全局777权限。
- 生产步骤：备份 → 安装各服务/依赖 → 放置外部凭据与运行资产 → 安装前端静态文件 → 配置systemd环境 → 配置Caddy证书与管理员密码哈希 → 检查localhost → 对外开放HTTPS。
- 服务更新前备份代码和配置，数据库通过SQLite backup API生成一致性备份；复制正在写入的单个.sqlite文件可能不完整。
- Caddy 示例不自带可信证书。域名可使用合适的ACME签发；IP证书需支持IP的CA和独立续期流程。本仓库不包含证书、ACME账号或生产Caddy密码哈希。
- 本次源码整理不替换线上服务，也不会自动登录、注册设备或重新上报位置。

## 管理页面与公开只读入口

`/` 为受 Caddy 登录保护的管理页面。`/view` 是**明确公开、免登录**的设备列表，可查看位置、轨迹和导出，链接持有者可以读取这些位置。后台对应 `/view/devices`、`/view/track` 仅允许GET/HEAD，使用字段白名单，不返回账号邮箱、EIK或私钥。不要仅靠隐藏按钮实现权限。

管理接口的 X-Local-Token 不是用户登录机制；真正的公网认证在网关。禁止直接将8870或采集器端口开放公网。若不需要公开位置，在Caddy中把 `/view*` 也放到认证范围。

轨迹 `/api/unified-track` 与 `/view/track` 支持 sn、start/end（Unix秒）、platform、limit、cursor：首批10点，后续200点，以时间和ID游标读取，并限制本次扫描的最大ID。加载完成后才启用完整导出。数据库写入新点不会导致分页重复；它不是覆盖全部字段更新的长期事务快照。

## 测试与常见问题

- `python -m unittest discover -s tests -p 'test_*.py'`：控制台、批量添加、公开只读隔离。
- 前端仓库 `npm test`：真实页面脚本组合、轨迹点击、时间选择器、分批加载、CSV/JSON导出。
- 管理页面401：检查网关登录；API403：检查Token、Origin和Host；公开POST403是正确行为。
- 能显示列表但查不到新位置：分别检查账号授权、代理/FCM连通、注册/续报、任务状态和日志，不能仅重启网页解决。
- 轨迹打不开：浏览器控制台、静态资源版本、time.js依赖和地图iframe配置；只读页没有管理员全局变量，通用组件不能依赖它们。
- 打印失败不是阿里云采集器问题；参见前端仓库本地打印服务。

## 源码与许可证

Google `upstream/` 包含 GoogleFindMyTools 派生代码及 GPL-3.0 许可证，保留 LICENSE、UPSTREAM_COMMIT（当前记录660be5395702ff7f1a6174f4ec4c865a57c7b527）。Apple Python库由 requirements安装；第三方组件遵循各自许可证，不因放入新仓库而重新许可。没有复制第三方二进制运行库。

明确排除：账号密码/Token、设备密钥与SN清单、位置数据库、生产日志、证书/SSH私钥、虚拟环境、编译产物、授权zip和设备烧录基线。缺失 `authorization-base.zip` 时授权助手下载入口会不可用；开发者需另行构建不含凭据的授权工具包。历史 burn-baseline.json 可选，不应复用生产基线到新环境。
