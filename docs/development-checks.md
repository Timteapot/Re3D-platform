# 开发检查与 CI

`scripts/check.ps1` 是本地和 GitHub Actions 共用的检查入口。默认检查平台后端与前端，不依赖 GPU、模型、兄弟算法仓库、真实 `.env` 或运行中的平台服务。

## 环境准备

使用 PowerShell 7、Python 3.12 和 Node.js ≥22.12.0；CI 使用 Node.js 24。平台 Python 环境独立于 Re3D/CUDA 环境。

在平台根目录首次准备环境：

```powershell
python -m venv .venv
pwsh -NoProfile -File scripts/check.ps1 -Install
```

已有 `.venv` 时直接使用，不覆盖环境。`-Install` 显式安装对应组件依赖：后端按 `requirements-dev.txt` 安装，前端执行 `npm ci`。默认检查只使用已安装依赖。`-PythonPath` 可指定解释器；未指定时优先使用平台 `.venv`，再使用 PATH 中的 Python。

脚本按自身路径定位仓库，因此也可以在其他目录以完整脚本路径调用。安装或检查失败会返回非零退出码。

## 常用命令

```powershell
# 默认完整检查：Python 依赖、后端测试、前端测试与构建
pwsh -NoProfile -File scripts/check.ps1

# 按改动范围检查
pwsh -NoProfile -File scripts/check.ps1 -Component backend
pwsh -NoProfile -File scripts/check.ps1 -Component frontend

# 只运行一次性 PostgreSQL 集成与迁移往返
pwsh -NoProfile -File scripts/check.ps1 -Component postgres

# 默认检查加 PostgreSQL
pwsh -NoProfile -File scripts/check.ps1 -Postgres

# 自定义解释器，仅准备并检查后端
pwsh -NoProfile -File scripts/check.ps1 -Component backend -Install -PythonPath ./.venv/Scripts/python.exe
```

| 范围 | 执行内容 | 外部条件 |
|---|---|---|
| backend | Python 版本检查、`pip check`、pytest 收集 `tests/unit` 与 SQLite API/Worker 集成测试 | 已安装开发 Python 依赖 |
| frontend | Node/npm 与依赖检查、`npm test`、`npm run build` | 前端依赖已按锁文件安装；build 已含 TypeScript 检查 |
| postgres | 临时 PostgreSQL 18 容器、升级 head、队列/API-Worker/传输租约集成测试、降级 `0001_job_queue` 后重新升级 head | 运行中的 Docker 引擎及可用的 `postgres:18` 镜像 |
| all | backend + frontend | 上述两个组件条件 |

`-Postgres` 为 all/backend 增加数据库检查。缺少依赖、Docker 引擎未运行或测试失败都使检查失败，不作为成功跳过。数据库检查复用 `deploy/postgres/run-integration-tests.ps1`，使用随机容器名、随机回环端口和不挂载数据卷的 `_test` 数据库，结束时清理测试容器，不操作持久数据库。

## 测试边界

- pytest 兼容现有 unittest 类，也收集使用 fixture/parametrize 的函数测试，避免旧 unittest discover 入口漏收集。
- 默认门禁验证模拟链路、契约、平台逻辑、Windows 进程监督与前端行为。阶段规划单元测试使用合成外部配置；它验证平台编排合同，不证明真实算法脚本或 GPU 已运行。
- Windows 监督进程死亡测试使用真实基础解释器，避免虚拟环境启动器影响 Job Object 句柄生命周期；合成监督脚本显式复用父测试进程的导入路径，不要求基础 Python 全局安装项目依赖。
- 监督进程的 stdout/stderr 写入测试临时文件；进程树未就绪时，失败信息包含退出码和各输出最后 4 KiB，随 pytest 失败日志保留。
- 本机冻结算法规划检查需要兄弟 `Re3D` 仓库。要显式执行这项检查，可单独运行以下命令；它不会执行真实重建：

```powershell
$previousLocalPipelineTests = $env:RE3D_RUN_LOCAL_PIPELINE_TESTS
$env:RE3D_RUN_LOCAL_PIPELINE_TESTS = '1'
try {
  ./.venv/Scripts/python.exe -m pytest tests/unit/test_high_profile.py -q
} finally {
  $env:RE3D_RUN_LOCAL_PIPELINE_TESTS = $previousLocalPipelineTests
}
```

- 默认入口屏蔽继承的业务运行配置并禁用外部 pytest 插件自动加载，使本地检查与 CI 使用同一测试边界。调用结束后恢复调用者的目录与环境变量。
- C-only 合成接线测试用 `tmp_path_factory.mktemp('cw')` 创建独占短根目录，避免长测试节点名称叠加 64 位来源 SHA 后超过 Windows 路径长度边界。测试保留预算 claim/charge、故障阻断、停止证明和资源阈值断言，不通过更改系统长路径策略、跳过测试或放宽生产保护解决临时路径问题。
- 前端构建产物和测试缓存是本地生成文件；普通 `npm run build` 不生成生产 `deployment.json`，正式发布仍遵循现有发布构建入口。

## GitHub Actions

`.github/workflows/check.yml` 在 pull request、main 分支 push 和手动 workflow_dispatch 时执行：

| Job | 环境 | 共用入口 |
|---|---|---|
| windows | Windows Server 2022、Python 3.12、Node.js 24 | `scripts/check.ps1 -Component all -Install` |
| postgres | Ubuntu 24.04、Python 3.12、Docker PostgreSQL 18 | `scripts/check.ps1 -Component postgres -Install` |

Windows job 保留 Windows Job Object 等系统契约的测试覆盖。Linux job 验证真实 PostgreSQL 的锁、租约和迁移，避免用 SQLite 替代数据库并发验收。

两个 job 均从 checkout 的提交恢复依赖；Actions 固定为发行 commit SHA，缓存键来自依赖清单/前端锁文件，token 仅授予读取仓库权限。新一轮检查会取消同分支旧运行，job 设置超时。

此工作流只检查代码，不部署服务器、不触发真实 GPU 任务，也不使用生产密钥。工作流提交并推送到 GitHub 后才会产生远端运行结果；仓库分支保护中的 required status checks 需要在首次运行后另行配置。
