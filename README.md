# Re3D Platform

Re3D Platform 是基于 Re3D 三维重建管线的非商业学习与工程实践项目。平台目标是为公开互联网用户提供注册、登录、多视图图片上传、异步三维重建、三分支结果查看和自动质量评估。

## 当前状态

项目处于阶段 0：基线冻结与平台骨架准备。

- Re3D 基线标签：`re3d-pipeline-v1.0.0`
- Re3D 基线提交：`d528b1474c55bf3b65a1253bd56ab4b5024adab2`
- 首期固定分支：`A-v4`、`B-v2`、`C`
- 当前开发环境：Windows
- 平台代码目录：`D:\3Dreconstruction\Re3D-platform`
- 运行数据目录：`D:\3Dreconstruction\Re3D-data`

基线的完整哈希和验证结果见 [`config/pipeline-baseline.json`](config/pipeline-baseline.json)，总体实施计划见 [`PROJECT_PLAN.md`](PROJECT_PLAN.md)。

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

## 已固定的首期规则

- 用户必须登录且验证邮箱后才能提交任务。
- 每个任务固定运行 A-v4、B-v2、C 三条分支。
- 原图、中间文件、日志、结果和评估以 `job_uuid` 为单位保存。
- 失败任务在状态和脱敏错误信息写入数据库后删除整个任务文件目录。
- 成功任务保留期限尚未确定；在确定前不得开放互联网部署。
- API 不直接运行 Re3D；独立 Worker 通过任务租约串行占用单张 GPU。

## 下一步

下一步先定义并测试 `pipeline-request`、`pipeline-event`、`pipeline-result` 和 `evaluation` 的 JSON Schema，然后用模拟 Worker 打通任务状态机，再创建前后端工程。
