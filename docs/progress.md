# 当前开发进度

更新时间：2026-09-24

## 总体状态

阶段 0 的核心验收闭环已经完成，当前开始进入阶段 1“平台骨架”。尚未进入公开部署阶段。

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

## 下一步

- [ ] 在本机 `re3d_platform_dev` 应用 `0002_user_auth` 迁移（需本地输入 `re3d_app` 密码）；
- [ ] 实现图片上传、输入校验和任务创建 API；
- [ ] 将工作台接入任务列表、详情、状态和取消接口；
- [ ] 增加邮箱验证、密码重置、登录限流和认证审计；
- [ ] 使用小型图片集执行一次真实重建集成验证；
- [ ] 将租约 Worker 从模拟执行扩展到受控真实 Re3D 子进程。

## 公开部署前仍需决定

- [ ] 成功任务原图、中间文件、日志和产物的保留期限；
- [ ] 单用户并发数、排队任务数、每日任务数和存储配额；
- [ ] 域名、TLS、邮件发送服务和验证码策略；
- [ ] 服务器 GPU、CPU、内存、磁盘和备份方案。
