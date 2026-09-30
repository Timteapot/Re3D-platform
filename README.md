# Re3D Platform

Re3D Platform 是基于 Re3D 三维重建管线的非商业学习与工程实践项目。平台目标是为公开互联网用户提供注册、登录、多视图图片上传、异步三维重建、三分支结果查看和自动质量评估。

## 当前状态

阶段 0 的核心验收闭环已经完成，项目已完成阶段 4 的首个本机真实 GPU 闭环验收，并完成 Windows 单机生产拓扑的非公网验收，尚未进入公开部署阶段。

- Re3D 活动基线标签：`re3d-pipeline-v1.1.0`
- Re3D 活动基线提交：`2c5ba174dae9fe53dcec8f7d8466793fdebf0c58`
- 首个冻结基线：`re3d-pipeline-v1.0.0`，保持不变并保留历史清单
- 首期固定分支：`A-v4`、`B-v2`、`C`
- 当前开发环境：Windows
- 平台代码目录：`D:\3Dreconstruction\Re3D-platform`
- 运行数据目录：`D:\3Dreconstruction\Re3D-data`

基线的完整哈希和验证结果见 [`config/pipeline-baseline.json`](config/pipeline-baseline.json)，总体实施计划见 [`PROJECT_PLAN.md`](PROJECT_PLAN.md)，平台直接依赖声明见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

当前已经可以通过 React 页面注册、登录、恢复会话、上传真实 JPEG/PNG 输入并创建任务。development/test 的默认网页提交进入模拟队列并允许显式 real；production 由服务端强制创建 real 任务。独立的真实 Worker 在 PostgreSQL 租约下监督 Re3D 子进程，处理取消、总超时和租约丢失，并汇总三分支产物和结构健康评估。2026-09-25 已使用 11 张真实图片完成本机 GPU 闭环验收；任务详情页现可通过受所有权保护的稳定任务 API 下载清单内 GLB、OBJ、MTL 和纹理，并可在 Three.js 查看器中切换和交互预览三分支 GLB。任务 API 边界见 [`docs/job-api.md`](docs/job-api.md)，上传边界见 [`docs/image-uploads.md`](docs/image-uploads.md)，下载边界见 [`docs/artifact-downloads.md`](docs/artifact-downloads.md)，查看器边界见 [`docs/glb-viewer.md`](docs/glb-viewer.md)，实测证据见 [`docs/real-gpu-acceptance-2026-09-25.md`](docs/real-gpu-acceptance-2026-09-25.md)。

## 仓库边界

本仓库只保存 Web 前端、API、Worker、Re3D 适配器、评估模块、部署配置和测试。Re3D 源码、模型权重、用户上传、运行中间文件和重建产物均不复制到本仓库。

```text
apps/web                 React 前端
apps/api                 FastAPI API
apps/worker              GPU 任务执行器
backend/re3d_adapter     Re3D 运行契约与产物解析
backend/evaluation       自动评估规则
backend/monitoring       真实任务私有资源采样
packages/contracts       共享 Schema 与生成类型
packages/ui              可选共享 UI 组件
config                   固定基线和非敏感配置
deploy                   部署编排与代理配置
docs                     架构、决策和运维文档
tests                    集成、端到端与测试夹具
```

## 本地准备

1. 复制 `.env.example` 为 `.env`，只在本机填写密钥和数据库凭据。
2. 保证 `RE3D_ROOT` 指向已通过 `doctor.ps1` 检查的 Re3D 基线。
3. 保证 `RE3D_DATA_ROOT` 位于代码仓库之外。
4. 在公开部署前明确成功任务的数据保留期限、生产任务限额取值、存储配额、域名、TLS、邮件服务和备份策略。

本地邮箱验证与密码重置使用只绑定回环地址的 Mailpit：

```powershell
& .\deploy\mailpit\start-local.ps1
& .\deploy\mailpit\run-acceptance.ps1
```

邮件界面位于 `http://127.0.0.1:8025`，具体配置和停止方式见 [`deploy/mailpit/README.md`](deploy/mailpit/README.md)。

首次创建包含数据库密码和随机 JWT 密钥的本机 `.env`：

```powershell
& .\deploy\development\initialize-local-env.ps1
```

脚本不会覆盖已有配置，且只输出不含秘密的就绪检查结果。说明见 [`deploy/development/README.md`](deploy/development/README.md)。

生产配置模板、按 `api` / `worker` / `all` 区分的无秘密就绪检查、前端生产构建、API/Worker 启动入口、Caddy 同源代理和本机闭环验收见 [`deploy/production/README.md`](deploy/production/README.md)。生产 PostgreSQL 的管理员/迁移/运行权限拆分以及备份恢复流程见 [`deploy/postgres/README.md`](deploy/postgres/README.md)。这些检查和脚本用于提前拒绝开发库、管理员数据库账户、不安全 Cookie、非 HTTPS 公共地址、未加密 SMTP、漂移的 Re3D 基线和不足的数据盘；检查通过本身不代表允许开放公网。

`.env`、用户数据、模型权重和运行产物不得提交到 Git。

前端首次安装和启动：

```powershell
cd D:\3Dreconstruction\Re3D-platform\apps\web
Copy-Item .env.example .env
npm ci
npm run dev
```

Vite 开发服务器只绑定 `127.0.0.1:5173`，并将 `/api` 代理到本机 FastAPI；它不能用作公开部署服务器。

## 已固定的首期规则

- 用户必须登录且验证邮箱后才能提交任务。
- 每个任务固定运行 A-v4、B-v2、C 三条分支。
- 原图、中间文件、日志、结果和评估以 `job_uuid` 为单位保存。
- 失败和取消任务会在状态和脱敏错误信息写入数据库后，由 Worker 周期清理整个任务文件目录；删除尝试和结果保留在数据库中。
- 稳定上传提交使用数据库共享的单用户待处理任务数和滚动 24 小时次数限制；成功创建和首次取消写入最小审计事件。
- 成功任务已具备原图、中间文件和产物分层清理能力，但保留天数未确定、命令默认 dry-run；在完成政策和执行验收前不得开放互联网部署。
- API 不直接运行 Re3D；独立 Worker 通过任务租约串行占用单张 GPU。

## 下一步

运行契约、Windows Worker、三分支模拟器、PostgreSQL 租约队列、认证、数据库共享登录/注册/认证邮件请求限流、脱敏认证审计及保留期清理、邮箱验证与密码重置前后端闭环、未验证账号任务创建权限控制、真实图片上传、数据库共享任务提交限额、任务创建/取消审计、上传磁盘安全线、稳定任务访问 API、任务详情/SSE、受控真实 Re3D 子进程、产物汇总、首版结构评估、受鉴权产物下载、三分支 GLB 在线预览、真实任务资源采样、失败/取消任务文件自动清理、成功任务分层保留清理、生产配置就绪检查、生产 PostgreSQL 权限/备份恢复合同、Windows 单机静态站点/API/持续 Worker/Caddy 非公网拓扑验收，以及绑定发布身份的 Windows 服务包、权限配置和安装后只读验收脚本已经完成。目标服务器服务安装与实测、生产邮件服务、管理员审计查询、生产限额定值、成功任务保留天数和计划执行验收、单用户存储配额、普通接口/IP 限流、集中资源监控告警及真实服务器备份仍是公开部署前置条件。

数据库队列的设计、初始化和当前边界见 [`docs/database-queue.md`](docs/database-queue.md)。
失败/取消任务的自动文件清理、审计字段和人工重试命令见 [`docs/failed-job-storage-cleanup.md`](docs/failed-job-storage-cleanup.md)。
真实任务 CPU、内存、数据盘和 GPU 私有采样契约见 [`docs/resource-monitoring.md`](docs/resource-monitoring.md)。
