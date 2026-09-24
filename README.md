# Re3D Platform

Re3D Platform 是基于 Re3D 三维重建管线的非商业学习与工程实践项目。平台目标是为公开互联网用户提供注册、登录、多视图图片上传、异步三维重建、三分支结果查看和自动质量评估。

## 当前状态

阶段 0 的核心验收闭环已经完成，项目正在进入阶段 1 的平台骨架开发。

- Re3D 活动基线标签：`re3d-pipeline-v1.1.0`
- Re3D 活动基线提交：`2c5ba174dae9fe53dcec8f7d8466793fdebf0c58`
- 首个冻结基线：`re3d-pipeline-v1.0.0`，保持不变并保留历史清单
- 首期固定分支：`A-v4`、`B-v2`、`C`
- 当前开发环境：Windows
- 平台代码目录：`D:\3Dreconstruction\Re3D-platform`
- 运行数据目录：`D:\3Dreconstruction\Re3D-data`

基线的完整哈希和验证结果见 [`config/pipeline-baseline.json`](config/pipeline-baseline.json)，总体实施计划见 [`PROJECT_PLAN.md`](PROJECT_PLAN.md)，平台直接依赖声明见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

当前已经可以通过 React 页面注册、登录、恢复会话、上传真实 JPEG/PNG 输入并创建开发任务。默认网页提交仍进入模拟队列；开发 API 可以显式创建 real 任务，由独立的真实 Worker 在 PostgreSQL 租约下监督 Re3D 子进程，处理取消、总超时和租约丢失，并汇总三分支产物和结构健康评估。真实控制器已通过轻量假管线验证，尚未执行本机小型图片集 GPU 验收。上传边界见 [`docs/image-uploads.md`](docs/image-uploads.md)，Worker 边界见 [`docs/worker.md`](docs/worker.md)。

## 仓库边界

本仓库只保存 Web 前端、API、Worker、Re3D 适配器、评估模块、部署配置和测试。Re3D 源码、模型权重、用户上传、运行中间文件和重建产物均不复制到本仓库。

```text
apps/web                 React 前端
apps/api                 FastAPI API
apps/worker              GPU 任务执行器
backend/re3d_adapter     Re3D 运行契约与产物解析
backend/evaluation       自动评估规则
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
4. 在公开部署前明确成功任务的数据保留期限、用户配额、域名、TLS、邮件服务和备份策略。

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
- 失败任务在状态和脱敏错误信息写入数据库后删除整个任务文件目录。
- 成功任务保留期限尚未确定；在确定前不得开放互联网部署。
- API 不直接运行 Re3D；独立 Worker 通过任务租约串行占用单张 GPU。

## 下一步

运行契约、Windows Worker、三分支模拟器、PostgreSQL 租约队列、认证、真实图片上传、任务详情/SSE，以及受控真实 Re3D 子进程、产物汇总和首版真实结构评估已经完成。下一步使用小型有效图片集执行一次显式 real 任务验收，再实现产物授权下载；邮箱验证、配额、限流和数据保留策略仍是公开部署前置条件。

数据库队列的设计、初始化和当前边界见 [`docs/database-queue.md`](docs/database-queue.md)。
