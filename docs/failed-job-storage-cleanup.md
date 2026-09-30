# 失败与取消任务存储清理

## 目标和范围

清理器只删除以下数据库终态对应的任务目录：

- `failed_input`；
- `failed_pipeline`；
- `failed_evaluation`；
- `cancelled`。

它不会删除 `succeeded` 或 `expired` 任务。成功任务由独立的分层保留清理器处理，不能复用失败任务整目录策略；具体见 [`success-storage-retention.md`](success-storage-retention.md)。

数据库中的任务行、终态、脱敏 `error_code`、开始/完成时间和清理审计字段会保留。文件清理后，任务详情返回 `not_available`，不会把已经按策略删除的目录误报为存储损坏。

## 数据库审计

迁移 `0008_failed_job_storage_cleanup` 为 `reconstruction_jobs` 增加：

| 字段 | 作用 |
|---|---|
| `storage_cleaned_at` | 任务目录已经删除或确认不存在的时间 |
| `storage_cleanup_attempted_at` | 最近一次删除尝试时间 |
| `storage_cleanup_attempts` | 累计尝试次数 |
| `storage_cleanup_last_error` | 最近一次失败的稳定错误码，不保存路径或异常正文 |

删除失败时记录 `TASK_DIRECTORY_REMOVE_FAILED`，后续扫描会重试。若目录已经不存在，则按幂等成功处理并写入 `storage_cleaned_at`。

## 自动执行

生产 `run-real-queued-loop` 在启动时执行一次扫描，之后默认每 300 秒扫描一次。任务完成失败或取消后默认保留 5 分钟，避免终态事务、API 响应和文件句柄关闭发生竞态；随后在下一次扫描中删除。

配置只放在 Worker 环境文件：

```dotenv
RE3D_FAILED_JOB_CLEANUP_GRACE_MINUTES=5
RE3D_FAILED_JOB_CLEANUP_INTERVAL_SECONDS=300
RE3D_FAILED_JOB_CLEANUP_BATCH_SIZE=100
```

每次扫描输出一条不含路径和异常正文的 JSON 摘要，包括扫描数、删除数、原本不存在数和失败任务 UUID。单个文件系统错误不会停止队列循环；数据库或配置错误仍会使 Worker 失败并交给服务恢复策略处理。

## 人工重试与验收

迁移数据库后，可以使用相同 Worker 环境执行一次性命令：

```powershell
& .\.venv\Scripts\python.exe -m apps.worker.main `
    cleanup-failed-job-storage `
    --grace-minutes 5 `
    --limit 100
```

命令只从数据库选择固定终态和 UUID，不接受任意文件路径。`--grace-minutes 0` 只应用于明确的人工即时清理或测试。

## 删除安全边界

唯一允许删除的目标形式为：

```text
<RE3D_DATA_ROOT>/jobs/<canonical-job-uuid>/
```

清理前会重新执行 UUID 规范化和 `TaskLayout` 边界验证。目标是文件、符号链接、目录联接点，或解析后不等于预期任务目录时，清理失败并只写稳定错误码。清理器不会接受数据库外的路径，也不会递归删除数据根目录或 `jobs` 根目录。
