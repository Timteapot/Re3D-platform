# Windows 生产服务托管

本目录把已经验收的 API、Worker 和 Caddy 前台入口转换为可由 Windows Service Control Manager 管理的三个独立服务。它不提交 WinSW 或 Caddy 二进制，也不会在普通验收中注册系统服务。

## 固定边界

- WinSW 固定为稳定版 `2.12.0`，使用 XML 配置和 bundled 模式；每个服务持有一份同源包装器副本和同名 XML。
- 服务 ID 固定为 `Re3DPlatformApi`、`Re3DPlatformWorker`、`Re3DPlatformCaddy`。
- 每个服务运行于对应的无密码 Windows 虚拟账户 `NT SERVICE\<服务 ID>`，不使用 `LocalSystem`，也不把账户密码写入 XML。
- 三个服务均为自动启动；API 和 Worker 使用延迟自动启动，Caddy 正常自动启动。
- 非零退出按 10 秒、30 秒、60 秒退避重启，稳定运行 1 小时后重置失败计数。
- 停止时先向父进程发送终止信号，最多等待 45 秒，再由包装器清理进程树。
- stdout/stderr 按 10 MiB 轮转，每个流最多保留 10 个历史文件。没有启用 WinSW 文档中标记存在问题的日志自动压缩功能。
- 服务 XML 作为不可变部署产物管理；修改后必须由管理员显式重装服务，受限服务账户不能修改自身 SCM 配置。
- 服务包记录平台 Git 提交和 `deployment.json` 摘要；安装器拒绝脏工作区、提交漂移、脏前端构建或构建提交不一致。

虚拟账户由 Windows 管理，不需要人工设置或轮换密码。安装脚本在服务创建后启用独立服务 SID，并按模块分配权限：

| 身份 | 读取 | 写入 |
|---|---|---|
| API | 平台代码、API 环境文件 | `RE3D_DATA_ROOT`、自身日志 |
| Worker | 平台代码、Worker 环境文件、Re3D 目录、已固化的外部 Python/TextureMesh 运行目录 | `RE3D_DATA_ROOT`、自身日志 |
| Caddy | 平台代码、前端构建、Caddy 二进制 | Caddy TLS/配置状态、访问日志 |

安装时 API 与 Worker 环境文件会被改为只允许 `Administrators`、`SYSTEM` 和对应服务 SID 读取。安装后普通开发账户可能无法继续读取这两个文件，这是预期的生产权限边界。

## 1. 准备二进制与生产文件

从官方 Release 获取 `WinSW-x64.exe` 2.12.0 和 Caddy 2.11.4，不要把它们提交到仓库。先完成生产前端构建、数据库迁移和 API/Worker 就绪检查：

```powershell
& .\deploy\production\build-web.ps1
& .\deploy\production\check-production-readiness.ps1 `
    -Component api `
    -EnvironmentFile .env.production
& .\deploy\production\check-production-readiness.ps1 `
    -Component worker `
    -EnvironmentFile .env.worker.production
```

## 2. 生成不可变服务包

建议使用受管理员保护且不在 Git 仓库内的目录：

```powershell
& .\deploy\production\windows-services\new-service-bundle.ps1 `
    -OutputRoot C:\ProgramData\Re3DPlatform `
    -WinSWPath C:\Install\WinSW-x64.exe `
    -CaddyPath C:\Program Files\Caddy\caddy.exe `
    -SiteAddress re3d.example.com
```

生成器会：

1. 验证两个环境文件都明确设置 `APP_ENV=production`；
2. 拒绝 Worker 环境中的 JWT、Cookie 和 SMTP 密钥；
3. 验证 API/Worker 使用同一个外部数据目录；
4. 按实际运行优先级解析驱动、MapAnything、MVSAnywhere Python 和 TextureMesh，并把外部运行目录固化到清单；
5. 要求前端 `deployment.json` 来自当前平台提交的干净构建；
6. 生成三份不含秘密值的 WinSW XML；
7. 将平台/前端提交、元数据摘要、WinSW SHA-256、路径和拓扑写入 `service-bundle.json`；
8. 创建独立日志目录和 Caddy 状态目录。

输出目录必须为空，生成器不会覆盖已有服务包。

## 3. 非特权检查

以下命令只读取服务包。加上 `-CheckExternalBinaryVersions` 后，会实际确认每份包装器为 WinSW 2.12.0、Caddy 为 2.11.4：

```powershell
& .\deploy\production\windows-services\test-service-bundle.ps1 `
    -BundleRoot C:\ProgramData\Re3DPlatform `
    -CheckExternalBinaryVersions
```

仓库级生成验收不注册服务，也不要求真实生产密钥：

```powershell
& .\deploy\production\windows-services\run-local-acceptance.ps1
```

## 4. 安装与启动

必须在提升权限的 PowerShell 中执行。默认只安装和设置权限，不启动服务：

```powershell
& .\deploy\production\windows-services\install-service-bundle.ps1 `
    -BundleRoot C:\ProgramData\Re3DPlatform
```

首次安装时也可显式要求按 API → Worker → Caddy 顺序启动：

```powershell
& .\deploy\production\windows-services\install-service-bundle.ps1 `
    -BundleRoot C:\ProgramData\Re3DPlatform `
    -StartServices
```

安装器拒绝覆盖同名现有服务，并在注册前重复执行源提交一致性、生产就绪检查、WinSW/Caddy 版本检查、包装器哈希检查和 Caddy 配置校验。它还会拒绝 `Everyone`、`Authenticated Users` 或内置 `Users` 对代码、服务包、数据、Re3D、外部 Worker 运行目录、环境文件父目录或 Caddy 安装目录拥有写权限的主机。任一服务注册失败时，只回滚本次已创建的服务，不删除数据和日志。

安装完成但未启动时，可由管理员执行：

```powershell
Start-Service Re3DPlatformApi
Start-Service Re3DPlatformWorker
Start-Service Re3DPlatformCaddy

Get-CimInstance Win32_Service |
    Where-Object Name -Like "Re3DPlatform*" |
    Select-Object Name, State, StartMode, StartName, ProcessId
```

## 5. 安装后只读验收

启动三个服务且 DNS/TLS 已生效后，在提升权限的 PowerShell 中运行：

```powershell
& .\deploy\production\windows-services\test-installed-services.ps1 `
    -BundleRoot C:\ProgramData\Re3DPlatform `
    -ReportPath C:\ProgramData\Re3DPlatform\installed-acceptance.json
```

默认从清单推导公网地址，也可用 `-PublicBaseUrl` 指定同一部署的验收入口。公开地址只接受 HTTPS；明文 HTTP 仅可用于回环验收。脚本只读取系统状态和发出 GET 请求，不启动、停止或重装服务。它验证：

- 三个服务的运行状态、自动启动、虚拟账户、进程、包装器路径、服务 SID 和失败恢复注册；
- 平台提交、前端构建提交和公开 `deployment.json` 一致；
- API 只监听 IPv4 回环，Caddy 能提供首页、代理生产健康接口并保留受保护 API 的 401；
- CSP、HSTS、缓存和服务端标识隐藏策略；
- HTTPS 证书由系统信任、主机名匹配且剩余有效期不少于 14 天；
- 环境文件、共享数据、Re3D、外部 Python/TextureMesh 和前端目录的服务 SID 权限；
- 三个服务已经产生日志，Worker 已进入 real 队列循环。

该检查不提交重建任务，也不能替代重启恢复、人工终止进程和真实 GPU 任务验收。

## 6. 卸载

卸载需要管理员权限和显式确认，并按 Caddy → Worker → API 顺序停止：

```powershell
& .\deploy\production\windows-services\uninstall-service-bundle.ps1 `
    -BundleRoot C:\ProgramData\Re3DPlatform
```

卸载只删除 Windows 服务注册。服务包、日志、环境文件、任务数据和 ACL 会保留，便于审计和恢复；脚本不会递归删除生产目录。

## 尚未完成的真实主机验收

仓库已经验证服务包生成、源提交绑定、XML 结构、秘密隔离和清理，并提供安装后只读验收脚本，但当前开发机没有安装这些服务。目标服务器仍必须完成：

- 以管理员权限安装并确认三个 `StartName` 均为预期虚拟账户；
- 重启服务器，确认自动启动顺序和 Caddy 外部可用性；
- 人工终止一次子进程，确认 10/30/60 秒失败重启和日志连续性；
- 验证 Worker 虚拟账户能访问 CUDA、驱动 Python 和 GPU；
- 验证 Caddy 虚拟账户能签发、保存并续期真实域名证书；
- 检查日志磁盘增长、Windows 事件日志和服务停止时的 Re3D 子进程清理。

以上验证完成前，不能把“服务配置已生成”等同于“目标服务器服务托管已验收”。
