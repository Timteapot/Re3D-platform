# Re3D Platform

Re3D Platform 是基于 Re3D 三维重建管线的非商业学习与工程实践项目。平台目标是为公开互联网用户提供注册、登录、多视图图片上传、异步三维重建、三分支结果查看和自动质量评估。

## 当前状态

核心功能闭环、首轮本机真实 GPU 验收和 Windows 单机生产拓扑的非公网验收已经完成。鉴于短期不进行公网发布，当前主线调整为 Windows 本机回环受限部署：新增独立 `restricted` 环境，稳定上传只创建真实任务，并强制 API、浏览器入口、可信代理、SMTP 和 PostgreSQL 保持在回环边界。目标云服务器保留为后续复测环境，现阶段不依赖其持续运行；公开发布仍受备案/域名、边缘防护、生产邮件、监控、压力测试和容量实测校准等条件约束。

- Re3D 活动基线标签：`re3d-pipeline-v1.1.2`
- Re3D 活动基线提交：`5acd79496fb3614133019b4ab590823696d8e958`
- 目标 GPU 兼容参数：Tesla T4（15 GiB）使用 MapAnything `batch_size=4`
- 离线模型加载：MVSAnywhere 固定从本地 DINOv2 `main` Torch Hub 缓存加载
- 首个冻结基线：`re3d-pipeline-v1.0.0`，保持不变并保留历史清单
- 首期固定分支：`A-v4`、`B-v2`、`C`
- 当前开发环境：Windows
- 平台代码目录：`D:\3Dreconstruction\Re3D-platform`
- 运行数据目录：`D:\3Dreconstruction\Re3D-data`

基线的完整哈希和验证结果见 [`config/pipeline-baseline.json`](config/pipeline-baseline.json)，总体实施计划见 [`PROJECT_PLAN.md`](PROJECT_PLAN.md)，平台直接依赖声明见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。当前完成度和待办以 [`docs/progress.md`](docs/progress.md) 为准，服务间数据流和模块边界以 [`docs/module-integration.md`](docs/module-integration.md) 为准。

当前已经可以通过 React 页面注册、登录、恢复会话、上传真实 JPEG/PNG 输入并创建任务。development/test 的默认网页提交进入模拟队列并允许显式 real；production 由服务端强制创建 real 任务。独立的真实 Worker 在 PostgreSQL 租约下监督 Re3D 子进程，处理取消、总超时和租约丢失，并汇总三分支产物和结构健康评估。2026-09-25 已使用 11 张真实图片完成本机 GPU 闭环验收；2026-10-08 又完成同规模真实任务的空间采样和容量报告验收；2026-10-10 在目标 Tesla T4 服务器上完成 `v1.1.2` 三分支空目录真实验收。任务详情页现可通过受所有权保护的稳定任务 API 下载清单内 GLB、OBJ、MTL 和纹理，并可在 Three.js 查看器中切换和交互预览三分支 GLB。任务 API 边界见 [`docs/job-api.md`](docs/job-api.md)，上传边界见 [`docs/image-uploads.md`](docs/image-uploads.md)，下载边界见 [`docs/artifact-downloads.md`](docs/artifact-downloads.md)，查看器边界见 [`docs/glb-viewer.md`](docs/glb-viewer.md)，实测证据见 [`docs/real-gpu-acceptance-2026-09-25.md`](docs/real-gpu-acceptance-2026-09-25.md)、[`docs/storage-capacity-acceptance-2026-10-08.md`](docs/storage-capacity-acceptance-2026-10-08.md) 和 [`docs/target-server-gpu-acceptance-2026-10-10.md`](docs/target-server-gpu-acceptance-2026-10-10.md)。

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
4. 成功任务原图、中间文件和产物三层保留期均为 30 天；持续 Worker 每 24 小时执行一次受审计 dry-run，实际删除只能通过人工双确认命令触发。

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

生产配置模板、按 `api` / `worker` / `all` 区分的无秘密就绪检查、前端生产构建、API/Worker 启动入口、Caddy 同源代理和本机闭环验收见 [`deploy/production/README.md`](deploy/production/README.md)。生产 PostgreSQL 的管理员/迁移/运行权限拆分以及备份恢复流程见 [`deploy/postgres/README.md`](deploy/postgres/README.md)。目标 Windows 主机安装前还应运行 [`test-target-host-prerequisites.ps1`](deploy/production/windows-services/test-target-host-prerequisites.ps1)，生成不含秘密的主机阻断项报告。这些检查用于提前拒绝开发库、管理员数据库账户、不安全 Cookie、非 HTTPS 公共地址、未加密 SMTP、漂移的 Re3D 基线、不兼容工具链和不足的数据盘；检查通过本身不代表允许开放公网。

短期本机受限部署使用 [`.env.restricted.example`](.env.restricted.example) 和 [`.env.worker.restricted.example`](.env.worker.restricted.example)。本机已创建独立数据库/最小权限角色、仓库外数据目录和受保护的私密环境文件，并通过全组件就绪检查与 Mailpit 认证邮件验收；安全边界和复现命令见 [`deploy/restricted/README.md`](deploy/restricted/README.md)。`restricted` 不等于 development：它不注册开发任务路由，也不允许模拟重建；同时它也不等于 production，允许回环 HTTP Cookie 和本机无 STARTTLS Mailpit，但拒绝局域网或公网地址。

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
- 单用户存储按未提交图片实际字节和已提交任务固定预留量计费；并发检查由 PostgreSQL 用户行锁串行化，失败任务或成功任务分层清理完成后释放。
- 单用户上传/下载使用 PostgreSQL 共享的连续字节令牌桶和过期并发租约；上传校验/落盘结束或下载响应结束时释放租约，超限返回稳定 429。
- 成功任务原图、中间文件和产物统一保留 30 天；持续 Worker 默认每日执行受审计 dry-run，实际删除仍是独立双确认命令；在完成目标服务器周期观察与执行验收前不得开放互联网部署。
- API 不直接运行 Re3D；独立 Worker 通过任务租约串行占用单张 GPU。

## 下一步

当前按以下顺序推进，不把“代码已实现”“本机可用”和“公网可用”混为一谈：

1. 增加可重复、仅绑定回环地址的静态前端/Caddy/API/Worker 启停与状态脚本；Mailpit 独立程序启停和全组件就绪检查已完成；
2. 使用代表性图片完成一次受限环境真实 GPU 全链路验收；
3. 完成进程重启恢复与 development、restricted、production 数据隔离验收；
4. 在不启动云服务器的情况下继续完善验证码、管理员二次验证、备份恢复和本机压力/低磁盘演练；
5. 未来重新进入公网阶段时，再启动目标服务器复测容量和 GPU，补齐备案或合规域名、TLS、生产 SMTP、边缘防护与集中监控。

因此当前版本可用于目标服务器部署工程和内部验收，不应直接向公开互联网开放注册与重建。

数据库队列的设计、初始化和当前边界见 [`docs/database-queue.md`](docs/database-queue.md)。
失败/取消任务的自动文件清理、审计字段和人工重试命令见 [`docs/failed-job-storage-cleanup.md`](docs/failed-job-storage-cleanup.md)。
真实任务 CPU、内存、数据盘、GPU 和任务目录空间峰值的私有采样契约见 [`docs/resource-monitoring.md`](docs/resource-monitoring.md)。
任务空间摘要的只读汇总、P50/P95 和预留值校准方法见 [`docs/storage-capacity-calibration.md`](docs/storage-capacity-calibration.md)。
管理员审计查询、脱敏边界和首名管理员引导见 [`docs/admin-audit.md`](docs/admin-audit.md)。
普通 API 来源 IP 限流、可信代理边界和 429 合同见 [`docs/api-rate-limiting.md`](docs/api-rate-limiting.md)。
单用户存储计费、释放时机、507 合同和当前估算边界见 [`docs/user-storage-quota.md`](docs/user-storage-quota.md)。
单用户上传/下载并发租约、字节令牌桶、429 合同和代理层边界见 [`docs/transfer-limits.md`](docs/transfer-limits.md)。
