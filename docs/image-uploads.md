# 图片上传与任务提交

## 1. 当前实现范围

上传接口目前只在 `APP_ENV=development` 或 `test` 时注册。它接收真实 JPEG/PNG 图片；提交不带请求体时使用 `execution_mode=simulated`，显式提交 `{"execution_mode":"real"}` 时进入真实队列。默认网页流程仍使用模拟模式，因此上传成功本身不等于真实 Re3D 已执行。

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/v1/uploads` | 创建或复用当前用户的上传会话 |
| GET | `/api/v1/uploads/{upload_id}` | 查询上传数量、字节数和图片元数据 |
| POST | `/api/v1/uploads/{upload_id}/images` | 使用 multipart 字段 `file` 上传一张图片 |
| DELETE | `/api/v1/uploads/{upload_id}/images/{image_id}` | 删除未提交会话中的一张图片 |
| POST | `/api/v1/uploads/{upload_id}/cancel` | 取消会话并删除未提交任务目录 |
| POST | `/api/v1/uploads/{upload_id}/submit` | 校验完整输入、生成契约并原子创建队列任务 |
| GET | `/api/v1/development/jobs?limit=20` | 查询当前用户最近任务 |

所有接口都要求 Bearer access token。查询其他用户的上传会话返回 404，不通过 ID 暴露对象是否存在。

## 2. 数据和事务边界

`0003_job_uploads` 增加：

- `job_uploads`：上传所有者、状态、幂等键、图片数量、总字节数和提交时间；
- `job_upload_images`：平台存储名、原始显示名、真实 MIME、尺寸、字节数和 SHA-256；
- 上传会话 ID 在提交后直接成为 `reconstruction_jobs.id` 和任务目录 UUID。

`0004_upload_lifecycle` 增加 `cancelled_at`、`cancellation_reason`、`storage_cleaned_at` 和维护查询索引。取消记录不会立即从数据库删除，因此能够区分用户取消和超时回收，并能判断目录删除是否成功。

创建上传会话时只建立 `jobs/<uuid>/input/images` 和隔离的 staging 目录，不创建队列任务。提交时在同一个数据库事务内完成：

1. 锁定上传会话；
2. 验证至少三张图片以及数据库计数；
3. 重新检查目录文件集合、大小和 SHA-256；
4. 原子写入 `input-manifest.json` 和 `pipeline-request.json`；
5. 插入 `queued` 任务；
6. 把上传状态改为 `submitted`。

因此未完成的上传不会被 Worker 领取，数据库也不会出现已经提交但没有队列任务的正常状态。

单张删除只允许发生在 `uploading` 状态。文件先原子移动到任务内 staging 墓碑，再删除数据库元数据；数据库事务失败时文件会移回原位。上传取消会先把数据库状态改为 `cancelled` 并删除图片元数据，然后只删除 `Re3D-data/jobs/<upload_uuid>` 对应目录。已经提交的上传不能使用上传取消接口，必须改走任务取消流程。

## 3. 输入安全规则

- 仅接受能被 Pillow 完整解码和验证的单帧 JPEG 或 PNG；
- 不相信扩展名和浏览器声明的 MIME，由服务端识别实际格式；
- 默认单张不超过 25 MiB、总量不超过 1 GiB、图片不超过 5000 万像素；
- 每个上传会话允许 3–150 张图片；
- 同一上传中 SHA-256 相同的图片被视为重复内容并拒绝；
- 原始文件名只保留为经过规范化的显示元数据；磁盘文件名由平台生成；
- 文件先写入任务内 staging，超过限制或解码失败时删除临时文件；
- 提交前再次计算哈希，阻止上传完成后文件被替换；
- manifest 和请求通过既有原子写入与 JSON Schema 校验。

环境变量：

```dotenv
UPLOAD_MAX_FILE_BYTES=26214400
UPLOAD_MAX_TOTAL_BYTES=1073741824
UPLOAD_MAX_PIXELS=50000000
UPLOAD_STALE_AFTER_HOURS=24
UPLOAD_CLEANUP_BATCH_SIZE=100
```

服务会拒绝不合理的配置值。反向代理仍需设置请求体大小、请求速率和连接超时；应用层限制不能替代代理层的早期拒绝。

## 4. 超时回收

默认将 24 小时没有更新的 `uploading` 会话标记为 `cancelled/expired`。维护命令同时重试此前取消成功但目录删除失败的记录：

```powershell
cd D:\3Dreconstruction\Re3D-platform
.\.venv\Scripts\python.exe -m dotenv -f .env run -- `
  .\.venv\Scripts\python.exe -m apps.worker.main cleanup-stale-uploads
```

命令输出扫描数、过期数、目录清理数和失败 UUID。它不会选择 `submitted` 上传，也不会处理已经入队的任务目录。公开部署时应由受控的计划任务周期执行；当前没有自动启动调度器。

## 5. 当前未完成

- 分块/断点续传和大文件对象存储；
- EXIF 隐私元数据清除策略；当前保留原始图片内容；
- 浏览器刷新后恢复未提交上传会话；
- 未验证邮箱、用户配额、并发数和提交频率限制；
- 上传病毒扫描、隔离进程解码和代理层请求限制；
- 真实图片质量预检；
- 任务详情、取消、SSE 进度和结果页面。

这些能力完成前，上传接口只能用于本机开发验证，不能直接开放到公开互联网。
