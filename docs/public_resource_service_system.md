# EchoFist 公共资源服务系统（周更 Feed + 众包 HTML）开发文档

## 1. 目标与边界

### 1.1 目标

- 通过众包提交的原始 HTML，补充/校验公共 KiwiSDR 目录信息，并生成可消费的周更 Feed。
- 在冷启动阶段提供更积极的采集提示，但通过预算/冷却/抽样机制控制打扰与滥用。

### 1.2 边界与约束（必须遵守）

- 客户端不做 device_id，仅使用服务端签发的 token。
- 服务端将 HTML 视为不可信输入：不渲染、严格大小限制、隔离解析、短期保留、强去重。
- 不引入 Web 框架；在共享主机上以 Python CGI 作为 HTTP 入口。
- 默认部署形态为“HTTP 入口（CGI）+ 后台 worker（nohup/cron）”，不要求自开端口常驻监听。

## 2. 部署形态与路径口径（与共享主机对齐）

### 2.1 HTTP 入口

- 入口脚本：`/micpet/web/echofist/submit.py`（Python CGI）
- 入口职责：
  - 接收并校验提交（HMAC 签名、时间窗、nonce 防重放、体积限制）
  - 快速落盘（写入隔离区 + 记录元数据）
  - 快速返回（避免执行时长不确定）
- 入口禁止事项：
  - 不解析、不渲染 HTML
  - 不做长时 CPU/网络操作（例如抓取、复杂解析、全量去重索引构建）

### 2.2 后台 worker

- 常驻/周期任务运行方式：`nohup` 或 `cron`
- worker 职责：
  - 对落盘的 HTML 进行隔离解析与字段提取
  - 进行强去重与归并（以 body_hash 为核心键）
  - 按周期生成周更 Feed（并可同步到 web 可访问目录）
  - 可选：抓取官方公共目录页面并与众包信息做一致性对比

### 2.3 存储位置与隔离

- 原始 HTML 必须存放在非 webroot 目录（例如 `~/private/echofist_public/` 或 `/tmp/echofist_public/`）。
- webroot 目录仅存放 CGI 脚本与可公开分发的 Feed 产物（不含原始 HTML）。

## 3. 提交协议（submit）

### 3.1 请求

- Method：POST
- Path：`/micpet/web/echofist/submit.py`
- Content-Type：`application/octet-stream`（直接提交原始 HTML bytes）
- 认证：`Authorization: Bearer <token>`

提交元数据字段（建议作为 HTTP Header，避免与 body 混合）：

- `X-EF-TS`：UTC 秒（int）
- `X-EF-Nonce`：随机 nonce（16–32 字节，base64url 编码）
- `X-EF-Body-Hash`：SHA-256(hex)（对原始 body bytes 计算）
- `X-EF-Sig`：HMAC-SHA256(hex 或 base64)（见 3.2）

### 3.2 签名计算（HMAC-SHA256）

- HMAC key：token 本身（token 视为 bearer secret，必须走 HTTPS）
- body_hash：`sha256(body_bytes).hexdigest()`
- 待签字符串（固定格式，含动作与路径防跨接口重放）：

```
POST
/micpet/web/echofist/submit.py
{ts}
{nonce}
{body_hash}
```

- `sig = HMAC_SHA256(key=token, message=待签字符串 UTF-8 编码)`，输出 hex 或 base64。

### 3.3 服务端校验顺序（必须一致）

1. 体积限制：读取 body bytes 上限（默认 2 MB），超限直接拒绝。
2. 校验 token：存在、未吊销、未过期、未被封禁。
3. 校验 ts：与服务端时间差在允许窗口内（默认 ±300 秒）。
4. 校验 nonce：对 `(token, nonce)` 做防重放；在时间窗内必须唯一（存储 nonce 哈希并设置 TTL）。
5. 重算 body_hash：与 `X-EF-Body-Hash` 必须一致。
6. 重算 sig：与 `X-EF-Sig` 进行 constant-time 比较。
7. 落盘与入队：写入隔离区，并记录元数据（token_id、ts、nonce_hash、body_hash、bytes、remote_addr、received_ts）。
8. 去重策略：若 `body_hash` 在保留窗口内已存在，按策略返回 duplicate（见 5.2）。

### 3.4 响应（建议）

- 200：接收成功
  - `{"status":"ok","body_hash":"...","queued":true}`
- 409：重复提交
  - `{"status":"duplicate","body_hash":"..."}`
- 400：字段缺失/格式错误
- 401：缺少或无效 token
- 403：token 被禁用或权限不足
- 413：payload 过大
- 429：触发限流或配额
- 500：内部错误（不得泄露敏感信息）

## 4. Token 签发与分层策略

### 4.1 冷启动默认策略（推荐）

- 邀请码兑换 token（trusted tier）
  - 邀请码一次性使用，服务端仅存储哈希。
  - token 为不透明随机串（建议前缀 `ef1_` + base64url(32 bytes)）。

### 4.2 公开申请策略（可选，扩大覆盖）

- 公开申请发放 trial token（trial tier）
  - trial token 有有效期（例如 7–30 天）。
  - 申请接口做 IP 级限流与冷却，避免批量申请。

## 5. 配额、预算、冷却与去重

### 5.1 配额建议（每 token）

- trial：
  - `4 / 天`，`12 / 周`，最小间隔 `30 分钟`
- trusted：
  - `12 / 天`，`60 / 周`，最小间隔 `10 分钟`

### 5.2 去重与计费口径

- 强去重键：`body_hash`
- 去重窗口：默认 7 天
- 重复提交策略：
  - 返回 409（duplicate）
  - 重复提交不计入配额或低计费（默认不计费）

### 5.3 冷启动“更积极提示”的约束落点

- 客户端层：提示/引导可以更积极，但必须尊重服务端返回的 429/409，并在本地进入冷却。
- 服务端层：最终以 token 配额 + 最小间隔 + IP 限流兜底，避免滥用。

## 6. HTML 处理与安全隔离

- HTML 永远不被渲染，不作为模板输入，不回显给任何外部请求。
- 解析隔离：
  - 仅在 worker 中解析
  - 采用最小化解析策略（优先提取字符串模式/URL/host:port/字段块）
  - 对解析错误容忍但计数，持续异常可触发 token 降级或封禁
- 数据保留：
  - 原始 HTML 短期保留（例如 7–14 天）
  - 派生结构化结果可长期保留（以满足周更与质量评估）

## 7. 周更 Feed 产物

### 7.1 Feed 文件建议

- 输出位置（web 可访问）：`/micpet/web/echofist/feed/weekly.json`（或按周分文件）
- 顶层字段建议：
  - `schema`：`ef-feed-kiwi-v1`
  - `generated_at`：UTC 秒
  - `period_start` / `period_end`：UTC 秒
  - `items_sha256`：对 items 的确定性序列化结果计算 SHA-256(hex)
  - `items`：条目数组（不包含原始 HTML）

条目字段建议：

- `server`：`host:port`
- `host` / `port`
- `public_updated_ts`（可选）
- `public_band_low_hz` / `public_band_high_hz`（可选）
- `source`：`public_directory` / `crowd_html`
- `evidence_hash`：用于一致性校验的 hash（例如 public_entry_hash 或归并后的证据摘要）

### 7.2 Feed 的签名与校验策略

当前仓库默认依赖未包含 Ed25519/通用非对称签名库，因此签名策略分两档：

- v1（默认可实现）：不提供可公开验证的签名，仅提供 `items_sha256` 完整性字段，并通过 HTTPS 分发。
- v1.1（可选增强）：引入非对称签名（例如 Ed25519），发布公钥并在 feed 中增加 `key_id` / `sig_alg` / `sig`，客户端可离线验签。

## 8. 运维与观测（最低要求）

- 记录以下审计事件：token 签发/吊销、提交接入、验签失败、限流触发、重复提交、解析失败统计。
- 不在日志中记录 token 明文与原始 HTML。
