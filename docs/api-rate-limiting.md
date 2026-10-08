# 普通 API 来源 IP 限流

更新时间：2026-10-08

## 作用范围

API 对所有 `/api/v1/` HTTP 请求执行数据库共享的来源 IP 固定窗口限流。`OPTIONS` 预检请求、`/health/live` 和非 API 页面不计数。认证模块原有的登录失败、注册、验证邮件和密码重置限流继续生效，因此这些入口会同时受到普通 API 总量上限和更严格的业务专项上限约束。

该层保护注册登录以外的任务查询、上传、取消、下载、SSE 建连和管理员审计等接口，避免单一来源持续占用 API 与数据库连接。它统计请求次数，不统计上传或下载字节数，也不替代任务提交次数限制和后续单用户存储容量配额。

## 配置

```dotenv
API_IP_RATE_LIMIT_WINDOW_SECONDS=60
API_IP_RATE_LIMIT_MAX_REQUESTS=300
```

development/test 在缺少配置时使用上述默认值。production 必须显式提供两个整数，生产就绪检查会拒绝缺失值。示例中的 60 秒/300 次只是首期起点，最终值应根据真实 SSE 重连、并发上传、模型下载和压力测试结果确定。

来源地址通过现有 `AUTH_TRUSTED_PROXY_CIDRS` 信任边界解析：只有直接连接来自可信代理网络时才读取 `X-Forwarded-For`。目标 Windows 单机拓扑中，Caddy 与 API 同机时通常只信任 `127.0.0.1/32,::1/128`，不能配置全网段信任。

## 数据与并发合同

- `api_rate_limit_buckets` 只保存由 JWT secret 参与 HMAC 计算的 64 位引用、窗口计数和时间，不保存原始 IP；
- PostgreSQL 行锁和唯一键处理多个 API 进程对同一来源的并发请求；
- 允许的响应包含 `X-RateLimit-Limit`、`X-RateLimit-Remaining` 和 `X-RateLimit-Reset-Seconds`；
- 超限返回 `429`、`Retry-After` 和 `X-Re3D-Error-Code: API_IP_RATE_LIMITED`；
- `cleanup-auth-security` 会按 `AUTH_THROTTLE_RETENTION_DAYS` 清理长期未更新的普通 API 限流桶；
- JWT secret 轮换后会生成新的 IP 引用；旧引用由上述维护任务按保留期清理。

## 部署边界

这是应用层滥用控制，不是抗 DDoS 设施。获得服务器和域名后，还需要在公网入口配置云防火墙或安全组、Caddy 请求体和超时边界，并根据威胁模型决定是否在域名前增加 CDN/WAF。边缘层负责在流量到达 API 和 PostgreSQL 之前丢弃大规模恶意请求；本模块负责多个 API 进程之间一致的业务请求计数。
