# 成功任务分层保留与磁盘安全线

## 当前边界

成功任务数据分为三个独立清理层级：

| 层级 | 删除内容 | 保留内容 |
|---|---|---|
| `input` | `input/images`、`input/staging` | 输入 manifest 与任务请求 |
| `runtime` | `runtime` 下的中间文件、日志和资源样本 | 数据库任务状态 |
| `artifacts` | `output` 下的 GLB/OBJ/MTL/纹理 | pipeline result、评估报告和最小审计 manifest |

产物层清理成功后，任务从 `succeeded` 转为 `expired`，API 不再生成下载 URL，产物下载接口也不再返回文件。保留的小型 manifest 和评估报告用于解释历史任务，不包含原图或模型正文。

迁移 `0010_success_storage_retention` 增加三个完成时间，以及最近尝试时间、尝试次数和稳定错误码。删除失败不会伪造完成时间，后续执行可以重试。迁移 `0011_success_retention_runs` 记录每次人工或计划运行的模式、策略快照、开始/完成时间、状态和无敏感信息统计摘要。

## 配置不等于自动删除

三个层级统一保留 30 天：

```dotenv
SUCCESS_INPUT_RETENTION_DAYS=30
SUCCESS_RUNTIME_RETENTION_DAYS=30
SUCCESS_ARTIFACT_RETENTION_DAYS=30
SUCCESS_RETENTION_CLEANUP_BATCH_SIZE=50
```

每个已设置天数必须在 1–3650 之间。候选时间以数据库 `finished_at` 为准，而不是容易被复制或修改的文件时间。

命令默认只预演，不修改任务状态或文件，但会向 `success_retention_runs` 写入审计记录：

```powershell
.\.venv\Scripts\python.exe -m apps.worker.main cleanup-success-job-storage `
  --env-file .env `
  --trigger manual
```

只有在人工检查预演数量并确认保留策略后，才允许显式执行：

```powershell
.\.venv\Scripts\python.exe -m apps.worker.main cleanup-success-job-storage `
  --env-file .env `
  --trigger manual `
  --execute `
  --confirm-delete
```

`--execute` 与 `--confirm-delete` 必须同时出现；只提供其中一个会以退出码 2 拒绝运行。`--env-file` 由 Python 进程直接读取，不依赖 PowerShell 脚本执行策略，且不会把连接串打印到输出。

计划任务的首阶段应只运行以下命令：

```powershell
.\.venv\Scripts\python.exe -m apps.worker.main cleanup-success-job-storage `
  --env-file .env.worker.production `
  --trigger scheduled
```

三层成功任务数据的保留期已统一确定为 30 天，并写入开发及生产配置示例。2026-09-30 对开发库执行的首轮 dry-run 显示三个层级候选数均为 0，没有删除文件。成功任务清理不会接入常驻重建循环；目标服务器仍需先完成备份确认和多轮计划 dry-run，再单独决定是否为计划任务增加两个删除确认参数。

## 路径安全

清理器只接受数据库中的规范 UUID，并从固定 `RE3D_DATA_ROOT/jobs/<job_uuid>` 派生目标。任务根目录或待清理子目录如果是符号链接、junction、普通文件或解析到非预期位置，清理会失败并写入稳定错误码，不会继续递归删除。

## 运行时磁盘安全线

`RE3D_MIN_FREE_DISK_BYTES` 现在不仅用于启动前就绪检查，也用于上传运行时：

- 创建新上传前，为一张最大尺寸图片预留检查空间；
- 每次写入图片前执行同样检查；
- staging 文件写入后再次检查，若跌破安全线则拒绝并删除 staging 文件；
- 上传提交前再次检查。

拒绝响应为 HTTP 507，稳定错误码是 `STORAGE_CAPACITY_FLOOR_REACHED`，并带 `Retry-After: 300`。已经存在的幂等上传查询和删除/取消仍可执行，以便用户回收空间。

该安全线是早期拒绝机制，不是严格容量预留：并发上传、Re3D 中间文件和其他主机进程仍可能消耗磁盘。公开部署前仍需增加任务运行前空间估算、低磁盘告警和单用户存储配额。
