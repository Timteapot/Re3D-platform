# 本机 C-only 临时实验路线与监督

状态：2026-10-15，定位为 `experiment/local-c-only` 分支上的临时实验，暂不合并到 `main`。300 图 C-only 候选已完成重建，但原附加门禁因混淆原始与加载顶点数而失败；修正门禁后，现有四产物只读复验通过，原失败记录不改写。新源码工具包未重新运行重建；实验提交、远端和归档的实际身份以仓库外发布回执为准。

本工具不改变网页默认 A-v4/B-v2/C 行为或冻结的 Re3D 算法基线，不操作服务器、队列、租约或平台服务，不执行 A/B，不是网页任务续跑接口。主线只接收独立验证的通用 CPU 监督、取证和 SfM 交接修复；实验代码及工具包留在独立分支。源码提交、推送和离线封装均不授权真实重建、UAC 或服务器操作。

## 清理影响

依据仓库外 `archive/2026-10-15-cleanup-execution/README.md`、删除回执及保护文件清单核对：14 个实际删除目标属于旧环境、根级旧产物/日志、弃选实验与旧缓存副本，与当前 SfM、原始 authority、选定 snapshot、工具、平台 `.venv` 及现用算法配置/环境/vendor 无路径交集。

当前 v3 DB、SfM 二进制/文本模型、指标、实际参数和输入清单的元数据存在；冻结脚本/配置和原工具闭包的小文件身份匹配，没有再次全量哈希图片、DB 或权重。清理记录的 2,729 文件哈希核对不包含 `Re3D-data`，不能据此证明私有 v3 内容完整。普通账户读取 `cpu-result.json` 和 v2 snapshot manifest 被拒绝，记录为 `denied / 未核实`，不是 `missing`。

上述清理影响盘点阶段不提升权限、不改 ACL、不删数据。后续经单独授权的复核已确认最终静态快照及模型一致性，并完成新目录的交接包、C profile 绑定和受监督导入；工作库侧车保留且不作为已准入来源。将来新任务仍须独立准入，历史成功回执不替代现存文件门禁。

## 路线与模块

天空蒙版的独立后半程实验见 [sky-mask-experiment.md](sky-mask-experiment.md)，包含输入准备、独立监督闭包和累计预算准入，默认不执行计算。它不改变本页现有执行器，也不授予同源预算重领或重置资格。

```text
完整 SfM + 原始输入 authority → 已验证的 SfM handoff
  → 新 _experiments/<scene>/work 中受监督导入
  → 02 相机 → 03 OpenMVS 输入 → 10 InterfaceCOLMAP
  → 11 稠密/融合 → 12 网格 → 13 纹理/标准化 → 90 仅 C 验证
```

- `c_integrity.py`：固定路径只读盘点，区分 missing/denied/reparse/hash mismatch，大文件哈希延后；始终不颁发执行许可。
- `c_profile.py`：严格本机配置、质量不变的线程 overlay、SfM 终态回执和剩余预算绑定。
- `c_stages.py`：复用冻结 Pipeline 规划 8 阶段，不执行/伪造 SfM，不修改旧 marker；C 四产物门禁。
- `c_runner.py`：复用 Job Object 子树、独立资源保护、C 盘小证据；顺序执行，任一失败阻止后续阶段。
- `scripts/run_local_c.py`：默认不执行，代码清单、CPU 运行时/DLL 清单、只读盘点及显式运行入口。

算法固定 `re3d-pipeline-v1.1.3 / e517a161b4c0895f8be4258fb339272e4f99e1d6`，不修改兄弟算法仓库、不需要 A/B 权重。保留 VC17 Interface/Densify/Reconstruct 与固定 VC18 Texture 两套独立 DLL 闭包。

## 配置与保护

模板为 `config/local-c-only.example.json`；来源 SHA 留空，未绑定时主动拒绝运行。

| 项目 | 默认值与边界 |
|---|---|
| 并行 | OpenMVS/纹理 8 线程，辅助 OpenMP/BLAS 线程池上限 8 |
| 质量 | 稠密 level1/min640/max1024、6 邻视图、原网格参数、seam 关闭、8192 图集上限，不隐式降低质量 |
| RAM | 主机可用至少 8 GiB，运行触线即停止自有子树 |
| D 盘 | 运行保留 20 GiB，默认额外预留 8 GiB，启动至少空闲 28 GiB；不是已测得的容量保证 |
| C 盘 | 只存小证据，创建至少空闲 10 GiB，运行线固定 2 GiB；不支持的自定义运行线会拒绝 |
| 采样 | 主机保护 1 秒，资源 5 秒，目录 15 秒；不查询 GPU |
| 预算 | 原 14,400 秒减去已接受 SfM 的约 4,920.218 秒，余量约 9,479.782 秒；由实际回执保守绑定，不重新给四小时 |

纹理输入分辨率不受稠密 max1024 自动限制；8192 是单图集尺寸上限，不是总内存上限。CPU 构建判断仅针对当前已核对的随仓程序；不是所有 OpenMVS 都为 CPU 的声明，也不是通用二进制安全认证。

预算包含本轮 C 导入、准入、计算和阶段门禁。停止计算后的只读取证最多另用 120 秒，不增加计算时间。C 盘受保护父目录按 CPU 结果 SHA 独占写入一次性预算 claim，结束另存 charge；同一源换场景也不能再次领取原余量。没有自动重试、参数调整、claim 删除或重置入口；未来重试须明确授权后补核账接线，先计入本轮 C 已用时间。

表中约 9,479.782 秒是 C 候选首次准入的余量，不是当前可再次领取的预算。本轮 C 结束后的实际 charge 留存约 7,234.922 秒；只读产物复验前后 claim/charge 字节未变。未绑定模板和重新封装工具包均不更新、继承执行许可或重置该账本。

## 操作入口

以下默认命令不探测运行库、不提升权限、不启动计算：

```powershell
.\.venv\Scripts\python.exe -I -B .\scripts\run_local_c.py
```

正式运行前先封装到独立工具根，始终用 `-I -B` 新进程，不能在已导入外来 backend 的长期解释器里启动。准备操作彼此独立，均不运行重建：

1. 开发目录可用 `--mode seal-tools` 独占创建候选 `c-only-tool-manifest.json`，身份是 `uncommitted_candidate`。正式源码包必须使用下述 Git 封装入口，清单采用 `git_commit` 和完整 `source_commit`；二者不能混称。不覆盖旧包。
2. `--mode seal-runtime --pipeline-root <绝对路径> --runtime-manifest <新的绝对 JSON 路径>`：记录 4 原生程序、完整两套 DLL、4 个辅助脚本与算法 Python，不复制环境/权重。
3. `--mode inspect --tool-manifest-sha256 <工具清单 SHA>`：固定依赖存在性盘点。缺失/拒绝访问返回失败，但通过也不表示完整 SfM/运行时准入。
4. 用现有 `run_hybrid_experiment.py` 的导出/验证接口生成完整 SfM handoff；受保护源仍需明确授权下的权限检查，本轮未自动执行。
5. 绑定 C profile 的源 CPU 结果 SHA、SfM 小终态回执 SHA 和 handoff SHA；`source.root` 是 handoff bundle，不是旧实验根。

### 不启动重建的只读 preflight

本节入口为 `590bb1d` 之后新增能力，既有 `590bb1d` 发布包不包含此模式；只按新包的明确源码提交和固定清单识别能力，不将未提交工作树混称为已发布工具。

`--mode preflight` 使用下方 execute 所需的同组 profile、来源、终态回执、工具和运行时 SHA 参数，但**不传** `--execute-c-only`。该命令复用冻结文件、来源/累计剩余预算绑定、运行时文件清单、主机资源快照和 handoff 校验；不导入/复制、不创建目标或证据目录、不领取或核销预算、不提权，也不启动线程或子进程。来源拒绝访问时明确失败，不自动请求 UAC。

通过状态仅为 `read_only_preflight_passed`，并明确 `execution_authorized:false`。Git 干净工作树身份、实际 Python 依赖导入、管理员证据/预算、受监督导入、阶段计划及执行时资源复核仍保留在 execute 中；该快照不是完整运行许可，也不能用 prepare/inspect 的通过替代它。读取期限为协作式上限，不能强行中断操作系统中阻塞的文件读取。

实际 SfM 导出/验证仍应使用独立的 hybrid 工具包及其自身 `hybrid-tool-manifest.json`。不能直接将 C 包内的 `run_hybrid_experiment.py` 当作完整 hybrid 工具包使用：它在 C 包中只是固定源码加载依赖，两个包的清单和文件闭包不同。若工作库存在 WAL/SHM，不删除或忽略；先只读核对最终一致性快照及模型，再决定经授权的交接方式。

实验内的固定操作入口也可以在 C 工具的完整哈希闭包加载后，直接调用已包含的 `hybrid_handoff` API；这不等于运行完整 hybrid CLI。`c_profile.bind_c_profile` 是无 I/O 的绑定函数，只消费调用方已核实的原始字节 SHA、终态回执、CPU profile 和 verified handoff。它将源 CPU 结果、profile、最终数据库、模型 gate、任务/scene、线程、快照来源和累计余量绑定到 C profile；不读取文件、写配置、领取预算或运行阶段。实际写入 profile 必须由明确入口独占创建新文件，并再次执行 preflight。

交接包、绑定 profile、运行时清单与准入记录留在 Git 之外；未提交候选必须标为 `uncommitted_candidate` 并记录实际文件哈希，不能冒用原发布提交。准入通过仍不启动 C，不创建真实 C 工作目录或 budget claim；管理员证据、单次预算领取、受监督导入和执行时资源保护仍属于另行授权的真实执行步骤。

只有 `--mode execute --execute-c-only` 与以下参数齐备才可能执行；普通账户不会自动请求 UAC：

```text
--profile <绑定的 C profile 绝对路径> --profile-sha256 <该文件 SHA>
--tool-manifest-sha256 <C 工具清单 SHA>
--pipeline-root <绝对管线路径>
--source-cpu-profile <原 v3 CPU profile 绝对路径>
--source-cpu-profile-sha256 <原终态记录的 profile SHA>
--terminal-receipt <accepted SfM 小终态回执绝对路径>
--runtime-manifest <CPU 运行时清单绝对路径>
--runtime-manifest-sha256 <该清单 SHA>
```

执行前核对干净冻结算法、源/新目标隔离、完整 SfM、主机资源和 NumPy/Pillow/SciPy/trimesh 实际导入。首次哈希 DLL 闭包；阶段前再次核对程序/脚本哈希和 DLL 元数据变化，不反复全量哈希权重。资源触线、探针失鲜、超时、日志/证据异常或非零退出均停止本工具拥有的子树，不关闭外部应用。

## 结果和局限

### 顶点计数与失败后产物复验

C-only 门禁区分 OBJ 原始 `v` 记录数与模型加载后的顶点数。UV 接缝或法线拆分会使加载后的顶点数大于原始坐标记录数，因此不要求两者相等；原始顶点数及 OBJ/GLB 加载顶点数仍须为有效正整数。原始三角面数、验证报告中的 OBJ/GLB 面数与 GLB 索引三角面数仍严格一致，材质引用、JPEG、GLB 格式及四产物哈希门禁不变。失败代码使用白名单稳定标识，保留 `C_OUTPUT_*`，不公开原始异常内容。

对已完成重建但门禁失败的既有产物，可在单独授权下执行只读复验，另存新回执；这不重跑阶段，不改写原产物、历史失败回执或预算 claim/charge，也不将原失败运行改为成功。复验使用独立封存的修复候选，仍保留 8 GiB 可用内存安全线与既有剩余预算。产物结构复验不等于几何精度认证、网页三分支验收或新一次计算许可。

- 资源日志为 `reports/resource-samples.jsonl`，包含主机 CPU/RAM、磁盘和目录，不等于进程独占内存；采样可能漏掉瞬时峰值。
- 每 5 秒输出白名单摘要，原始环境/私有命令日志不进入摘要。已执行候选使用包外、单次授权的任务绑定 wrapper 发布普通账户可读状态；这些真实绑定文件不随源码包分发。新任务仍须单独确定读取和启动方式，不能直接复用固定 CPU v3 或历史 C launcher。
- `reports/c-stage-XX.json` 是阶段通过回执，不是独立数据副本。中断保留工作文件，但不承诺 OpenMVS 阶段内部恢复，不自动跳 marker。
- `reports/c-result.json` 是监督阶段结果；执行总入口在释放防睡眠请求、确认预算 charge 后另存 `reports/c-final-status.json`。最终成功必须核对后者和独立证据，不能只看前者的 succeeded 或 stdout 退出码。
- 失败/超时/取消先写 C 盘小证据，再有界核对来源；不自动删除失败数据。
- 成功要求 8 阶段、C 的 OBJ/MTL/JPEG/GLB 哈希/大小/引用/面数/格式门禁、进程树停止、观察结束、资源关闭、小证据及来源未改动。结构门禁不是有真值的几何精度认证。
- 未接回旧网页任务；完整 C/DMAP 跨机器交接尚未实现。将来让服务器 A/B 复用本机稠密结果还需资产清单与路径重绑定验收。

源码提交/推送与工具封装不授权启动服务器、UAC 或真实重建。2026-10-15 既有 C 四产物复验通过只证明该批产物的结构与哈希门禁，不代表新源码包已实跑、几何精度已认证、网页三分支已完成或允许重领预算。

## 从已提交源码封装

`scripts/build_local_c_toolpack.py` 只读取干净 HEAD 的固定 Git blob，统一为 UTF-8/LF，生成与恢复内容同字节的 23 文件代码清单。封装须基于 `experiment/local-c-only` 的明确提交，不以 `main` 的通用修复提交替代实验身份。默认仅准备；只有 `--build-toolpack` 才在全新目录写 ZIP、恢复验证目录和 `package-receipt.json`，绝不启动包内计算入口：

```powershell
.\.venv\Scripts\python.exe -I -B .\scripts\build_local_c_toolpack.py `
  --repository-root 'D:\3Dreconstruction\Re3D-platform' `
  --source-commit '<完整的当前提交 SHA>' `
  --output-dir 'D:\3Dreconstruction\Re3D-data\releases\<新的发布目录>' `
  --build-toolpack
```

包包含固定代码、未绑定的 C 配置模板、C/SfM 交接说明和第三方声明。它不包含管理员历史 v1 launcher、真实 profile、原图、数据库/断点、环境、原生 DLL/EXE、模型、凭据或原始日志，不是环境备份或真实任务恢复包。机器相关的原生运行时清单另在包外本地绑定，不提交 Git。恢复校验只证明包完整、默认入口不计算，不能代表 C 运行准入或容量通过。每次实际提交、远端和归档身份以仓库外发布回执为准，不回写历史验收记录。
