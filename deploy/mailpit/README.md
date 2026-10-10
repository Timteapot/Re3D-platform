# 本地认证邮件捕获

该目录使用固定版本 Mailpit v1.31.3 为 Windows 开发环境提供本机 SMTP 捕获服务。SMTP 和 Web UI 分别只绑定 `127.0.0.1:1025` 与 `127.0.0.1:8025`，不会监听局域网地址。

Mailpit 只用于开发和验收，不能作为生产邮件服务。当前提供两种互斥运行方式：Docker 容器和 Windows 独立程序。两者都最多保留 500 封邮件，不能同时占用相同端口。

## Docker 方式

```powershell
cd D:\3Dreconstruction\Re3D-platform
& .\deploy\mailpit\start-local.ps1
& .\deploy\mailpit\run-acceptance.ps1
```

浏览器打开 `http://127.0.0.1:8025` 查看验证和重置邮件。`.env` 使用以下开发配置：

```dotenv
APP_PUBLIC_BASE_URL=http://localhost:5173
SMTP_HOST=127.0.0.1
SMTP_PORT=1025
SMTP_FROM=no-reply@example.invalid
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_STARTTLS=false
```

修改 `.env` 后必须重新启动 API。上述无认证、无 TLS 设置只适合回环地址上的本地捕获服务。

## 停止或清除

```powershell
# 停止但保留已捕获邮件
& .\deploy\mailpit\stop-local.ps1

# 删除容器及其中的开发邮件
& .\deploy\mailpit\stop-local.ps1 -Remove
```

验收测试使用临时 SQLite 数据库和临时任务目录，不会连接或修改 `re3d_platform_dev`。

## Windows 独立程序方式

Docker Desktop 不可用时可使用固定的官方 Windows amd64 发布包：

```powershell
cd D:\3Dreconstruction\Re3D-platform
& .\deploy\mailpit\install-standalone.ps1
& .\deploy\mailpit\start-standalone.ps1
& .\deploy\mailpit\run-acceptance.ps1 -StartupMode Existing
```

安装脚本只接受 v1.31.3 的固定发布地址和以下 SHA-256：

- 压缩包：`863e9502d4e0f14a78c0f91c5091797b1c7b7b7e3fc7e5eab62e5770ce44b76e`
- `mailpit.exe`：`ee0b025bc9f61e6856d6032128408ee6fe1627f510c118a4fa0faa7bfdb7cd33`

程序以隐藏窗口启动，并将 SMTP 与 Web UI 分别固定在 `127.0.0.1:1025` 和 `127.0.0.1:8025`，不启用 POP3。PID、日志、数据库和捕获邮件保存在 `D:\3Dreconstruction\Re3D-data\_services\mailpit`，不与重建任务数据混用。停止服务不会删除这些数据：

```powershell
& .\deploy\mailpit\stop-standalone.ps1
```

`run-acceptance.ps1` 也可使用 `-StartupMode Standalone` 自动启动独立程序；当服务已运行时使用 `Existing` 可避免改变服务状态。

自动化执行环境可能在启动命令结束时回收子进程，此时使用附着模式，当前 PowerShell 会保持运行直到服务停止：

```powershell
& .\deploy\mailpit\start-standalone.ps1 -StayAttached
```
