# Re3D Platform Contracts

本目录保存平台 API、Worker、Re3D 适配器和评估器之间的版本化 JSON 契约。

## v1 文件

| Schema | 生产方 | 消费方 | 推荐文件名 |
|---|---|---|---|
| `pipeline-request` | API/任务编排器 | Worker | `pipeline-request.json` |
| `pipeline-event` | Worker | API/进度投影器 | `pipeline-events.jsonl` |
| `pipeline-result` | Worker | API、评估器 | `pipeline-result.json` |
| `evaluation` | 评估器 | API、前端 | `evaluation.json` |
| `resource-sample` | 真实 Worker | 私有运维诊断 | `resource-samples.jsonl` |
| `storage-usage` | 真实 Worker | 私有容量校准 | `storage-usage.json` |
| `storage-capacity-report` | 运维工具 | 私有容量决策 | 标准输出 JSON |

Schema 位于 `schemas/v1`，使用 JSON Schema Draft 2020-12，`contract_version` 固定为 `1.0`。首期不允许普通用户改变分支或 Re3D 基线，因此请求 Schema 将三分支 `A-v4`、`B-v2`、`C`、标签 `re3d-pipeline-v1.1.3`、提交和配置哈希固定为常量。

请求和结果都包含 `execution_mode`。模拟 Worker 只能处理 `simulated`，真实适配器与真实队列 Worker 只能处理 `real`，两类产物不得混用。restricted/production 稳定上传只提交 `real`；development/test 默认 `simulated` 并允许显式 `real`。模拟产物用于开发和隔离测试，不代表真实重建结果。

请求输入允许 3–300 张图片，这只是契约范围，不表示任何主机都已通过 300 图容量验收。API 将上传检查结果、任务路径和执行预算写入不可变请求；已有任务不会随环境配置调整而重写。`limits.timeout_seconds` 的契约范围为 60–604800 秒，当前 API 默认使用 3600 秒，独立直接 300 图实验预设使用 14400 秒。执行档预检、磁盘准入和实际资源验收仍是 Worker 与部署层的职责。

## 验证

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests/unit -p "test_*.py" -v
```

示例位于 `examples/v1/valid`。事件和资源采样示例是 JSONL，每一行必须分别独立符合对应 Schema。资源采样和空间摘要是任务私有运维证据，不进入用户 API 或质量评分。

需要验证 PostgreSQL 并发、迁移或完整任务闭环时，使用 [`deploy/postgres/README.md`](../../deploy/postgres/README.md) 中的一次性容器流程，不将契约测试指向开发、受限或生产数据。

## 版本规则

- 增加可选字段且旧消费者可以忽略时，可保留主版本并增加 Schema 修订记录。
- 删除字段、修改字段语义、收紧已投入使用的取值或改变状态含义时，发布新的契约主版本目录。
- Worker 必须拒绝不支持的 `contract_version`，不能猜测兼容。
- 平台数据库应同时记录契约版本、Re3D Git SHA、配置 SHA-256 和评估规则版本。
