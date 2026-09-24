# PostgreSQL 任务状态机、租约与心跳

## 1. 作用范围

本模块负责持久化任务状态，并保证同一 GPU 资源在任意时刻只被一个 Worker 租用。它不保存图片或模型正文，也还没有把现有 `simulate` / `real-dry-run` CLI 自动包装成数据库队列消费者。

核心代码：

- `backend/db/state_machine.py`：允许的任务状态转换；
- `backend/db/models.py`：任务与资源租约模型；
- `backend/db/queue.py`：入队、原子领取、续租、取消和状态推进；
- `backend/db/heartbeat.py`：长任务后台周期续租；
- `backend/db/migrations`：Alembic 数据库迁移。

## 2. 数据模型

`reconstruction_jobs` 保存用户、执行模式、固定管线身份、输入 manifest 摘要、状态、进度、错误码和时间戳。图片、中间文件、日志和模型仍保存在 `Re3D-data/jobs/<job_uuid>`。

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

租约 token 只能隔离数据库写入。真实 Re3D 接入时还必须用进程控制器终止失去租约的子进程，否则旧进程仍可能继续写任务目录。

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

安装依赖：

```powershell
python -m pip install -r requirements-dev.txt
```

设置开发数据库连接并执行迁移：

```powershell
$env:DATABASE_URL = 'postgresql+psycopg://<user>:<password>@127.0.0.1:5432/re3d_platform'
python -m alembic -c alembic.ini upgrade head
```

数据库密码只放在未提交的 `.env` 或系统秘密存储中，不写入仓库、日志或任务 manifest。

## 7. 已验证与尚未完成

已验证：

- SQLite 单元测试覆盖完整状态机和顺序租约语义；
- PostgreSQL 18 真实迁移、并发领取、过期接管和旧 token 隔离；
- Alembic 可从空数据库升级到 `0001_job_queue`。

尚未完成：

- API 创建任务及用户对象级权限；
- 队列消费者把租约包裹到模拟和真实 Re3D 执行；
- Re3D 子进程取消、超时和租约丢失终止；
- 事件日志向数据库进度投影；
- 多 GPU 资源注册和调度。
