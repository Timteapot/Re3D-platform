# 生产部署配置与就绪检查

本目录定义公开部署前的配置检查、生产构建、进程入口和同源反向代理边界。Caddy 在真实域名上负责申请和续期 TLS 证书，但本仓库不会注册域名、开放防火墙或创建真实生产资源。就绪检查只返回非敏感摘要，不输出数据库密码、JWT 密钥或 SMTP 密码。

## 当前支持的首个拓扑

当前任务请求保存了输入目录等宿主机路径，API 负责写入上传文件，Windows GPU Worker 负责读取同一任务目录。因此首个可验收拓扑必须让 API 和 Worker 使用同一套 Windows 路径语义并访问同一个 `RE3D_DATA_ROOT`。可先同机部署，反向代理只把 API 暴露给互联网，Re3D Worker 和 PostgreSQL 不直接暴露。

把 API 迁移到 Linux、Worker 留在 Windows 的拆分拓扑尚不能直接采用：虽然数据库队列可以跨主机访问，但任务路径没有抽象为跨平台共享存储键。该改造应在后续单独完成，不能通过简单复制环境变量规避。

## 配置方法

1. 将仓库根目录的 `.env.production.example` 复制为 `.env.production`，将 `.env.worker.production.example` 复制为 `.env.worker.production`；两个实际文件均被 Git 忽略。
2. 替换全部 `replace-*` 值，使用独立生产数据库、随机 JWT 密钥、正式域名和真实 SMTP 服务。
3. Worker 使用独立环境文件，不能获得 JWT、Cookie 或 SMTP 凭据；API 与 Worker 只共享受限数据库 URL、数据根目录和必要的 Re3D 配置。
4. 创建仓库外的生产数据目录，评估容量后明确 `RE3D_MIN_FREE_DISK_BYTES`。
5. 先迁移生产数据库，再执行对应组件检查。

生产数据库角色拆分、迁移、备份和临时恢复演练见 [`../postgres/README.md`](../postgres/README.md)。API/Worker 的 `DATABASE_URL` 只能使用 `re3d_runtime`，不能使用管理员或 `re3d_migrator`。

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
- `worker`：不要求 JWT/SMTP 密钥，检查受限 PostgreSQL、租约参数、数据盘、资源采样配置、固定 Re3D tag/commit/config 和驱动 Python。
- `all`：执行两组检查，适用于首个单机 Windows 生产验收环境。

数据库检查会拒绝 SQLite、开发/测试库、`postgres`/`re3d_migrator` 用户，以及拥有 superuser、建库、建角色、复制、绕过行安全、临时表或 schema 创建权限的账户。存储检查会拒绝代码仓库内部目录、不可写目录和低于显式容量下限的磁盘。

## 构建和进程入口

本机验收固定使用 Caddy `2.11.4`。Caddy 二进制不提交到仓库，应从官方发行渠道安装；生产主机升级版本前需要重新执行本节验收。前端构建默认要求 Git 工作区干净，并使用锁文件重新安装依赖：

```powershell
& .\deploy\production\build-web.ps1
```

产物位于 `apps/web/dist`，其中 `deployment.json` 记录源提交、构建时间、脏工作区标记和静态资源数量。不要手工修改构建目录。

以下三个入口均以前台进程运行，便于交给后续 Windows 服务管理器托管；它们本身不创建服务、不会配置自动重启：

```powershell
# 仅监听回环地址，公网请求必须经过 Caddy。
& .\deploy\production\run-api.ps1 `
    -EnvironmentFile .env.production `
    -Port 8000

# 持续领取 real 队列任务；没有任务时按 RE3D_WORKER_POLL_SECONDS 休眠。
& .\deploy\production\run-worker.ps1 `
    -EnvironmentFile .env.worker.production

# 真实域名会启用 Caddy 自动 HTTPS；API 上游只允许回环地址。
& .\deploy\production\run-caddy.ps1 `
    -CaddyPath C:\path\to\caddy.exe `
    -SiteAddress re3d.example.com `
    -BindAddress 0.0.0.0 `
    -ApiUpstream 127.0.0.1:8000 `
    -MaxRequestBody 27MB
```

`run-caddy.ps1` 拒绝非回环地址上的明文 HTTP。Caddy 为 `/api/*` 和 `/health/*` 提供同源代理、单请求体上限和禁止缓存策略，为带内容哈希的 `/assets/*` 提供不可变缓存，其余页面执行 SPA 回退和重新验证缓存；同时设置 CSP、点击劫持防护、MIME 嗅探防护、来源策略和权限策略。默认 `27MB` 为单张 25 MiB 图片和 multipart 开销预留空间，修改 `UPLOAD_MAX_FILE_BYTES` 时必须同步复核该值。API 自身不对公网监听，Worker 不开启网络端口，PostgreSQL 也不应直接暴露到互联网。

基于 WinSW 的三个独立 Windows 虚拟账户、自动启动、失败重启、日志轮转、最小 ACL、平台/前端发布身份绑定、安装后只读验收和卸载流程见 [`windows-services/README.md`](windows-services/README.md)。仓库已完成无系统改动的服务包生成验收；实际服务器安装、重启和故障恢复仍需单独验收。

## 非公网生产拓扑验收

验收会构建前端，启动一次性 PostgreSQL 18，应用全部迁移，再启动生产模式 API、持续 Worker 和仅绑定回环地址的 Caddy。它检查生产路由边界、SPA 回退、受保护 API 的 401 透传、超限请求拒绝、缓存规则、安全响应头和监听范围；结束时自动删除数据库容器、临时密钥、测试数据和全部子进程。

```powershell
& .\deploy\production\run-local-acceptance.ps1 `
    -CaddyPath C:\path\to\caddy.exe
```

依赖已经由锁文件安装时可加 `-SkipWebInstall`。该验收使用临时数据库，不读取或清理开发/生产数据库。2026-09-29 已在 Windows、PostgreSQL 18 和 Caddy 2.11.4 上通过完整验收。

## 检查通过不等于允许公网发布

该检查只能证明配置与依赖符合当前程序契约。以下阻断项仍需逐步实现和验收：

- 在真实目标 PostgreSQL 上执行已验收的建库/迁移脚本，并完成一次异地备份恢复演练；
- 在目标服务器安装已生成的 API、Worker 和 Caddy Windows 服务，并完成重启、故障恢复、GPU 与证书权限验收；
- 使用真实域名验证公网 DNS、Caddy 自动 TLS、80/443 防火墙边界和外部访问；
- 任务并发、每日次数、存储配额及接口限流；
- 成功任务数据保留期和对应清理执行器（失败/取消任务已由 Worker 自动清理）；
- 将已有任务级 GPU、CPU、内存和数据盘样本接入集中监控与告警；
- 生产 SMTP、验证码/防滥用策略和隐私说明验收。

因此，在上述阻断项完成前，即使命令返回 `"status": "ready"`，也只能进入下一项部署工程，不能把服务开放到公网。
