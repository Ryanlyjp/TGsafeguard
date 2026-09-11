# TGsafeguard

面向自有或已获得明确授权的 Telegram 账号的多账号管理与登录安全防护服务。

TGsafeguard 使用 Telegram Bot 作为管理入口，通过 Telethon 维护用户 Session，并提供可选的 Telegram Mini App 网页控制台。数据持久化在 PostgreSQL，手机号、Session、2FA 信息和登录邮箱等敏感字段使用 Fernet 加密保存。

> 本项目只能用于本人拥有或已获得明确授权的账号。Telegram 用户 Session 具有接近完整客户端的权限，请把服务器、数据库、Bot Token 和 Fernet 密钥视为高敏感资产。

## 主要功能

- 手机号验证码、二维码和 Telethon StringSession 登录
- 多账号列表、状态、重连、导入、导出和安全删除
- 姓名、简介、用户名、头像、隐私和 2FA 管理
- SpamBot 检查与 `777000` 服务消息实时监听
- 登录提醒触发的自动登录邮箱换绑
- Telegram Bot 内手动更换已有登录邮箱
- Telegram Bot 与 Mini App 一键生成新邮箱并立即完成换绑
- Cloudflare TempMail、Gmail IMAP 和自建 TempMail 三种收件后端
- 自建 TempMail 持久化 HMAC Webhook 与精准 API 轮询兜底
- 目标授权列表、操作速率和随机延迟
- 发送、关注、反应、浏览和转发等单账号或批量任务
- 管理员操作审计、任务记录和 PostgreSQL 备份
- Telegram Bot 与移动端 Mini App 两套管理界面

## 工作原理

```text
管理员 Telegram 账号
        |
        +-- 管理 Bot -----------+
        |                        |
        +-- Telegram Mini App ---+--> TGsafeguard --> PostgreSQL
                                             |
                                             +--> Telethon 用户 Session
                                             |
                                             +--> TempMail / Gmail
```

每个 active Telegram Session 都会建立独立 Telethon 连接。服务实时接收来自 `777000` 的登录提醒，保存并通知管理员。对于不在白名单中的账号，系统可以立即换绑登录邮箱，也可以按账号设置固定等待窗口，将窗口内提醒合并后只换绑一次。`777000` 的 Premium 开通、赠送、续费、取消或到期通知会跳过等待窗口并立即触发保护。

登录邮箱保护不会长期扫描全部邮箱。保护事件触发后，系统创建新的精确邮箱地址，请求一次 Telegram 邮箱验证码，并在限定时间内读取验证码：

- Cloudflare TempMail：Webhook 写入本地 PostgreSQL
- Gmail：通过 IMAP 查询转发邮件
- 自建 TempMail：持久化 HMAC Webhook 优先，Bearer API 精确轮询兜底

邮件必须同时通过收件地址、接收时间、Telegram 发件人、Login 用途和验证码一致性校验。服务重启后会恢复未结束的保护窗口和原验证码等待，不会自动重复发码。

## 运行要求

- Debian 12 或兼容 Linux
- Python 3.11+
- PostgreSQL 14+
- Telegram Bot Token
- Telegram API ID 与 API Hash
- 一个或多个管理员 Telegram 用户 ID
- Mini App 或邮件 Webhook 需要公网 HTTPS 地址

## 快速开始

### 1. 获取代码并安装依赖

```bash
git clone https://github.com/Ryanlyjp/TGsafeguard.git
cd TGsafeguard
./ops/install_debian12.sh
cp .env.example .env
```

### 2. 生成 Fernet 密钥

```bash
.venv/bin/python -c \
'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

把输出写入 `.env` 的 `FERNET_KEY`。已有数据后不能随意更换该密钥，否则已加密的数据将无法读取。

### 3. 填写必需配置

```env
BOT_TOKEN=<BOTFATHER_TOKEN>
TG_API_ID=<TELEGRAM_API_ID>
TG_API_HASH=<TELEGRAM_API_HASH>
ADMIN_IDS=<TELEGRAM_USER_ID>
DATABASE_URL=postgresql+asyncpg://<USER>:<PASSWORD>@127.0.0.1:5432/<DATABASE>
FERNET_KEY=<GENERATED_FERNET_KEY>
```

暂时不使用登录邮箱保护时，应明确关闭：

```env
LOGIN_EMAIL_PROTECTION_ENABLED=false
```

### 4. 初始化数据库并启动

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m app.main
```

Bot 启动后，管理员发送 `/start` 或 `/menu` 即可打开主菜单。

生产环境的独立用户、目录权限、systemd、PostgreSQL、HTTPS、备份和升级步骤见 [DEPLOY.md](DEPLOY.md)。

## 账号接入

支持三种方式：

1. `/login`：输入手机号和 Telegram 验证码，账号启用 2FA 时继续输入密码。
2. `/qr_login`：使用另一台已登录 Telegram 客户端扫码授权。
3. `/import_session`：导入已有 Telethon StringSession。

导入后，Session 会加密保存到 PostgreSQL。系统同一时间只保留每个账号的一条 active Session。Session 失效或 Telegram 返回授权密钥异常时，账号会被标记为不可用，不再参与批量任务和自动保护。

## 登录邮箱保护

Bot 与 Mini App 的单账号操作页提供两种主动操作：“更换登录邮箱”用于手动填写新地址并提交邮件验证码；“一键换绑邮箱”会按当前默认域名生成新地址，自动收取验证码并完成换绑，不依赖 `777000` 消息。两种操作都使用 Telegram 的登录邮箱 Change 流程，只适用于已经设置登录邮箱的账号；如果账号从未设置登录邮箱，必须在 Telegram 登录流程返回 `SetUpEmailRequired` 时完成首次 Setup，不能通过 Change 流程补设。

一键换绑成功后，同一账号尚未到期的延迟保护窗口会标记为已被手动操作取代，避免窗口到期后再次换绑。该手动任务不会新增伪造的 `777000` 服务消息；如果服务在等待邮件验证码期间重启，本次手动任务不能恢复，需要等待验证码过期后重新点击。

### 触发规则

- 仅处理当前 Session 建立后出现的新 `777000` 登录验证码或 Premium 通知。
- 首次接入账号时已经存在的提醒只作为基线保存，不触发自动换绑。
- 白名单账号只通知管理员，不自动换绑。
- 每个账号可以设置 `0-720` 小时的等待窗口；`0` 表示立即换绑。
- Premium 通知始终立即触发；如果已有延迟窗口，会提前结束该窗口。
- 同一窗口内的新提醒会合并统计，不延长窗口，也不重复请求验证码。

### 收件后端

域名和接收后端可以在 Bot 的“安全防护 -> 邮箱域名管理”中维护。初始映射也可以写入 `.env`：

```env
LOGIN_EMAIL_ALIAS_DOMAINS=mail-a.example.com,mail-b.example.net
LOGIN_EMAIL_DOMAIN_BACKENDS=mail-a.example.com=cloudflare,mail-b.example.net=selfhosted
```

每个域名只能选择实际已经配置好的后端：

| 后端 | 适用场景 | 读取方式 |
| --- | --- | --- |
| `cloudflare` | Cloudflare Email Routing / TempMail Worker | 带共享密钥的 Webhook |
| `gmail` | Catch-all 转发到 Gmail | 只读 IMAP |
| `selfhosted` | 自建 TempMail 接收域名 MX | HMAC Webhook + Bearer API 轮询 |

自建 TempMail 的配置、接口契约、安全校验和回退方法见 [docs/selfhosted-tempmail.md](docs/selfhosted-tempmail.md)。

### 关键时间配置

```env
LOGIN_EMAIL_POLL_TIMEOUT_SECONDS=300
LOGIN_EMAIL_POLL_INTERVAL_SECONDS=3
LOGIN_EMAIL_CATCHUP_SECONDS=600
SERVICE_MONITOR_INTERVAL_SECONDS=300
```

- `POLL_TIMEOUT`：一次换绑等待验证码的总时长。
- `POLL_INTERVAL`：换绑期间查询邮件后端的间隔。
- `CATCHUP`：Session 重连后补拉近期 `777000` 提醒的时间窗口。
- `SERVICE_MONITOR_INTERVAL`：检查并恢复 active Session 连接的周期。

## Mini App

Mini App 是与管理 Bot 共用后端的移动网页控制台，覆盖账号、登录、Session、资料、隐私、2FA、登录邮箱、服务消息、目标授权、速率和批量任务等核心功能。

```env
MINI_APP_ENABLED=true
MINI_APP_HOST=127.0.0.1
MINI_APP_PORT=8080
MINI_APP_PUBLIC_URL=https://your-domain.example/mini-app
MINI_APP_AUTH_MAX_AGE_SECONDS=600
```

生产环境应让应用只监听本地回环，再通过 Nginx、Caddy 或 Cloudflare Tunnel 提供 HTTPS。`MINI_APP_PUBLIC_URL` 必须是 Telegram 客户端可访问的完整 `/mini-app` 地址。

Mini App 的静态页面可以公开加载，但所有业务 API 都会验证：

1. Telegram 注入的 `initData` HMAC 签名
2. `auth_date` 是否在允许时间内
3. Telegram 用户 ID 是否属于 active 管理员

直接使用普通浏览器调用 `/mini-app/api/*` 返回 401 是正常行为。公开 URL 不是密码，不能替代 Telegram 签名验证和管理员授权。

## 批量操作安全

批量操作必须先把目标加入授权列表，避免误操作未批准的用户、频道或群组。速率配置格式为：

```text
/rate set <scope> <max_actions> <per_seconds> <jitter_min> <jitter_max>
```

例如：

```text
/rate set batch 5 60 2 6
```

表示批量任务每 60 秒最多执行 5 个动作，每个动作再随机等待 2 到 6 秒。Telegram 仍可能根据账号、目标和操作类型施加额外限制。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `/start`、`/menu` | 打开管理菜单 |
| `/app` | 打开 Mini App |
| `/status` | 查看系统状态 |
| `/accounts` | 管理账号 |
| `/login`、`/qr_login` | 添加账号 |
| `/import_session`、`/export_session` | 导入或导出 Session |
| `/security` | 登录邮箱保护、域名和白名单 |
| `/service_monitor_on` | 连接 active 账号并启动实时监听 |
| `/service_monitor_off` | 停止监听并断开账号 |
| `/target_allowlist` | 管理批量任务授权目标 |
| `/rate` | 查看或调整操作速率 |
| `/backup` | 创建 PostgreSQL 备份 |
| `/help` | 查看完整命令说明 |

## 数据与安全

- `.env`、Bot Token、API Hash、数据库密码、Fernet 密钥、Session、Webhook 密钥和邮件 API Key 不得提交到 Git。
- `.env` 建议权限为 `600`，应用使用独立的非 root 系统用户运行。
- PostgreSQL 和应用 HTTP 端口默认只监听本地回环。
- Mini App 请求使用 Telegram `initData` 签名和管理员身份双重检查。
- 自建 TempMail Webhook 使用 HMAC-SHA256、时间窗口、投递 UUID、精确域名和幂等键。
- Session 导出文件等同于登录凭据，使用后应立即移入安全存储并删除临时副本。
- 批量任务只允许访问显式授权目标，并受速率与随机延迟控制。

## 测试

```bash
python -m pip install -r requirements-dev.txt
PYTHONPATH=. pytest -q
python -m compileall -q app tests alembic
```

GitHub Actions 会在 push 和 pull request 时运行测试与编译检查。

## 项目结构

```text
app/bot/           Telegram 管理 Bot
app/tg/            Telethon 账号操作与连接池
app/services/      邮箱保护、任务、速率、审计等业务逻辑
app/webapp/        Mini App HTTP API 与静态界面
app/db/            SQLAlchemy 模型和数据库连接
alembic/           PostgreSQL 数据库迁移
ops/               Debian 安装脚本和 systemd unit
tests/             自动化测试
```

## 升级与回退

升级前应备份 PostgreSQL 和 `.env`，保留原 Fernet 密钥，然后执行：

```bash
git pull --ff-only
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/alembic upgrade head
sudo systemctl restart tg-account-bot
```

完整的安装、升级、备份、恢复和故障排查流程见 [DEPLOY.md](DEPLOY.md)。

## License

本项目采用 [MIT License](LICENSE)。
