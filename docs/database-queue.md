# PostgreSQL 任务状态机、租约与心跳

## 1. 作用范围

本模块负责持久化任务状态，并保证同一 GPU 资源在任意时刻只被一个 Worker 租用。它不保存图片或模型正文。`run-queued-once` 只消费模拟任务；`run-real-queued-once` 独立消费 real 任务，并在同一资源租约下监督真实 Re3D 子进程。

核心代码：

- `backend/db/state_machine.py`：允许的任务状态转换；
- `backend/db/models.py`：任务与资源租约模型；
- `backend/db/queue.py`：入队、原子领取、续租、取消和状态推进；
- `backend/db/heartbeat.py`：长任务后台周期续租；
- `backend/db/migrations`：Alembic 数据库迁移；
- `backend/jobs/development.py`：创建开发专用模拟任务和三分支请求；
- `backend/worker/queued.py`：分别领取 simulated/real 任务、续租并投影状态；
- `apps/api/main.py`：开发环境创建、查询和取消接口。

## 2. 数据模型

`users` 保存规范化身份、Argon2id 密码哈希和账号状态；`refresh_sessions` 保存 refresh token 摘要、轮换族和撤销状态。`job_uploads` 保存尚未提交或已经提交的上传会话，`job_upload_images` 保存各图片的平台文件名、实际格式、尺寸、字节数和 SHA-256。提交成功后，上传会话 UUID 同时成为 `reconstruction_jobs.id` 和任务目录名；半成品上传不会提前进入队列。

`reconstruction_jobs.user_id` 通过外键关联真实用户，并保存执行模式、固定管线身份、输入 manifest 摘要、状态、进度、错误码和时间戳。图片、中间文件、日志和模型保存在 `Re3D-data/jobs/<job_uuid>`。

`worker_leases` 每行代表一个可独占资源。首期迁移创建 `gpu:0`：

```text
gpu:0
  ├─ job_id
  ├─ worker_id
  ├─ lease_token
  ├─ leased_at
  ├─ heartbeat_at
  └─ expires_at
```

`lease_token` 是每次领取生成的新 UUID。Worker 的心跳和状态更新必须同时匹配资源、job、worker 和 token；过期任务被接管后，旧 Worker 无法再更新数据库状态。

## 3. 原子领取流程

PostgreSQL 事务按以下顺序执行：

1. 使用 `SELECT ... FOR UPDATE` 锁定 `gpu:0`；
2. 如果槽位存在未过期租约，立即返回无任务；
3. 如果原租约过期且任务未终止，优先恢复该任务；
4. 否则使用 `FOR UPDATE SKIP LOCKED` 按优先级和排队时间选择任务；
5. 将 `queued` 转为 `preparing`；
6. 写入新 token、Worker ID、心跳和到期时间；
7. 提交事务后才允许 Worker 启动管线。

先锁资源槽可以落实首期单 GPU 并发为 1；`SKIP LOCKED` 使未来增加多个 GPU 槽位时，不会让 Worker 因另一条队列记录被锁而阻塞。

## 4. 心跳和接管

默认建议租约 60 秒、心跳 20 秒。`LeaseHeartbeatLoop` 在后台续租并向执行控制器暴露两个信号：

- `cancel_requested`：API 已请求取消，执行控制器应终止子进程并把任务转为 `cancelled`；
- 心跳异常：`raise_if_failed()` 抛出错误，执行控制器不得再提交进度或成功结果。

租约 token 隔离数据库写入；真实执行控制器同时监听心跳状态，并在失去租约时终止整个子进程树，避免旧进程继续写任务目录。恢复后的 Worker 仍会重新校验已有结果的任务身份、文件大小和 SHA-256，不能仅凭文件存在就跳过执行。

## 5. 状态机边界

正常流程：

```text
draft → uploading → validating_input → queued → preparing → sfm
      → dense_reconstruction → meshing → texturing
      → validating_output → evaluating → succeeded → expired
```

允许的失败终态按阶段限制：

- 上传或输入校验：`failed_input`；
- 准备到输出校验：`failed_pipeline`；
- 自动评估：`failed_evaluation`；
- 非终态任务：`cancelled`。

状态不能跳步，进度不能回退，终态不能重新排队。重试将来应创建新的 attempt 和请求文件，而不是修改已经终止的 attempt。

## 6. 本地初始化

创建隔离环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

在 Windows 本机创建开发数据库和受限用户：

```powershell
& deploy/postgres/bootstrap-dev.ps1
& deploy/postgres/verify-and-migrate-dev.ps1
```

第一条命令以管理员身份创建 `re3d_platform_dev` 和受限角色 `re3d_app`；第二条命令以应用角色验证权限并执行迁移。数据库密码只放在未提交的 `.env` 或系统秘密存储中，不写入仓库、日志或任务 manifest。

PostgreSQL 集成测试必须使用临时 Docker 数据库：

```powershell
& deploy/postgres/run-integration-tests.ps1
```

测试数据库名称必须以 `_test` 结尾，测试代码会主动拒绝 `re3d_platform_dev`。临时容器不挂载持久卷，脚本结束时自动删除。

## 7. 已验证与尚未完成

已验证：

- SQLite 单元测试覆盖完整状态机和顺序租约语义；
- PostgreSQL 18 真实迁移、并发领取、过期接管和旧 token 隔离；
- Alembic 可从空数据库升级到 `0004_upload_lifecycle`，并完成 `0004 → 0001 → 0004` 往返；
- 本机开发库使用 `re3d_app` 完成权限探针和首次迁移；
- 测试库名称保护会拒绝开发库，临时 Docker 测试脚本会自动清理容器。
- 开发 API 创建任务后，队列 Worker 能在 PostgreSQL 中领取、续租、执行三个模拟分支并提交终态；
- 任务查询按 access token 对应的 `user_id` 做对象范围过滤，取消的排队任务不会被领取；
- `execution_mode=simulated` 的 Worker 不会领取或接管 real 任务，real Worker 也只领取 real 任务；
- 模拟评估报告通过 evaluation v1 Schema，并明确不给出真实质量分数。
- 已认证用户可以上传并完整解码 JPEG/PNG；提交前会复核文件集合、大小和摘要，然后原子创建队列任务。
- 未提交上传支持单张删除、用户取消和超时回收；取消记录保留原因和存储清理完成时间。
- real Worker 已实现子进程树终止、总超时、步骤事件、单调数据库进度、三分支产物哈希校验和结构健康评估，并通过轻量伪管线闭环测试。
- 11 图 real 任务已在临时 PostgreSQL 18 上完成受租约保护的本机 GPU 验收，任务以 attempt 1、progress 100、succeeded 终止。

尚未完成：

- 邮箱验证、密码重置、认证限流和审计；
- 图片 EXIF 清除；
- GPU、显存和磁盘资源采样；
- 失败任务目录清理和成功任务保留策略；
- 多 GPU 资源注册和调度。
