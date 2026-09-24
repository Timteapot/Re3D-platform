# Windows Worker、模拟执行与受控真实 Re3D

## 当前实现范围

当前 Worker 包含相互隔离的模拟消费者和真实消费者。模拟消费者用于日常前后端联调；真实消费者只领取 `execution_mode=real` 的任务，在 PostgreSQL 租约下启动固定 Re3D v1.1.0，并在运行期间监督心跳、取消、总超时和子进程树。

模拟器不会启动 Re3D、不会占用 GPU，也不会把模拟指标解释为真实重建质量。请求和结果都必须包含 `execution_mode: simulated`，避免模拟数据进入真实任务统计。

真实 dry-run 要求 `execution_mode: real`，会调用 Re3D `scripts/run_pipeline.py --dry-run`，但只打印预期命令，不运行 COLMAP、模型或 OpenMVS。它生成预检报告，不生成 `pipeline-result.json`，因此不能被解释为重建成功。

真实队列执行使用独立命令 `run-real-queued-once`。没有该显式命令时，默认模拟 Worker 不会领取 real 任务。当前网页仍固定提交 simulated，real 模式只通过开发 API 显式请求，防止误启动耗时 GPU 作业。

## 模块职责

| 模块 | 作用 |
|---|---|
| `paths.py` | 从数据根目录和 UUID 推导唯一任务目录，阻止绝对路径、反斜杠和 `..` 越界 |
| `contracts.py` | 加载 JSON Schema，并在组件边界验证请求、事件和结果 |
| `events.py` | 追加 JSONL 事件、刷新磁盘、恢复连续序号并保护终态 |
| `io.py` | 文件 SHA-256 和同目录临时文件加 `os.replace` 的原子写入 |
| `input_validation.py` | 共享的 manifest、文件名、数量、字节数和符号链接检查 |
| `simulation.py` | 模拟阶段、生成三分支 GLB、恢复检查点并汇总结果 |
| `real.py` | 校验 Re3D Git/配置身份、执行 dry-run/真实管线、映射步骤并汇总产物 |
| `process.py` | 以独立进程组运行 Re3D，监督取消、超时、租约健康并终止整棵子进程树 |
| `settings.py` | 从参数或环境变量读取数据根目录、Worker 与 Re3D 位置 |
| `backend/worker/queued.py` | 按 execution mode 领取任务、维持租约并推进数据库状态 |
| `backend/evaluation/simulation.py` | 生成不虚构几何质量分数的开发评估报告 |
| `backend/evaluation/real.py` | 使用版本化阈值评估 SfM、深度保留率、网格和产物完整性 |
| `apps/worker/main.py` | Windows 命令行入口和稳定退出码 |

## 输入前置条件

Worker 不负责接收浏览器上传。API/编排器需要先创建：

```text
Re3D-data/jobs/<job_uuid>/
├── input/
│   ├── images/
│   └── input-manifest.json
└── manifests/
    └── pipeline-request.json
```

模拟器会同时核对：

- 目录名与请求 `job_id` 一致；
- 请求满足 `pipeline-request` Schema；
- `execution_mode` 为 `simulated`；
- 输入 manifest SHA-256 与请求一致；
- manifest 图片数、磁盘实际文件数和请求图片数一致；
- 图片实际总字节数与请求一致。

任一检查失败都不会生成成功结果。

## 执行方式

```powershell
cd D:\3Dreconstruction\Re3D-platform
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:RE3D_DATA_ROOT = "D:\3Dreconstruction\Re3D-data"
$env:RE3D_WORKER_ID = "windows-gpu-01"
.\.venv\Scripts\python.exe -m apps.worker.main simulate --job-id <job_uuid>
```

也可以显式传入 `--data-root` 和 `--worker-id`，命令行参数优先于环境变量。

真实 dry-run：

```powershell
$env:RE3D_ROOT = "D:\3Dreconstruction\Re3D"
.\.venv\Scripts\python.exe -m apps.worker.main real-dry-run --job-id <job_uuid>
```

未设置 `RE3D_DRIVER_PYTHON` 时，适配器从 Re3D 的 `configs/paths.local.json` 读取 `mapanything_python`。

显式创建 real 任务后，使用真实队列消费者：

```powershell
.\.venv\Scripts\python.exe -m apps.worker.main run-real-queued-once
```

该命令会重新核对数据库任务所有者、attempt、执行模式、输入 manifest、Re3D Git 提交和配置哈希。运行完成后验证 `output/validation.json`，为 A-v4、B-v2、C 的 GLB、OBJ、MTL 和纹理计算大小与 SHA-256，并原子写入 `pipeline-result.json` 和 `evaluation.json`。

真实子进程只继承 Windows/PATH、CUDA/NVIDIA 和明确允许的 Re3D 运行变量，不继承 `DATABASE_URL`、JWT、SMTP 密码或测试数据库地址。Windows 使用 `taskkill /T /F` 终止进程树；Linux 迁移后使用独立进程组的 TERM/KILL 两阶段终止。

成功时标准输出只包含一个 JSON 摘要，不输出图片路径、用户信息或内部 traceback。退出码约定：

- `0`：成功或安全复用已有结果；
- `2`：请求、完整性、路径或受控运行错误；
- `75`：开发专用模拟中断，用于恢复测试。

## 事件和恢复

事件通过追加 `manifests/pipeline-events.jsonl` 保存，每行完成后执行 `flush` 和 `fsync`。重新启动时，Worker 验证所有旧事件的 Schema、任务身份和连续序号。

模拟器以“已记录且哈希匹配的分支产物事件”作为恢复检查点：

- 已完成分支不会重复生成或重复发送产物事件；
- 未完成分支继续执行；
- 恢复动作追加 `WORKER_RESUMED` 警告事件；
- 已存在且身份匹配的最终结果会直接复用；
- 终态事件之后禁止继续追加普通事件；
- 事件文件末行不完整时拒绝自动猜测或截断，等待明确的恢复策略。

开发测试可以使用 `--crash-after-branch A-v4` 主动在检查点中断。该参数不能出现在生产 Worker 服务配置中。

## 原子写入

产物清单、输出校验和最终结果先写入目标目录内的随机临时文件，刷新并关闭后通过 `os.replace` 替换目标文件。因此 API 不会读取到只写了一部分的 JSON。

事件文件采用追加模式，依赖数据库任务租约保证同一任务只有一个 Worker 写入。开发队列消费者使用：

```powershell
.\.venv\Scripts\python.exe -m apps.worker.main run-queued-once
```

`run-queued-once` 只领取 `execution_mode=simulated`；`run-real-queued-once` 只领取 `execution_mode=real`。两者共享同一资源槽，因此不会在同一 GPU 上并发执行。独立的 `simulate` 与 `real-dry-run` 命令仍是开发诊断入口，不会自动取得租约。

真实管线的细粒度 Re3D 步骤写入 `pipeline-events.jsonl`。数据库状态机保持单调，分支交错执行期间主要停留在 `dense_reconstruction`，同时持续增加进度；管线退出后再完成产物校验和评估状态。取消最迟在下一次数据库心跳后被进程监督器观察到。

## 模拟 GLB

每条分支生成一个只包含 glTF 2.0 asset 和空场景的最小 GLB。文件具有正确的 `glTF` 头、版本、总长度和 JSON chunk，可用于验证：

- 文件归档和下载；
- MIME 类型；
- SHA-256；
- 前端 Three.js 加载流程；
- A/B/C 分支切换。

它不包含网格、材质或纹理，不能作为重建质量样例。

## 尚未实现或尚未验收

- GPU/CPU/磁盘资源采样；
- 失败任务目录清理；
- handoff、连通分量、非流形边等更完整的真实评估指标；
- 任意时刻进程崩溃后的部分文件修复。
- 使用真实 Re3D 和小型图片集完成一次从 real 入队到终态的 GPU 验收。

因此当前代码已经具备真实执行控制路径，但在小型真实数据集验收完成前，不能宣称真实网页重建已经可用。

## 下一步

下一步使用小型有效图片集显式创建 real 任务，运行一次受租约保护的真实 Re3D 验收，并根据实际耗时、日志和评估报告修正错误分类；之后实现经过鉴权的产物下载。API 仍只读取数据库投影和受控产物，不直接运行管线。
