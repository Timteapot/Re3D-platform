# 当前开发进度

更新时间：2026-09-24

## 总体状态

当前处于阶段 0“基线冻结与准备”。尚未进入公开部署阶段。

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

## 下一步

- [ ] 使用小型图片集执行一次真实重建集成验证；
- [ ] 将数据库租约接入模拟 Worker，完成 API → PostgreSQL → Worker 闭环；

## 公开部署前仍需决定

- [ ] 成功任务原图、中间文件、日志和产物的保留期限；
- [ ] 单用户并发数、排队任务数、每日任务数和存储配额；
- [ ] 域名、TLS、邮件发送服务和验证码策略；
- [ ] 服务器 GPU、CPU、内存、磁盘和备份方案。
