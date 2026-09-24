# PostgreSQL 开发数据库

## 本机开发实例

使用 PostgreSQL 管理员账户运行：

```powershell
& deploy/postgres/bootstrap-dev.ps1
```

脚本会安全提示输入两类密码：

1. 已有 `postgres` 管理员密码；
2. 新的 `re3d_app` 密码，需要重复确认。

密码不会写入脚本或 Git。脚本创建：

- 数据库 `re3d_platform_dev`；
- 受限登录角色 `re3d_app`；
- 仅限该开发数据库的连接、临时表和 `public` schema 使用/建表权限。

`re3d_app` 不具有超级用户、创建数据库、创建角色、复制或绕过行级安全策略的权限，连接数限制为 10。

创建成功后，在未提交的 `.env` 中配置：

```dotenv
DATABASE_URL=postgresql+psycopg://re3d_app:<URL编码后的密码>@127.0.0.1:5432/re3d_platform_dev
```

使用受限应用用户验证权限并执行迁移：

```powershell
& deploy/postgres/verify-and-migrate-dev.ps1
```

验证脚本只在进程内存和子进程环境中临时保存密码，结束前会清除变量。它会创建并立即删除权限探针表，然后执行 Alembic；不会运行测试、清表或回滚迁移。

## 集成测试

集成测试不得复用 `re3d_platform_dev`。使用以下脚本创建无持久卷的临时 PostgreSQL 18 容器：

```powershell
& deploy/postgres/run-integration-tests.ps1
```

脚本会随机生成测试密码和宿主机端口，执行 Alembic、PostgreSQL 租约并发/接管测试，以及 API → PostgreSQL → Worker → 三分支产物闭环测试，并在成功或失败后停止容器。容器使用 `--rm`，不会保留测试数据库。

两个 PostgreSQL 脚本会优先使用仓库内 `.venv\Scripts\python.exe`，没有虚拟环境时才回退到当前 `python`。推荐先按 [`docs/development-api-worker.md`](../../docs/development-api-worker.md) 创建项目虚拟环境。

测试代码只读取：

```text
RE3D_TEST_DATABASE_URL
```

该变量必须指向 PostgreSQL，且数据库名称必须以 `_test` 结尾。测试如果检测到 `re3d_platform_dev` 或其他非测试库名称会立即拒绝运行，避免回滚、清表或故障注入影响开发数据。
