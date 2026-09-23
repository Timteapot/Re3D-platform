# Re3D 运行契约 v1

## 1. 为什么需要契约

当前 Re3D 入口 `scripts/run_pipeline.py` 使用 CLI 参数启动，并通过控制台文本报告步骤。文本适合人工调试，但不适合平台可靠地恢复任务、推送进度或区分可重试与不可重试错误。

运行契约在 API、Worker、Re3D 适配器和评估器之间建立稳定边界。它不替换 Re3D 内部清单，而是把内部结果归一化为平台能够长期读取的格式。

## 2. 文件生命周期

```text
API/编排器
  └─ 写入 manifests/pipeline-request.json
       ↓
Worker 获取租约并校验请求
  ├─ 追加 manifests/pipeline-events.jsonl
  ├─ 调用固定版本 Re3D
  ├─ 校验并归档 output/A-v4、output/B-v2、output/C
  └─ 原子写入 manifests/pipeline-result.json
       ↓
评估器读取 result 和诊断清单
  └─ 原子写入 reports/evaluation.json
```

请求在进入 `queued` 状态前生成，创建后不可修改。结果和评估先写入同目录临时文件，完成 `flush` 和关闭后再原子重命名，防止 API 读到半个 JSON 文件。

## 3. 请求契约

`pipeline-request` 包含以下不可变信息：

- `request_id`、`job_id`、尝试次数和创建时间；
- 不包含邮箱、用户名等 Worker 不需要的身份信息，只传内部 `user_id`；
- Re3D 标签、提交 SHA 和配置 SHA-256；
- 输入 manifest 哈希、图片数和总字节数；
- 固定的 `A-v4`、`B-v2`、`C` 三分支；
- 相对任务目录的存储位置；
- 超时、磁盘配额和单 GPU 并发限制；
- 用于防止重复执行的幂等键。

Worker 需要先验证 Schema，再验证当前检出的 Re3D SHA、配置哈希、输入 manifest 哈希和任务租约。任一不一致都不得启动 GPU 进程。

## 4. 事件契约

`pipeline-events.jsonl` 是追加日志。每一行是完整 JSON 对象，单行写入完成后换行并刷新。核心规则：

- 同一 `job_id + attempt` 的 `sequence` 从 1 开始严格递增；
- `event_id` 全局唯一，API 可以据此幂等消费；
- `stage_progress` 必须包含阶段和进度；
- `artifact_created` 必须包含分支和产物元数据；
- `job_failed` 必须包含稳定错误码、分类、是否可重试和安全消息；
- traceback、命令行、服务器绝对路径和令牌不得进入事件文件或 API 响应。

JSON Schema 能校验单个事件；序号连续性、合法状态转换和终态唯一性由 Worker/API 集成测试负责。

## 5. 结果契约

`pipeline-result` 只在任务进入终态时生成，描述：

- 整体状态与耗时；
- 实际运行的管线版本和配置哈希；
- 输入图片数、SfM 注册数和平均重投影误差；
- A-v4、B-v2、C 各自的状态、耗时、产物和摘要指标；
- 输入、事件和输出验证报告的相对路径；
- 失败时的稳定错误对象。

整体状态为 `succeeded` 时，Schema 要求三条分支都成功。允许失败结果记录已成功分支，便于诊断，但失败任务的文件仍按项目规则在终态持久化后整体删除。

## 6. 评估契约

`evaluation` 明确使用 `scope = structural_health`。共享维度包括输入、SfM 和性能；每条分支分别包含深度、网格和产物完整性。

每个检查项必须包含实际值、单位、证据文件和状态。阈值存在时一并保存，保证页面结论可解释。`limitations` 至少包含一条限制说明，明确没有真值时不能宣称几何绝对精度。

评分允许为 `null`。在评估规则尚未校准时，平台可以只显示 `pass/warning/fail/not_available` 和原始指标，不应为了页面效果强行生成总分。

## 7. 路径与安全

所有跨组件契约只允许使用任务根目录内的正斜杠相对路径：

- 允许：`output/A-v4/mesh.glb`
- 拒绝：`D:/Re3D/output/mesh.glb`
- 拒绝：`../other-job/mesh.glb`
- 拒绝：`\\server\\share\\mesh.glb`

Worker 将相对路径解析为绝对路径后，还必须再次检查最终路径位于当前 `jobs/<job_uuid>` 内。Schema 校验是第一层约束，不能替代文件系统边界检查。

## 8. 与当前 Re3D 的映射

| Re3D 现有信息 | v1 契约位置 |
|---|---|
| `input/input-manifest.json` | request 输入摘要、result 诊断引用 |
| `reconstruction_metrics.json` | result `input_summary`、evaluation `sfm` |
| A/B `consistency_manifest.json` | result 分支 metrics、evaluation `depth` |
| `artifact_manifest.json` | result 分支 artifacts |
| `outputs/<scene>/validation.json` | result output validation、evaluation mesh/artifacts |
| CLI 阶段输出 | 由适配器转换为版本化 event，不直接向前端透传 |

下一实现步骤是编写 Windows Worker 适配器：读取请求、校验基线、把任务相对目录映射到 Re3D、生成结构化事件并归一化结果。该步骤需要对 Re3D 增加可配置的工作/输出/日志根目录，不能继续依赖算法仓库下固定的 `work`、`outputs` 和 `logs`。
