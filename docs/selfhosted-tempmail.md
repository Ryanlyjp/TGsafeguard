# 自建 TempMail 接入

TGsafeguard 可以接入兼容接口的自建 TempMail 服务作为登录邮箱保护的收件后端。接入同时使用持久化 Webhook 和 Bearer API 精准轮询：Webhook 负责及时推送，轮询负责故障兜底。

## 前置条件

- TempMail 已部署，`tgmag` 所在主机能够访问其 HTTP(S) 根地址。
- 登录邮箱保护使用的域名已在 TempMail 中启用，域名 MX 指向 TempMail 的 Postfix。
- 准备一个 TempMail 账号 API Key；该账号需要能够创建邮箱、按地址查箱并读取邮件。
- 生成一份至少 32 个非空白字符的独立 Webhook 密钥；TempMail 与 `tgmag` 必须使用同一个值。
- API Key 和 Webhook 密钥只保存在配置中，不要写入仓库或日志。

TempMail 的 Catch-all 可以保持关闭。`tgmag` 会在请求 Telegram 发码前创建完整目标地址，并显式传入 `subdomain_mode=off`，不会受 TempMail 全局 API 子域策略影响。

## 配置

在 `tgmag/.env` 增加：

```env
LOGIN_EMAIL_PROTECTION_ENABLED=true
LOGIN_EMAIL_ALIAS_DOMAINS=mail.example.com
LOGIN_EMAIL_DOMAIN_BACKENDS=mail.example.com=selfhosted
LOGIN_EMAIL_SELFHOSTED_TEMPMAIL_URL=https://tempmail.example.com
LOGIN_EMAIL_SELFHOSTED_TEMPMAIL_API_KEY=tm_replace_with_account_api_key
LOGIN_EMAIL_SELFHOSTED_WEBHOOK_SECRET=replace_with_at_least_32_random_characters
```

URL 填根地址，不要附带 `/api`、查询参数或账号密码。URL 与 API Key 必须同时配置。配置 Webhook 密钥后，即使 `MINI_APP_ENABLED=false`，tgmag 也会在 `MINI_APP_HOST:MINI_APP_PORT` 启动仅含 Webhook 路由的监听器。

在 TempMail 管理后台的“系统设置 → tgmag Webhook”中填写：

- 接收地址：`https://<tgmag-host>/webhooks/selfhosted-tempmail`
- 签名密钥：与 `LOGIN_EMAIL_SELFHOSTED_WEBHOOK_SECRET` 完全相同
- 域名：勾选参与推送的域名；需要时再勾选“包含子域名”
- 保存后启用 Webhook

TempMail 不判断邮件是否为 OTP。它会可靠推送所选域名的邮件，由 tgmag 执行账号上下文和验证码校验。

## 接口流程

每次新换绑按以下顺序执行：

1. `GET /api/mailboxes/lookup?address=<完整地址>` 检查邮箱是否存在。
2. 不存在时调用 `POST /api/mailboxes` 精确创建邮箱。
3. 发码前记录并持久化本次请求时间，再向 Telegram 请求一次登录邮箱验证码。
4. TempMail 收信时，把邮件与 Webhook outbox 在同一事务落库；邮箱过期不会删除待投递内容。
5. tgmag 验证 HMAC 和 5 分钟时间窗，以 `(邮件 ID, 完整收件地址)` 幂等保存并唤醒该地址的等待流程。
6. 等待流程优先读取 Webhook 已落库邮件，同时继续按完整邮箱名轮询 TempMail API。
7. 仅接受接收时间不早于本次请求、目标邮箱一致、发件人为 `LOGIN_EMAIL_SENDER`、正文明确用于 Login 且主题与正文验证码一致的邮件。

这里的 API 轮询只在已经触发换绑、等待新邮箱验证码期间运行，默认每 3 秒一次；账号日常守护由 Telegram `777000` 服务消息实时监听触发，不会每 3 秒扫描邮箱。成功取码时，tgmag 会在日志记录 `source=webhook` 或 `source=polling` 以及收件域名，不记录完整邮箱或验证码。

服务在等待邮件期间重启时会恢复原等待，不会重新创建邮箱或再次向 Telegram 发码。

## 投递安全与重试

TempMail 使用 `HMAC-SHA256(secret, timestamp + "." + raw_body)` 签名，并发送 `X-TempMail-Delivery`、`X-TempMail-Timestamp` 和 `X-TempMail-Signature`。tgmag 拒绝错误签名、过期时间戳及未映射为 `selfhosted` 的域名。

Webhook 请求固定 10 秒超时且不跟随重定向。失败后按固定退避重试，成功收到 2xx 才删除 outbox；超过 24 小时的待投递记录会清理。进程在 tgmag 成功保存后、TempMail 删除 outbox 前中断时可能重复投递，但 tgmag 的复合主键会去重。

## 连通性检查

先在部署主机验证账号 API Key：

```bash
curl -fsSL \
  -H "Authorization: Bearer <TEMPMAIL_API_KEY>" \
  "https://tempmail.example.com/api/me"
```

再确认目标域名已由 TempMail 管理。`tgmag` 的“检查邮件接收”会验证 API URL、认证和当前域名实际使用到的后端；它不会发送测试邮件。

常见失败含义：

| 现象 | 检查项 |
| --- | --- |
| HTTP 401 | API Key 无效或不属于当前 TempMail 实例 |
| 创建邮箱返回域名不可用 | 目标域名未在 TempMail 中启用，或域名映射填写错误 |
| 邮箱已创建但收不到信 | 域名 MX、Postfix 和 TempMail 投递日志 |
| TempMail outbox 持续重试 | Webhook URL、反向代理、两端密钥和 tgmag 监听地址 |
| 收到邮件但取码超时 | 发件人、邮件时间、Login 用途文字和验证码是否符合校验条件 |

tgmag 复用现有 `temp_mail_messages`，不新增 Alembic 迁移。TempMail 老库需执行 `sql/migrate_v13.sql`，正常启动时兼容初始化也会创建 outbox 和默认设置。回退时先在 TempMail 关闭 Webhook，再移除 tgmag 的 Webhook 密钥；保留 URL/API Key 即可继续使用轮询通道。
