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

认证路由在 development、test 和 production 环境均注册。开发模拟任务路由仍只在 development/test 注册，但现在也必须提供合法 Bearer token；客户端不再能够通过请求体或查询参数声明 `user_id`。

## 2. 数据模型

`0002_user_auth` 增加：

- `users`：规范化用户名、规范化邮箱、密码哈希、角色、启用状态、邮箱验证状态和登录时间；
- `refresh_sessions`：refresh token 的 SHA-256、token family、绝对到期时间、使用/撤销时间和轮换后继；
- `reconstruction_jobs.user_id → users.id` 外键，阻止任务引用不存在的用户。

数据库从不保存明文密码或明文 refresh token。Access token 只包含用户 UUID、token 类型、签发/到期时间、issuer、audience 和随机 `jti`，不包含邮箱、用户名或密码信息。

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
```

Access token 不存数据库，退出后可能继续有效至短 TTL 结束；退出会立即阻止继续刷新。需要即时撤销 access token 时，应另行引入 token version 或短期 denylist，当前首期采用 15 分钟窗口。

## 4. 密码与登录规则

- 用户名规范化为小写，长度 3–32，只允许字母、数字、点、下划线和连字符；
- 邮箱使用 `email-validator` 做离线语法和 Unicode 规范化，不在请求中执行 DNS 查询；
- 密码长度 12–128，使用 `pwdlib` 推荐的 Argon2id 参数；
- 用户不存在时仍执行一次 dummy Argon2 校验，降低通过响应时间枚举账号的风险；
- 登录失败统一返回“用户名/邮箱或密码错误”；
- 被停用用户不能登录、刷新或调用受保护接口。

当前不检查泄露密码库，也没有登录限流。公开互联网部署前必须补充 IP/账号双维度限流、失败退避、审计事件和必要时的验证码。

## 5. Cookie 与环境约束

Refresh Cookie 属性：

- `HttpOnly`：浏览器脚本不可读取；
- `SameSite=Lax`：降低跨站请求携带风险；
- `Path=/api/v1/auth`：不会发送给任务、静态资源等其他路径；
- production 强制 `Secure=true`，只能经 HTTPS 发送；
- development/test 默认允许 `Secure=false`，仅用于 `127.0.0.1` HTTP 联调。

公开部署仍应校验可信 Origin、配置严格 CORS、全站 HTTPS，并在反向代理层设置安全响应头。SameSite 不能替代全部 CSRF 防护。

## 6. 本地配置

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
```

production 环境如果设置 `REFRESH_COOKIE_SECURE=false`，应用会拒绝启动。示例占位 JWT secret 或不足 32 字节的 secret 同样会被拒绝。

## 7. 本地调用示例

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

## 8. 尚未完成

- 邮箱验证令牌和邮件发送；
- 密码重置和修改密码后撤销全部会话；
- 登录/注册限流、验证码和安全审计；
- 邮箱验证状态与前端提交权限联动；
- 管理员停用用户的受保护接口；
- 多设备会话列表与单独撤销；
- 生产环境密钥管理和 JWT secret 轮换。

因此当前认证实现可以支持后续前后端联调，但还不是公开互联网部署的完整安全控制集。
