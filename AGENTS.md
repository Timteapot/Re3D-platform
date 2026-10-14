# Re3D-platform 工作约定

保持客观。依据代码和实际执行结果描述行为，区分已实现、计划中和未验收能力；历史验收记录只证明对应版本与样本。

## 项目边界

- 本仓库维护平台 Web、API、Worker、共享契约和部署工具。兄弟 `Re3D` 仓库维护算法，`Re3D-data` 保存用户与实验数据；平台变更不自动扩展到这些目录。
- `apps/web` 是 React/TypeScript 前端；`apps/api` 提供 HTTP API；`backend/worker` 与 `backend/re3d_adapter` 消费 PostgreSQL 队列并监督独立重建进程。API 请求进程不执行 GPU 重建。
- 保留任务所有权、邮箱验证、可撤销认证、配额、租约和 fencing token 约束；单 GPU 任务并发为 1。修改 API/JSON Schema 时同步检查 `packages/contracts` 和前后端消费者。
- 当前运行边界是 Windows 本机或 SSH 回环转发的 restricted 环境。CI 通过不代表真实 GPU、服务器容量或公网部署验收通过。

## 环境与检查入口

- 使用 PowerShell 7、Python 3.12 和满足 `apps/web/package.json` engines 的 Node.js；CI 使用 Node.js 24。平台 `.venv` 与算法/CUDA 环境分开维护。
- 首次准备平台环境：`python -m venv .venv`，然后执行 `pwsh -NoProfile -File scripts/check.ps1 -Install`。已有 `.venv` 不重建；依赖升级作为明确变更处理。
- 后端改动执行 `pwsh -NoProfile -File scripts/check.ps1 -Component backend`；前端改动执行 `pwsh -NoProfile -File scripts/check.ps1 -Component frontend`。
- 提交前执行默认完整门禁：`pwsh -NoProfile -File scripts/check.ps1`。Python 使用 pytest 同时收集 unittest 类和 pytest 函数；前端 build 已包含 TypeScript 检查。
- 涉及数据库、迁移、队列领取/续租/接管或传输租约时，增加 `pwsh -NoProfile -File scripts/check.ps1 -Component postgres`，使用一次性 Docker PostgreSQL 18 与 `_test` 数据库。
- 默认检查不加载 `.env`，不启动应用、SMTP 或真实重建。兄弟算法仓库的规划检查通过 `RE3D_RUN_LOCAL_PIPELINE_TESTS=1` 显式启用；它不属于默认 CI 门禁。详细参数和准入见 `docs/development-checks.md`。

## 修改与验证

- 开始前核对 Git 状态，保留已有改动。每次变更围绕一个可独立验收的目标；并行任务明确文件负责人和运行资源。
- 先复现或确认现有行为，再做必要修改。复用既有脚本与架构；新增运行时依赖需要说明用途与兼容性。
- 测试覆盖真实风险与行为边界。普通单元测试使用临时目录、SQLite 和合成配置，不依赖本机兄弟仓库、模型、运行数据库或私密配置。
- 数据库集成测试只使用可丢弃的 `_test` 数据库；故障注入和迁移往返不作用于 development/restricted/production 数据库。
- 真实 GPU 重建、独立长测、远程操作、部署和数据清理由对应任务明确授权后执行，并遵守现有操作入口的准入条件；日常代码检查不触发这些操作。
- `.env`、密钥、数据库、模型、用户文件和产物留在 Git 之外。检查日志不输出私密配置；生成目录如 `.venv`、`node_modules`、`dist`、`.tmp-tests` 不手动加入提交。

## 文档与交付

- `README.md` 说明当前入口和架构；`docs/progress.md` 说明状态；`PROJECT_PLAN.md` 说明有效计划；操作步骤以对应开发/部署文档为准。
- 更新受影响的使用说明与契约。保留带日期的历史验收记录，避免把新行为写回历史结果。
- 交付说明包含行为变化、实际运行的检查及结果、跳过或未验证事项。遇到失败，说明失败阶段、退出码和相关证据；不把未执行的 CI/PostgreSQL/GPU 检查写成通过。
