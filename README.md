# kube-wl-ai

高校 AI 集群的 Kubernetes GPU 资源调度与管理系统。

后端为 **FastAPI + SQLAlchemy 2.0 (async) + Alembic**，前端为 **Vue 3 + Vuetify 4 + Vite**，
两者以 OpenAPI 为唯一契约；系统只**集成**集群侧既有的 Prometheus / Grafana，不负责部署监控栈。

> 完整的架构设计、数据模型、权限模型与分期计划见 [`agents.md`](./agents.md)。
> 面向代码评审的目录结构与业务说明见 [`CODE_OVERVIEW.md`](./CODE_OVERVIEW.md)。
> 重写前的原始实现冻结在 [`legacy/`](./legacy/README.md)。

---

## 当前进度

**Phase 0 – 9 全部完成。** 19 个后端路由模块、19 个前端视图（已无占位页）。

| 阶段 | 内容 | 状态 | 冒烟验收 |
| --- | --- | --- | --- |
| Phase 0 | 工程基础（仓库、CI、容器化、前后端骨架、设计系统） | ✅ 完成 | — |
| Phase 1 | 身份、权限（RBAC + Data Scope）与用户/组管理 | ✅ 完成 | 33 |
| Phase 2 | 工作空间、资源与配额 | ✅ 完成 | 39 |
| Phase 3 | 容器实例端到端（SSH / 终端 / IDE） | ✅ 完成 | 49 |
| Phase 4 | 应用程序与自定义任务 | ✅ 完成 | 61 |
| Phase 5 | 镜像与存储 | ✅ 完成 | 65 |
| Phase 6 | 用量中心 | ✅ 完成 | 61 |
| Phase 7 | Grafana 监控集成 | ✅ 完成 | 43 |
| Phase 8 | API 开放、审计与上线（含 Helm Chart） | ✅ 完成 | 60 |
| Phase 9 | 收尾：系统设置页、计算资源页、移除占位页 | ✅ 完成 | 23 |

**验收状态**：`pytest` **565 passed**；冒烟脚本 **9 个阶段 434 项**在干净库上按序全通过；
`ruff` / `mypy` / `vue-tsc` / `vite build` / `helm lint` 全干净。

### 尚未完成的部分（需要集群侧配合，不是代码问题）

- **Helm Chart 从未真正 `helm install` 过。** 已过 `helm lint`、两条 RBAC 分支的渲染，
  以及针对真实 1.28 API Server 的 `kubectl apply --dry-run=server`；但 `values.yaml` 中的
  镜像仓库地址、StorageClass 名称、LDAP 服务账号口令**仍是占位值**。
- **`legacy/` 目录尚未删除**（原计划 Phase 8 删除）。它包含旧代码中泄漏的 kubeconfig 与
  硬编码 `SECRET_KEY`，删除前需**先确认集群侧凭证已吊销并轮换**。
- 待外部确认：既有 Prometheus 是否带 `namespace` / `pod` 标签、是否存在 RWX StorageClass、
  LDAP 服务账号凭证。详见 [`agents.md` §14.4](./agents.md)。

---

## 仓库结构

```
kube-wl-ai/
├── backend/          FastAPI 服务（api / worker / beat 共用镜像）
├── frontend/         Vue 3 SPA（Vite 构建，nginx 托管并反代）
├── deploy/
│   ├── docker-compose.dev.yml  开发环境一键启动
│   ├── grafana/                面板生成器 + provisioning（Phase 7）
│   └── helm/kube-wl-ai/        应用本体 Chart，不含监控栈（Phase 8）
├── design/           原型截图（功能与字段基线，非视觉基线）
├── legacy/           重写前的旧代码，只读留档（未删除，见上）
├── scripts/          OpenAPI 生成 / 迁移 / 密钥生成 / 冒烟与契约校验
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

### 从另一台机器访问前端

`npm run dev` 默认只绑 **loopback**，所以在共享机器上跑不会悄悄变成一个谁都能开的服务。
远程访问按你的网络形态选一种：

**A. SSH 端口转发（推荐）** —— 不暴露任何端口，不需要改配置，HMR 也能用：

```bash
ssh -L 5173:127.0.0.1:5173 <user>@<服务器地址>
# 然后浏览器打开 http://localhost:5173
```

**B. 同一局域网直连** —— 只暴露前端一个端口。写进 `frontend/.env.local` 即可：

```bash
VITE_DEV_HOST=0.0.0.0
VITE_DEV_PORT=5181
```

然后 `npm run dev`，浏览器打开 `http://<服务器IP>:5181`。也可以只在命令行临时指定：

```bash
VITE_DEV_HOST=0.0.0.0 npm run dev -- --port 5181
```

**C. 通过域名（隧道 / Ingress / 云开发代理）** —— 除了绑定，还要声明主机名：

```bash
VITE_DEV_HOST=0.0.0.0 VITE_ALLOWED_HOSTS=dev.example.edu npm run dev
```

裸 IP 和 `localhost` 本来就允许，**域名不在名单里会被返回 403** —— 那个 403 长得像应用坏了，
实际是 Vite 的 DNS rebinding 防护。若前面还有一层终止 TLS 的代理，热更新还需要指到公网端点：

```bash
VITE_HMR_HOST=dev.example.edu VITE_HMR_CLIENT_PORT=443 VITE_HMR_PROTOCOL=wss npm run dev
```

> 没有提供「关掉主机检查」的开关（即 `allowedHosts: true`）。那是常见的建议，但在这里是错的默认值：
> 它会让任何解析到本机的页面都能访问开发服务器，而那正是这项检查要挡的东西。

**为什么不需要配 CORS。** 前端所有 API 调用都用相对路径（`/api/v1`），由 dev server 在
**服务端**代理到后端。浏览器只跟 `http://<服务器IP>:5181` 这一个源打交道，从不发起跨域请求 ——
所以后端可以留在 loopback，也不需要往 `CORS_ORIGINS` 里加任何东西。
反过来说，如果你绕开代理、让浏览器直连 `http://<服务器IP>:8000`，那才会真的跨域，
届时必须把前端源加进后端的 `CORS_ORIGINS`。

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

**拉到新代码后先跑迁移。** 模型加了列而库没跟上时，`/readyz` 会返回 503 并说明该做什么：

```console
$ curl -s localhost:8000/readyz
{"status":"degraded","checks":{"database":"ok",
 "schema":"error: database at 3f0dbd1b4617, code expects b7c41a9e5d02; run `alembic upgrade head`"}}

$ ./scripts/migrate.sh upgrade head    # 修好它
```

不跑迁移的话，进程能启动、`/healthz` 也是 200，但第一个碰到新列的请求（比如登录）会在
ORM 里抛出 `no such column`，表现为 500 加一大段堆栈——真正的原因只是漏了一条命令。

---

## 验收

除单元测试外，项目带两套端到端验收：**后端冒烟**（对着真实运行的服务发 HTTP）
与**前端视图冒烟**（在 happy-dom 里真实挂载每个视图并断言渲染内容）。

```bash
# 在仓库根目录执行。先记下当前位置 —— smoke-env.sh 会 cd 到 backend/，
# 而下面的冒烟脚本要从仓库根目录运行。
REPO_ROOT="$(pwd)"

# 1) 重建干净的 SQLite 库、跑迁移、创建首个管理员，并导出所需环境变量
source scripts/smoke-env.sh

# 2) 启动后端（此时已在 backend/ 下；K8S_ENABLED=false 是必须的，见下方警告）
K8S_ENABLED=false uvicorn app.main:app --port 8000 &

# 3) 回到仓库根目录，依次跑九个阶段
cd "$REPO_ROOT"
for n in 1 2 3 4 5 6 7 8 9; do python3 scripts/smoke_phase$n.py || break; done

# 前端契约与视图（同样在仓库根目录）
python3 scripts/check-frontend-types.py    # 前端类型 ↔ OpenAPI schema 比对
node scripts/smoke_frontend_views.mjs      # 挂载全部视图 + 内容/反向断言
```

> ⚠️ 冒烟脚本会连真实集群，**除非显式 `K8S_ENABLED=false`**。不要指着生产 kubeconfig 跑。
> `smoke-env.sh` 只设置本进程的环境变量，所以第 2 步必须自己带上 `K8S_ENABLED=false`。
>
> Phase 1–4 的脚本使用固定夹具名称，因此**不可重复执行**（第二次会 409）；
> Phase 5–9 可重复。九者在干净库上按序执行全部通过（428 项）。
>
> Phase 7 的项数随 Grafana 配置变化：未配置时 43 项，`SMOKE_EXPECT_GRAFANA=0` 时 44 项，
> `=1` 时 53 项——因为它走的是不同的分支，而两条分支都必须被跑过。

---

## 配置

所有配置通过环境变量注入，完整清单见 `backend/.env.example` 与 `frontend/.env.example`。

> ⚠️ **绝不要提交 `.env`、kubeconfig 或任何凭证。** `.gitignore` 已覆盖常见形态；
> CI 与 pre-commit 也会拦截私钥。

关键变量：

| 变量 | 说明 |
| --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://...`（生产）/ `sqlite+aiosqlite://...`（本地、测试） |
| `SECRET_KEY` | 会话与令牌签名根密钥，**必须**在生产环境替换（不足 32 字节时生产环境拒绝启动） |
| `AUTH_PROVIDERS` | `local` / `ldap` / `oidc` 的逗号分隔列表，按顺序尝试 |
| `K8S_ENABLED` / `K8S_IN_CLUSTER` / `KUBECONFIG` | 集群访问方式；关闭后实例与任务接口不可用，元数据管理不受影响 |
| `PROMETHEUS_URL` | 既有 Prometheus 地址（可选，用量统计不依赖它） |
| `GRAFANA_PUBLIC_URL` | Grafana 的同源挂载路径，**为空即视为未接入**，监控中心只显示平台自身统计 |
| `STORAGE_CLASS_HOME` / `_DATA` / `_SHARE` | 三类持久卷的 StorageClass，留空会在用户创建数据盘时才失败 |
| `INSTANCE_SSH_HOST` | 展示给用户的 SSH 地址；留空回落到节点主机名，仅单节点集群可用 |

部署后可在 **系统设置**（`/admin/settings`，全局管理员）查看运行时配置、
各集成接入状态，以及一份**配置体检**——每条告警都指明「什么问题、什么后果、改哪个变量」。

---

## 安全须知

重写前的代码库中曾提交过**明文 kubeconfig token 与 Flask `SECRET_KEY`**。
这些文件已在 Phase 0 归档时移出主代码路径，但**凭证本身仍应在集群侧吊销并轮换**
——`legacy/` 目录至今仍保留着那份存档，删除前请先完成轮换。
详见 [`legacy/README.md`](./legacy/README.md)。

平台侧的权限模型为**三层**：RBAC（`resource:action` 码）→ 数据范围（行级 `WHERE`）
→ 对象/配额约束。前端 `usePermissions()` 的角色判断**只用于 UX**，后端始终是安全边界。

> **Harbor 机器人账户、Grafana 签名密钥、LDAP 绑定口令一律不进本仓库**，
> 生产环境走 `deploy/helm/kube-wl-ai/` 的 Secret（`scripts/helm-gen-keys.sh`
> 生成 RS256 密钥对，私钥不落盘）。
