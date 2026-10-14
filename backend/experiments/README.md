# 独立断点性能实验

此模块仅用于经过明确授权的 Windows 受限环境实验，不是平台任务恢复接口。不会提交平台任务、更新旧任务/租约、启动服务，或修改冻结 Re3D 管线。

## 服务器完整实验运行边界

- 四项平台服务必须停止；不得存在其他重建进程或上传。
- Windows 平台 venv 使用重定向启动器时，只认可经直接父子关系、实际解释器路径、等价参数及创建时间验证的自有启动器；运行中固定创建时间防止 PID 复用，不豁免无关 Python 或其他祖先。
- 默认要求空队列。只有明确启用 `--allow-expired-source-job` 时，允许保留指定旧任务的 `sfm` 记录及已经过期的租约；仍不修改它们。
- 平台和管线版本、配置 SHA、Python/PyCOLMAP 版本必须符合固定身份。实验工具通过独立 overlay 部署，不写入服务器仓库。
- 模型校验清单 `models/checksums.json` 固定为冻结 Git 版本的 LF 指纹，仅允许 CRLF→LF 的换行转换；其余字节及其他 SHA 门禁保持严格校验。实际执行报告分别保存原始字节 SHA 和 LF SHA；此检查不重新计算所有模型权重的哈希。
- 断点必须通过 DONE、manifest、文件 SHA、300 图名称映射、特征/匹配数据库结构及完整性验证。源断点只读，每轮建立新的 `_experiments` 副本，不复制旧平台请求、事件、结果或伪造阶段完成标记。

## 实际监督

| 检查 | 周期 | 停止条件 |
| --- | --- | --- |
| 磁盘 / 主机 RAM | 1 秒 | 空闲磁盘不高于 20 GiB，或可用 RAM 不高于 32 GiB |
| GPU / 显存 | 5 秒 | GPU 探针不可用，或剩余显存不高于 2 GiB |
| 服务 / 队列隔离 | 3 秒 | 其他重建、服务启动、活动上传或不允许的任务/租约 |
| 资源日志 | 5 秒及阶段变化 | 写入失败或探针/线程超过新鲜度期限 |
| 实验目录大小 | 15 秒 | 遍历不跟随 reparse；部分样本记录局限，扫描挂起则停止 |

启动前要求至少 84 GiB 数据盘空闲（64 GiB 预留 + 20 GiB 安全线）。这些是采样保护，不是操作系统硬配额，也不能保证捕捉瞬时尖峰。主机 CPU 是观测项，不是强制 8 线程总上限：COLMAP 可以创建多个线程池。

Windows 子进程先挂起，再加入私有 Job Object 后开始计算；超时、健康检查失败和监督器退出会结束该 Job 的进程树。停止时确认成员退出，避免仍有计算进程时读取数据库。保护失败保留不完整独立实验，不覆盖重用。

## 4 / 8 线程短时对照

`scripts/run_checkpoint_experiment.py` 校验部署清单后加载模块。默认只读预检，不执行匹配；实际执行需要 `--execute-benchmark`、新实验路径和断点路径。

两轮各使用相同初始数据库 SHA、同名预处理图片/掩码、相同质量参数，仅改变 `--num-threads` 为 4 和 8。默认每轮 180 秒；到时安全结束自有进程树，再用 SQLite online backup 包含已提交 WAL 并验证完整性、图像/特征映射和 pair 结构。

私有 `reports` 保存参数、资源 JSONL、pair 计数、结束后的数据库快照及结果。原始 SfM 日志不输出到公开终端，也不提交 Git。终端仅输出阶段、白名单吞吐摘要和稳定错误代码。

新增 `complete_pair_records` 包含已经处理但匹配为空的图像对，不等于有效几何匹配数。单次固定 4→8 顺序受缓存、初始化和剩余图像对复杂度影响；短窗口不能证明完整 SfM/三分支完成时间或几何质量。GPU 配置保持不变，这个 CPU 实验不构成 GPU 阶段性能验收。

## 模块

- `checkpoint.py`：只读验证、全新副本导入、独立 provenance。
- `resources.py`：实际资源保护、新鲜度检查和白名单采样。
- `process.py`：有界输出及自有进程树监督。
- `preflight.py`：只读服务/队列与运行时身份门禁。
- `runner.py`：4/8 短测对照编排、吞吐计时和一致性结束快照。
- `stages.py` / `run_profiled_colmap.py`：真实 31 阶段计划、产物门禁和 SfM/Mapper 调用点参数接线。
- `supervision.py` / `long_runner.py`：全程预算、阶段推进、8/12 准入与完整长测旁路。
- `snapshot_store.py` / `evidence.py`：周期一致性数据库断点、有效指针和独立 C 盘小型终态证据。

修改冻结配置或运行身份时必须重新审查门禁并使用新的实验目录，不得借用旧的成功标记跳过计算。

## 本机 CPU 分段实验

`hybrid_checkpoint.py`、`cpu_runner.py`、`hybrid_handoff.py` 与 `scripts/run_hybrid_experiment.py` 提供独立分段路径；完整操作见 [本机 CPU 与服务器交接](../../docs/hybrid-cpu-sfm.md)。CPU 档只监督 `01-colmap`，不连接数据库队列、不启动平台服务、不构造 GPU 探针。它采用本机 32 逻辑 CPU/约 32 GiB RAM 的单独候选配置，不复用上文服务器 32 GiB 可用内存门槛。

默认准备不等于预检通过或开始重建。完整 CPU 成功后才允许导出 SfM；服务器导入只恢复至新目录，后续 `02-extract-cameras` 及三分支仍需单独执行，不自动写回旧网页任务。

## 离线偏高实验档（2026-10-14）

`high_profile.py` 与 `scripts/preview_high_profile.py` 仅生成/检查配置和本机命令预览，不启动子进程、连接服务器或写入实验运行目录。当前独立候选数据位于仓库外 `Re3D-data/experiments/lighthouse300-high-cpu-v1`。

| 项目 | 偏高候选 | 依据和边界 |
| --- | --- | --- |
| SfM提取/匹配 | 12 | 2026-10-14同源8/12短窗口准入通过，12吞吐约高22.2%；多个线程池，不是全局12线程 |
| Mapper/局部及全局BA | 32 | 显式预算替代自动；不能声称比原自动更快 |
| OpenMVS A/B/C及纹理 | 32 | 40逻辑CPU下预留余量，尚无300图完整阶段峰值 |
| A外层batch / dense-head minibatch | 4 / 1 | 保留已兼容配置；2仅列待准入候选，未启用 |
| B源视角/尺寸/refinement | 7 / 384×384 / 1 | 增大属于几何工作量变化，不当作纯提速 |
| GPU任务/分支并发 | 1 / 串行 | 没有完整GPU阶段峰值，不提高并发 |

`pipeline.high-cpu.json` 的差异仅为四个 OpenMVS/texture线程字段；冻结管线仍不读取 SfM/Mapper线程新增键。`adapt_sfm_argv` 明确生成 `--num-threads 12`；`mapper_budget_adapter` 设置顶层 `IncrementalPipelineOptions.num_threads` 和子层 `mapper.num_threads`，覆盖初次与 deferred retry 选项工厂，并在退出后恢复工厂。两个适配器已接入独立长测的真实子阶段并完成本机测试；12/32 线程候选尚未完成服务器真实长测。当前 4/8 短测 CLI 不接受此高档 profile。

本机stage预览不等于服务器执行参数。其路径需在服务器重启后从真实 `paths.local.json` 重新生成；原生dry-run省略最终输出校验，该校验仍是完整验收必需项。不得直接用冻结 `run_pipeline.py --config pipeline.high-cpu.json` 误认为SfM也已变成12/Mapper32。

### 已接线保护与服务器执行前门禁

- 保持旧任务/租约和原断点不动；新Scene、新根目录，仅导入验证后的DB及同名输入，不复制旧完成标记。
- 每10分钟生成包含已提交WAL的一致性SfM快照，保留最近两份已验证独立快照；不复制活动 `.db` 充当断点，不删除源断点。
- 资源保护或异常先结束自有Job并确认退出。用独立、有界取证策略保存终态，不把已经sticky trip的运行健康检查直接当作取证准入。
- 私有C盘优先保留小型白名单终态证据，准入要求至少10GiB空闲、运行安全线2GiB；D盘风险时先小证据、后按空间决定是否生成大型快照/hash。
- 保留阶段事件、时长、实际argv/选项、pair计数、RAM/显存峰值与磁盘最小空闲及各探针观测时间。GPU候选启用前还需批次前准入、PyTorch批次峰值及有限/有效深度对照。
- CPU80%/60秒只作为候选提升判据，不是现有自动取消规则；CPU满载本身不能直接认定危险。总预算保持4小时，超时不自动延长；OOM后不自动重试。

长测运行器、周期断点与 C 盘终态取证已完成本机接线及合成/原生微型进程验收，不等于完整 300 图或 GPU 阶段通过。高档 profile 保持 `autorun=false`、`server_applied=false`、`full_guarded_launcher_ready=false`；服务器实际预检及同源 8/12 线程短测通过后，才可另行授权完整运行。

`2358f9e` 独立包已完成上述短窗口准入，详情见 [目标服务器 CPU 验收](../../docs/target-server-cpu-admission-2026-10-14.md)。180 秒窗口只实际验证终态快照，未触发 600 秒周期快照；后续完整模式继续使用此包，不因文档更新替换运行代码或重新绑定工具 SHA。

## 工具包与恢复边界

`scripts/build_long_toolpack.py` 从指定 Git 提交读取源码允许列表，生成独立 ZIP 和完整内容清单。`tool-manifest.json.files` 固定为 14 个运行代码文件；配置、文档和完整归档身份由另一份 `bundle-manifest.json` 记录。包不包含平台依赖环境、模型、驱动、数据库、原图、来源断点、私密配置或原日志，因此不是新服务器完整环境封装。

工具的 `source_commit` 是本次源码来源；`runtime_platform_commit` 固定为 `6e8ad0c`，算法管线仍为 `re3d-pipeline-v1.1.3` / `e517a16`。服务器执行要求运行平台仓库干净且 HEAD 符合此基线。工具解包到独立新目录，不写入 `D:\Re3D-platform` 或冻结算法仓库；入口优先加载解包工具模块，普通 backend 依赖仍来自平台。

默认离线检查示例（本机 Python 可直接调用，无需激活 PowerShell 脚本）：

```powershell
& 'D:\3Dreconstruction\Re3D-platform\.venv\Scripts\python.exe' '<解包工具目录>\scripts\run_long_experiment.py' --platform-root 'D:\3Dreconstruction\Re3D-platform' --profile '<解包工具目录>\config\performance-profile.json'
```

输出应为 `prepared_no_execution`。这不会连接服务器、查询数据库或运行 GPU。后续真实服务器执行需要单独授权，不能将上述离线检查当作服务器环境验收或直接给旧网页任务标记成功。
