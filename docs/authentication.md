# 用户认证与刷新会话

## 1. 当前实现

认证 API 已提供以下接口：

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/v1/auth/register` | 创建普通用户，保存 Argon2id 密码哈希 |
| POST | `/api/v1/auth/login` | 校验用户名或邮箱，签发 access token 和 refresh Cookie |
| POST | `/api/v1/auth/refresh` | 原子轮换 refresh token 并签发新 access token |
| POST | `/api/v1/auth/logout` | 撤销当前 refresh session 并清除 Cookie |
| GET | `/api/v1/auth/me` | 使用 Bearer access token 返回当前用户 |
| POST | `/api/v1/auth/email-verification/request` | 登录后申请邮箱验证邮件 |
| POST | `/api/v1/auth/email-verification/confirm` | 消费一次性令牌并确认邮箱 |
| POST | `/api/v1/auth/password-reset/request` | 使用统一响应申请密码重置邮件 |
| POST | `/api/v1/auth/password-reset/confirm` | 消费一次性令牌、更新密码并撤销刷新会话 |

认证路由和稳定 `/api/v1/jobs` 任务访问路由在 development、test 和 production 环境均注册。开发模拟任务创建路由仍只在 development/test 注册，但也必须提供合法 Bearer token；客户端不能通过请求体或查询参数声明 `user_id`。

登录与邮箱验证是两个独立权限层级。未验证用户仍可登录、刷新会话、读取和取消自己的已有任务；创建上传会话、上传图片、提交上传以及调用开发模拟任务创建接口必须通过 `email_verified` 服务端校验，否则统一返回 403。前端禁用入口只是交互提示，不能替代这一 API 权限边界。

## 2. 数据模型

`0002_user_auth` 增加：

- `users`：规范化用户名、规范化邮箱、密码哈希、角色、启用状态、邮箱验证状态和登录时间；
- `refresh_sessions`：refresh token 的 SHA-256、token family、绝对到期时间、使用/撤销时间和轮换后继；
- `reconstruction_jobs.user_id → users.id` 外键，阻止任务引用不存在的用户。

`0005_auth_audit_throttle` 增加：

- `auth_events`：注册、登录、刷新与退出的成功、失败、阻断和 refresh token 重放事件；
- `auth_throttle_buckets`：跨 API 进程共享的账户/IP 固定窗口失败计数和阻断期限。

`0006_registration_throttle` 增加：

- `auth_registration_buckets`：跨 API 进程共享的用户名、邮箱和 IP 注册尝试计数及阻断期限。

`0007_auth_action_tokens` 增加：

- `auth_action_tokens`：邮箱验证和密码重置令牌的 SHA-256、用途、到期、消费和撤销状态；
- `auth_action_request_buckets`：验证邮件和密码重置请求的身份/IP 共享限流；
- 将认证事件扩展到 `email_verification` 和 `password_reset`。

数据库从不保存明文密码或明文 refresh token。审计表不保存原始用户名、邮箱、IP 或 User-Agent：登录标识符和 IP 使用 JWT secret 做带域分离的 HMAC-SHA-256，User-Agent 只保存 SHA-256。Access token 只包含用户 UUID、token 类型、签发/到期时间、issuer、audience 和随机 `jti`，不包含邮箱、用户名或密码信息。轮换 JWT secret 会同时改变后续审计指纹，因此跨轮换周期不能直接用指纹关联事件；正式密钥轮换方案需同时定义审计关联边界。

## 3. Token 流程

```text
登录
  ├─ access token：HS256 JWT，默认 15 分钟，响应 JSON 返回
  └─ refresh token：随机 48 字节，HttpOnly Cookie，数据库只存 SHA-256

刷新
  ├─ 锁定当前 refresh session
  ├─ 撤销旧 token
  ├─ 在同一 family 中创建新 token，保持原始绝对到期时间
  └─ 返回新 access token 并覆盖 Cookie

旧 token 重放
  └─ 撤销该 family 尚未撤销的所有 refresh session

邮箱验证/密码重置
  ├─ 生成 48 字节随机一次性令牌
  ├─ 数据库只保存 SHA-256，原令牌只进入待发送邮件
  ├─ 新请求撤销同一用户、同一用途的旧令牌
  ├─ 邮件链接把令牌放在 URL fragment，避免随首个页面请求发送给服务器
  └─ 成功消费后立即失效；密码重置同时确认邮箱并撤销该用户全部 refresh session
```

Access token 不存数据库，退出后可能继续有效至短 TTL 结束；退出会立即阻止继续刷新。需要即时撤销 access token 时，应另行引入 token version 或短期 denylist，当前首期采用 15 分钟窗口。

JWT 验证允许最多 5 秒的主机时钟偏差，用于容纳 API 与 PostgreSQL/Docker 运行在不同系统时的亚秒级时间差。签名、签发方、受众、token 类型和到期时间仍必须通过验证；超过 5 秒的未来签发令牌会被拒绝。

## 4. 密码与登录规则

- 用户名规范化为小写，长度 3–32，只允许字母、数字、点、下划线和连字符；
- 邮箱使用 `email-validator` 做离线语法和 Unicode 规范化，不在请求中执行 DNS 查询；
- 密码长度 12–128，使用 `pwdlib` 推荐的 Argon2id 参数；
- 用户不存在时仍执行一次 dummy Argon2 校验，降低通过响应时间枚举账号的风险；
- 登录失败统一返回“用户名/邮箱或密码错误”；
- 被停用用户不能登录、刷新或调用受保护接口。

当前不检查泄露密码库；公开互联网部署前仍需评估泄露密码检查和必要时的验证码。

## 5. 登录、注册限流与审计

默认策略：

- 15 分钟窗口内，同一账户允许 5 次失败；用户名和邮箱会归并到同一内部用户桶；
- 同一客户端 IP 允许 30 次失败，用于限制攻击者轮换用户名；
- 达到阈值的本次请求仍返回统一的 401，后续请求在 15 分钟阻断期内返回 429 和 `Retry-After`；
- 登录成功只重置账户桶，不重置 IP 桶，避免攻击者用自己的有效账户清除来源限制；
- 未知用户也进入基于规范化标识符的账户桶并执行 dummy Argon2 校验；
- 计数保存在 PostgreSQL，可由多个 API 进程共享，服务重启不会清除阻断状态。

客户端 IP 默认取 TCP 对端，不直接相信 `X-Forwarded-For`。只有对端位于 `AUTH_TRUSTED_PROXY_CIDRS` 时，才从代理链右向左跳过可信代理并选择第一个不可信地址。production 必须显式配置可信代理网段，否则应用拒绝启动；不能把 `0.0.0.0/0` 或任意公网范围配置为可信代理。

审计记录覆盖注册、登录、refresh token 轮换/重放和有效 refresh session 的退出。随机伪造或缺失的 refresh Cookie 不写入数据库，避免未认证请求无限制造审计记录。

注册默认使用 60 分钟窗口：同一规范化用户名或邮箱允许 3 次有效注册尝试，同一客户端 IP 允许 10 次。成功注册、重复用户名/邮箱等通过请求模型和服务校验的尝试都会计数，因此不能通过批量创建不同账户或重复触发唯一约束绕过；达到阈值后的后续请求返回 429 和 `Retry-After`。请求模型直接拒绝的畸形请求不写数据库，仍应由反向代理承担连接级和通用请求速率限制。

验证邮件和密码重置申请也使用 60 分钟窗口：同一用户/邮箱允许 3 次、同一 IP 允许 10 次。登录用户请求验证邮件超过限制时返回 429；密码重置申请无论邮箱是否存在、是否受限都返回相同 202 文本，降低账户枚举风险。已知账户的邮件通过响应后的后台任务发送，SMTP 延迟不进入客户端响应主体。

认证维护命令默认删除 90 天前的认证事件、已终止或过期 7 天的一次性令牌，并删除 7 天未更新且不在阻断期的登录/注册/认证邮件请求限流桶。每张表每次最多处理 1000 行，重复运行直到删除数为零即可完成积压清理：

```powershell
.\.venv\Scripts\python.exe -m apps.maintenance.main cleanup-auth-security
```

可通过 `--event-retention-days`、`--action-token-retention-days`、`--throttle-retention-days` 和 `--limit` 临时覆盖配置。该命令只清理认证安全表，不删除用户、refresh session、重建任务或任务文件。正式部署时应由计划任务定期运行并监控返回的 JSON 结果。受管理员权限保护的只读审计 API 已实现，脱敏字段和首名管理员引导见 [`admin-audit.md`](admin-audit.md)；管理员前端、二次验证和细分权限仍未实现。

## 6. Cookie 与环境约束

Refresh Cookie 属性：

- `HttpOnly`：浏览器脚本不可读取；
- `SameSite=Lax`：降低跨站请求携带风险；
- `Path=/api/v1/auth`：不会发送给任务、静态资源等其他路径；
- production 强制 `Secure=true`，只能经 HTTPS 发送；
- development/test 默认允许 `Secure=false`，仅用于 `127.0.0.1` HTTP 联调。

公开部署仍应校验可信 Origin、配置严格 CORS、全站 HTTPS，并在反向代理层设置安全响应头。SameSite 不能替代全部 CSRF 防护。

## 7. 本地配置

生成至少 32 字节的随机签名密钥：

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

将输出写入未提交的 `.env`：

```dotenv
JWT_SECRET=<随机值>
JWT_ISSUER=re3d-platform
JWT_AUDIENCE=re3d-platform-api
ACCESS_TOKEN_TTL_MINUTES=15
REFRESH_TOKEN_TTL_DAYS=7
REFRESH_COOKIE_NAME=re3d_refresh
REFRESH_COOKIE_SECURE=false
AUTH_LOGIN_WINDOW_MINUTES=15
AUTH_LOGIN_ACCOUNT_MAX_FAILURES=5
AUTH_LOGIN_IP_MAX_FAILURES=30
AUTH_LOGIN_BLOCK_MINUTES=15
AUTH_REGISTRATION_WINDOW_MINUTES=60
AUTH_REGISTRATION_IDENTITY_MAX_ATTEMPTS=3
AUTH_REGISTRATION_IP_MAX_ATTEMPTS=10
AUTH_REGISTRATION_BLOCK_MINUTES=60
AUTH_ACTION_REQUEST_WINDOW_MINUTES=60
AUTH_ACTION_REQUEST_IDENTITY_MAX_ATTEMPTS=3
AUTH_ACTION_REQUEST_IP_MAX_ATTEMPTS=10
AUTH_ACTION_REQUEST_BLOCK_MINUTES=60
AUTH_EMAIL_VERIFICATION_TTL_HOURS=24
AUTH_PASSWORD_RESET_TTL_MINUTES=30
AUTH_TRUSTED_PROXY_CIDRS=
AUTH_EVENT_RETENTION_DAYS=90
AUTH_ACTION_TOKEN_RETENTION_DAYS=7
AUTH_THROTTLE_RETENTION_DAYS=7
AUTH_CLEANUP_BATCH_SIZE=1000
APP_PUBLIC_BASE_URL=http://localhost:5173
SMTP_HOST=127.0.0.1
SMTP_PORT=1025
SMTP_FROM=no-reply@example.invalid
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_STARTTLS=false
SMTP_TIMEOUT_SECONDS=10
```

development/test 允许不配置 `SMTP_HOST`，此时请求仍创建随后会被标记投递失败的令牌，不会把明文令牌输出到响应或日志。本地可通过 `deploy/mailpit/start-local.ps1` 启动只绑定回环地址的 Mailpit，并用 `deploy/mailpit/run-acceptance.ps1` 验证注册、SMTP 投递、邮箱确认、任务权限解锁和密码重置闭环。production 除既有 Cookie、可信代理和 JWT secret 检查外，还强制要求 SMTP 主机、STARTTLS、非 `.invalid` 发件地址和 HTTPS `APP_PUBLIC_BASE_URL`，否则应用拒绝启动。

## 8. 本地调用示例

```powershell
$password = "replace-with-a-local-test-password"
$registerBody = @{
    username = "local-user"
    email = "local-user@example.com"
    password = $password
} | ConvertTo-Json

$user = Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/auth/register `
  -ContentType application/json `
  -Body $registerBody

$loginBody = @{
    identifier = "local-user"
    password = $password
} | ConvertTo-Json

$login = Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/auth/login `
  -ContentType application/json `
  -Body $loginBody `
  -SessionVariable authSession

$headers = @{ Authorization = "Bearer $($login.access_token)" }
Invoke-RestMethod `
  -Uri http://127.0.0.1:8000/api/v1/auth/me `
  -Headers $headers
```

不要把示例密码用于真实账号，不要把 access token、refresh Cookie 或 JWT secret 复制到日志和提交记录。

## 9. 尚未完成

- 验证码和审计查询权限；
- 生产 SMTP 提供商选择、退信处理和邮件送达监控；
- 管理员停用用户的受保护接口；
- 多设备会话列表与单独撤销；
- 生产环境密钥管理和 JWT secret 轮换。

因此当前认证实现可以支持后续前后端联调，但还不是公开互联网部署的完整安全控制集。
