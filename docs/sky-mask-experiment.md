# 本机天空蒙版 C 路线：仅 SfM 后半程临时实验

本功能仅属于 `experiment/local-c-only`，不合并 `main`，不改变网页默认三分支或冻结算法 `e517a161b4c0895f8be4258fb339272e4f99e1d6`。输入准备与受监督执行使用不同入口，均默认无操作，不自动提升权限、连接服务器或重试。启动前检查不是执行许可。

## 作用范围

复用既有 SfM 相机和文本模型，天空蒙版仅影响后续 OpenMVS 稠密及纹理输入。旧特征、匹配和相机没有使用这批天空蒙版，不能称为“完整 masked SfM”。真实 Densify/Texture 的过滤效果仍须单独授权并验收；准备脚本读取成功不等于重建已成功。

冻结的 `prepare_dataset.py` 和 `prepare_openmvs_texture_input.py` 只读取图像 alpha；JPG 没有 alpha，会生成全白标签。因此适配层不只复制 PNG 或增加一个配置路径，而是：

1. 绑定已验收的外部蒙版清单、原图 SHA 和两种命名标签，核对尺寸、二值及全白例外。
2. 按图像 SHA-256 建立原始图片与平台改名图片的一一映射。禁止按序号匹配；拒绝歧义、缺项或额外输入，保留 SfM 文本模型的原有文件名。
3. 仅在新实验目录执行固定哈希的两个准备脚本。通过**模块局部** Image 读取适配将 `source_alpha AND sky_keep` 作为内存 alpha，冻结文件不变，其他模块的 PIL 行为不变。
4. 冻结 OpenMVS 准备脚本实际用合并后的 alpha 进行前景颜色延伸，并写出 `<SfM-image-stem>.mask.png`。输入照片仍为原始字节的独立副本，派生的 OpenMVS RGB 图不是原图修改。
5. 读取输出标签，逐图核对像素摘要、命名和清单，不能用“文件存在”代替实际消费证据。绑定原始天空标签与有效 alpha 的区别；Lighthouse 的 JPG 没有原透明区域，二者应逐像素相等。

蒙版黑色 `0` 为排除，白色 `255` 为保留。`final-v2` 共300图、两套命名共600个PNG；`00057.jpg`、`00302.jpg` 保守全白，不能把它们描述成完成了有效天空剔除。结构周围天空与栏杆孔隙有意保留。

## 模块与默认行为

- `backend/experiments/sky_masks.py`：素材绑定、固定准备脚本消费与像素门禁。
- `backend/experiments/sky_sfm_binding.py`：仅图像及五个文本模型的只读 SHA 映射；不读取数据库或二进制模型。
- `backend/experiments/sky_mask_plan.py`：复用原8个 C 阶段，只替换03准备阶段。其余7阶段命令不变，Densify 使用目标 images 的 `--mask-path`、黑色忽略；Texture 保留黑色忽略及图片旁 `.mask.png` 约定。OpenMVS/Texture 为8线程。
- `scripts/run_sky_mask_experiment.py`：默认无操作；显式准备模式只生成独立素材、绑定、阶段计划和检查报告，不执行该计划。
- `sky_assets.py`：核对已验收标签、派生 RGB、实际消费回执及五个文本模型，生成内容哈希库存；稠密和纹理阶段启动前复核。
- `c_budget_chain.py`：只读核对原始 C claim／charge 与全部连续续接记录，拒绝缺号、分叉、未结算 claim、回滚或增加额度；后续明确执行时才允许排他创建下一条续接 claim。
- `sky_supervision.py`、`sky_executor.py`：独立准入及原 `CStageSupervisor` 接线，沿用 Job Object、资源保护、证据、停树与收尾证明；不改旧 C 执行入口。
- `scripts/run_sky_c_supervised.py`：31 文件独立封印闭包，先验证代码清单再导入模块，03 子进程以 `-I -B` 验证同一封印；旧 C-only 的23文件闭包不扩张。

```powershell
.\.venv\Scripts\python.exe -I -B .\scripts\run_sky_mask_experiment.py
```

输出 `not_executed / execution_authorized:false`，不探测服务、加载 `.env`、读取私有预算、启动后台线程或发起网络访问。

## 准备与复查

真实准备应在既有算法 Python 中运行，以复用 NumPy、Pillow、SciPy 及当前 CPU 原生运行时；不安装依赖或重新封装整套环境。模式为 `--mode prepare --prepare-inputs`，必须提供：

```text
--source <原始图片目录> --masks <final-v2目录> --manifest-sha256 <蒙版清单SHA>
--pipeline-root <冻结算法目录>
--handoff-root <已验证SfM交接包> --handoff-sha256 <handoff清单SHA>
--sfm-input-manifest <与handoff绑定的原SfM输入清单>
--runtime-manifest <现有CPU运行时清单> --runtime-manifest-sha256 <其SHA>
--output <父目录已存在、目标尚不存在的新准入目录>
--experiment-root <_experiments下尚不存在的新计算目录，仅记录不创建>
```

新准入目录包含 `binding.json`、`plan.json`、`dataset/`、`openmvs-input/` 和 `preflight.json`。两份 `sky-mask-consumption.json` 保存实际读取和输出标签的逐图摘要。复制的五个文本模型与 handoff 中对应哈希一致；不复制/哈希大数据库或权重。所有原图、旧成果、旧清单和预算保持不变。

`--mode preflight --binding <文件> --binding-sha256 <SHA> --output <准入目录> --pipeline-root <算法目录> --experiment-root <新计算目录>` 仅重新读取准备结果并检查计划，不写入、不重复制、不重准备或领取预算。无需为了查看已有报告而重复整轮准备。

资源检查保持主机可用 RAM 至少8 GiB、D盘运行保留20 GiB、启动空闲至少28 GiB。准备检查发生在小图像操作之间，有600秒期限，不是实际重建阶段的1秒子树保护或峰值保证。

## 与真实执行的边界

`mask_input_and_plan_preflight_passed` 仅证明素材、改名映射、准备脚本消费、文本模型和 CPU 运行时身份、阶段命令接线通过。它不是计算许可，未启动 Interface/Densify/Reconstruct/Texture，也未验证原生过滤后的几何效果。

现有 C 监督器及网页入口不被这个工具改写。独立监督入口只替换03准备命令，其余七阶段沿用冻结 C 计划。实际执行前仍需明确授权、当前资源及完整账本复核、排他预算领取和新工作目录；没有原生计算准入或自动续跑。

旧 C 已经消耗过同源额度，不能换 scene 重领原始余量。sky profile 内部保留原 SfM 预算证据，另外绑定 C 根 claim／charge、最新链尾和剩余额度上限。核账必须读取固定私有账本下同源的全部记录，保守采用更大的累计消耗、更小的剩余；普通 token 无权读取时失败关闭，不能用历史公开摘要代替真实账本。准入既不写 claim／charge，也不消耗或重置计算额度；未来执行从准入开始扣时，收尾另写不可覆盖的续接 charge。

`run_sky_c_supervised.py --mode preflight` 必须提供封印工具 SHA、sky profile及其 SHA、原 CPU profile及其 SHA、已验收终态、CPU runtime清单及其 SHA、冻结算法目录，以及 `--output` 指定的全新小回执路径。输出不能位于原图、蒙版、SfM 来源、旧成果、计算或私有证据目录内。它只读核对源 SfM、蒙版和已准备输入，不导入或复制模型、不创建计算目录、不启动原生程序、监视线程、防睡眠或预算 claim。通过状态为 `read_only_sky_c_preflight_passed`，其中 `execution_authorized:false`。机器状态变化后不能把旧回执视为启动许可证。

后续真实执行另需 `--mode execute --execute-sky-c-only`。该开关不能用于绕过管理员权限、最新账本链尾、8 GiB可用内存线或累计四小时限制；入口不自动弹 UAC。准备和离线合成测试不证明 OpenMVS 实际过滤、模型完整性或几何精度。

算法环境可能安装了另一个名为 `scripts` 的包；CPU运行时验证器按本仓库精确文件路径加载，不能依赖该名称的隐式模块解析。源码候选尚未提交或封装时必须保留 `uncommitted_candidate` 身份，不冒充已有发布提交。

## 2026-10-15 本机准备结果

本轮真实300图已通过改名 SHA 一一映射、两条冻结准备路径各300张蒙版的实际消费与逐像素摘要门禁、五个 SfM 文本模型副本哈希及120个 CPU运行时文件检查。仅输入准备与计划检查通过，未执行原生重建、未创建计算场景或领取预算。专项测试85项通过，共享后端门禁1034项通过、2项按环境/显式准入条件跳过。

机器相关素材和完整回执留在仓库外：`Re3D-data/experiments/lighthouse300-sky-c-admission-20261015-v1`，汇总为 `Re3D-data/acceptance-logs/lighthouse300-sky-c-input-admission-20261015.json`。该历史结果只证明本轮未提交候选及对应素材，不能替代后续真实执行的监督闭包和累计预算准入。

## 2026-10-15 监督闭包与只读预算准入

在独立31文件候选闭包中接入准备适配器，专项合成回归310项通过。一次授权的只读管理员核账及最终准入于北京时间23:18完成，耗时约23秒：300图素材、源 SfM、120个 CPU 运行时文件和八阶段计划通过；账本仅有原始已结算 C 运行，没有未结算或隐藏续接，核账前后清单及哈希相同。

总额度14400秒；SfM已消耗4920.218秒、此前 C 已消耗2244.860秒，累计7165.078秒，剩余7234.922秒。新 profile不改变原 SfM 预算证据，通过独立账本链绑定这份剩余上限，未写新 claim／charge或重置预算。内存可用安全线仍为8 GiB，OpenMVS及Texture仍为8线程。

机器回执位于 `Re3D-data/_deployment/local-c-sky-supervision-admission-20261015-v1/final-admission.json`，封印 SHA 为 `2b0053cededf4eb71c744cd7b54be45471945e6b4eeed71995c14282c61ebe7a`。候选基于 `52775f6453ef6bfab32684e2ffc6160ccf55d924` 上的未提交源码，不是已发布 clean HEAD。只读管理员进程已退出；没有创建计算目录、启动重建、修改源权限或访问服务器。该结果仍不是蒙版原生重建、模型视觉或几何精度验收。

该轮共享后端检查在入口测试 fixture 冻结前已启动，结果为1250通过、3失败、2跳过，不能记为最终源码的全量绿灯。3项混合快照失败在冻结源码上定点复测通过，且已包含于该轮最终310项专项通过中；该轮检查结束时31源码文件与准入候选逐字节一致。该轮未重复整轮后端检查，后续提交前需在不再并发编辑的冻结快照上执行完整门禁。历史记录见 `Re3D-data/acceptance-logs/lighthouse300-sky-c-supervision-admission-20261015.json`。

## 从明确实验分支提交封装

`scripts/build_sky_c_toolpack.py` 从干净的 `experiment/local-c-only` 当前 HEAD 的 Git blobs 构建，拒绝其他分支、不同提交或脏工作区。代码和说明规范化为 UTF-8/LF；不从工作区读取运行数据，也不重新进行真实预算核账。

```powershell
.\.venv\Scripts\python.exe -I -B .\scripts\build_sky_c_toolpack.py `
  --repository-root 'D:\3Dreconstruction\Re3D-platform' `
  --source-commit '<完整的实验分支提交 SHA>' `
  --output-dir 'D:\3Dreconstruction\Re3D-data\releases\<全新发布目录>' `
  --build-toolpack
```

默认不构建、不执行。正式工具清单使用 `source_state:git_commit` 和完整 `source_commit`，仍固定31文件监督闭包；额外的输入准备脚本作为 support file 列入包级哈希清单，不扩大封印导入白名单。包还包含未绑定模板、说明和第三方声明，不包含真实 profile、私密配置、密码、原图、数据库、断点、环境、模型或重建成果。

封装完成后离线解包并核对全部成员哈希，检查默认入口无计算行为；外部 `package-receipt.json` 记录源码提交、ZIP SHA-256、成员清单、工具及包清单 SHA 和恢复验证结果。哈希用于相对可信回执检测变动，不是独立签名或新的运行许可。旧候选的只读准入不自动变成新发布包的真实重建验收；实际启动仍需重新绑定发布身份、读取最新预算链、检查资源并取得明确授权。
