# 受鉴权的重建产物下载

## 当前范围

稳定任务 API 为成功任务提供以下接口：

```text
GET /api/v1/jobs/{job_id}/artifacts/{branch}/{kind}
```

固定选择器：

- `branch`：`A-v4`、`B-v2`、`C`；
- `kind`：`glb`、`obj`、`mtl`、`texture`。

该路由与任务列表、详情、取消和 SSE 一样，在 development、test 和 production 环境注册。旧 `/api/v1/development/jobs/...` 路径仅在 development/test 保留为迁移兼容入口，并从 OpenAPI 隐藏。邮箱验证状态、用户级任务提交限制、单用户存储配额、普通 API 来源 IP 请求频率限制和成功产物 30 天保留策略已经接入，但路由可在 production 注册仍只表示接口边界稳定，不代表下载带宽/并发控制、任务预留值实测校准、目标服务器保留策略观察和部署加固已经完成。

## 服务端判定顺序

每次下载依次执行：

1. 验证 Bearer access token；
2. 使用 token 对应的内部用户 ID 查询任务，跨用户访问与不存在任务统一返回 404；
3. 要求数据库任务状态、管线结果和目标分支均为 `succeeded`；
4. 验证 pipeline request、pipeline result、evaluation 的 Schema、任务身份、执行模式和来源哈希；
5. 只从目标分支的 result artifacts 中选择请求的固定 kind，不接受客户端路径；
6. 要求清单路径和 MIME 与固定白名单完全一致；
7. 使用 `TaskLayout` 再次执行任务目录边界和符号链接解析检查；
8. 重新计算实际文件大小和 SHA-256，并与结果清单比较；
9. 以 `Content-Disposition: attachment` 返回文件。

响应同时设置：

```text
Cache-Control: private, no-store
X-Content-Type-Options: nosniff
ETag: "<artifact-sha256>"
```

服务端不会把真实磁盘路径、SHA-256 或任意诊断文件内容加入详情响应。详情只返回固定下载 URL；该 URL 本身不包含令牌。

成功任务达到产物保留期并由显式清理命令处理后，状态转为 `expired`，详情保留评估和结果摘要但不再生成下载 URL；下载接口返回产物不可用。最终采用 410 还是 404 的产品语义仍待确定。

## 前端流程

任务详情页不会使用普通 `<a href>` 直接导航，因为 access token 只保存在内存中。页面通过认证上下文发起带 `Authorization` 请求头的 Fetch，收到 Blob 后创建短期 `blob:` URL 并触发本地保存，随后撤销 URL。

这意味着浏览器下载时会暂时把完整文件保存在内存中。当前真实 GLB 约 30–36 MB，可用于首期本机开发；更大的 49/100 图产物需要评估浏览器内存、Range 请求或对象存储短期签名 URL。

## 稳定错误语义

| HTTP | 含义 |
|---:|---|
| 401 | 缺少、过期或无效的 access token |
| 404 | 任务对当前用户不可见，或请求的分支/产物类型不在结果清单中 |
| 409 | 任务尚未成功完成、结果不可用，或文件未通过路径/大小/SHA-256 完整性复核 |
| 422 | branch 或 kind 不属于固定枚举 |

内部文件路径和具体完整性失败原因不会返回浏览器，避免泄露服务器布局。

## 与后续模块的接口

- GLB 查看器已复用同一认证 Fetch，把 GLB Blob 转成短期对象 URL 后交给 Three.js `GLTFLoader`；切换分支时重新执行服务端所有权和完整性复核；
- OBJ/MTL/纹理按钮用于离线检查和学习用途；
- 任务过期或清理后，数据库状态必须先转为 `expired`，接口随即停止提供文件；
- 对象存储迁移时，`ArtifactReader` 的所有权和清单复核仍应保留在签名 URL 之前。
