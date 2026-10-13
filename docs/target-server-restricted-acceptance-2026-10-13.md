# 目标服务器 restricted 四服务验收（2026-10-13）

## 结论

Re3D Platform 提交 `a202d77274b7aab0ed6525f0903ef973c0a193b7`
已在目标 Windows GPU 服务器完成回环受限部署基线验收。Mailpit、API、
持续真实 Worker 和 Caddy 以四个独立 Windows 虚拟服务账户运行，启动类型均为
`Manual`；API、网页代理、Mailpit、SMTP、回环监听边界、秘密与数据 ACL、
Worker 当前进程的启动预检和队列循环证据均通过。

该结果证明四服务部署基线可以在当前主机持续运行，不代表已完成一次由网页提交的
真实重建任务，也不证明主机重启恢复、故障恢复、300 图容量或公网发布条件已经通过。

## 发布身份与回滚证据

| 项目 | 验收值 |
|---|---|
| 平台提交 | `a202d77274b7aab0ed6525f0903ef973c0a193b7` |
| 升级前提交 | `1d136cc64ce76f2540eefc498514a57e11194466` |
| Git bundle SHA-256 | `4B18CB15C63D9C375FCC5C7F9F22C40990777A069CE255757F8C6B352C432303` |
| Web ZIP SHA-256 | `5738D1776CF55184AA0CFC090132CAC591911140C0D09957D133E39AA496D346` |
| 服务清单 schema | `1.1` |
| 验收报告 | `C:\ProgramData\Re3DRestricted\acceptance-a202d77.json` |
| 验收报告 SHA-256 | `74CC25B35FA5AA75A7EFE63098EEDA0F0A0201598C6D9317B8CE785D57BE586C` |
| 旧运行包归档 | `C:\ProgramData\Re3DRestricted-archive-1d136cc-20261013T021922Z` |
| 旧网页校验副本 | `D:\Re3D-data\_deployment\web-backup-1d136cc-before-a202d77` |
| 原子替换网页回滚目录 | `D:\Re3D-data\_deployment\web-replaced-1d136cc-by-a202d77` |
| 运行根 ACL 备份 | `C:\ProgramData\Re3D-AclBackups\re3d-runtime-root-acl-backup-a202d77-20261013T022018Z.json` |

升级前只读预检确认仓库、网页和旧运行包均为 `1d136cc`，四个服务处于
`Manual/Stopped/PID 0`，受限端口无监听，数据库活动任务数为 0，两个上传制品的
服务器端 SHA-256 与本机一致。旧服务卸载后，服务、相关进程和受限端口均为 0；
旧运行包的两份私有环境文件在归档前后哈希不变，ACL 仅允许 SYSTEM 和
Administrators。

## 服务与边界验收

正式验收从 `2026-10-13T02:22:45.2334276Z` 开始，报告检查时间为
`2026-10-13T02:23:04.0725204Z`。验收覆盖：

- 四个服务均为 `Running`、`Manual`，并使用各自的
  `NT SERVICE\Re3DRestricted*` 虚拟账户；
- `127.0.0.1` / `::1` 上的 1025、8025、8000、8080 四类监听均由预期服务
  的子进程持有；
- API 直接健康检查、Caddy 同源代理、React 入口、Mailpit UI 和 SMTP 连通性通过；
- API/Worker 私密环境、数据根、Worker 临时目录、网页目录和 Mailpit 状态目录的
  服务账户边界通过；
- Worker 当前 Python 进程与固定解释器镜像、精确命令行和服务进程树一致；
- Worker 当前进程产生的新鲜 `worker-runtime-preflight=ready` 与
  `real-worker-loop=started` JSON 证据顺序正确，且进程在取证后保持稳定。

验收后又执行一次独立只读复核。复核确认 Git 工作树干净，平台、Web 和运行清单
提交均为 `a202d77`，Web 有 3 个构建资源，四个服务 PID 未变化，API 与 Worker
进程身份仍有效，四个回环监听持续存在，数据库活动任务为 0。运行根由
Administrators 持有并关闭继承，41 个当时存在的运行目录项通过无宽泛写权限检查；
ACL 备份文件只允许 SYSTEM 和 Administrators，私有环境文件哈希未变化。

## 运行边界与下一步

服务采用 `Manual` 是有意设计：云实例重启或节省停机恢复后不会自动领取 GPU
任务，操作者必须重新运行 `start-service-bundle.ps1` 并复核状态。当前服务只监听
服务器回环地址，本机浏览器必须通过 SSH 本地端口转发访问，不能开放服务器
8000/8080/8025 安全组入站规则。

下一轮验收按以下顺序进行：

1. 建立 SSH 回环隧道，从网页注册并验证测试账号；
2. 使用代表性小图片集提交一次 real 任务，确认 PostgreSQL 租约、受限 Worker
   服务、Re3D 三分支、自动评估、Three.js 预览和授权下载完整闭环；
3. 在无活动任务时验证服务停止/启动、云主机重启和可控故障恢复；
4. 扩容数据盘或明确清理旧结果后，再进行 200、250、300 图阶梯测试并校准
   64 GiB 暂定任务预留。
