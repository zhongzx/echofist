# EchoFist 公共资源服务系统在共享网站空间的部署可行性调研报告（www.micpet.com）

## 1. 调研目标与结论摘要
本次调研聚焦于：在服务商提供的“虚拟网站空间（非 VPS）+ SSH 通道”环境中，是否能承载 EchoFist 未来“公共资源服务系统”的部署需求。结论如下：

- 可行：该环境可作为 EchoFist 的“运行节点/公共入口”承载位，支持 Python 运行、依赖安装、出站 WebSocket、后台任务脱离 SSH 存活、并支持 Python 以 Web 脚本形式被 HTTP 触发执行。
- 不建议：将其作为“需要自开端口、常驻监听、强 SLA 的服务端平台”。原因是共享主机具备明显的系统能力/命令权限限制，平台行为不可完全控。

## 2. 环境概况（已验证事实）
- 系统信息：Debian GNU/Linux 12 (bookworm)，内核 `6.6.138-sure`。
- SSH 通道使用方式：SSH 通道是必须通过管理员人工复制粘贴实施。
- 账号与权限形态：
  - 家目录 `/home/philinice` 顶层由 root 持有（`drwxr-x---+ root:philinice`），但子目录存在可写区域。
  - 可写目录：`~/private`、`~/apps`、`/tmp` 可写；`~/www` 顶层不可写，但其下存在可写业务目录树。
- 命令执行限制（共享主机特征明显）：
  - `curl` 对当前用户 Permission denied（不可执行）。
  - `uptime`、`df` 等部分 `/usr/bin/*` 命令存在 Permission denied。
  - 根目录 `/` 不可列出（Permission denied）。

## 3. Python 运行与依赖安装可行性
- Python 版本：`Python 3.11.2` 可用（`/usr/bin/python3`）。
- venv 可用：在可写目录 `~/private` 成功创建虚拟环境（`python3 -m venv ~/private/ef_probe_venv`）。
- 第三方依赖安装验证：在 venv 内安装 `websockets==16.0` 成功。

结论：满足 EchoFist 运行时“Python 3.10+、可隔离依赖安装”的基础要求。

## 4. 网络出站能力（KiwiSDR 接入关键前置）
- HTTPS 出站：Python `urllib` 对外 HEAD 请求成功（示例返回 200）。
- DNS 策略：
  - `example.com / google.com / cloudflare.com` 可正常解析。
  - `echo.websocket.events` 在该环境无法解析（DNS 策略/黑名单/可用域名集限制）。
- 出站 WebSocket（wss）验证成功：
  - `wss://ws.postman-echo.com/raw`：成功收发（recv: ping）
  - `wss://echo.websocket.in/`：成功收发（返回服务端响应文本）

结论：虽然存在“部分域名不可解析”的现象，但出站 wss/WebSocket 能力本身可用，KiwiSDR 的出站连接在网络层面具备可行性（后续需用目标 KiwiSDR 域名/地址做一次针对性连通性验证）。

## 5. 后台任务与脱离 SSH 存活能力
- 短时后台任务验证：`nohup sh -c 'date; sleep 180; date' &` 成功，日志包含开始与结束时间戳。
- SSH 断开后存活验证：
  - 断线重连后，后台进程仍在运行，且被 PID 1 收养（PPID=1）：
    - `sh -c date; sleep 600; date`
    - `sleep 600`

结论：该环境支持“后台任务常驻式运行”的基本形态，适合运行 EchoFist 的后台 worker（例如 KiwiSDR 长连接、解码、落库）。

## 6. 网站目录与域名映射（www.micpet.com）
- 可读/可写的网站目录树：关键目录存在并可写：
  - `/home/philinice/www/www/micpet` 可写
  - `/home/philinice/www/www/micpet/web` 可写
- 入口文件：`/home/philinice/www/www/micpet/web/index.php` 存在。
- 域名 URL 前缀映射事实：
  - 访问 `https://www.micpet.com/about.php` 实际跳转/落点为 `http://www.micpet.com/micpet/web/`。
  - 说明对外可访问的路径前缀为 `/micpet/web/`（docroot/alias/重写由服务商 vhost 层完成的可能性高）。

结论：在该主机上，`www.micpet.com` 对应业务系统目录明确，且具备可控写入能力，可部署 HTTP 触发的无头接口。

## 7. Python 作为 Web 脚本（CGI）可行性验证
已完成“最小化探测目录”验证，并按约定保留探测目录：

- 探测目录：`/home/philinice/www/www/micpet/web/ef_probe/`
- 关键配置：
  - `.htaccess`：
    - `Options +ExecCGI`
    - `AddHandler cgi-script .py`
  - `test.py`：shebang 指向 `/usr/bin/python3`
- 对外访问验证：
  - URL：`http://www.micpet.com/micpet/web/ef_probe/test.py`
  - 返回：`python_cgi_ok`

结论：该网站空间支持通过 `.htaccess` 启用 Python CGI，因此“公共服务系统”可按“网站系统（HTTP 触发、无 UI）”的方式，用 Python 实现入口逻辑，无需切换语言到 PHP。

## 8. 建议的部署形态（结合共享主机约束）
在不引入 Web 框架的前提下，推荐采用“HTTP 入口 + 后台 worker”的组合：

- HTTP 入口（Python CGI）负责：
  - 接收外部请求（对话内容/事件数据）
  - 轻量校验（例如 token）
  - 快速落盘（SQLite 或文件队列）
  - 快速返回（避免超时/执行时长限制）
- 后台 worker（Python + nohup/cron）负责：
  - KiwiSDR 长连接与持续采集
  - CW 解码/特征提取/自动化流程
  - 读入 HTTP 入口产生的数据并处理（如需要）

## 9. 风险与注意事项
- 平台强管控风险：curl/部分系统命令不可执行、系统可见性受限，说明环境不可完全控；对“强 SLA 公共服务端”有天然风险。
- DNS 可用域名集：存在部分域名不可解析现象；对 KiwiSDR 目标域名需单独验证。
- 路径映射复杂度：`/micpet/web/` 的 URL 前缀由 vhost 层配置决定，后续新增接口路径应遵循既有前缀，避免误判 404。

## 10. 结论（可部署性判断）
- 可以部署：EchoFist 公共服务系统的“网站式无头入口（Python CGI）+ 后台任务处理（nohup/cron）”形态。
- 不建议：将该环境作为“需要自开端口、稳定长时在线监听、可完全掌控系统资源与网络策略”的服务端平台。
