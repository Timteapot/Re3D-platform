# 本地认证邮件捕获

该目录使用固定版本 `axllent/mailpit:v1.31.3` 为 Windows 开发环境提供本机 SMTP 捕获服务。SMTP 和 Web UI 分别只绑定 `127.0.0.1:1025` 与 `127.0.0.1:8025`，不会监听局域网地址。

Mailpit 只用于开发和验收，不能作为生产邮件服务。容器默认最多保留 500 封邮件，不挂载宿主机数据卷；停止容器不会删除邮件，使用 `-Remove` 删除容器时邮件一并删除。

## 启动与验收

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
