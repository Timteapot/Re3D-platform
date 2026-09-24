# React 前端开发说明

## 1. 当前页面

前端位于 `apps/web`，当前包含：

| 路由 | 页面 | 当前作用 |
|---|---|---|
| `/` | 主页 | 解释三分支重建流程和当前系统边界 |
| `/about` | 项目说明 | 技术说明、使用建议、非商业及第三方声明入口 |
| `/auth/register` | 注册 | 调用后端注册 API，进行基础表单校验 |
| `/auth/login` | 登录 | 获取短期 access token，并支持安全的登录后返回路径 |
| `/workspace` | 重建工作台 | 受保护页面；当前展示模块就绪状态，上传尚未接入 |

工作台未伪造上传或重建功能。没有上传 API 时，按钮保持禁用并明确标记边界。

## 2. 会话模型

- access token 只保存在 React 内存状态，不写入 `localStorage`、`sessionStorage` 或可被第三方脚本读取的持久存储；
- refresh token 仅由后端写入 HttpOnly Cookie，前端请求使用 `credentials: include`，JavaScript 不读取 Cookie；
- 页面启动时调用 `POST /api/v1/auth/refresh` 恢复会话；
- 并发刷新使用同一个 in-flight Promise，避免 React StrictMode 或多个请求同时轮换同一 token；
- 未登录访问 `/workspace` 时跳转到登录页，返回路径只允许站内绝对路径，并拒绝协议相对、反斜杠和控制字符；
- 退出时无论后端响应是否成功都会清空内存状态，后端成功时同时撤销 refresh session。

当前 API 客户端只覆盖认证接口。下一阶段添加任务请求客户端时，应在 access token 过期后只进行一次刷新重试，避免无限重试。

## 3. 本地运行

先保证 FastAPI 运行在 `http://127.0.0.1:8000`，然后：

```powershell
cd D:\3Dreconstruction\Re3D-platform\apps\web
Copy-Item .env.example .env
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173`。Vite 将 `/api` 代理到 `VITE_API_PROXY_TARGET`，浏览器仍使用相对路径，因此开发环境不需要开放跨域 Cookie。生产环境应由同一站点的反向代理分别提供静态前端和 `/api`。

## 4. 验证命令

```powershell
npm run typecheck
npm test
npm run build
npm audit --audit-level=moderate
```

构建产物位于被 Git 忽略的 `apps/web/dist`。`package-lock.json` 必须提交，CI 和部署使用 `npm ci`，避免依赖解析漂移。

## 5. 当前边界

- 尚未实现图片上传、任务列表、任务详情、轮询/SSE、GLB 查看器和评估卡片；
- 邮箱验证、密码重置和限流尚未实现，因此不能公开注册；
- Vite 开发服务器只绑定 `127.0.0.1`，不能作为生产服务器；
- 生产静态文件、TLS、安全响应头、内容安全策略和反向代理规则尚未实现。
