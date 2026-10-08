# 管理员审计查询与角色引导

更新时间：2026-10-01

## 目标与边界

管理员审计接口用于查看已经由业务模块写入数据库的最小安全记录，不读取 Re3D 自由文本日志、上传图片、模型文件、密码、令牌或邮件正文。接口在 development、test 和 production 环境均注册，但只有当前数据库角色为 `admin` 的活动用户可以访问。

当前提供四组只读接口：

```text
GET /api/v1/admin/audit/auth-events
GET /api/v1/admin/audit/task-events
GET /api/v1/admin/audit/retention-runs
GET /api/v1/admin/audit/role-changes
```

所有接口使用 Bearer access token，普通用户返回 403；缺少或无效令牌返回 401。响应设置 `Cache-Control: no-store` 和 `Pragma: no-cache`。分页参数固定为 `limit=1..100`、`offset=0..10000`，查询会多取一条记录生成 `has_more`，不执行无界全表导出。

## 脱敏规则

- 认证事件不返回用户名、邮箱、原始 IP、User-Agent、refresh session ID 或完整指纹；
- identifier、IP 和 User-Agent 的 HMAC/SHA-256 只返回前 12 个十六进制字符作为同类事件关联引用；
- 任务事件只返回内部 user/job UUID、固定动作、执行模式、结果和稳定原因码；
- 保留策略运行只返回固定策略、时间、状态和由清理器生成的无秘密计数报告；
- 角色变更只返回内部 UUID、前后角色、执行入口和稳定原因码。

这些接口用于运维定位，不是向普通用户开放的活动记录，也不替代数据库备份、集中日志和监控告警。

## 创建首名管理员

公开注册只能创建 `user`。先注册并验证一个受控运维账号，再从安全终端使用该账号的内部 UUID 执行：

```powershell
& .\.venv\Scripts\python.exe -m dotenv -f .env.production run -- `
    .\.venv\Scripts\python.exe -m apps.maintenance.main `
    set-user-role `
    --user-id 00000000-0000-0000-0000-000000000000 `
    --role admin `
    --confirm-role-change
```

命令不接受邮箱作为目标，避免把个人信息写入 shell 历史或运维输出。没有 `--confirm-role-change` 时拒绝修改；重复设置相同角色返回 `changed=false` 且不写入伪变更记录；不能提升停用账号，也不能降级最后一名活动管理员。实际变更写入 `admin_role_change_events`。

使用生产数据库时，命令必须通过受保护环境文件或当前进程提供受限 `re3d_runtime` URL，不能把数据库 URL 作为命令行参数，也不能把管理员或迁移账户长期写入平台配置。角色变化会在下一次受保护请求时生效，因为 API 每次都会从数据库重新读取当前用户；无需把角色写入长期 JWT 声明。

## 当前未覆盖

- 还没有管理员前端页面；当前通过 OpenAPI/受控 API 客户端查询；
- 尚未实现管理员操作二次验证、细分权限和紧急访问流程；
- offset 分页适用于首期有界审计查询，大规模事件库应改用复合游标；
- 普通 API 来源 IP 共享限流已经实现；集中审计存储、异常告警和管理员入口的二次验证仍是公网发布前置项。
