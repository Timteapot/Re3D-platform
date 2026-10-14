# 本机 CPU SfM 与服务器交接

此流程把独立 300 图实验的完整 SfM 放在本机运行，完成后将经过校验的数据库与模型交给服务器继续后续阶段，减少服务器等待 CPU 匹配的在线时间。它不是平台任务恢复接口，不更新旧任务、租约、请求、结果或网页进度，也不自动启动服务器或 GPU 运算。

## 当前断点

2026-10-14 已下载最新 12 线程短测的停止后一致性数据库，并复用此前已保存的原图、预处理图片与掩码。本机 SHA、来源身份、SQLite 完整性和新目录恢复已通过。数据库为 442,941,440 字节，含 300 张图像、26,842 条已处理图像对记录；已处理记录包含空匹配，不等于有效几何匹配数，也不表示完整 SfM 成功。

原静态断点与最新数据库保持分开保存，原清单不被改写。恢复验证目录仅用于证明可恢复，不作为实际运行目录。每次真实 CPU 实验、导出和服务器导入都使用新的路径，不覆盖已有实验。仓库外验收记录为 `Re3D-data/acceptance-logs/lighthouse300-hybrid-download-recovery-20261014.json`。

服务器节省停机已由操作人员确认。驱动、环境、原始数据和私密配置仍需按各自备份策略保留；这一断点下载不等于完整服务器恢复封装。

## 模块与边界

| 工具 | 作用 | 不执行的操作 |
|---|---|---|
| `hybrid_checkpoint.py` | 原静态输入与最新 SQLite 快照绑定，校验并建立独立副本 | 修改源断点清单、覆盖旧任务、伪造完成标记 |
| `cpu_runner.py` / `run_cpu_sfm_stage.py` | 单个 `01-colmap` 阶段，实际提取/匹配/Mapper 参数检查，进程树监督和资源保护 | GPU 查询、后续三分支、PostgreSQL 队列或平台服务操作 |
| `snapshot_store.py` / `evidence.py` | 周期 SQLite online backup 与私有 C 盘终态证据 | 未验证快照晋级、无限保留、自动扩容或延长预算 |
| `hybrid_handoff.py` | 完整 SfM 导出、新目录导入、数据库/模型/指标复核 | 旧网页任务续跑、启动剩余计算、认证几何精度 |
| `build_hybrid_toolpack.py` | 从干净 HEAD 的固定 Git 文件生成独立工具包和 SHA 清单，离线解包验证 | 打包秘密、图片、数据库、模型、完整 Python 环境或服务器镜像 |

工具入口在导入运行模块前核对外部固定的 SHA 清单，使用独立模块命名空间和固定源码加载，避免加载平台包初始化逻辑或未固定缓存字节码。算法解释器仍是本机已有环境，工具包不替代其安装和兼容性验收。

## 本机候选配置

参考配置位于 [`config/hybrid-cpu.example.json`](../config/hybrid-cpu.example.json)，针对本机 32 逻辑 CPU、34,141,495,296 字节物理 RAM 和现有 Python 3.12.14 / PyCOLMAP 3.13.0 CPU 环境制定；其他主机不能直接套用。

| 项目 | 候选值与作用 |
|---|---|
| 提取/穷举匹配 | 12 线程；可另建 8 线程 profile，不同时运行两个实验 |
| Mapper/BA | 顶层与 Mapper 预算 16 线程，不宣称是进程总线程硬上限 |
| 数据盘 | 8 GiB 预留 + 20 GiB 安全线，启动空闲至少 28 GiB；运行中空闲不高于安全线即停止自有计算树 |
| 可用主机 RAM | 必须高于 8 GiB；每秒检查，探测失败或失鲜也停止 |
| 采样 | 主机资源每 5 秒、目录大小每 15 秒；CPU 档不创建 GPU 探针 |
| 断点 | 每 600 秒 SQLite online backup，验证后保留本实例最近两份有效快照 |
| 执行预算 | 14,400 秒，全程不按阶段重置、不自动延长 |
| C 盘证据 | 新建私有证据目录要求空闲至少 10 GiB，运行下限 2 GiB；先保存小型终态，再尝试 D 盘最终快照和来源复核 |

实际执行需要 Windows 管理员权限和已有的 `C:\ProgramData\Re3DRestricted` 普通目录，私有子目录只授予 Administrators 与 SYSTEM。该父目录及本机空闲容量在开始真实运行前单独预检；默认准备不会创建它。固定管线必须保持干净的 `re3d-pipeline-v1.1.3`，解释器路径必须与 `paths.local.json` 相同。

资源值是候选保护策略，不是操作系统硬配额。周期采样可能遗漏瞬时内存尖峰或临时文件峰值，不能保证断电时的最新进度。匹配短测通过也不证明本机完整 Mapper/BA 或服务器后续阶段通过。

## 入口和操作顺序

下列命令中的变量是非秘密路径或校验摘要。`$ToolRoot` 指向新工具包恢复目录；`$ToolSha` 必须来自封装时保存在包外的 `hybrid-tool-manifest.json` 摘要，不能只信任接收包自报的值。

```powershell
$Python = 'D:\3Dreconstruction\Re3D-platform\.venv\Scripts\python.exe'
$ToolRoot = 'D:\3Dreconstruction\Re3D-data\releases\<新工具包>\restore-verification'
$ProfileFile = 'D:\3Dreconstruction\Re3D-platform\config\hybrid-cpu.example.json'
$ToolSha = '<封装记录中的64位SHA256>'
```

1. 本机开发目录可以先执行纯准备。命令只读 profile 并检查配置形状，不读输入断点，不探测主机/GPU/队列，不建立实验目录。输出准备通过不代表实际运行准入通过。

   ```powershell
   & $Python 'D:\3Dreconstruction\Re3D-platform\scripts\run_hybrid_experiment.py' --mode prepare --profile $ProfileFile
   ```

2. 本轮修改提交完成后，从干净 HEAD 封装新工具。默认构建入口只准备，实际封装必须显式 `--build-toolpack`。源提交须为完整 HEAD SHA，输出目录必须不存在；有未提交或未跟踪文件时拒绝封装。

   ```powershell
   & $Python 'D:\3Dreconstruction\Re3D-platform\scripts\build_hybrid_toolpack.py' --build-toolpack --repository-root 'D:\3Dreconstruction\Re3D-platform' --output-dir 'D:\3Dreconstruction\Re3D-data\releases\<新工具包>'
   ```

3. 另行授权真实 CPU 运行后，使用管理员窗口及封装摘要执行。CPU 入口自己创建全新副本，不能指向恢复验证目录或先前已经恢复的目录。`verify-source`、`restore-source` 为独立诊断命令，不必在每次 CPU 运行前重复执行；真实入口已包含必要准入和来源复核。

   ```powershell
   & $Python "$ToolRoot\scripts\run_hybrid_experiment.py" --mode cpu --execute-cpu --profile $ProfileFile --tool-manifest-sha256 $ToolSha --pipeline-root 'D:\3Dreconstruction\Re3D' --platform-root 'D:\3Dreconstruction\Re3D-platform'
   ```

   完整 CPU 成功要求进程树/断点线程确证停止、资源保护无异常、SfM 数据库和二进制/文本模型通过门禁、来源未修改及终态证据保存。只有匹配记录增加、退出码为零或文件存在都不够。

4. 成功后导出至全新包目录。入口复核实际 CPU 参数、完整模型与静态输入，不导出伪造阶段标记或平台请求。输出的 handoff manifest SHA 应独立记录，作为服务器接收门禁。

   ```powershell
   & $Python "$ToolRoot\scripts\run_hybrid_experiment.py" --mode export-sfm --profile $ProfileFile --tool-manifest-sha256 $ToolSha --pipeline-root 'D:\3Dreconstruction\Re3D' --bundle-root 'D:\3Dreconstruction\Re3D-data\handoffs\<新SfM包>'
   ```

5. 之后再恢复服务器，传输 SfM 包、新工具及所需来源断点。接收 profile 的来源路径需对应服务器已保存的同 SHA 数据；线程/运行时项描述本机 SfM 来源，导入模式不据此启动服务器 CPU 计算。可先 `--mode verify-handoff`；真正导入用相同参数追加新的目标目录。

   ```powershell
   & $ServerPython "$ServerToolRoot\scripts\run_hybrid_experiment.py" --mode import-sfm --profile $ServerReceiveProfile --tool-manifest-sha256 $ToolSha --bundle-root $ServerBundle --handoff-manifest-sha256 $HandoffSha --target-root 'D:\Re3D-data\_experiments\<新服务器实验>'
   ```

   导入重新检查 SHA、300 图/特征/图像对身份和 SfM 模型；保留导入前数据库及原指标，仅把指标的 `binary_model`、`text_model` 两项路径重绑到新目录。返回的下一阶段是 `02-extract-cameras`，没有执行它。

## 后续服务器验收

导入通过后，仍需接入从 `02-extract-cameras` 开始的受监督后续执行、目标机容量/GPU/模型/停止服务和只读队列门禁，并单独授权。不能直接调用原完整长测入口来宣称跳过 CPU：其旧路径会恢复原断点并重新执行 `01-colmap`。

服务器剩余步骤包含相机导出、C 分支稠密重建、A/B 推理、融合、网格和贴图，仍有 CPU 工作，不是纯 GPU 阶段。最终要验收三分支 12 个标准产物、SHA/GLB/结构指标、阶段与 RAM/VRAM/磁盘观测，独立结果不自动改写旧网页任务成功状态。
