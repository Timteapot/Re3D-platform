# PostgreSQL 开发与生产数据库

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

验证脚本只在进程内存和子进程环境中临时保存密码，结束前会清除变量。它会创建并立即删除权限探针表，然后执行 Alembic，并显示当前用户数、上传会话数、图片数、活动 refresh session 数、认证事件数和活动阻断桶数；不会运行测试、清表或回滚迁移。

`0002_user_auth` 会为任务所有者增加用户外键。如果旧开发库中存在没有对应用户的历史模拟任务，迁移会安全失败，不会伪造用户或删除任务；应先人工确认这些开发记录的处理方式。

`0003_job_uploads` 增加归属于用户的上传会话和图片元数据表。它不会扫描、导入或删除 `Re3D-data` 中的已有文件。

`0004_upload_lifecycle` 增加取消时间、取消原因、目录清理完成时间和维护查询索引。迁移本身只修改数据库结构，不删除任何任务目录。

`0005_auth_audit_throttle` 增加脱敏认证事件和共享登录限流桶；`0006_registration_throttle` 增加共享注册尝试限流桶；`0007_auth_action_tokens` 增加邮箱验证、密码重置一次性令牌及请求限流桶；`0008_failed_job_storage_cleanup` 增加失败/取消任务文件清理审计字段；`0009_task_submission_policy` 增加任务创建/取消审计表；`0010_success_storage_retention` 增加成功任务分层保留审计字段。迁移本身不删除文件，也不修改已有用户密码、refresh token 或任务终态。

## 生产数据库权限模型

生产环境固定使用以下边界：

- PostgreSQL 管理员只负责创建角色和数据库，不进入 API/Worker 配置；
- `re3d_migrator` 拥有 `re3d_platform` 数据库对象，只在执行 Alembic 和人工备份时使用，连接上限为 2；
- `re3d_runtime` 供 API 和 Worker 使用，连接上限为 50，只能连接数据库、使用 `public` schema，并对业务表执行 `SELECT/INSERT/UPDATE/DELETE`；
- `re3d_runtime` 不能创建数据库、角色、schema 对象或临时表，不能 `TRUNCATE`，不能修改 `alembic_version`；
- 两个角色都没有 superuser、建库、建角色、复制、绕过行安全或其他角色成员资格。

先在无持久卷的 PostgreSQL 18 容器中执行完整合同验收：

```powershell
& .\deploy\postgres\run-production-database-acceptance.ps1
```

该脚本随机生成三组密码和端口，连续执行两次角色/数据库初始化以验证幂等性，然后执行 Alembic、运行账户权限验证、应用驱动写入、生产数据库就绪检查、自定义格式备份、SHA-256 校验和随机临时库恢复。恢复时还会确认测试业务行存在。无论成功或失败，脚本都会停止容器并删除临时备份；它不会连接 `re3d_platform_dev`。

确认临时验收通过后，才可在目标 PostgreSQL 实例运行以下命令：

```powershell
& .\deploy\postgres\bootstrap-production.ps1
& .\deploy\postgres\migrate-production.ps1
```

两个脚本通过安全输入读取密码。密码只短暂存在于当前 PowerShell 和子进程环境，不出现在命令行参数、脚本或 Git 中。建库脚本可重复执行，但如果已有 `re3d_platform` 并非由 `re3d_migrator` 拥有，会停止而不是擅自修改所有权。每次部署新迁移都应再次运行 `migrate-production.ps1`，使新表获得运行账户权限，并重新验证完整权限边界。

API 和 Worker 只配置运行账户：

```dotenv
DATABASE_URL=postgresql+psycopg://re3d_runtime:<URL编码后的密码>@127.0.0.1:5432/re3d_platform
```

数据库不应直接监听公网。若不在同机，应只允许来自应用/Worker 私网地址的加密连接，并在实际服务器方案确定后补充 PostgreSQL TLS 和防火墙验收。

### 备份与恢复演练

备份目标必须在源码仓库之外，而且脚本拒绝覆盖已有文件：

```powershell
& .\deploy\postgres\backup-production.ps1 `
    -BackupPath E:\Re3D-backups\re3d-platform-2026-09-29.dump
```

脚本生成 PostgreSQL custom-format 文件和同名 `.sha256` 文件。数据库备份包含用户邮箱、认证状态、令牌摘要、审计记录和任务元数据，必须加密存储、限制读取并保存到另一故障域；当前脚本没有替代异地备份策略。

恢复演练只允许恢复到脚本生成的 `re3d_restore_test_<随机值>` 临时库，验证校验和、Alembic revision 和 12 张必需表，完成后强制断开连接并删除临时库：

```powershell
& .\deploy\postgres\test-production-restore.ps1 `
    -BackupPath E:\Re3D-backups\re3d-platform-2026-09-29.dump
```

该命令不会覆盖 `re3d_platform`。正式上线前仍需在真实目标实例生成一次备份，并在隔离环境完成恢复演练，记录恢复时间和恢复点目标。

## 集成测试

集成测试不得复用 `re3d_platform_dev`。使用以下脚本创建无持久卷的临时 PostgreSQL 18 容器：

```powershell
& deploy/postgres/run-integration-tests.ps1
```

脚本会随机生成测试密码和宿主机端口，执行 Alembic、PostgreSQL 租约并发/接管测试，以及认证用户 → 上传生命周期 → API → PostgreSQL → Worker → 三分支产物闭环测试。测试完成后还会执行 `head → 0001 → head` 迁移往返。成功或失败后都会停止容器；容器使用 `--rm`，不会保留测试数据库。

PostgreSQL 脚本会优先使用仓库内 `.venv\Scripts\python.exe`，没有虚拟环境时才回退到当前 `python`。推荐先按 [`docs/development-api-worker.md`](../../docs/development-api-worker.md) 创建项目虚拟环境。

测试代码只读取：

```text
RE3D_TEST_DATABASE_URL
```

该变量必须指向 PostgreSQL，且数据库名称必须以 `_test` 结尾。测试如果检测到 `re3d_platform_dev` 或其他非测试库名称会立即拒绝运行，避免回滚、清表或故障注入影响开发数据。
