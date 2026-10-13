# Re3D Platform

Re3D Platform 是基于 Re3D 多视图三维重建管线的非商业学习与工程实践项目，提供用户认证、图片上传、异步重建、三分支预览与下载，以及可解释的结构健康评估。当前部署范围为 Windows 本机或通过 SSH 回环转发访问的受限环境；公开互联网部署属于后续路线，尚未开放。

## 当前状态

状态核对日期：2026-10-13。代码实现、验收证据和目标机部署版本分别记录，不以本机测试通过替代服务器容量或公网安全验收。

| 范围 | 已具备能力 | 尚未完成 |
|---|---|---|
| 平台闭环 | 认证、邮箱验证、密码重置、上传、排队、进度、取消、评估、预览和下载 | 管理员 UI、验证码、完整失败样本与压力验收 |
| 受限服务器部署 | 独立服务账户；网页 11 图真实任务；不刷新详情页自动显示 Three.js 画布和模型 | 主机重启与可控故障恢复验收 |
| 管线容量 | Tesla T4 上 Lighthouse 150 图三分支断点续跑验收 | 300 图实际执行与连续资源观测 |
| 直接 300 图实验 | 输入校验完成；新增 restricted 专用 64/20 GiB 实验档和运行期低磁盘取消 | 新发布部署、服务包轮换及真实任务验收 |
| 公网发布 | production 模板、权限拆分和部署工具 | 合规入口、域名/TLS、生产邮件、边缘防护、集中监控和实机验收 |

最近已验收的服务器平台版本为 `29eda46`；直接 300 图更新尚未部署。本次受限实验与低盘保护的本机复核为后端 259 项、前端 30 项测试通过及候选构建成功。测试含受控进程树取消，不代表目标 GPU 容量或全部场景已验收。

活动算法基线为 `re3d-pipeline-v1.1.3`，提交 `e517a161b4c0895f8be4258fb339272e4f99e1d6`。首期固定运行 `A-v4`、`B-v2`、`C`；Tesla T4 使用 MapAnything `batch_size=4`，MVSAnywhere 从本地 DINOv2 Torch Hub 缓存加载。完整版本、配置与模型清单身份见 [固定基线](config/pipeline-baseline.json)，历史冻结版本保留而不覆盖。

## 前后端框架与运行架构

平台采用浏览器单页应用、独立 HTTP API 和独立 GPU Worker。重建不在浏览器或 API 请求进程内运行，API 与 Worker 通过 PostgreSQL 队列及仓库外任务文件对接。

```text
浏览器 React SPA
    │ 同源 /api 请求、鉴权 SSE、产物 Blob
    ▼
Caddy ── 静态 Web 文件
    │ 反向代理
    ▼
FastAPI / Uvicorn ── PostgreSQL（认证、任务、租约、配额、审计）
    │                         ▲ 领取 / 心跳 / 状态推进
    │ 上传与授权读取           │
    ▼                         │
仓库外任务文件 ◀──── 独立 Worker
                              ├─ Re3D：A-v4 / B-v2 / C
                              ├─ 产物校验与结构健康评估
                              └─ 资源采样与清理审计
```

### 前端：React、TypeScript 与 Vite

前端位于 `apps/web`，由 Vite 构建为静态 HTML/CSS/JavaScript。依赖由 [package.json](apps/web/package.json) 和 [package-lock.json](apps/web/package-lock.json) 固定。

| 组件 | 固定版本 | 项目职责 |
|---|---|---|
| React | 19.2.0 | 页面组件、表单交互和模型查看器生命周期 |
| TypeScript | 5.9.2 | 编译期类型检查、接口响应与状态类型 |
| Vite | 7.3.6 | 开发服务、模块加载、静态构建和代码分块 |
| React Router | 7.18.4 | 页面路由、共享布局、受保护工作台与登录返回路径 |
| TanStack Query | 5.89.0 | 任务/配额缓存、查询轮询、提交/取消操作和缓存更新 |
| Three.js | 0.186.1 | GLB 解析、WebGL 渲染、轨道控制、取景、光照和模型姿态 |
| Vitest / Testing Library | 5.0.1 / 16.3.0 | 请求、路由、组件与模型生命周期测试 |

模块分工：

- `main.tsx` 组合 QueryClient、BrowserRouter 和 AuthProvider；`App.tsx` 定义页面路由，`AppShell.tsx` 提供共享导航与布局。
- `pages/` 实现主页、项目说明、认证/恢复页面、上传工作台和任务详情。
- `auth/AuthContext.tsx` 管理内存 access token、会话恢复与刷新；refresh token 由后端签发并保存在浏览器的 HttpOnly Cookie 中，服务器保存会话及令牌摘要。access token 不写入 URL 或浏览器持久存储。
- `api/http.ts` 封装原生 Fetch、相对 `/api` 路径、Bearer 请求头和 Cookie。认证、任务和上传调用由各模块的 API 封装承担；TypeScript 类型当前手工维护，尚无 OpenAPI 自动生成流程。
- `jobs/events.ts` 通过带认证 Fetch 消费 SSE；详情页更新查询缓存，连接不可用时通过普通查询轮询恢复状态。
- `jobs/GlbViewer.tsx` 按需加载。GLB 先通过授权 Fetch 取得 Blob，再以临时对象 URL 交给 Three.js；切换分支或卸载组件时释放模型、纹理、渲染器与对象 URL。
- `styles.css` 为当前样式实现。UI 优化可以独立于后端接口和 GPU 执行开展。

| 页面 | 当前路径 | 作用 |
|---|---|---|
| 主页 | `/` | 项目入口与流程概览 |
| 项目说明 | `/about` | 技术、使用方式和第三方说明 |
| 注册/登录 | `/auth/register`、`/auth/login` | 用户认证 |
| 邮箱与密码恢复 | `/auth/verify-email`、`/auth/forgot-password`、`/auth/reset-password` | 一次性令牌操作 |
| 工作台 | `/workspace` | 图片上传、配额、提交和最近任务 |
| 任务详情 | `/workspace/jobs/:jobId` | 进度、取消、预览、下载和评估 |

Vite 开发服务仅绑定 `127.0.0.1:5173`，将 `/api` 代理到本机 API。部署使用静态构建和 Caddy SPA 回退，不使用 Vite 开发服务作为部署入口。详细结构见 [前端文档](docs/frontend.md)。

### 后端：FastAPI、PostgreSQL 与独立 Worker

后端使用 Python，API、Worker 和维护命令分进程运行。平台 Python 环境与 Re3D/CUDA 推理环境分离，不复制算法权重或算法运行环境。

| 组件 | 固定版本或已验收基线 | 项目职责 |
|---|---|---|
| FastAPI / Pydantic | 0.141.1 / 2.13.5 | HTTP 路由、请求/响应模型校验和 OpenAPI 描述 |
| Uvicorn | 0.53.0 | ASGI API 服务与 SSE 响应 |
| SQLAlchemy / psycopg | 2.0.34 / 3.2.3 | 数据模型、事务、数据库连接与并发锁 |
| Alembic | 1.13.3 | 数据库迁移；当前 head 为 `0016_expand_image_count_300` |
| PostgreSQL | 已验收环境为 18 | 认证、持久任务队列、资源租约、共享限流和审计 |
| pwdlib / PyJWT | 0.3.1 / 2.15.0 | Argon2id 密码哈希与短期 JWT access token |
| Pillow / jsonschema | 12.3.0 / 4.23.0 | 图片实际解码、像素限制与文件契约校验 |
| Caddy / WinSW / Mailpit | 2.11.4 / 2.12.0 / 1.31.3 | 同源入口、Windows 服务封装和受限认证邮件捕获 |

锁定 Python 依赖见 [API](requirements-api.txt)、[Worker](requirements-worker.txt) 和 [开发/迁移](requirements-dev.txt) 清单；Caddy、WinSW、Mailpit 是外部部署工具。

- `apps/api` 注册认证、上传、稳定任务访问、下载及管理员审计接口。稳定前缀为 `/api/v1/auth`、`/api/v1/uploads`、`/api/v1/jobs` 和 `/api/v1/admin/audit`。
- `backend/auth` 验证用户身份、邮箱所有权和可撤销会话；业务模块检查任务所有权。认证限流、来源 IP 限流、任务限制、存储配额与传输额度由数据库共享。
- `backend/uploads` 使用独立上传会话保存半成品。提交时复核图片、哈希与额度，冻结输入/request 清单，再创建 queued 任务。API 不同步运行 GPU 重建。
- `backend/db` 管理任务状态与 `gpu:0` 租约。Worker 原子领取、续租并持有 fencing token；失效 Worker 不得提交状态或终态。单 GPU 并发固定为 1。
- `apps/worker` 与 `backend/worker` 消费队列；`backend/re3d_adapter` 检查固定管线、输入和路径，监督取消/超时及真实进程树，归档三分支产物。可选独立低磁盘保护在空闲达到安全线或探测失败时取消任务；受限 300/20 实验档强制启用，标准档默认不改变。
- `backend/evaluation` 生成版本化 `evaluation.json`，当前只评估结构健康，不测量有真值的几何精度，综合分数为 `null`。
- `backend/monitoring` 保存任务资源与目录空间样本；`apps/maintenance` 提供就绪检查、审计维护、容量汇总和显式清理命令。

数据库保存认证、上传元数据、任务、租约和审计；图片、日志、结果与评估正文位于 `RE3D_DATA_ROOT/jobs/<job_uuid>`。当前使用本地文件系统；对象存储和签名下载 URL 尚未实现。共享 JSON Schema 位于 [packages/contracts](packages/contracts/README.md)，并不自动生成前后端类型。

## 环境与项目约束

| 环境 | 用途与边界 |
|---|---|
| `development` / `test` | 支持模拟链路与显式真实任务；开发专用路由只在这两类环境注册 |
| `restricted` | real-only；API、Web、数据库、SMTP 和可信代理限定回环地址；独立数据库与数据根 |
| `production` | 真实任务及公网安全配置要求；已有模板和工具，尚未完成公网验收 |

- 仅用于非商业学习与工程实践；第三方代码、模型和数据的许可与署名要求仍需分别遵守。
- 用户须登录并验证邮箱后上传或提交任务，只能访问自己的任务与产物。
- JPEG/PNG 默认限制为 3–300 张、单图 25 MiB、总量 1 GiB、单图最多 5000 万像素。支持上限不代表任意 300 图任务已通过容量验收。
- 每个任务固定 A-v4、B-v2、C；普通用户不能提交任意管线参数、命令或服务器路径。
- 存储按未提交输入实际字节和已提交任务固定预留量记账，不涉及商业收费。预留是准入机制，不是运行中磁盘硬封顶。
- 应用层上传/下载令牌桶和并发租约不提供网络 Mbps 整形或边缘抗攻击能力。
- 失败/取消任务经过宽限期与终态持久化后清理任务目录，保留最小审计。
- 成功任务输入、运行文件和产物采用 30 天保留策略；自动周期仅受审计 dry-run。实际删除需要独立双确认命令，不能宣称到期自动删除。
- 密钥、真实 `.env`、数据库、用户文件、模型权重和运行产物不得提交 Git；数据目录位于代码仓库之外。

平台、算法和数据分别维护。当前本机目录为 `D:\3Dreconstruction\Re3D-platform`、`D:\3Dreconstruction\Re3D`、`D:\3Dreconstruction\Re3D-data`；目标机路径由配置和部署清单指定。

## 本机开发与验证

开发使用 Python 3.12 平台虚拟环境、Node.js ≥22.12.0、锁定前端依赖和 PostgreSQL。数据库集成测试使用临时 Docker PostgreSQL，禁止复用开发或受限数据库。

1. 按 [数据库文档](deploy/postgres/README.md) 准备独立数据库与角色。
2. 按 [开发环境文档](deploy/development/README.md) 初始化私密配置并检查环境；已有配置不得覆盖。
3. 按 [API/Worker 文档](docs/development-api-worker.md) 启动进程，按 [Mailpit 文档](deploy/mailpit/README.md) 准备认证邮件，按 [前端文档](docs/frontend.md) 安装依赖并启动页面。

```powershell
# 平台根目录：单元测试使用临时文件和 SQLite
.\.venv\Scripts\python.exe -m unittest discover -s tests\unit

# apps/web 目录
npm test
npm run build
```

`npm run build` 用于构建检查；部署发布须使用 [build-web.ps1](deploy/production/build-web.ps1) 生成绑定干净 Git 提交的 `deployment.json`。Windows 脚本可使用临时进程执行策略，无需修改全局策略。

## 后续计划

1. **直接 300 图受限验收**：采用 `gpu-experiment-300-20`，核验目标机实际空闲磁盘 ≥84 GiB、总显存 ≥14 GiB、空闲显存 ≥12 GiB，队列/租约空闲；通过后应用独立预设并重新生成服务包。用户配额 128 GiB、任务预留 64 GiB、安全线 20 GiB、执行预算 4 小时、资源/目录采样 5/15 秒，低磁盘保护每 1 秒独立检查。标准 `gpu-large-300` 仍要求 139 GiB；受限实验不改变其下限。结果用于校准容量，不预先承诺耗时或成功。
2. **可恢复部署封装**：保存版本锁、工具与模型哈希、离线依赖、非秘密模板和受保护备份，补齐 restricted 数据库恢复演练。功能发布包不能替代完整环境封装。
3. **实例释放前验收**：验证配置重建、数据恢复和新环境真实小任务后，再决定释放旧实例；补充服务/主机重启与故障恢复验收。
4. **本机 UI 优化**：保持接口和权限合同，优化主页、按钮、动效、布局、加载/错误状态与响应式体验，使用模拟任务和已归档 GLB，不依赖服务器持续开机。
5. **公网阶段（暂缓）**：另行完成合规入口、真实域名/TLS、生产 SMTP、验证码、管理员二次验证、集中告警、保留策略观察和压力测试。

## 文档导航

| 文档 | 职责 |
|---|---|
| [开发进度](docs/progress.md) | 当前完成度、验收层级与阻断项 |
| [项目计划](PROJECT_PLAN.md) | 有效范围、后续阶段与验收条件 |
| [模块集成](docs/module-integration.md) | 运行时数据流、模块边界与对接 |
| [前端](docs/frontend.md) | 前端结构与交互 |
| [认证](docs/authentication.md)、[任务](docs/job-api.md)、[上传](docs/image-uploads.md) | 当前 API 和业务合同 |
| [下载](docs/artifact-downloads.md)、[查看器](docs/glb-viewer.md) | 授权产物与三维预览 |
| [受限部署](deploy/restricted/README.md)、[生产部署](deploy/production/README.md) | 操作与环境要求 |
| [直接 300 图准备](docs/direct-300-local-readiness-2026-10-13.md) | 输入、配置与应用顺序 |
| [低磁盘自动取消](docs/low-disk-guard.md) | 独立保护、取消原因与限制 |
| [第三方声明](THIRD_PARTY_NOTICES.md) | 平台依赖及算法声明来源 |

带日期的验收记录保存当时版本、输入和结果，不作为持续更新的当前状态说明。状态以进度文档为准，操作以对应模块或部署文档为准。

活跃文档维护项目介绍、实现状态、有效计划、使用说明与约束；采用客观的角色和系统描述，不保存对话式交付叙述。计划明确标注未实现或未验收事项，操作说明保留必要的安全提示与确认步骤。
