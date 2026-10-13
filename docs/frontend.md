# 前端框架、页面与集成边界

## 1. 框架与代码结构

前端是位于 `apps/web` 的 React 单页应用，不承担任务调度或重建计算。依赖版本由 `package.json` 和 `package-lock.json` 固定；开发和构建要求 Node.js 22.12.0 或更高版本，浏览器访问静态产物不依赖 Node.js 服务。

| 技术 | 固定版本 | 职责与使用位置 |
|---|---|---|
| React / React DOM | 19.2.0 | 组件渲染、Hook 和页面交互；`main.tsx` 使用 `createRoot` 和 `StrictMode` |
| TypeScript | 5.9.2 | 页面、请求响应、认证状态和查看器模型的静态类型检查 |
| Vite | 7.3.6 | 本地开发、同源 API 代理、构建和按需代码分包；不作为部署后的 HTTP 服务 |
| React Router | 7.18.4 | `BrowserRouter`、声明式路由和受保护页面入口 |
| TanStack Query | 5.89.0 | 服务端状态缓存、异步查询、提交/取消 mutation、任务列表和详情更新 |
| Three.js | 0.186.1 | WebGL 渲染、GLTFLoader、OrbitControls、模型姿态和包围盒适配 |
| Vitest / Testing Library / jsdom | 依赖锁定文件 | 页面、认证、请求、事件和查看器生命周期回归测试 |

API 使用浏览器原生 `fetch`，没有 Axios；认证状态使用 React Context，没有 Redux。界面使用 `src/styles.css` 和原生组件样式，没有引入独立 UI 组件框架。主要模块为：

```text
src/
├── main.tsx                  # QueryClient、路由、认证 Provider 与样式入口
├── App.tsx                   # 页面路由
├── auth/                     # 内存会话、刷新轮换、站内返回路径、路由保护
├── api/http.ts               # Fetch、Bearer 请求头、Cookie 和统一 HTTP 错误
├── pages/                    # 主页、说明、账号操作、工作台与任务详情
├── jobs/                     # 上传/任务 API、SSE、GLB 分支及模型显示逻辑
├── components/AppShell.tsx   # 非认证页面的共享布局
└── styles.css                # 共享样式和响应式布局
```

`AuthContext` 管理登录身份和 access token；TanStack Query 管理任务等服务端数据；页面内部 `useState` 管理表单、上传选择、下载状态和查看器交互。查询键包含用户 ID，避免不同登录身份复用同一任务缓存。查询默认失败重试一次，不在窗口重新聚焦时自动刷新；工作台和任务详情另行设置周期查询。

## 2. 页面路由

前端位于 `apps/web`，当前包含：

| 路由 | 页面 | 当前作用 |
|---|---|---|
| `/` | 主页 | 解释三分支重建流程和当前系统边界 |
| `/about` | 项目说明 | 技术说明、使用建议、非商业及第三方声明入口 |
| `/auth/register` | 注册 | 调用后端注册 API，进行基础表单校验 |
| `/auth/login` | 登录 | 获取短期 access token，并支持安全的登录后返回路径 |
| `/auth/verify-email` | 邮箱验证 | 消费邮件中的一次性令牌并解锁上传权限 |
| `/auth/forgot-password` | 密码找回 | 使用统一响应申请密码重置邮件 |
| `/auth/reset-password` | 密码重置 | 消费一次性令牌并更新密码 |
| `/workspace` | 重建工作台 | 受保护页面；分批上传、删图、取消、按服务端环境策略提交任务并轮询最近任务 |
| `/workspace/jobs/:jobId` | 任务详情 | 受保护页面；实时进度、任务取消、产物下载、三分支 GLB 预览和自动评估摘要 |

工作台上传真实 JPEG/PNG；development/test 默认创建 simulated 任务并明确标记模拟结果，restricted/production 由服务端强制创建 real 任务。任务详情不直接接收服务器文件路径，只消费后端经过所有权校验和契约裁剪的摘要。三维查看器通过带 Bearer 令牌的 Fetch 获取 GLB Blob，再交给按需加载的 Three.js 模块；令牌不进入模型 URL。

## 3. API、实时状态与三维预览

前端通过相对路径 `/api/v1/auth`、`/api/v1/uploads` 和 `/api/v1/jobs` 与 FastAPI 对接。API 负责鉴权、上传校验、配额、队列和结果摘要；PostgreSQL 负责持久化状态；独立 Worker 调用固定 Re3D 管线并生成评估。浏览器不直接访问数据库、任务目录或 GPU。

任务详情同时使用 5 秒轮询和带 Bearer 认证的 SSE Fetch 流。SSE 将数据库状态投影更新到 Query 缓存；观察到终态时，页面重新查询完整详情和列表，以取得已经完成的结果及评估摘要。轮询作为断线降级，不需要通过刷新页面取得成功结果。

`JobDetailPage` 仅在具有结果摘要时通过 React `lazy` 加载 `GlbViewer`。查看器使用 `GLTFLoader` 解析认证下载得到的完整 GLB，使用 `OrbitControls` 实现旋转、缩放和平移。显示层会进行模型居中、默认坐标姿态校正、启发式初始视角和纹理显示处理，并提供三个轴向角度调整；这些操作不修改服务器产物或评估指标。预览可用性从无变为有时会初始化画布，切换分支和离开页面时释放旧资源。具体边界见 [`glb-viewer.md`](glb-viewer.md)。

## 4. 会话模型

- access token 只保存在 React 内存状态，不写入 `localStorage`、`sessionStorage` 或可被第三方脚本读取的持久存储；
- refresh token 仅由后端写入 HttpOnly Cookie，前端请求使用 `credentials: include`，JavaScript 不读取 Cookie；
- 页面启动时调用 `POST /api/v1/auth/refresh` 恢复会话；
- 并发刷新使用同一个 in-flight Promise，避免 React StrictMode 或多个请求同时轮换同一 token；
- 未登录访问 `/workspace` 时跳转到登录页，返回路径只允许站内绝对路径，并拒绝协议相对、反斜杠和控制字符；
- 退出时无论后端响应是否成功都会清空内存状态，后端成功时同时撤销 refresh session。
- SSE 使用带 Bearer 请求头的 Fetch 流，不在查询参数、浏览器历史或代理日志中放置 access token；连接不可用时以 5 秒轮询降级。

认证后的 API 客户端在 access token 过期后合并 refresh 请求并只重试原请求一次，避免并发轮换和无限重试。

## 5. 本地运行

先保证 FastAPI 运行在 `http://127.0.0.1:8000`，然后：

```powershell
cd D:\3Dreconstruction\Re3D-platform\apps\web
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173`。Vite 将 `/api` 代理到 `VITE_API_PROXY_TARGET`，浏览器仍使用相对路径，因此开发环境不需要开放跨域 Cookie。生产环境应由同一站点的反向代理分别提供静态前端和 `/api`。

## 6. 验证命令

```powershell
npm run typecheck
npm test
npm run build
npm audit --audit-level=moderate
```

构建产物位于被 Git 忽略的 `apps/web/dist`。`package-lock.json` 必须提交，CI 和部署使用 `npm ci`，避免依赖解析漂移。

## 7. 当前边界与后续工作

- 已实现分批上传、单张删除、上传取消、提交、最近任务轮询、任务详情/取消、SSE 状态流、评估摘要、带认证的产物下载和三分支 GLB 查看器；尚未实现刷新后恢复未提交会话；
- 查看器会把完整 GLB 暂存在浏览器内存中；当前真实 11 图产物可用，扩大输入规模前需评估 Range 请求、渐进加载或对象存储；
- 登录、注册、邮箱验证和密码重置前后端流程及限流已经实现；上传提交已接入单用户待处理任务数、滚动 24 小时限制和存储配额，普通 API 也已接入来源 IP 共享限流；公开注册仍须另行完成生产邮件服务、容量校准、防滥用措施和公网安全验收；
- Vite 开发服务器只绑定 `127.0.0.1`，不能作为生产服务器；
- 已实现 Caddy 生产静态文件、同源 API 代理、自动 TLS 入口、安全响应头和内容安全策略，并完成回环地址上的非公网拓扑验收；真实域名、证书签发和外部访问尚未验收。

受限部署通过回环地址和 SSH 转发访问，不以公网发布为当前目标。后续 UI 优化可以在本机调整主页、按钮、动效、布局和响应式显示，保持现有 API、鉴权、任务状态与结果契约不变。
