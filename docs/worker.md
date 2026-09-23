# Windows Worker 与模拟执行模式

## 当前实现范围

当前 Worker 是真实 GPU 接入前的可执行骨架。它读取和验证正式 v1 契约，模拟共享阶段和 A-v4、B-v2、C 三条分支，生成结构化事件、最小合法 GLB、产物清单、输出校验和最终结果。

模拟器不会启动 Re3D、不会占用 GPU，也不会把模拟指标解释为真实重建质量。请求和结果都必须包含 `execution_mode: simulated`，避免模拟数据进入真实任务统计。

## 模块职责

| 模块 | 作用 |
|---|---|
| `paths.py` | 从数据根目录和 UUID 推导唯一任务目录，阻止绝对路径、反斜杠和 `..` 越界 |
| `contracts.py` | 加载 JSON Schema，并在组件边界验证请求、事件和结果 |
| `events.py` | 追加 JSONL 事件、刷新磁盘、恢复连续序号并保护终态 |
| `io.py` | 文件 SHA-256 和同目录临时文件加 `os.replace` 的原子写入 |
| `simulation.py` | 模拟阶段、生成三分支 GLB、恢复检查点并汇总结果 |
| `settings.py` | 从参数或环境变量读取数据根目录和 Worker 身份 |
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
python -m pip install -r requirements-worker.txt
$env:RE3D_DATA_ROOT = "D:\3Dreconstruction\Re3D-data"
$env:RE3D_WORKER_ID = "windows-gpu-01"
python -m apps.worker.main simulate --job-id <job_uuid>
```

也可以显式传入 `--data-root` 和 `--worker-id`，命令行参数优先于环境变量。

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

事件文件采用追加模式，依赖数据库任务租约保证同一任务只有一个 Worker 写入。当前模拟器尚未实现数据库租约，所以不能把它直接作为多进程生产队列使用。

## 模拟 GLB

每条分支生成一个只包含 glTF 2.0 asset 和空场景的最小 GLB。文件具有正确的 `glTF` 头、版本、总长度和 JSON chunk，可用于验证：

- 文件归档和下载；
- MIME 类型；
- SHA-256；
- 前端 Three.js 加载流程；
- A/B/C 分支切换。

它不包含网格、材质或纹理，不能作为重建质量样例。

## 尚未实现

- PostgreSQL 任务领取、租约和心跳；
- 真实 Re3D 子进程调用；
- GPU/CPU/磁盘资源采样；
- 用户取消和超时终止子进程；
- 失败任务目录清理；
- 评估器执行；
- 任意时刻进程崩溃后的部分文件修复。

因此当前结果证明的是平台协议、目录边界和恢复骨架可用，不代表真实重建已经接入。

## 下一步

下一步是在保留 `re3d-pipeline-v1.0.0` 标签不变的前提下，为 Re3D 新增可配置的工作、输出和日志根目录，形成新的候选管线版本；随后实现真实适配器的 `dry-run`，把 Re3D 步骤映射为本次定义的结构化事件。
