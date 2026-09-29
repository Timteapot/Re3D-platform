# 生产部署配置与就绪检查

本目录定义公开部署前的配置检查边界，不负责签发域名证书，也不会创建或修改真实生产资源。就绪检查只返回非敏感摘要，不输出数据库密码、JWT 密钥或 SMTP 密码。

## 当前支持的首个拓扑

当前任务请求保存了输入目录等宿主机路径，API 负责写入上传文件，Windows GPU Worker 负责读取同一任务目录。因此首个可验收拓扑必须让 API 和 Worker 使用同一套 Windows 路径语义并访问同一个 `RE3D_DATA_ROOT`。可先同机部署，反向代理只把 API 暴露给互联网，Re3D Worker 和 PostgreSQL 不直接暴露。

把 API 迁移到 Linux、Worker 留在 Windows 的拆分拓扑尚不能直接采用：虽然数据库队列可以跨主机访问，但任务路径没有抽象为跨平台共享存储键。该改造应在后续单独完成，不能通过简单复制环境变量规避。

## 配置方法

1. 将仓库根目录的 `.env.production.example` 复制为 `.env.production`；后者已被 Git 忽略。
2. 替换全部 `replace-*` 值，使用独立生产数据库、随机 JWT 密钥、正式域名和真实 SMTP 服务。
3. 创建仓库外的生产数据目录，评估容量后明确 `RE3D_MIN_FREE_DISK_BYTES`。
4. 先迁移生产数据库，再执行对应组件检查。

```powershell
Copy-Item .env.production.example .env.production
& .\deploy\production\check-production-readiness.ps1 -Component all
```

也可以分别检查进程权限边界：

```powershell
& .\deploy\production\check-production-readiness.ps1 -Component api
& .\deploy\production\check-production-readiness.ps1 -Component worker
```

- `api`：检查生产认证约束、HTTPS 公共地址、上传限制、SMTP STARTTLS、受限 PostgreSQL、迁移版本和数据盘。
- `worker`：不要求 JWT/SMTP 密钥，检查受限 PostgreSQL、租约参数、数据盘、固定 Re3D tag/commit/config 和驱动 Python。
- `all`：执行两组检查，适用于首个单机 Windows 生产验收环境。

数据库检查会拒绝 SQLite、开发/测试库、`postgres` 用户，以及拥有 superuser、建库、建角色、复制或绕过行安全权限的账户。存储检查会拒绝代码仓库内部目录、不可写目录和低于显式容量下限的磁盘。

## 检查通过不等于允许公网发布

该检查只能证明配置与依赖符合当前程序契约。以下阻断项仍需逐步实现和验收：

- 生产数据库创建、迁移与备份恢复演练；
- 前端静态构建、反向代理、TLS 和安全响应头；
- 任务并发、每日次数、存储配额及接口限流；
- 成功任务数据保留期和定时清理；
- 失败/取消任务目录的终态清理执行器；
- GPU、CPU、内存、磁盘监控与告警；
- 生产 SMTP、验证码/防滥用策略和隐私说明验收。

因此，在上述阻断项完成前，即使命令返回 `"status": "ready"`，也只能进入下一项部署工程，不能把服务开放到公网。
