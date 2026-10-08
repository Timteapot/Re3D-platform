# 任务存储容量校准报告

## 目标与边界

容量报告工具只读扫描 `<RE3D_DATA_ROOT>/jobs/<job_uuid>/reports/storage-usage.json`，将真实 Worker 生成的任务空间摘要汇总为运维 JSON。它用于目标服务器实测后校准 `RE3D_JOB_STORAGE_RESERVATION_BYTES`，不修改任务、数据库或任何摘要文件，也不输出任务 UUID、用户身份和文件路径。

报告只采纳同时满足以下条件的样本：

- `storage-usage` 1.0 合同有效；
- 状态为 `complete`，最终分层数据存在且没有采样错误；
- 摘要任务 UUID 与所在任务目录一致；
- 分层总量、三分支集合、最终值与峰值之间的算术关系一致。

`partial` 和 `unavailable` 样本只计入 `excluded_non_complete_samples`；损坏、路径边界异常或语义不一致的摘要只计入 `invalid_samples`。报告不会暴露对应任务的身份。

## 执行

在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -m apps.maintenance.main report-storage-capacity `
  --data-root D:\3Dreconstruction\Re3D-data
```

也可以省略 `--data-root`，改用当前进程的 `RE3D_DATA_ROOT`。命令仅向标准输出写入一份符合 `storage-capacity-report` 1.0 合同的 JSON，可由调用方重定向到目标服务器的受保护验收目录。

可调整参数：

- `--minimum-samples`：生成推荐值所需的完整样本数，默认 5；
- `--safety-factor`：观测最大峰值的安全系数，默认 1.25，允许 1–5；
- `--rounding-mib`：推荐值向上取整的 MiB 粒度，默认 256。

参数只影响当前报告，不会自动改写 `.env` 或生产配置。

## 统计与推荐规则

- P50、P95 使用 nearest-rank 方法，结果始终是实际观测值；
- `task_peak_bytes` 来自各任务的 `peaks.total_bytes`；
- `final_tiers` 汇总 input、runtime、output、reports、manifests 和 other 的最终值与总体占比；
- `branches` 汇总 A-v4、B-v2、C 的最终产物字节数及其在 output 中的占比；
- 推荐值为“观测最大峰值 × 安全系数”，然后向上取整到配置粒度。

默认完整样本少于 5 个时，报告状态为 `insufficient_data`，仍输出已有统计，但 `recommendation` 为 `null`。这不代表第 5 个样本后数据自动具有代表性；运维人员仍需覆盖不同图片数量、分辨率、场景复杂度和成功分支组合。

## 当前限制

任务峰值来自周期扫描，可能漏掉两个采样点之间创建并删除的短生命周期文件。建议在目标 GPU 服务器使用代表性数据集执行多轮真实任务，并结合数据盘剩余空间、GPU 并发数、上传暂存空间和操作系统余量决定最终生产配额。当前默认 2 GiB 预留在完成该验收前保持不变，但不能视为可靠的最坏情况上限。
