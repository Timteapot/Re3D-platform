# Re3D Web 三维重建平台项目计划

文档版本：1.2
编制日期：2026-09-23
活动算法基线：Re3D 标签 `re3d-pipeline-v1.1.0`，提交 `2c5ba174dae9fe53dcec8f7d8466793fdebf0c58`
平台目录：`D:\3Dreconstruction\Re3D-platform`
数据目录：`D:\3Dreconstruction\Re3D-data`

## 1. 项目目标

在现有 Re3D 多视图三维重建管线之上，建设一个部署于公开互联网、仅用于学习、研究和项目实践的非商业 Web 平台。平台应允许用户注册、登录、上传多视图照片、提交异步重建任务、查看任务进度、在线预览三维结果，并查看自动生成的重建质量评估。

平台的首个可交付版本应完成以下闭环：

```text
注册/登录
  → 创建重建任务
  → 上传并校验图片
  → 排队执行 Re3D
  → 收集 OBJ/GLB/纹理及诊断数据
  → 自动评估
  → 在线查看、对比分支结果并下载产物
```

## 2. 项目边界

### 2.1 本期范围

- 用户注册、邮箱验证、登录、退出、会话续期和密码重置；
- 用户只能访问自己的任务、图片、评估和产物；
- JPG、JPEG、PNG 多图上传；
- 每个首期任务默认执行 Re3D A-v4、B-v2、C 三条分支，并分别展示和评估结果；
- 异步任务排队、阶段进度、失败原因、有限重试和取消；
- GLB 在线预览及 OBJ、MTL、纹理、GLB 下载；
- 输入质量、SfM、深度一致性、网格结构和产物完整性评估；
- 主页、项目说明、使用说明和第三方声明；
- 管理员查看任务健康状态和磁盘占用的基础功能；
- 本地开发、测试环境和单 GPU 部署流程。

### 2.2 暂不纳入首期

- 收费、订阅、商业服务和多租户计费；
- 社交登录、短信登录和复杂组织权限；
- 用户公开分享模型或模型市场；
- 多 GPU 分布式重建；
- 在线网格编辑、标注和人工修补；
- 将自动评估宣称为有真值依据的几何精度；
- 自动训练或微调 MapAnything、MVSAnywhere；
- 允许匿名用户提交重建任务。

## 3. 当前基线与进入条件

### 3.1 已具备

- Re3D 本机环境检查通过；
- 17 个现有单元测试通过；
- 已有多组 A/B/C 完整重建产物；
- 管线已有阶段日志、输入清单、SfM 指标、深度一致性清单、DMAP 校验和最终产物验证；
- 已输出浏览器可读取的 GLB；
- 已补充非商业用途下的第三方资源与署名说明。

### 3.2 平台开发前必须冻结

1. 保留标签 `re3d-pipeline-v1.0.0` 和提交 `d528b1474c55bf3b65a1253bd56ab4b5024adab2` 作为第一版冻结基线；平台活动基线升级为只增加隔离运行目录接口的 `re3d-pipeline-v1.1.0`，不无版本地修改管线行为；
2. 为管线定义机器可读的运行契约，包括参数、阶段、退出码、日志和产物；
3. 首期任务固定执行 A-v4、B-v2、C 三条分支；用户界面解释三条分支并提供结果切换和对比，不允许普通用户通过参数绕过平台固定配置；
4. 确认 MapAnything 当前权重的准确来源；无法补录时继续按 CC-BY-NC 非商业权重处理；
5. 把所有阈值和资源限制放入环境配置，不写死在页面或业务代码中。

### 3.3 当前阶段 0 进度

- 已完成 Re3D 标签、Git SHA、配置哈希和模型清单哈希冻结；
- 已完成 `pipeline-request`、`pipeline-event`、`pipeline-result`、`evaluation` v1 JSON Schema；
- 已完成契约有效样例、负向校验和跨文件一致性测试；
- 已完成 Windows Worker 目录边界、事件日志、模拟执行、原子结果和检查点恢复；
- 已完成 Re3D v1.1.0 隔离运行目录和平台真实适配器 dry-run；
- 已完成 PostgreSQL 最小任务状态机、Alembic 首次迁移、单 GPU 租约和后台心跳基础层；
- 已完成开发 API → PostgreSQL → 租约 Worker → A-v4/B-v2/C 模拟产物 → 模拟评估报告闭环；
- 临时 Docker PostgreSQL 端到端测试已覆盖迁移、队列领取、Worker 执行和 API 终态查询；
- 下一步建立用户数据模型和注册/登录 API，再将开发接口替换为真实认证主体。

## 4. 推荐技术架构

### 4.1 技术栈

| 层级 | 推荐技术 | 用途 |
|---|---|---|
| 前端 | React、TypeScript、Vite | 页面和交互基础 |
| 路由与请求 | React Router、TanStack Query | 路由、缓存、轮询和请求状态 |
| 三维显示 | Three.js、React Three Fiber | GLB 加载、轨道控制、灯光和模型信息 |
| 后端 API | FastAPI、Pydantic | REST API、参数校验和 OpenAPI |
| 数据访问 | SQLAlchemy、Alembic | ORM 和数据库迁移 |
| 数据库 | PostgreSQL | 用户、任务、阶段、产物和评估元数据 |
| 任务调度 | 独立 Worker + PostgreSQL 任务租约 | 单 GPU 排队、崩溃恢复和并发控制 |
| 文件存储 | 开发期本地文件系统；接口按对象存储抽象 | 上传图片、日志、评估和模型产物 |
| 反向代理 | Nginx 或同类代理 | TLS、静态资源、请求大小和超时控制 |
| 测试 | Pytest、Vitest、Playwright | 单元、集成和端到端测试 |

首期采用 PostgreSQL 任务租约而不是额外引入消息中间件，以降低 Windows 本地开发和单 GPU 调度的复杂度。任务量上升后，可以在不改变 API 的前提下替换为 Redis/RabbitMQ 等持久队列。

### 4.2 服务关系

```text
Browser
  │
  ├── Frontend
  │      └── 说明页、认证、任务工作台、三维查看器、评估面板
  │
  └── Backend API
         ├── PostgreSQL
         ├── File/Object Storage
         └── Reconstruction Worker（单 GPU 并发 1）
                ├── Pipeline Adapter
                ├── Re3D
                └── Evaluation Service
```

API 进程不得同步执行 Re3D，也不得接受用户提供的本地路径或命令。Worker 只接收平台生成的 UUID、受控配置和已完成校验的任务目录。

## 5. 目录和仓库规划

### 5.1 平台代码目录

建议将 `D:\3Dreconstruction\Re3D-platform` 初始化为独立 Git 仓库：

```text
Re3D-platform/
├── apps/
│   ├── web/                    # React 前端
│   ├── api/                    # FastAPI API
│   └── worker/                 # GPU 任务执行器
├── packages/
│   ├── contracts/              # OpenAPI 生成类型或共享 Schema
│   └── ui/                     # 可选的共享 UI 组件
├── backend/
│   ├── migrations/             # Alembic 数据库迁移
│   ├── re3d_adapter/           # Re3D 运行契约和产物解析
│   └── evaluation/             # 自动评估规则
├── deploy/                     # 部署编排、代理和环境模板
├── docs/
│   ├── architecture.md
│   ├── api.md
│   ├── evaluation.md
│   ├── operations.md
│   └── third-party.md
├── tests/
│   ├── integration/
│   ├── e2e/
│   └── fixtures/
├── .env.example
├── README.md
└── PROJECT_PLAN.md
```

Re3D 算法仓库和平台仓库保持分离。平台通过配置项 `RE3D_ROOT` 和固定的管线版本调用 Re3D，不复制其模型、vendor 目录或运行环境。

### 5.2 数据目录

`D:\3Dreconstruction\Re3D-data` 不进入 Git，建议结构如下：

```text
Re3D-data/
├── jobs/
│   └── <job_uuid>/
│       ├── input/
│       │   ├── images/
│       │   └── input-manifest.json
│       ├── runtime/
│       │   ├── work/
│       │   └── logs/
│       ├── output/
│       │   ├── A-v4/
│       │   ├── B-v2/
│       │   └── C/
│       ├── reports/
│       │   └── evaluation.json
│       ├── manifests/
│       │   ├── pipeline-request.json
│       │   ├── pipeline-events.jsonl
│       │   └── artifact-manifest.json
│       └── state.json
├── tmp/
└── quarantine/
```

原图、中间文件、日志、三个分支的成功产物和评估报告均以 `job_uuid` 为唯一任务单元保存。原始文件名只作为显示元数据保存；磁盘文件使用平台生成的安全名称。任务目录只能由受控的 `jobs` 根目录和平台生成的 UUID 推导，不能直接拼接用户名、邮箱或用户输入。

Worker 以 `jobs/<job_uuid>/input/images` 作为 Re3D 的图片输入，并使用 `job_uuid` 作为 `scene`。平台适配器将工作目录、输出目录和日志目录指向同一任务目录下的受控子目录，避免运行数据散落在算法仓库中。清理时只允许整体处理一个已进入终态的任务目录。

## 6. 功能设计

### 6.1 用户注册与登录

首期支持用户名、邮箱和密码：

- 用户名和邮箱唯一；
- 密码使用 Argon2id 哈希，不保存或记录明文密码；
- 短时 access token；
- refresh token 使用 HttpOnly、Secure、SameSite Cookie，并支持旋转和服务端撤销；
- 登录、注册和刷新接口设置速率限制；
- 注册完成后必须验证邮箱，未验证账号不能提交重建任务；
- 支持一次性、短时有效的密码重置令牌；
- 对注册、登录、找回密码和任务提交设置 IP 与账号双维度限流，达到风险阈值时启用验证码；
- 记录最近登录时间，不记录密码或完整令牌；
- 普通用户和管理员两种角色；
- 所有任务、文件和下载接口执行对象级所有权检查。

由于平台面向公开互联网，邮箱验证、密码重置、登录防爆破和任务配额属于首期上线条件，不延后到公开部署之后。

### 6.2 重建任务

每个任务使用不可预测的 UUID，由平台生成，不允许用户指定 Re3D `scene`。推荐状态机：

```text
draft
  → uploading
  → validating_input
  → queued
  → preparing
  → sfm
  → dense_reconstruction
  → meshing
  → texturing
  → validating_output
  → evaluating
  → succeeded
```

终止状态：

- `failed_input`：图片数量、格式、尺寸、质量或覆盖不满足要求；
- `failed_pipeline`：重建子进程失败；
- `failed_evaluation`：模型已产生，但评估步骤失败；
- `cancelled`：用户或管理员取消；
- `expired`：按数据保留策略清理。

同一 GPU 首期只运行一个任务。任务租约必须包含 Worker ID、租约到期时间和心跳，防止 Worker 崩溃后任务永久停留在运行状态。

失败任务在错误状态、错误码和脱敏诊断写入数据库后，整体删除对应任务目录；数据库仅保留最小审计记录，不保留原图、中间文件、日志正文或不完整产物。成功任务的原图、中间文件和产物保留期限暂未决定，必须在公开部署前确定并配置。

### 6.3 上传和输入校验

推荐初始限制，最终应通过配置调整：

- 图片数量：3–150 张；
- 单文件上限：25MB；
- 单任务原始文件总量上限：2GB；
- 只接受通过文件签名识别的 PNG、JPEG；
- 所有图片成功解码后再进入队列；
- 拒绝异常像素数量和解压缩炸弹；
- 检查统一尺寸、重复文件哈希、极端长宽比和空 alpha 前景；
- 计算模糊度、曝光分布和相邻图片相似度，严重问题阻止运行，其余作为警告；
- EXIF 方向先归一化，再复制到管线输入；
- 输入确认后生成不可变 `input-manifest.json`。

### 6.4 管线适配器

平台不能依赖解析自由文本日志判断成功。需要在 Re3D 与平台之间定义以下文件契约：

`pipeline-request.json`：

- schema version；
- job UUID；
- pipeline Git SHA；
- 输入 manifest SHA-256；
- 配置文件 SHA-256；
- 分支选择；
- 创建时间和资源限制。

`pipeline-events.jsonl`：

- event schema version；
- stage；
- state；
- timestamp；
- progress current/total；
- user-facing message code；
- optional diagnostic data。

`pipeline-result.json`：

- success/failure；
- stage results；
- exit code；
- artifacts；
- validation report；
- error category；
- retryable；
- duration and resource observations。

准备改造 Re3D：

1. 增加结构化事件输出，同时保留现有文本日志；
2. 增加任务级锁，拒绝同一场景并发执行；
3. 完成标记写入临时文件后原子重命名；
4. 完成标记包含输入、配置和代码版本哈希；
5. 增加取消检查、阶段超时和子进程树终止；
6. 增加可配置的 `work`、`outputs` 和 `logs` 根目录；
7. 将常见失败映射为稳定错误码，而不是只返回 Python traceback。

## 7. 自动评估设计

### 7.1 评估定位

在没有真值网格、激光扫描或人工标注时，平台只能评估输入质量、管线健康、几何结构和跨视角一致性，不能证明模型与真实物体之间的绝对精度。

页面使用“结构健康评估”或“重建质量诊断”，避免使用“精度百分比”。如果提供总分，应称为“工程健康分”，并允许用户展开查看原始指标和规则。

### 7.2 指标来源

| 类别 | 指标 | 数据来源 |
|---|---|---|
| 输入 | 图片数、尺寸一致性、重复率、模糊度、曝光、前景比例 | 上传预检、`input-manifest.json` |
| SfM | 注册图像率、稀疏点数、观测数、平均轨迹长度、平均重投影误差、延迟注册和排除视角 | `reconstruction_metrics.json` |
| 深度 | 完成尺度校正的视角率、每图稀疏样本数、尺度分布 | `sparse_calibration_manifest.json` |
| 一致性 | 平均/最小深度保留率、支持视角数、平均置信度 | `consistency_manifest.json` |
| 交接 | DMAP 数量、尺寸、相机、view ID、payload、数值和哈希 | handoff validation/immutability 报告 |
| 网格 | 顶点、面、有限顶点率、退化面、连通分量、最大分量占比、边界边、非流形边、包围盒 | `evaluate_mesh_quality.py` |
| 纹理与产物 | GLB/OBJ 可加载、面数一致、纹理引用、文件大小 | `validation.json`、artifact manifest |
| 性能 | 总耗时、阶段耗时、峰值磁盘、失败阶段 | Worker 事件和资源采样 |

### 7.3 首期判定方式

每项指标输出 `pass`、`warning`、`fail`、`not_applicable`，附带：

- 实际值；
- 阈值；
- 解释；
- 用户可采取的改进建议；
- 证据文件和字段。

建议将评估分为五个维度：

1. 输入质量；
2. 相机与 SfM；
3. 深度覆盖和跨视角一致性；
4. 网格结构；
5. 产物完整性。

阈值不能一次写死。先用现有 49 图和 100 图场景作为基线，再增加弱纹理、反光、遮挡、模糊、视角不足和尺寸不一致等失败样本，形成 `evaluation-rules-v1.json`。

可以使用以下起始规则，但必须经过测试集校准：

| 指标 | Pass | Warning | Fail |
|---|---:|---:|---:|
| 注册图像率 | ≥ 90% | 70%–90% | < 70% |
| 平均重投影误差 | ≤ 1.5 px | 1.5–3 px | > 3 px |
| 尺度校正成功视角率 | ≥ 95% | 80%–95% | < 80% |
| 分支平均深度保留率 | ≥ 50% | 30%–50% | < 30% |
| 有限顶点率 | 100% | — | < 100% |
| 最大连通分量面占比 | ≥ 85% | 60%–85% | < 60% |
| 最终产物验证 | 全通过 | — | 任一失败 |

`evaluation.json` 至少包含：评估规则版本、管线版本、总状态、维度状态、原始指标、规则结果、建议、生成时间和输入/产物哈希。

### 7.4 页面展示

- 顶部展示整体状态和一句可行动结论；
- 使用五个维度卡片展示 pass/warning/fail；
- 原始数值和阈值可展开；
- 在三维查看器旁展示网格面数、文件大小、连通性和纹理状态；
- A/B/C 多分支完成时并排比较，不把不同分支的分数解释为绝对精度；
- 对失败输入给出重新拍摄建议，例如补充连续视角、固定焦距、减少模糊和反光。

## 8. 前端信息架构

### 8.1 主页 `/`

- 项目一句话说明；
- “开始重建”主按钮；
- 上传到产出的四步流程；
- 示例模型或轻量截图；
- 输入要求和预计耗时提示；
- 非商业学习项目声明；
- 页脚包含项目说明、使用说明、第三方与许可证入口。

### 8.2 技术与项目说明 `/about`

可以采用页内章节或标签页：

- 项目简介；
- 三维重建基本流程；
- Re3D A/B/C 管线说明；
- 支持和不适合的拍摄对象；
- 拍摄与上传说明；
- 自动评估含义和局限；
- 第三方资源、论文引用和许可证；
- 隐私与数据保留说明。

第三方声明内容以 Re3D 的 `THIRD_PARTY_NOTICES.md` 为来源，平台文档应保存同步日期和对应 Re3D Git SHA。

### 8.3 注册和登录

- `/register`：用户名、邮箱、密码、确认密码、用途和隐私提示；
- `/login`：用户名或邮箱、密码；
- `/account`：基本资料、会话退出、任务和数据删除入口；
- 未登录用户访问任务页时重定向到登录并保留返回地址。

### 8.4 重建工作台

- `/reconstructions/new`：拖拽上传、图片缩略图、输入检查、分支或质量档位；
- `/reconstructions`：任务历史、状态、创建时间、耗时和删除；
- `/reconstructions/:id`：统一展示输入、进度、输出和评估。

任务详情页建议四个标签：

1. `输入`：图片列表、输入诊断、配置摘要；
2. `进度`：当前阶段、阶段时间线、可读日志和取消操作；
3. `三维结果`：GLB 查看器、分支切换、模型信息和下载；
4. `自动评估`：维度状态、指标、阈值、建议和报告下载。

## 9. API 初稿

### 9.1 认证

```text
POST /api/v1/auth/register
POST /api/v1/auth/login
POST /api/v1/auth/refresh
POST /api/v1/auth/logout
GET  /api/v1/users/me
```

### 9.2 重建任务

```text
POST   /api/v1/reconstructions
GET    /api/v1/reconstructions
GET    /api/v1/reconstructions/{job_id}
DELETE /api/v1/reconstructions/{job_id}
POST   /api/v1/reconstructions/{job_id}/cancel
POST   /api/v1/reconstructions/{job_id}/retry
GET    /api/v1/reconstructions/{job_id}/events
```

### 9.3 文件和结果

```text
POST /api/v1/reconstructions/{job_id}/inputs
POST /api/v1/reconstructions/{job_id}/submit
GET  /api/v1/reconstructions/{job_id}/inputs
GET  /api/v1/reconstructions/{job_id}/artifacts
GET  /api/v1/reconstructions/{job_id}/artifacts/{artifact_id}/download
GET  /api/v1/reconstructions/{job_id}/evaluation
```

事件接口首期可以使用 Server-Sent Events；它适合单向进度更新，断线恢复比为此场景引入双向 WebSocket 更简单。数据库状态仍是事实来源，前端断线后必须能通过普通 GET 恢复。

## 10. 数据模型初稿

| 实体 | 关键字段 |
|---|---|
| `users` | id、username、email、password_hash、role、status、created_at、last_login_at |
| `refresh_sessions` | id、user_id、token_hash、expires_at、revoked_at、device_info |
| `reconstruction_jobs` | id、user_id、status、branch_policy、pipeline_sha、input_hash、config_hash、progress、error_code、created/started/finished_at |
| `job_inputs` | id、job_id、original_name、storage_key、sha256、size、width、height、mime_type、validation_status |
| `job_stages` | id、job_id、stage、attempt、status、started_at、finished_at、exit_code、message_code |
| `worker_leases` | job_id、worker_id、leased_at、heartbeat_at、expires_at |
| `artifacts` | id、job_id、branch、kind、storage_key、sha256、size、content_type |
| `evaluation_reports` | id、job_id、rules_version、overall_status、report_json、created_at |
| `audit_events` | id、user_id、job_id、event_type、created_at、safe_metadata |

数据库只保存文件元数据和存储键，不存大图片、网格或完整日志正文。

## 11. 安全和数据治理

- 所有密钥从环境变量或密钥管理系统注入，不提交到 Git；
- 密码使用 Argon2id；日志过滤密码、令牌、Cookie 和用户本地路径；
- 上传文件使用文件签名和实际解码双重校验；
- API 永远不接受任意操作系统路径；
- 子进程使用参数数组，不经 shell 拼接；
- Worker 使用低权限账户，只能访问 Re3D 和受控数据目录；
- 用户下载通过鉴权接口或短期签名 URL；
- 所有查询包含 `user_id` 所有权条件；
- 删除任务采用状态确认和延迟清理，避免运行中删除；
- 成功任务的原图、中间文件、日志和产物作为一个任务单元保留，具体期限待定并由环境配置控制；在期限确定前不得开放互联网部署；
- 失败任务在终态和脱敏错误信息持久化后删除整个任务文件目录，只保留数据库中的最小审计记录；
- 清理前解析并核对目标绝对路径必须是 `Re3D-data/jobs` 下的单个 UUID 任务目录，禁止对数据根目录、通配符或用户输入路径执行递归删除；
- 第三方说明页和隐私说明页在未登录状态下也可访问。

## 12. 测试计划

### 12.1 单元测试

- 注册、密码策略、令牌旋转和撤销；
- 用户对象级权限；
- 状态机合法转换；
- 文件名、MIME、哈希和路径安全；
- Re3D manifest 和事件解析；
- 评估规则边界；
- 清理策略不越过数据根目录。

### 12.2 集成测试

- PostgreSQL 迁移和事务；
- 上传、提交、排队、租约、心跳和崩溃恢复；
- API 到模拟 Worker 的完整流程；
- Re3D dry-run 合同测试；
- 产物权限和 Range 下载；
- 任务取消和超时。

### 12.3 GPU 端到端测试

至少维护以下数据集：

- 小型快速冒烟集：8–12 图；
- 标准成功集：当前 49 图场景；
- 大型成功集：当前 100 图场景；
- 视角不足、模糊、重复图片、弱纹理、尺寸不一致等预期失败集；
- 运行中取消和失败后重试场景。

GPU E2E 不应在每次普通前端提交时全部执行；快速合同测试进入持续集成，完整 GPU 回归按发布候选执行。

### 12.4 前端端到端测试

- 注册、登录、退出；
- 上传校验和错误展示；
- 创建任务及进度恢复；
- 成功、输入失败、管线失败和取消页面；
- GLB 加载失败降级；
- 评估指标和建议展示；
- 用户 A 无法访问用户 B 的任务。

## 13. 阶段计划与交付物

按单人全栈开发估算，总量约 36–55 个有效工作日；算法运行环境迁移、云资源申请等待和大规模调参不计入该估算。

### 阶段 0：基线冻结与准备，3–5 日

- 初始化平台仓库、分支策略和环境模板；
- 建立数据目录和 `.gitignore`；
- 冻结 Re3D SHA、模型哈希和配置；
- 定义 pipeline request/event/result schema；
- 建立小型冒烟数据集；
- 输出架构决策记录。

验收：平台可用模拟任务完整走通状态机，Re3D dry-run 合同通过。

### 阶段 1：平台骨架，4–6 日

- React 与 FastAPI 工程；
- PostgreSQL、Alembic、配置管理；
- 健康检查、结构化日志和统一错误响应；
- OpenAPI 到前端类型生成；
- 基础 CI。

验收：前后端、数据库可一键启动，迁移和基础测试通过。

### 阶段 2：用户系统，4–6 日

- 注册、登录、刷新、退出；
- 角色和对象级权限；
- 登录/注册/账户页面；
- 速率限制和安全测试。

验收：两个用户的数据完全隔离，令牌可撤销，密码不出现在日志和数据库明文字段。

### 阶段 3：上传与任务编排，6–9 日

- 多文件上传、校验、manifest；
- 任务状态机、数据库租约、Worker 心跳；
- SSE 进度和取消；
- 任务列表与详情进度页。

验收：模拟长任务可以排队、恢复、取消，单 GPU 并发限制生效。

### 阶段 4：Re3D 集成，7–10 日

- Re3D 结构化事件、锁、哈希完成标记、可配置根目录；
- Worker 管线适配器；
- 错误分类和产物归档；
- 49 图、100 图端到端回归。

验收：网页提交任务后能得到可下载 GLB，Worker 重启不会破坏已完成任务或重复并发执行。

### 阶段 5：自动评估，5–8 日

- 评估规则和 schema；
- 接入 SfM、深度、handoff、网格和产物指标；
- 失败建议库；
- 评估 API 与页面。

验收：成功集、失败集得到可解释且稳定的结果；每个结论都能追溯到原始指标。

### 阶段 6：三维结果页和内容页，5–8 日

- Three.js GLB 查看器；
- 分支切换、模型信息和下载；
- 首页、技术介绍、使用说明、第三方声明；
- 加载性能和移动端基本适配。

验收：当前最大 GLB 有加载进度、错误降级和内存提示，第三方声明可从所有公开页面到达。

### 阶段 7：部署加固，2–3 日起

- TLS、反向代理、请求限制和备份；
- 数据保留与自动清理；
- 部署检查、监控、告警和恢复演练；
- 发布候选 GPU 回归；
- 运维手册和已知限制。

验收：全新环境可按文档部署；服务重启、Worker 崩溃、磁盘接近阈值和任务失败均有明确处理路径。

## 14. MVP 验收标准

1. 新用户可注册、验证邮箱、登录、重置密码、退出并恢复会话；
2. 用户只能查看和下载自己的数据；
3. 用户可上传合规图片并看到明确的预检结果；
4. 提交后 API 立即返回 job UUID，不阻塞等待重建；
5. 同一 GPU 不会并发运行两个重建任务；
6. 任务详情显示当前阶段、进度、耗时和可读错误；
7. 49 图标准集可从网页完成 A-v4、B-v2、C 三条分支的完整重建；
8. 成功任务可切换查看三条分支的 GLB，并下载标准产物；
9. 页面展示结构健康评估、原始指标、阈值和改进建议；
10. 输入失败、管线失败、评估失败和取消状态可区分；
11. 成功任务按已配置的保留策略整体清理，失败任务的文件目录在终态持久化后整体删除；
12. 首页、项目说明、使用说明和第三方声明完整可访问；
13. 自动测试、数据库迁移和部署文档可重复执行；
14. 不在 Git、日志、API 响应或浏览器中暴露密码、令牌、服务器绝对路径和模型权重。

## 15. 开发前准备清单

### 15.1 产品与规则

- [x] 公开范围确定为公开互联网；
- [x] 首期固定执行 A-v4、B-v2、C 三条分支，不允许普通用户跳过分支；
- [ ] 确定单用户任务数、上传上限和每天提交次数；
- [x] 失败任务文件按任务单元删除，仅保留脱敏最小审计记录；
- [ ] 确定成功任务中原图、中间文件、日志和产物的统一或分级保留时间；
- [ ] 准备隐私说明、非商业说明和第三方声明页面文案；
- [ ] 定义“工程健康分”是否展示；如展示，必须公开维度和规则版本。

### 15.2 代码与环境

- [x] 初始化 `Re3D-platform` Git 仓库；
- [x] 创建 `.editorconfig`、`.gitignore`、`.env.example` 和项目说明；
- [ ] 固定 Node.js、Python、PostgreSQL 和前后端依赖版本；
- [ ] 建立开发、测试、生产三套环境配置；
- [x] 在环境模板中设置 `RE3D_ROOT=D:\3Dreconstruction\Re3D`；
- [x] 在环境模板中设置 `RE3D_DATA_ROOT=D:\3Dreconstruction\Re3D-data`；
- [ ] 确认 Worker 账户对 Re3D 只需读取代码和模型，对数据目录具有受限读写权限；
- [ ] 记录 GPU、驱动、CUDA、两个 Python 环境和 OpenMVS 二进制身份。

### 15.3 数据与测试集

- [ ] 从已有数据中选择一个 8–12 图快速测试集；
- [ ] 固定 49 图和 100 图成功基线；
- [ ] 创建可公开用于演示的自有图片集；
- [ ] 准备至少六类预期失败输入；
- [ ] 给每个测试集记录授权、来源、图片数、尺寸、预期状态和允许保留期限；
- [ ] 不把真实用户上传数据加入 Git 或测试 fixture。

### 15.4 基础设施

- [ ] 准备 PostgreSQL 实例和数据库备份位置；
- [ ] 申请域名和受信任的 TLS 证书；
- [ ] 评估服务器 GPU 显存、CPU、RAM、磁盘和网络；
- [ ] 为中间文件预留容量并设置磁盘使用告警；
- [ ] 确定对象存储或本地存储方案；
- [ ] 确定日志、指标和错误告警方案；
- [ ] 准备最小权限的部署账户和密钥管理方式；
- [ ] 为第三方许可证、对应源码和 SBOM 预留公开入口。

### 15.5 建议最先完成的四个技术验证

1. API 创建模拟任务，Worker 通过数据库租约领取并持续上报进度；
2. 从 `Re3D-data/jobs/<job_uuid>/input/images` 调用一次 Re3D dry-run；
3. 用小型数据集完成一次真实重建，把三条分支的 GLB 归档到 `Re3D-data/jobs/<job_uuid>/output`；
4. 前端加载该 GLB，并同时展示从现有 JSON 报告生成的评估卡片。

完成这四项后再大规模开发页面，可以尽早暴露任务编排、文件权限、模型加载和评估数据契约中的问题。

## 16. 主要风险与控制

| 风险 | 影响 | 控制措施 |
|---|---|---|
| GPU 任务耗时长 | HTTP 超时、体验差 | 异步任务、SSE、数据库状态恢复 |
| 同名或并发任务污染 | 产物损坏 | UUID、任务锁、单 GPU 并发 1、原子标记 |
| 旧完成标记误复用 | 返回错误结果 | 输入/配置/代码哈希参与缓存键 |
| 输入质量不可控 | 高失败率 | 上传预检、可读错误、拍摄建议 |
| 无真值却给出高分 | 误导用户 | 使用结构健康评估、公开规则和局限 |
| GLB 较大 | 浏览器卡顿 | 加载进度、网格简化、LOD、压缩 |
| 中间文件快速增长 | 磁盘耗尽 | 配额、保留策略、清理任务和告警 |
| 上传文件攻击 | 服务或解析器风险 | 魔数、解码、像素限制、隔离目录、低权限 Worker |
| 第三方许可遗漏 | 无法公开发布 | 第三方页面、来源清单、SBOM、权重不随包分发 |
| Windows 路径写死 | 环境不可移植 | 路径配置化、`pathlib`、平台适配器和合同测试 |

## 17. 项目完成定义

项目不是以“页面能够打开”作为完成标准，而是以一个受控用户能够安全、可重复地完成“上传—排队—重建—评估—查看—下载—清理”的全流程为完成标准。算法版本、输入、配置、评估规则和产物必须可以追溯；失败必须可分类；用户数据必须隔离；第三方来源和非商业边界必须可见。
