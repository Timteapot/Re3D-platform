# 开发 API → PostgreSQL → Worker 闭环

## 1. 本步完成了什么

当前开发环境已经形成第一个可执行的后端闭环：

```text
已登录开发客户端
  → FastAPI 从 Bearer token 取得用户并创建 simulated 任务
  → 写入 Re3D-data/jobs/<job_uuid> 和 PostgreSQL
  → 独立 Worker 取得 gpu:0 租约
  → 生成 A-v4 / B-v2 / C 三个最小 GLB
  → 生成模拟结构健康报告
  → PostgreSQL 状态变为 succeeded
  → FastAPI 查询任务终态
```

API 进程不执行重建，只负责校验请求、创建任务文件和写队列。Worker 是唯一执行管线并推进运行状态的组件，这一边界后续可以直接替换为真实 Re3D 执行器。

## 2. 安全边界

当前路由使用 `/api/v1/development` 前缀，只有 `APP_ENV=development` 或 `APP_ENV=test` 时才注册。`APP_ENV=production` 时这些路由返回 404。

开发任务接口必须使用 Bearer access token，服务端从令牌和数据库取得内部用户 ID，不再接受客户端提交的 `user_id`。这些任务路由仍只能绑定 `127.0.0.1` 用于本机联调，不能暴露到公开互联网。

旧的 `simulated-jobs` 接口仍生成平台开发字节；新的上传接口会保存并校验真实 JPEG/PNG。两者进入的仍是 simulated Worker：模拟 GLB 是格式合法的空场景，不包含网格、材质或纹理。评估报告因此将整体状态写为 `not_available`、分数写为 `null`，只把文件完整性标为通过，避免伪造质量结论。

## 3. 初始化项目虚拟环境

从平台仓库执行：

```powershell
cd D:\3Dreconstruction\Re3D-platform
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
```

依赖分层：

- `requirements-worker.txt`：契约、数据库和 Worker；
- `requirements-api.txt`：在 Worker 基础上增加 FastAPI、Pydantic 和 Uvicorn；
- `requirements-dev.txt`：在 API 基础上增加 Alembic 和测试客户端。

仓库脚本会优先使用 `.venv\Scripts\python.exe`，避免影响 Anaconda 或系统 Python。

## 4. 配置开发环境

将 `.env.example` 复制为未提交的 `.env`，填写已创建的 `re3d_app` 密码。至少确认：

```dotenv
APP_ENV=development
DATABASE_URL=postgresql+psycopg://re3d_app:<URL编码后的密码>@127.0.0.1:5432/re3d_platform_dev
RE3D_DATA_ROOT=D:/3Dreconstruction/Re3D-data
RE3D_GPU_RESOURCE=gpu:0
RE3D_LEASE_SECONDS=60
RE3D_HEARTBEAT_SECONDS=20
RE3D_WORKER_ID=windows-gpu-01
JWT_SECRET=<至少32字节随机值>
REFRESH_COOKIE_SECURE=false
```

`.env` 已被 Git 忽略。不要把数据库密码、JWT secret 或 token 放进文档、日志、任务 manifest 或提交历史。

首次使用认证接口前，以受限应用用户应用最新迁移：

```powershell
& deploy/postgres/verify-and-migrate-dev.ps1
```

## 5. 启动 API

第一个 PowerShell 窗口：

```powershell
cd D:\3Dreconstruction\Re3D-platform
.\.venv\Scripts\python.exe -m uvicorn apps.api.main:create_app `
  --factory `
  --env-file .env `
  --host 127.0.0.1 `
  --port 8000
```

检查存活状态：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
```

## 6. 登录后创建并执行模拟任务

先按 [`authentication.md`](authentication.md) 注册并登录，得到 `$login.access_token`。然后创建任务：

```powershell
$body = @{
    image_count = 3
    idempotency_key = "manual-development-001"
} | ConvertTo-Json
$headers = @{ Authorization = "Bearer $($login.access_token)" }

$job = Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/development/simulated-jobs `
  -ContentType application/json `
  -Body $body `
  -Headers $headers
$job
```

同一个用户重复提交同一个 `idempotency_key` 会返回原任务，HTTP 状态从 201 变为 200，不会重复创建目录或队列记录。

第二个 PowerShell 窗口运行一个任务：

```powershell
cd D:\3Dreconstruction\Re3D-platform
.\.venv\Scripts\python.exe -m dotenv -f .env run -- `
  .\.venv\Scripts\python.exe -m apps.worker.main run-queued-once
```

当前命令每次最多领取一个任务；没有任务时返回 `{"claimed": false, "status": "idle"}`。长期轮询服务、退避和 Windows 服务托管将在真实执行控制器阶段补充。

查询结果：

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v1/development/jobs/$($job.job_id)" `
  -Headers $headers
```

任务目录包含：

```text
Re3D-data/jobs/<job_uuid>/
├── input/
├── runtime/
├── output/
│   ├── A-v4/mesh.glb
│   ├── B-v2/mesh.glb
│   └── C/mesh.glb
├── reports/evaluation.json
└── manifests/
    ├── pipeline-request.json
    ├── pipeline-events.jsonl
    ├── pipeline-result.json
    └── output-validation.json
```

## 7. 接口清单

| 方法 | 路径 | 当前作用 |
|---|---|---|
| GET | `/health/live` | 进程存活和环境标识 |
| POST | `/api/v1/uploads` | 创建或恢复当前用户的幂等上传会话 |
| GET | `/api/v1/uploads/{upload_id}` | 查询当前用户的上传会话和图片元数据 |
| POST | `/api/v1/uploads/{upload_id}/images` | 上传并校验一张 JPEG/PNG |
| DELETE | `/api/v1/uploads/{upload_id}/images/{image_id}` | 删除未提交上传中的一张图片 |
| POST | `/api/v1/uploads/{upload_id}/cancel` | 取消未提交上传并清理任务目录 |
| POST | `/api/v1/uploads/{upload_id}/submit` | 复核输入并创建 simulated 队列任务 |
| GET | `/api/v1/development/jobs` | 列出当前用户最近的任务 |
| POST | `/api/v1/development/simulated-jobs` | 以当前登录用户创建幂等模拟任务 |
| GET | `/api/v1/development/jobs/{job_id}` | 查询当前用户的任务 |
| POST | `/api/v1/development/jobs/{job_id}/cancel` | 取消当前用户的任务 |

查询或取消其他用户的任务会统一返回 404，避免泄露任务是否存在。用户 ID 只来自服务端验证后的 access token。

## 8. 测试方式

不依赖外部服务的测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

真实 PostgreSQL 集成测试：

```powershell
& .\deploy\postgres\run-integration-tests.ps1
```

后一个脚本只使用随机临时 Docker PostgreSQL 18。测试数据库必须以 `_test` 结尾；代码会拒绝 `re3d_platform_dev`。脚本不挂载数据卷，结束后删除容器，因此不会清理或回滚开发数据。

真实图片工作流的请求示例、安全限制和目录结构见 [`image-uploads.md`](image-uploads.md)。

## 9. 仍未完成

- 邮箱验证、密码重置和登录限流；
- API 返回产物和评估报告；
- SSE 实时进度；
- 真实 Re3D 子进程、取消、超时和租约丢失终止；
- 真实 SfM、深度、网格和纹理评估；
- 失败任务目录自动清理；
- 三维查看器。

因此该闭环证明的是“平台编排边界可工作”，不代表系统已具备公开部署条件。
