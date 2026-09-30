# 真实任务资源采样

## 目标与边界

真实 Worker 在 Re3D 管线和后续评估执行期间，周期记录主机 CPU、物理内存、数据盘和指定 NVIDIA GPU 指标。这些数据用于判断长时间无管线日志时进程是否仍在计算，以及发现显存、内存或数据盘压力。

采样文件是私有运维证据：

- 不通过用户 API 返回；
- 不参与三维重建质量评分；
- 采样失败不会将重建任务改判为失败；
- 不保存用户名、主机名、文件绝对路径或进程命令行。

当前实现是任务级采样，尚不是集中监控和告警系统。

## 存储位置和契约

每个真实任务的样本追加到：

```text
<RE3D_DATA_ROOT>/jobs/<job_uuid>/runtime/metrics/resource-samples.jsonl
```

每行都独立符合 `resource-sample` 1.0 JSON Schema，包含：

- CPU 区间利用率和逻辑核心数；
- 物理内存已用量、总量和利用率；
- `RE3D_DATA_ROOT` 所在数据盘的已用、剩余、总量和利用率；
- GPU 索引、名称、利用率、已用/总显存和温度；
- 稳定错误码，不记录异常正文。

第一个样本没有前一个 CPU 计数器差值，因此状态为 `warming_up` 且 CPU 利用率为 `null`。后续样本状态为 `complete`；任一组件无法采样时为 `partial`。

## 配置

Worker 环境文件使用：

```dotenv
RE3D_RESOURCE_MONITOR_ENABLED=true
RE3D_RESOURCE_SAMPLE_INTERVAL_SECONDS=15
RE3D_NVIDIA_SMI_PATH=C:/Windows/System32/nvidia-smi.exe
```

- 周期允许 5–300 秒；默认 15 秒。
- Windows 生产环境建议显式固定 `nvidia-smi.exe` 路径。
- Linux 迁移时可以留空路径，由 Worker 从 `PATH` 查找 `nvidia-smi`，也可指定绝对路径。
- `RE3D_GPU_RESOURCE=gpu:0` 会映射到 NVIDIA GPU 0；无法映射的资源名不会执行外部命令，只写入 `GPU_INDEX_UNMAPPED`。

## 执行与保留语义

监控器随真实 Worker 任务启动，先立即采样，随后按周期采样，退出时再写入一个最终样本。CPU 和内存使用 Windows 或 Linux 系统计数器，GPU 使用最长 3 秒的受控 `nvidia-smi` 查询。

采样文件跟随任务目录保留策略：失败/取消任务会在宽限期后连同目录删除；成功任务的资源样本属于 `runtime` 层，只会在显式配置保留天数并执行成功任务清理后删除。后续的集中指标和告警不应依赖永久保留任务目录。

2026-09-30 已在当前 Windows 开发机上验证 CPU、内存、数据盘和 NVIDIA GeForce RTX 4060 Laptop GPU 采样。这不替代目标服务器的服务账户 GPU 权限验收。
