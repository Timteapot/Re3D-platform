# 第三方依赖与声明

Re3D Platform 是仅用于学习、研究和实践真实项目流程的非商业项目。非商业用途声明不会替代或放宽任何第三方许可证；每项依赖仍按其原始条款使用和分发。

本文记录平台仓库当前锁定的直接 Python 和 JavaScript 依赖。Re3D 算法、模型权重、COLMAP、OpenMVS 等资源不在本仓库分发，其详细身份和更严格的非商业边界见 [Re3D 第三方声明](https://github.com/Timteapot/Re3D/blob/main/THIRD_PARTY_NOTICES.md)。

## 直接 Python 依赖

| 依赖 | 固定版本 | 用途 | 许可证 | 上游 |
|---|---:|---|---|---|
| jsonschema | 4.23.0 | JSON Schema 契约校验 | MIT | [python-jsonschema/jsonschema](https://github.com/python-jsonschema/jsonschema) |
| psycopg / psycopg-binary | 3.2.3 | PostgreSQL 驱动 | LGPL-3.0 | [psycopg/psycopg](https://github.com/psycopg/psycopg) |
| SQLAlchemy | 2.0.34 | ORM 与事务 | MIT | [sqlalchemy/sqlalchemy](https://github.com/sqlalchemy/sqlalchemy) |
| FastAPI | 0.141.1 | HTTP API 与 OpenAPI | MIT | [fastapi/fastapi](https://github.com/fastapi/fastapi) |
| Pydantic | 2.13.5 | API 数据校验 | MIT | [pydantic/pydantic](https://github.com/pydantic/pydantic) |
| Uvicorn | 0.53.0 | ASGI 服务 | BSD-3-Clause | [encode/uvicorn](https://github.com/encode/uvicorn) |
| email-validator | 2.3.0 | 邮箱语法与规范化 | Unlicense | [JoshData/python-email-validator](https://github.com/JoshData/python-email-validator) |
| Pillow | 12.3.0 | 上传图片格式识别、解码和尺寸校验 | MIT-CMU | [python-pillow/Pillow](https://github.com/python-pillow/Pillow) |
| pwdlib | 0.3.1 | Argon2id 密码哈希封装 | MIT | [frankie567/pwdlib](https://github.com/frankie567/pwdlib) |
| PyJWT | 2.15.0 | JWT 签发与校验 | MIT | [jpadilla/pyjwt](https://github.com/jpadilla/pyjwt) |
| python-multipart | 0.0.32 | multipart 图片上传解析 | Apache-2.0 | [Kludex/python-multipart](https://github.com/Kludex/python-multipart) |
| Alembic | 1.13.3 | 数据库迁移 | MIT | [sqlalchemy/alembic](https://github.com/sqlalchemy/alembic) |
| httpx2 | 2.13.1 | 开发测试 HTTP 客户端 | BSD-3-Clause | [PyPI](https://pypi.org/project/httpx2/) |

`uvicorn[standard]`、`pwdlib[argon2]`、JSON Schema format 校验和上述包还会安装传递依赖。公开镜像或安装包发布前，应从实际锁定环境生成完整 SBOM 和许可证清单，而不能只依赖本文件的直接依赖列表。

## 直接 JavaScript 依赖与开发工具

前端完整版本和传递依赖由 `apps/web/package-lock.json` 固定。

| 依赖 | 固定版本 | 用途 | 许可证 | 上游 |
|---|---:|---|---|---|
| React / React DOM | 19.2.0 | 前端组件与浏览器渲染 | MIT | [facebook/react](https://github.com/facebook/react) |
| React Router DOM | 7.18.4 | 客户端路由和受保护页面 | MIT | [remix-run/react-router](https://github.com/remix-run/react-router) |
| TanStack Query | 5.89.0 | 后续任务请求缓存与状态管理基础 | MIT | [TanStack/query](https://github.com/TanStack/query) |
| Vite | 7.3.6 | 开发服务器和生产构建 | MIT | [vitejs/vite](https://github.com/vitejs/vite) |
| TypeScript | 5.9.2 | 静态类型检查 | Apache-2.0 | [microsoft/TypeScript](https://github.com/microsoft/TypeScript) |
| Vitest | 5.0.1 | 前端单元与组件测试 | MIT | [vitest-dev/vitest](https://github.com/vitest-dev/vitest) |
| Testing Library | 16.3.0 / 6.8.0 / 14.6.1 | React DOM 与交互测试 | MIT | [testing-library](https://github.com/testing-library) |
| jsdom | 27.0.0 | 测试用 DOM 环境 | MIT | [jsdom/jsdom](https://github.com/jsdom/jsdom) |

`@vitejs/plugin-react`、类型声明和上述 JavaScript 包的传递依赖仅用于构建或测试，也受各自许可证约束。当前锁文件经 `npm audit` 检查为零个已知漏洞；该结果只代表检查当时的审计数据库状态，必须在后续提交和发布前重新执行。

## 分发边界

- `.venv`、数据库、用户上传、模型权重和 Re3D 运行产物不进入 Git；
- 不将 Re3D 的模型或第三方二进制复制到平台 Python 包或前端静态资源；
- 构建容器或安装包时保留第三方许可证文本、版权声明和源码获取要求；
- 更换依赖版本、部署方式或项目用途时重新检查许可证；
- 第三方软件按其许可证提供，项目不额外提供适销性或特定用途保证。

最后核对日期：2026-09-24。
