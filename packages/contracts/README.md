# Re3D Platform Contracts

本目录保存平台 API、Worker、Re3D 适配器和评估器之间的版本化 JSON 契约。

## v1 文件

| Schema | 生产方 | 消费方 | 推荐文件名 |
|---|---|---|---|
| `pipeline-request` | API/任务编排器 | Worker | `pipeline-request.json` |
| `pipeline-event` | Worker | API/进度投影器 | `pipeline-events.jsonl` |
| `pipeline-result` | Worker | API、评估器 | `pipeline-result.json` |
| `evaluation` | 评估器 | API、前端 | `evaluation.json` |

Schema 使用 JSON Schema Draft 2020-12，`contract_version` 固定为 `1.0`。首期不允许普通用户改变分支或 Re3D 基线，因此请求 Schema 将三分支、标签、提交和配置哈希固定为常量。

请求和结果都包含 `execution_mode`。模拟 Worker 只能处理 `simulated`，后续真实适配器只能处理 `real`，两类产物不得混用。

## 验证

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -p "test_*.py" -v
```

示例位于 `examples/v1/valid`。事件示例是 JSONL，每一行必须独立符合 `pipeline-event.schema.json`。

## 版本规则

- 增加可选字段且旧消费者可以忽略时，可保留主版本并增加 Schema 修订记录。
- 删除字段、修改字段语义、收紧已投入使用的取值或改变状态含义时，发布新的契约主版本目录。
- Worker 必须拒绝不支持的 `contract_version`，不能猜测兼容。
- 平台数据库应同时记录契约版本、Re3D Git SHA、配置 SHA-256 和评估规则版本。
