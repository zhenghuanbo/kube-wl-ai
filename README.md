# kube-wl-ai

高校 AI 集群的 Kubernetes GPU 资源调度与管理系统。

后端为 **FastAPI + SQLAlchemy 2.0 (async) + Alembic**，前端为 **Vue 3 + Vuetify 4 + Vite**，
两者以 OpenAPI 为唯一契约；系统只**集成**集群侧既有的 Prometheus / Grafana，不负责部署监控栈。

> 完整的架构设计、数据模型、权限模型与分期计划见 [`agents.md`](./agents.md)。
> 重写前的原始实现冻结在 [`legacy/`](./legacy/README.md)。

---

## 当前进度

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| Phase 0 | 工程基础（仓库、CI、容器化、前后端骨架、设计系统） | ✅ 完成 |
| Phase 1 | 身份、权限（RBAC + Data Scope）与用户/组管理 | ⏳ 待开始 |
| Phase 2 | 工作空间、资源与配额 | ⏳ |
| Phase 3 | 容器实例端到端（SSH / 终端 / IDE） | ⏳ |
| Phase 4 | 应用程序与自定义任务 | ⏳ |
| Phase 5 | 镜像与存储 | ⏳ |
| Phase 6 | 用量中心 | ⏳ |
| Phase 7 | Grafana 监控集成 | ⏳ |
| Phase 8 | API 开放、审计与上线 | ⏳ |

---

## 仓库结构

```
kube-wl-ai/
├── backend/          FastAPI 服务（api / worker / beat 共用镜像）
├── frontend/         Vue 3 SPA（Vite 构建，nginx 托管并反代）
├── deploy/           docker-compose（开发）与 Helm Chart（生产，Phase 8）
├── design/           原型截图（功能与字段基线，非视觉基线）
├── legacy/           重写前的旧代码，只读留档，Phase 8 删除
├── scripts/          gen-openapi.sh / migrate.sh
└── .github/workflows CI
```

---

## 本地开发

### 方式一：docker compose（推荐，一条命令）

```bash
docker compose -f deploy/docker-compose.dev.yml up --build
```

| 服务 | 地址 |
| --- | --- |
| 前端 | http://localhost:8080 |
| 后端 API | http://localhost:8000 |
| API 文档 | http://localhost:8000/docs |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

### 方式二：分别启动

**后端**

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env          # 按需修改
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload --port 8000
```

**前端**

```bash
cd frontend
npm install
cp .env.example .env.local    # 按需修改
npm run dev                   # http://localhost:5173
```

开发服务器把 `/api` 与 `/ws` 代理到 `http://localhost:8000`（可用 `VITE_DEV_PROXY_TARGET` 覆盖）。
前端**不包含任何硬编码后端地址**，生产环境与开发环境都只使用相对路径。

---

## 常用命令

```bash
# 后端质量门禁
cd backend
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy app
.venv/bin/python -m pytest -q

# 前端质量门禁
cd frontend
npm run typecheck
npm run build

# 数据库迁移
./scripts/migrate.sh upgrade head
./scripts/migrate.sh revision --autogenerate -m "描述"

# 重新生成 API 契约（前端类型由此生成）
./scripts/gen-openapi.sh

# 安装 git 钩子
pre-commit install
```

---

## 配置

所有配置通过环境变量注入，完整清单见 `backend/.env.example` 与 `frontend/.env.example`。

> ⚠️ **绝不要提交 `.env`、kubeconfig 或任何凭证。** `.gitignore` 已覆盖常见形态；
> CI 与 pre-commit 也会拦截私钥。

关键变量：

| 变量 | 说明 |
| --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://...`（生产）/ `sqlite+aiosqlite://...`（本地、测试） |
| `AUTH_PROVIDERS` | `local` / `ldap` / `oidc` 的逗号分隔列表，Phase 1 生效 |
| `SECRET_KEY` | 会话与令牌签名根密钥，**必须**在生产环境替换 |
| `K8S_IN_CLUSTER` / `KUBECONFIG` | 集群访问方式，Phase 2 起生效 |
| `PROMETHEUS_URL` / `GRAFANA_URL` | 既有监控栈地址，Phase 6/7 生效 |

---

## 安全须知

重写前的代码库中曾提交过**明文 kubeconfig token 与 Flask `SECRET_KEY`**。
这些文件已在 Phase 0 归档时移除，但**凭证本身仍应在集群侧吊销并轮换**。
详见 [`legacy/README.md`](./legacy/README.md)。
