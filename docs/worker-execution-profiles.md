# Worker 执行档与大任务运行前检查

## 1. 目的

执行档描述目标主机必须满足的容量条件，不修改 Re3D 算法或固定配置。Re3D v1.1.3 的 MapAnything `batch_size=4` 仍由受 Git 提交和 SHA-256 保护的 `configs/pipeline.json` 决定。

当前提供四个执行档：

| 执行档 | 用途 | 图片规模声明 | 最低任务预留 | 数据盘安全线 | 最低显存 | 启动时最低空闲显存 |
|---|---|---:|---:|---:|---:|---:|
| `baseline` | 本机功能开发和小样本验收 | 不作大任务容量声明 | 1 GiB | 使用环境值 | 仅要求 CUDA 可用 | 不额外限制 |
| `gpu-large-100` | 目标服务器 100 图阶梯验收 | 100 | 16 GiB | 50 GiB | 14 GiB | 12 GiB |
| `gpu-large-150` | 目标服务器 150 图验收 | 150 | 32 GiB | 75 GiB | 14 GiB | 12 GiB |
| `gpu-large-300` | 目标服务器 300 图容量实验 | 300 | 64 GiB | 75 GiB | 14 GiB | 12 GiB |

Lighthouse 150 图已在目标 Tesla T4 服务器完成三分支管线验收，最终任务目录约 8.97 GiB；但该次直接管线运行没有保存连续 GPU 峰值和平台任务空间采样，因此 32 GiB 仍不是已证明的最坏情况。`gpu-large-300` 的 64 GiB 预留是容量实验的保守准入值，不代表 300 图已经通过。当前验收计划为一次直接 300 图实验，不执行原 200/250/300 阶梯；准备和验收边界见 [`direct-300-local-readiness-2026-10-13.md`](direct-300-local-readiness-2026-10-13.md)。

## 2. 配置

API 和 Worker 必须使用相同的任务预留值。生产示例当前采用：

```dotenv
RE3D_EXECUTION_PROFILE=gpu-large-300
RE3D_JOB_STORAGE_RESERVATION_BYTES=68719476736
RE3D_MIN_FREE_DISK_BYTES=80530636800
```

API 另配置 128 GiB 用户配额：

```dotenv
RE3D_USER_STORAGE_QUOTA_BYTES=137438953472
```

本机日常受限开发环境使用 `baseline`，采用 16 GiB 任务预留。服务器已验收的部署同样是小任务配置；直接 300 图预设和发布包已经准备，尚未应用至目标服务器。目标机需要以至少 139 GiB 空闲数据盘、14 GiB 总显存、12 GiB 空闲显存、固定 batch size 4 和 CUDA 就绪通过运行前检查，再使用 `gpu-large-300` 进行实验。连续资源采样用于验收和校准，切换执行档本身不表示容量已通过。

直接 300 图 API 请求采用 `RE3D_JOB_TIMEOUT_SECONDS=14400`，每用户最多
1 个非终态任务；Worker 资源采样为 5 秒，任务目录采样为 15 秒。独立配置
文件是 `deploy/restricted/profiles/direct-300.json`，只包含非秘密参数。

## 3. API 提交准入

上传数据已经占用磁盘，所以提交任务时只计算尚未落盘的预留部分：

```text
新增空间需求 = max(0, 任务预留 - 当前上传实际字节)
允许提交条件 = 当前磁盘可用空间 - 新增空间需求 >= 磁盘安全线
```

不满足时返回已有的 `STORAGE_CAPACITY_FLOOR_REACHED` 507 合同，上传保持可修改状态，任务不会入队。

## 4. Worker 启动预检

持续真实 Worker 在领取任务前执行一次 fail-closed 预检：

1. 数据盘可用空间至少等于安全线加一个完整任务预留；
2. `nvidia-smi` 能读取配置的 `gpu:N`；
3. GPU 总显存和当前空闲显存满足执行档；
4. Re3D driver Python 中的 PyTorch 能访问同一 CUDA 设备；
5. 已安装 Re3D 配置中的 MapAnything batch size 仍为 4。

预检只输出设备名称、容量和执行档等无秘密摘要。失败时 Worker 以受控错误退出，不会先领取数据库任务。

## 5. 仍需目标服务器验证

- 300 图的穷举特征匹配图片对数量约为 150 图的四倍，耗时可能超线性增长；
- 64 GiB 只用于准入估算，不是文件系统硬限额；
- 75 GiB 是启动/上传准入的磁盘安全线，`gpu-large-300` Worker 启动前至少需要 139 GiB 可用空间；任务运行中可能超出预留，因此不保证结束时仍有 75 GiB；
- 多任务保留 30 天时，应按计划保留的任务数量另行配置 300–500 GiB 独立数据盘；
- 实测后使用 `report-storage-capacity` 更新任务预留，不能直接把暂定值视为最终生产值。
