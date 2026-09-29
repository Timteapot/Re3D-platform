# 稳定任务 API

## 路由

任务访问统一使用 `/api/v1/jobs`：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/v1/jobs?limit=20` | 列出当前用户最近任务 |
| GET | `/api/v1/jobs/{job_id}` | 读取任务状态投影 |
| GET | `/api/v1/jobs/{job_id}/detail` | 读取经过契约裁剪的输入、结果和评估摘要 |
| GET | `/api/v1/jobs/{job_id}/events` | 读取带 Bearer 认证的 SSE 状态流 |
| POST | `/api/v1/jobs/{job_id}/cancel` | 请求取消当前用户任务 |
| GET | `/api/v1/jobs/{job_id}/artifacts/{branch}/{kind}` | 读取经过完整性复核的固定产物 |

这些路由在 development、test 和 production 环境注册。所有用户身份只来自经过校验的 Bearer access token；任务不存在和跨用户访问统一返回 404。

读取、下载和取消已有任务不要求邮箱已验证。development/test 的 `/api/v1/development/simulated-jobs` 创建接口要求邮箱已验证；这与上传提交接口采用同一服务端权限边界。

## 环境边界

| 接口组 | development/test | production |
|---|---:|---:|
| `/api/v1/auth` | 已注册 | 已注册 |
| `/api/v1/jobs` | 已注册 | 已注册 |
| `/api/v1/uploads` | 已注册；默认 simulated，可显式 real | 已注册；只允许 real |
| `/api/v1/development/simulated-jobs` | 已注册 | 不注册 |
| `/api/v1/development/jobs` 兼容入口 | 已注册但不进入 OpenAPI | 不注册 |

production 上传提交由服务端强制使用 `execution_mode=real`；客户端不能在生产环境创建模拟任务。上传和真实任务创建边界已经稳定，但任务限流、配额、保留期和部署加固仍是公开启用前置条件。

## 响应边界

- 状态列表只读取 PostgreSQL 投影；
- 详情在返回前验证 request、result 和 evaluation Schema 及跨文件身份；
- 响应不包含服务器文件路径、产物 SHA-256 或私有日志；
- 产物地址始终返回稳定 `/api/v1/jobs/...` 路径，即使详情通过开发兼容入口读取；
- 取消、查询、SSE 和下载使用同一对象所有权规则；
- 下载额外复核任务终态、固定分支/类型、规范化路径、大小和 SHA-256。

## 兼容策略

旧 `/api/v1/development/jobs` 只在 development/test 暂时保留，避免本机脚本在迁移期间中断。它从 OpenAPI 隐藏，新代码和文档不得继续引用该入口。production 不注册兼容入口，后续确认外部脚本全部迁移后可以删除。

## 当前未完成

- 用户/来源 IP 限流、并发与存储配额；
- 任务访问和取消审计；
- Caddy 已配置 SSE 立即转发并完成普通 API 代理验收；仍需完成带认证的长连接、断线恢复和大文件并发下载验收；
- 成功任务保留期及过期后的 410/404 产品语义。
