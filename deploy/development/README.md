# Windows 本机运行配置

该目录负责创建和检查未提交的 `.env`。初始化脚本只接受本机 `re3d_platform_dev` 数据库和受限角色 `re3d_app`，自动生成 48 字节随机 JWT 密钥，并在成功前检查：

- PostgreSQL 数据库、角色和 Alembic head；
- Re3D 根目录、Git 提交、标签、配置哈希和 driver Python；
- `Re3D-data` 目录存在且可写；
- Mailpit SMTP 可以连接；
- development 认证及 Cookie 配置有效。

```powershell
cd D:\3Dreconstruction\Re3D-platform
& .\deploy\mailpit\start-local.ps1
& .\deploy\development\initialize-local-env.ps1
```

数据库密码通过安全提示输入，不回显。脚本不会覆盖已有 `.env`；验证失败时会删除本次刚生成的无效文件。后续可以重复执行只读检查：

```powershell
& .\deploy\development\check-local-readiness.ps1
```

`.env` 包含数据库密码和 JWT 密钥，已经被 Git 忽略，不得复制到聊天、日志或仓库。
