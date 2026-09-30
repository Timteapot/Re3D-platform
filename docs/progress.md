# 当前开发进度

更新时间：2026-09-30

## 总体状态

阶段 4 的首轮本机真实 GPU 验收和 Windows 单机生产拓扑非公网验收已经完成，当前进入部署工程增量阶段。尚未进入公开部署阶段。

## 已完成

- [x] Re3D 管线冻结为 `re3d-pipeline-v1.0.0`；
- [x] 发布 `re3d-pipeline-v1.1.0` 隔离运行目录接口，原 v1.0.0 标签保持不变；
- [x] 记录 Re3D Git SHA、配置 SHA-256 和模型清单 SHA-256；
- [x] 初始化并推送 `Re3D-platform` 仓库；
- [x] 固定首期 A-v4、B-v2、C 三分支策略；
- [x] 固定按任务组织数据和失败任务文件清理规则；
- [x] 定义 pipeline request/event/result 和 evaluation v1 JSON Schema；
- [x] 添加契约示例、负向校验和跨文件一致性测试。
- [x] 实现 Windows Worker 配置、任务目录边界和事件日志；
- [x] 实现 A-v4、B-v2、C 三分支模拟执行和最小合法 GLB；
- [x] 实现结果原子写入、重复执行复用和检查点恢复测试。
- [x] 实现真实 Re3D 安装身份校验、隔离命令构建和 dry-run 步骤映射；
- [x] 使用真实 Re3D 入口和三张有效图片完成本机集成 dry-run。
- [x] 建立 PostgreSQL 任务状态机、Alembic 首次迁移和固定 `gpu:0` 资源槽；
- [x] 实现原子领取、优先级排序、租约续期、过期接管、取消标记和 fencing token；
- [x] 实现 Worker 后台心跳控制器，并使用 PostgreSQL 18 验证并发领取和接管。
- [x] 创建本机 `re3d_platform_dev` 和受限角色 `re3d_app`，以应用角色完成首次迁移；
- [x] 强制 PostgreSQL 集成测试仅使用名称以 `_test` 结尾的临时 Docker 数据库。
- [x] 创建项目专用 Python 3.12 `.venv`，固定 API、Worker、迁移和测试依赖；
- [x] 实现仅在 development/test 环境注册的模拟任务创建、查询和取消 API；
- [x] 将数据库租约接入模拟 Worker，按 `execution_mode` 隔离模拟与真实任务；
- [x] 完成 API → PostgreSQL → Worker → A-v4/B-v2/C GLB → API 终态查询闭环；
- [x] 为模拟任务生成契约有效、明确不提供真实质量分数的结构健康报告；
- [x] 使用临时 Docker PostgreSQL 18 验证上述完整闭环，测试结束后自动删除容器。
- [x] 新增 `users`、`refresh_sessions` 及任务所有者外键迁移；
- [x] 使用 Argon2id 保存密码哈希，未知用户登录执行 dummy 哈希校验；
- [x] 实现注册、登录、刷新、退出和当前用户 API；
- [x] 实现短期 JWT access token、HttpOnly refresh Cookie、轮换和重放族撤销；
- [x] 将开发任务接口改为 Bearer 认证，不再接受客户端声明的用户 UUID；
- [x] 在临时 PostgreSQL 18 验证 `0002` 升级、降级和重新升级。
- [x] 初始化 React、TypeScript、Vite、React Router 和 TanStack Query 前端；
- [x] 实现主页、项目说明、注册、登录和受保护的重建工作台骨架；
- [x] 实现 access token 内存保存、HttpOnly Cookie 会话恢复、退出和刷新请求合并；
- [x] 完成前端类型检查、单元测试、生产构建、依赖安全审计和浏览器布局检查。
- [x] 新增上传会话和图片元数据表，未提交输入与 GPU 队列隔离；
- [x] 实现受认证保护的 JPEG/PNG multipart 上传、解码、像素、容量和重复哈希校验；
- [x] 提交时重新验证目录与哈希，生成不可变 manifest/request 并原子创建模拟任务；
- [x] 将 React 工作台接入图片选择、上传进度、任务提交和最近任务轮询。
- [x] 实现未提交图片单张删除、上传会话取消和取消审计；
- [x] 实现可配置的上传超时回收命令，以及失败目录清理重试；
- [x] 将工作台改为可审阅的分批上传流程，并接入删图和取消操作。
- [x] 实现任务详情页、任务取消、带认证 SSE 状态流和轮询降级。
- [x] 实现显式 real 任务提交，并与默认 simulated 消费者隔离。
- [x] 实现受数据库租约保护的真实 Re3D 进程控制器、总超时和进程树终止。
- [x] 限制真实子进程环境变量，避免继承数据库、JWT 和 SMTP 凭据。
- [x] 实现真实三分支 GLB/OBJ/MTL/纹理哈希归档和 pipeline result。
- [x] 实现基于 SfM、深度保留率、网格和产物完整性的首版真实结构健康评估。
- [x] 使用 11 张真实 JPEG 完成 API → 临时 PostgreSQL → real Worker → Re3D A-v4/B-v2/C → 自动评估 → API 终态的本机 GPU 验收；三分支产物与结构健康评估均通过。
- [x] 为 Re3D Git 身份检查限定并显式信任已配置根目录，避免提升权限环境中的 Git safe-directory 误判。
- [x] 为 JWT 校验增加 5 秒有界时钟偏差容忍，覆盖 Windows 主机与 Docker PostgreSQL 亚秒级时钟差。
- [x] 实现基于任务所有权、成功终态、结果契约、固定产物类型和 SHA-256 复核的产物下载 API；
- [x] 在任务详情页列出并下载 A-v4、B-v2、C 的 GLB/OBJ/MTL/纹理，令牌只通过 Authorization 请求头发送。
- [x] 实现按需加载的 Three.js GLB 查看器、A-v4/B-v2/C 分支切换、自动取景和轨道控制；
- [x] 复用带认证的产物 Fetch 加载模型，并覆盖空场景、加载失败、WebGL 不可用和 GPU 资源释放边界。
- [x] 将任务列表、详情、取消、SSE 和产物读取迁移到各环境注册的 `/api/v1/jobs` 稳定路由；
- [x] 将开发旧路径保留为不进入 OpenAPI 的兼容入口，并验证 production 不注册模拟任务创建和旧路径。
- [x] 在 production 注册稳定上传接口，由服务端默认且仅允许创建 real 任务，并验证客户端不能提交 simulated 任务。
- [x] 实现数据库共享的账户/IP 登录失败窗口、429 `Retry-After` 和可信代理地址解析；
- [x] 实现注册、登录、刷新、重放和退出的脱敏认证事件，并新增 `0005_auth_audit_throttle` 迁移。
- [x] 实现用户名/邮箱/IP 共享注册限流、认证事件与闲置限流桶批量清理，并新增 `0006_registration_throttle` 迁移和维护命令。
- [x] 实现邮箱验证与密码重置后端 API、一次性摘要令牌、SMTP 适配、请求限流和会话撤销，并新增 `0007_auth_action_tokens` 迁移。
- [x] 实现邮箱验证、忘记密码和重置密码页面，并在后端强制未验证账号不能上传或创建任务；
- [x] 固定并启动本地 Mailpit SMTP 捕获环境，完成注册、真实 SMTP 投递、邮箱确认、任务权限解锁和密码重置验收。
- [x] 增加安全的本机 `.env` 初始化器和无秘密就绪检查，验证受限开发库、Alembic head、Re3D 固定身份、数据目录与 SMTP；
- [x] 增加生产配置模板和按 API/Worker/单机全量区分的无秘密就绪检查，拒绝开发库、管理员数据库账户、不安全认证/邮件配置、Re3D 基线漂移和低容量数据盘；
- [x] 增加生产 PostgreSQL 管理员/迁移/运行权限拆分、迁移授权、自定义格式备份和随机临时库恢复脚本，并在一次性 PostgreSQL 18 中验证运行账户写入与数据行恢复；
- [x] 增加 Windows 生产 API/Worker 前台启动入口、持续 real 队列消费、前端静态构建元数据和 Caddy 同源反向代理配置；
- [x] 使用一次性 PostgreSQL 18 完成非公网生产拓扑验收，验证生产路由边界、SPA 回退、受保护 API 透传、分层缓存、安全响应头和回环监听；
- [x] 增加 API/Worker/Caddy 的 WinSW 服务包生成、独立无密码虚拟账户、自动启动、失败退避重启、日志轮转、最小 ACL 和安全卸载脚本，并完成无系统改动验收；
- [x] 将服务包绑定到平台 Git 提交和干净前端构建，增加目标 Windows 主机服务状态、ACL、TLS、HTTP 边界、发布身份和 Worker 队列循环的只读验收脚本；
- [x] 增加失败/取消任务目录的 Worker 周期清理、安全路径边界、失败重试和数据库审计，并新增 `0008_failed_job_storage_cleanup` 迁移；
- [x] 增加真实任务 CPU、内存、数据盘和 NVIDIA GPU 私有 JSONL 采样，完成 Windows 实机指标验证；
- [x] 增加数据库共享的单用户非终态任务数、滚动 24 小时提交数限制，创建/取消审计和 `0009_task_submission_policy` 迁移；

## 下一步

- [ ] 在目标 Windows 服务器安装服务包，并完成重启、故障恢复、GPU 权限和真实域名证书验收；
- [ ] 增加受管理员权限保护的审计查询和验证码；
- [ ] 增加存储容量配额、普通接口/IP 限流、管理员审计查询、验证码和反向代理验收；
- [ ] 将任务级资源样本接入集中监控、阈值告警和运维保留策略。

## 公开部署前仍需决定

- [ ] 成功任务原图、中间文件、日志和产物的保留期限；
- [ ] 确认生产单用户待处理任务数、每日任务数的最终取值，并设计存储配额；
- [ ] 域名、TLS、邮件发送服务和验证码策略；
- [ ] 服务器 GPU、CPU、内存、磁盘和备份方案。
