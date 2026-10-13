# 直接 300 图实验：本机准备（2026-10-13）

## 当前范围

当前实验方案为直接尝试一次 Lighthouse 300 图任务，本轮不安排先行
200/250 图重建。本机代码、运行参数、输入集及操作入口已准备完成；
功能提交 `4ccd70e` 已推送，对应源码和前端发布包已校验。目标服务器尚未
应用新配置或提交 300 图任务。这里的配置是实验预算，不是容量通过声明。

功能发布包不包含完整推理环境、驱动、模型、数据库或私密配置；不能替代
新服务器环境封装。后续文档修改不自动更新已生成的发布包，部署发布仍需
保持源码提交、Web 元数据和目标机服务清单一致。

冻结 Re3D 基线仍为 `re3d-pipeline-v1.1.3`，提交
`e517a161b4c0895f8be4258fb339272e4f99e1d6`；不修改算法、图片分辨率或固定的
MapAnything `batch_size=4`。平台不新增 API、请求字段或数据库迁移。

## 实验配置

独立的非秘密配置保存于
[`deploy/restricted/profiles/direct-300.json`](../deploy/restricted/profiles/direct-300.json)。
日常本机 `baseline` 环境文件不切换到服务器的大任务执行档。

| 参数 | 直接 300 图实验值 | 作用 |
|---|---:|---|
| Worker 执行档 | `gpu-large-300` | 启动前检查目标 GPU、CUDA、磁盘和固定 batch size |
| API 重建执行预算 | 14400 秒（4 小时） | 写入新任务的不可变请求；超时监督真实管线子进程 |
| 每用户非终态任务上限 | 1 | 防止重复提交多个待运行的大任务 |
| 单用户存储配额 | 128 GiB | 可容纳现有两次小任务 32 GiB 预留加本轮 64 GiB |
| 每新任务预留 | 64 GiB | API 和 Worker 使用一致的准入与记账参数 |
| 数据盘安全线 | 75 GiB | 保留现有磁盘安全余量要求 |
| Worker 启动所需实际空闲空间 | 139 GiB | 75 GiB 安全线加 64 GiB 新任务预留 |
| 最低总显存 / 启动时最低空闲显存 | 14 / 12 GiB | 通过大任务预检后才领取队列任务 |
| CPU、内存、GPU 资源采样 | 每 5 秒 | 用于本轮资源峰值观测 |
| 任务目录采样 | 每 15 秒 | 用于空间增长、最终值和观测峰值汇总 |
| 图片 / 单文件 / 总输入上限 | 3–300 张 / 25 MiB / 1 GiB | 保持现有上传合同 |
| 成功数据保留 | 原图、运行文件、产物各 30 天 | 保持既有保留策略，自动路径仅 dry-run |

`RE3D_JOB_TIMEOUT_SECONDS` 由 API 读取，默认 3600 秒，允许 60–604800 秒。
提交时值被冻结到 `pipeline-request.json.limits.timeout_seconds`；修改配置
不会重写已有任务。真实 Worker 恢复任务时扣除已消耗的时间。4 小时约束
管线子进程，平台后续产物核验与自动评估还有额外耗时，它不是整机计费时长
的严格上限。64 GiB 预留当前也不是运行中强制磁盘封顶，应持续检查实际空间。

## 300 图输入

- 源目录：`C:\Users\57735\Desktop\GoogleDownload\Lighthouse\Lighthouse`；
- 本机输入目录：
  `D:\3Dreconstruction\Re3D-data\datasets\tanks-and-temples\lighthouse\subset-300`；
- 从按文件名排序的 309 张 JPEG 均匀选取 300 张，保留首尾，原始字节不变；
- 全部为 2048×1080 JPEG，总量 167,770,663 字节（约 160.0 MiB）；
- 最大单图 1,049,879 字节，未发现重复内容；
- 300/300 源文件与副本大小、SHA-256 一致，平台实际图片检查器解码均通过；
- `selection-manifest.json` SHA-256：
  `3d42d8e506350e545cd22430bd40ed61672e2598563c27f48ad139440f9da854`；
- `SOURCE.md` 保留数据来源和非商业使用说明，源图和旧 150 图集保持不变。

可复跑准备与检查，目标已存在时只验证，不覆盖：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  D:\3Dreconstruction\Re3D-platform\deploy\experiments\prepare-lighthouse-input.ps1
```

输入、清单和验收产物放在数据根，不进入平台代码仓库。网页只选取这 300 张
JPEG，不上传目录内的 JSON 或 Markdown 文件。

## 下次服务器开机后的应用顺序

1. 恢复 SSH，先只读检查磁盘实际空闲、GPU、队列和租约。此前约 110 GiB
   空闲是历史记录，不能作为当前值；达不到 139 GiB 时先扩容或处理已备份
   的旧数据，配置工具不会降低安全线或删除结果。
2. 轮换旧服务包时先在旧源码和旧 Web 仍匹配清单的状态下完成卸载与归档，
   然后更新到测试通过的干净平台提交并准备对应前端产物。
3. 在目标机 PowerShell 7 预览参数，再应用到已有 API/Worker 私密环境文件：

   ```powershell
   pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File `
     D:\Re3D-platform\deploy\restricted\set-direct-300-profile.ps1 `
     -ProjectRoot D:\Re3D-platform

   pwsh.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File `
     D:\Re3D-platform\deploy\restricted\set-direct-300-profile.ps1 `
     -ProjectRoot D:\Re3D-platform -Apply
   ```

   默认 dry-run 只输出白名单运营参数；Apply 先验证两个文件、检查服务停止、
   创建受保护备份，再独占锁就地写入并保留原 owner/ACL。未知字段、数据库
   密码和 JWT 等内容不输出、不替换。运行异常触发内容回滚；断电或强制结束
   进程的恢复依赖 `.restricted-profile-backups` 中的受保护副本，该目录被 Git
   忽略。既有 bundle 存在时摘要会提示 `requires_bundle_regeneration=true`。
4. 用新源码、前端和环境配置重新生成完整服务包，通过静态与安装后验收。
   当前包固定环境和构建哈希，修改源码或配置后须重新生成，不能只更新源码
   或复制单个配置文件到运行包。
5. 验证 API 摘要 `uploads.job_timeout_seconds=14400`、配额 128 GiB、任务预留
   64 GiB；验证 Worker 为 `gpu-large-300` 且采样 5/15 秒，队列和租约空闲。
6. 通过 SSH 回环网页提交这一组 300 图，立即记录新任务 UUID 并开始只读监控。
   保持详情页打开，在终态后验收三分支、12 个标准产物、评估、租约释放和
   大 GLB 的自动显示与交互。

## 验收与停止条件

必须记录输入注册数量、各阶段耗时、CPU/主机内存/显存/磁盘观测峰值、
`storage-usage.json`、产物大小/hash 与自动评估。显存不足、连续资源错误或
磁盘逼近安全线时，应通过现有取消机制停止任务并保存失败证据。运行中
监控不会根据 64 GiB 预留自动中止任务。

直接 300 图会少一次阶梯缓冲；成功或失败都应据本轮证据决定下一步参数，
不预先承诺完成耗时或容量上界。完成后转移并验证结果，再停止服务和连接，
按运维流程执行节省停机。
