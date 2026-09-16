# AGENTS.md

本文件是本仓库的**权威技术指南**，面向在此工作的 AI 编码代理与人类开发者。
**修改任何代码前请先通读本文件**，尤其是第 6 节（权限）与第 13 节（当前进度）。

---

## 1. 项目概述

`kube-wl-ai` 是面向**高校 AI 集群**的 Kubernetes GPU 资源调度与管理系统。
集群由 A100 / H100 / V100 等 GPU 服务器组成，已统一纳入 K8s 管理。

系统提供 Web 界面 + REST API，管理以下对象：

| 对象 | 说明 |
| --- | --- |
| **用户 / 用户组** | 一个实验室 = 一个用户组；用户归属于组 |
| **工作空间** | **等于一个 K8s namespace**。一个组可创建多个工作空间；**一个用户只属于一个工作空间**（无切换器） |
| **资源组** | 服务器分组，分「公共 / 专用」，配置 CPU/GPU/内存规格与最大运行时长，**授权给工作空间** |
| **容器实例** | 选镜像 + 资源组 + 规格 + 存储 → K8s Deployment/Service/Ingress/PVC，支持 SSH、终端、Jupyter、VSCode |
| **应用程序** | 带介绍信息的可复用实例形态 |
| **自定义任务** | 直接运行 shell 命令，结束即退出（K8s Job） |
| **存储** | 文件存储 / 公共数据集 / 对象存储，基于 PV/PVC |
| **镜像** | 对接 Harbor，区分个人 / 官方 / 共享 |
| **用量中心** | 统计用量（**不计费**），管理员可跨用户/组查询导出 |
| **监控中心** | **只集成**集群侧既有的 Prometheus + Grafana |

### 角色模型

| 角色 | 作用域 | 权限 |
| --- | --- | --- |
| `observer` 观察用户 | 全局 | 只读，**零写权限**，不能提交任务 |
| `user` 普通用户 | 全局 | 管理自己的实例/任务/镜像/存储 |
| `group_admin` 组管理员 | 本组 | 普通用户权限 + 建工作空间、分配用户、查本组 |
| `global_admin` 全局管理员 | 全局 | 超级管理员，含用户管理 / 用户组管理 |

---

## 2. 技术栈

### 后端 `backend/`

| 用途 | 选型 |
| --- | --- |
| Web 框架 | FastAPI ≥0.115 + Uvicorn[standard] |
| ORM / 迁移 | SQLAlchemy 2.0 (async) + asyncpg + Alembic |
| 校验 / 配置 | Pydantic v2 + pydantic-settings |
| 异步任务 | Celery 5 + Redis 7（worker + beat） |
| 认证 | ldap3（FreeIPA 优先）、authlib（OIDC 预留）、PyJWT、passlib[argon2] |
| K8s | `kubernetes` 官方 Python client |
| 可观测 | structlog、prometheus-client |
| Python | **3.13** |

### 前端 `frontend/`

| 用途 | 选型 |
| --- | --- |
| 框架 | Vue **3.5** + TypeScript + `<script setup>` |
| 组件库 | Vuetify **4**（`vite-plugin-vuetify` 按需 tree-shaking） |
| 构建 | Vite **8**（底层 Rolldown） |
| 状态 / 路由 | Pinia 4 / Vue Router 5 |
| 图表 / 终端 | ECharts 6 / `@xterm/xterm` 6 |
| 图标 | `@mdi/font` |

> ⚠️ **Vite 8 使用 Rolldown**：`build.rollupOptions.output.manualChunks` **只接受函数**，
> 对象形式会构建失败。优先使用 Rolldown 原生的 `advancedChunks`。

### 数据库

生产 PostgreSQL 16；本地开发与单元测试 SQLite（`aiosqlite`）。

---

## 3. 仓库结构

```
kube-wl-ai/
├── AGENTS.md  README.md  .gitignore  .pre-commit-config.yaml
├── .github/workflows/ci.yml
├── design/                       # 原型截图（功能/字段基线，非视觉基线）
├── docs/                         # 设计与运维文档
├── legacy/                       # 重写前旧代码，只读留档，Phase 8 删除
├── scripts/
│   ├── gen-openapi.sh            # 导出 OpenAPI → frontend/openapi/schema.json
│   └── migrate.sh                # Alembic 包装脚本
├── deploy/
│   ├── docker-compose.dev.yml
│   ├── grafana/dashboards/       # Phase 7
│   └── helm/kube-wl-ai/          # Phase 8（不含监控栈）
├── backend/
│   ├── Dockerfile  pyproject.toml  alembic.ini
│   ├── requirements.txt  requirements-dev.txt  .env.example
│   ├── alembic/                  # env.py + versions/
│   ├── app/
│   │   ├── main.py               # create_app()，应用工厂
│   │   ├── core/                 # config / logging / errors / deps
│   │   ├── db/                   # base(DeclarativeBase) / session(engine)
│   │   ├── models/               # ORM 模型（Phase 1 起）
│   │   ├── schemas/              # Pydantic 出入参
│   │   ├── services/             # 业务逻辑唯一落点
│   │   ├── auth/                 # providers / jwt / policy.py（权限判定）
│   │   ├── k8s/                  # gateway 抽象 + impl + Fake + manifests
│   │   ├── integrations/         # harbor / grafana / prometheus
│   │   ├── workers/              # celery_app + tasks
│   │   ├── api/v1/               # routers
│   │   └── tests → ../tests
│   └── tests/
└── frontend/
    ├── Dockerfile  nginx.conf  vite.config.ts  tsconfig.json  .env.example
    ├── openapi/schema.json       # 由 gen-openapi.sh 生成（纳入版本控制）
    └── src/
        ├── main.ts  App.vue
        ├── plugins/vuetify.ts    # 主题（material-vue 色板）
        ├── styles/               # tokens.scss / overrides.scss
        ├── router/index.ts       # 路由 + meta.roles 守卫
        ├── config/navigation.ts  # 侧栏单一数据源（按角色过滤）
        ├── stores/               # Pinia: auth / ui
        ├── composables/          # usePermissions()
        ├── api/                  # axios 实例（Phase 1 起）
        ├── types/                # 由 OpenAPI 生成的类型
        ├── layouts/  components/  views/
```

---

## 4. 架构总览

```
Browser (Vue3 SPA)
     │  同源：/  /api  /ws  /grafana
     ▼
nginx ──► api (FastAPI/uvicorn) ──┬─► PostgreSQL
     │                            ├─► Redis ◄── worker / beat (Celery)
     │                            ├─► Kubernetes API
     │                            ├─► FreeIPA (LDAP)
     │                            ├─► Harbor
     │                            └─► Prometheus / Grafana（既有）
```

**设计原则**

1. **API-first**：OpenAPI 是前后端唯一契约，前端类型由它生成。
2. **控制面 / 数据面分离**：API 只写数据库与 K8s 期望状态；实际收敛由 Celery 协调循环完成。
3. **声明式 + 协调循环**：所有 K8s 负载由 reconcile 任务周期性对齐，不做一次性 fire-and-forget。
4. **同源部署**：前端只使用相对路径，**禁止硬编码后端地址**。
5. **最小权限与纵深防御**：RBAC + 数据范围 + 对象归属 + K8s RBAC 四层设防。
6. **只集成不部署**：Prometheus / Grafana 由集群侧提供。

---

## 5. 数据模型

分 7 组。**Alembic 迁移是唯一建表方式，禁止 `create_all()`。**

### 5.1 身份与访问

`users` · `groups` · `user_groups(role_in_group, is_primary)` · `roles` · `user_roles(scope, group_id)` ·
`permissions(code)` · `role_permissions` · `refresh_tokens` · `api_tokens` · `ssh_keys` · `audit_logs`

### 5.2 工作空间（核心）

| 表 | 关键约束 |
| --- | --- |
| `workspaces` | `k8s_namespace` **唯一**，命名 `wl-{group}-{name}`（DNS-1123）；属于一个 `group` |
| `workspace_members` | `user_id` **唯一** → 保证「一用户一工作空间」 |
| `workspace_resource_grants` | 工作空间 ↔ 资源组授权。公共资源组对所有空间可用；专用资源组必须显式授权 |

> **不存在**「用户组 ↔ 资源组」的直接关联表。资源授权一律通过工作空间进行。

### 5.3 资源与配额

`resource_groups(kind=public|private, k8s_label_selector, network_type, max_runtime_seconds)` ·
`resource_specs(cpu_milli, memory_mb, gpu_type, gpu_count)` · `resource_group_specs` ·
`runtime_limits` · `resource_group_limits` · `compute_nodes` · `gpu_devices` ·
`quotas(scope_type=user|group|workspace, ...)`

> `gpu_type` 是**字符串枚举**（如 `v100` / `a100` / `h100`），**不是**整数。

### 5.4 存储

`storages(kind=personal_home|personal_data|group_share|dataset|object, capacity_gb, used_gb, storage_class, medium, pvc_name, k8s_namespace, status)`

### 5.5 镜像

`images(registry, project, repository, tag, digest, kind=personal|official|shared, visibility, framework, python_version, cuda_version, os_version, usage_scope, size_bytes, description, synced_at)`

### 5.6 工作负载

`app_templates` · `applications` · `instances` · `tasks` · `workload_events` · `usage_daily`

### 5.7 从旧模型继承时的修正

旧库（`legacy/backend/app/model/`）存在以下缺陷，**新模型必须避免**：
`created_by`/`user_id` 字段不一致、`__repr__` 引用不存在字段、写入非数据库列属性、
同名类覆盖导入、`GPU_type` 用整数、时间字段 `NOT NULL` 且无默认值。

---

## 6. 权限模型（务必理解）

系统**不是纯 RBAC**。三层协同：

```
第一层  功能权限 (RBAC)        「你能执行这个操作吗？」
        user → role → permission → has_permission("instance:create")

第二层  数据范围 (Data Scope)  「你能对哪些数据执行？」
        global_admin → 全部 / group_admin → 本组 / user & observer → 本人
        在 SQL 查询层强制注入 WHERE —— 不是前端过滤，也不是查出后再筛

第三层  对象与限额约束         「这一次具体能不能做？」
        对象归属 / 组归属 / 工作空间归属 / 资源组授权 / 配额与时长
```

### 判定链

```python
get_current_user                          # 解析 JWT → User
  → require("instance:create")            # 第一层，无权限 → 403
    → resolve_scope(user)                 # 第二层，得到 all | group_ids | self
      → scope_filter(stmt, model, user)   # 行级 WHERE 注入
        → ensure_object_access(user, obj) # 对象级归属校验
          → ensure_quota(ws, spec)        # 限额（与权限正交，失败返回 409）
```

### 硬性要求

1. **所有数据过滤在查询层完成**，不查出全部再在 Python 里筛。
2. **详情与操作类接口必须显式做对象级校验**（`GET /instances/{id}`、`POST /instances/{id}/stop`）。
3. **权限不足 403；配额不足 409** —— 两者语义不同，不要混用。
4. 判定逻辑集中在 `app/auth/policy.py`，**对外只暴露 `require()` / `scope_filter()` / `ensure_object_access()`**，
   以便将来替换为 Casbin / OPA 时不改调用方。
5. **前端权限只做 UX，不是安全边界**。绕过前端的请求必须同样被后端拒绝。

### 与 K8s RBAC 的区别

| 层面 | 主体 | 作用 |
| --- | --- | --- |
| 本系统 RBAC | 登录用户 | 谁能通过 API 让系统做什么 |
| K8s RBAC | 后端 ServiceAccount | 后端进程能操作集群哪些资源，限 `wl-managed=true` 命名空间 |

---

## 7. 认证

- `AuthProvider` 抽象：`authenticate()` / `fetch_groups()`。
- 实现优先级：**LDAP（FreeIPA）** → OIDC（预留）→ Local（应急/测试，生产可禁用）。
- Access JWT 15 分钟（前端内存持有）+ Refresh token 7 天，**轮换且哈希入库**，
  存于 `httpOnly + Secure + SameSite=Lax` Cookie。
- WebSocket 握手使用**短期一次性 ticket**，不复用 access token。
- 首次登录 JIT 开户；FreeIPA 组映射本地实验室组；命中约定 IPA 组者自动获得 `observer`。
- **LDAP 不可用时明确报错，绝不隐式放行。**

---

## 8. API 约定

- 前缀 `/api/v1`；健康检查在根路径（`/healthz`、`/readyz`、`/metrics`）。
- **统一错误信封**（`app/core/errors.py` 已实现）：

  ```json
  {"error": {"code": "not_found", "message": "Resource not found", "detail": {}}}
  ```

- **统一分页信封**（`app/schemas/common.py`）：`{"items": [...], "meta": {"page","page_size","total","pages"}}`
- 业务错误一律抛 `AppError` 子类，**不要直接返回 `JSONResponse` 或裸 `HTTPException`**（后者会丢掉信封）。
- 列表接口的查询参数遵循 `page` / `page_size`（≤200）。
- 变更 OpenAPI 后必须重新运行 `./scripts/gen-openapi.sh` 并提交 `frontend/openapi/schema.json`。

---

## 9. Kubernetes 编排

- 每个工作空间一个 namespace，标签 `wl-managed=true`；配套 `ResourceQuota` + `LimitRange` +
  默认拒绝 `NetworkPolicy`。
- **实例** → `Deployment(replicas=1)` + `Service(ClusterIP)` + 可选 `Ingress(/x/{id}/)`。
- **任务** → `batch/v1 Job`（`backoffLimit=0`、`ttlSecondsAfterFinished`）。
- **GPU** → `resources.limits["nvidia.com/gpu"]`（整卡），`nodeSelector` 走资源组与加速卡型号标签 + tolerations。
- **SSH（唯一方案）**：实例内运行 `sshd` + `Service type=NodePort`；用户公钥存 `ssh_keys`，
  以 Secret 挂载为 `authorized_keys`。
- **浏览器终端**：`WS /ws/terminal/{id}` → K8s `exec`。
- **IDE**：`ide_type=none|jupyter|vscode|webssh`，经 Ingress `/x/{id}/` 暴露，后端签发短时票据。
- **协调**：beat 周期 reconcile（指数退避）+ K8s watcher → Redis pub/sub → SSE 推送。
- **命令注入防护**：Job 命令必须用 `command` / `args` 数组，**禁止 shell 字符串拼接**。
- **测试隔离**：单元测试一律使用 `K8sGateway` 的 Fake 实现，不接触真实集群。

---

## 10. 外部集成

| 服务 | 方式 |
| --- | --- |
| **FreeIPA** | LDAPS，`uid={username},cn=users,cn=accounts,{base_dn}` |
| **Harbor** | API v2.0；按组建 project 与只读 robot 账号；在 namespace 生成 `imagePullSecret` |
| **Prometheus** | 既有实例，用于用量聚合（GPU 秒 / CPU 核秒 / 内存 GB 秒） |
| **Grafana** | 既有实例；开启 `GF_AUTH_JWT_ENABLED`，JWKS 指向后端 `/.well-known/jwks.json`；后端签发 5 分钟 JWT 并**强制锁定** `var-namespace` / `var-pod` |

**用量口径**：底层按 **GPU 秒** 累计，展示折算为**卡时（GPU·h）**，**不做计费**。

**Grafana 隔离三重保险**：① 目录 `AI-Admin` / `AI-User`；② 后端代签 URL 时锁定变量；
③ `/monitor/*` 接口同样走 `scope_filter`。未配置 Grafana 时降级提示，不影响其他模块。

---

## 11. 前端约定

### 11.1 设计系统源自 material-vue

视觉语言来自 `legacy/frontend/src/styles/_variables.scss`（flatlogic/material-vue），
已完整移植到 `src/styles/tokens.scss`：

| 令牌 | 值 |
| --- | --- |
| primary / secondary | `#536DFE` / `#FF5C93` |
| error / warning / success / info | `#FF4081` / `#FFC260` / `#3CD4A0` / `#9013FE` |
| body 背景 / 侧栏悬停 | `#F6F7FF` / `#F3F5FF` |
| 正文 / 次要文字 / 卡片标题 | `#6e6e6e` / `#b9b9b9` / `#4a4a4a` |
| 卡片阴影 | `0 3px 11px 0 #E8EAFC, 0 3px 3px -2px #B2B2B21A, 0 1px 8px 0 #9A9A9A1A` |
| 内容内边距 / 顶栏 / 侧栏 | 24px / 64px / 240px（展开）· 96px（收起） |

**样式实现方式**：Vuetify 4 通过 CSS 变量主题化，所以用 `plugins/vuetify.ts`（色板）
+ `styles/overrides.scss`（卡片阴影、侧栏底色、标题层级）。
**不要**沿用 Vuetify 2 的 SASS 变量覆盖写法。

**内容分工**：`design/` 原型只提供**功能、字段、信息架构**；**视觉一律以 material-vue 为准**。
原型中的黄色高亮、重边框表头、密集文字链操作**不采纳**。

### 11.2 导航与权限

- 侧栏由 `src/config/navigation.ts` **单一数据源**生成，按当前用户角色过滤，并自动丢弃空分组。
- 路由通过 `meta.roles` 与 `meta.title` 声明；守卫与侧栏使用同一份角色数组。
- 组件内使用 `usePermissions()` 做菜单/按钮级判断。
- **禁止**在页面里散落硬编码的角色字符串数组。

### 11.3 其他约定

- 所有 API 调用走统一的 axios 实例（Phase 1 落地），负责注入 token、自动刷新、统一错误提示。
- 组件用 `<script setup lang="ts">`；样式用 `<style scoped lang="scss">` 并 `@use '@/styles/tokens' as *`。
- 状态用 `<StatusChip>`（`src/components/common/StatusChip.vue`）渲染，保证全站语义色一致。
- 通用组件：`PageHeader` / `StatusChip` / `EmptyState` / `FilterBar` / `ConfirmDialog` / `TabTable`。
- **禁止**引入 Vuetify 以外的组件库。

---

## 12. 开发流程与质量门禁

```bash
# 后端
cd backend
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy app          # strict 模式
.venv/bin/python -m pytest -q

# 前端
cd frontend
npm run typecheck           # vue-tsc
npm run build

# 契约
./scripts/gen-openapi.sh    # 改动 API 后必须重新生成并提交

# 迁移
./scripts/migrate.sh revision --autogenerate -m "描述"
./scripts/migrate.sh upgrade head
```

CI（`.github/workflows/ci.yml`）对以上全部内容做门禁，并额外验证两个 Docker 镜像可构建。
**任何一项红灯都不得合并。**

### 编码规范

- 标识符英文，注释与提交信息可用中文。
- 后端：业务逻辑一律放 `app/services/`，API 层只做校验与调用。
- 后端：所有 ORM 模型必须导入到 `app/models/__init__.py`，否则 Alembic autogenerate 会漏表。
- 时间字段一律带时区（`DateTime(timezone=True)`）。
- **禁止提交** `.env`、kubeconfig、任何 token/私钥/密码；`.gitignore` 与 pre-commit 已覆盖常见形态。

---

## 13. 当前进度

### ✅ Phase 0 — 工程基础（已完成）

| 交付 | 说明 |
| --- | --- |
| 版本控制 | `git init` + 根 `.gitignore` + `.pre-commit-config.yaml` |
| 旧代码归档 | `backend/`、`frontend/` → `legacy/`；已清除明文 kubeconfig 与 `SECRET_KEY` |
| 后端骨架 | `create_app()` 工厂、pydantic-settings 配置、structlog、统一错误信封、`/healthz` `/readyz` `/metrics`、Alembic(async)、ruff/mypy(strict)/pytest |
| 前端骨架 | Vite 8 + Vue 3.5 + Vuetify 4 + Pinia 4 + Router 5 + TS；material-vue 令牌与布局；6 个通用组件；按角色过滤的侧栏；全部路由可达（无 404 死链） |
| 容器化 | `deploy/docker-compose.dev.yml` + 前后端 Dockerfile + nginx 同源反代 |
| CI | lint / types / tests / 迁移可用性 / 双镜像构建 |
| 脚本 | `gen-openapi.sh`（已产出 `frontend/openapi/schema.json`）、`migrate.sh` |

**Phase 0 验收结果**：后端 `/healthz` 200、`/readyz` 200（含 DB 检查）、`/docs` 200、
OpenAPI 正常；`ruff` + `mypy --strict` + `pytest` 全绿；前端 `vue-tsc` 无错误、生产构建成功、
dev server 各模块编译通过、设计令牌已进入产物 CSS。

### ✅ Phase 1 — 身份、权限与用户/组管理（已完成）

| 交付 | 说明 |
| --- | --- |
| 认证 | `AuthProvider` 可插拔；LDAP/FreeIPA 主认证、OIDC 预留、本地应急账户；JIT 开户、目录组映射、刷新令牌轮换与重放检测 |
| 权限 | 三层模型（RBAC → Data Scope → 对象/配额），统一收口在 `app/auth/policy.py` |
| API | `/auth`、`/users`、`/groups`、`/roles`、`/permissions`，共 28 个端点 |
| 审计 | 纯 ASGI 中间件记录全部写操作、403 与 `/auth/*` 的 401/429，服务层可写领域级明细 |
| 运维 | `python -m app.cli bootstrap-admin` / `sync-rbac`：首个账户与目录同步 |
| 前端 | 真实登录、内存级 access token + 静默刷新（并发去重）、统一错误提示、用户管理页、用户组管理页 |
| 测试 | 122 项通过，含角色 × 端点 × 数据范围验收矩阵与越权用例 |

**Phase 1 验收结果**：
- 质量门禁：`ruff` + `mypy --strict`（58 文件）+ `pytest`（122 项）全绿；前端 `vue-tsc` 无错误、生产构建成功。
- 真实进程联调：`scripts/smoke_phase1.py` 对 uvicorn 实例执行 30 项检查全部通过
  （登录、组与用户 CRUD、组角色作用域、越权拦截、自我保护、账户停用、审计落库）。
- 权限模型实测：组管理员解析出 42 项权限、数据范围 `group`；跨租户读取返回 404 而非 403；
  组管理员建用户 / 建组 / 自行提权均被拒；所有者撤销自己的 `global_admin` 被拒。
- 刷新令牌链路：`wl_refresh` 为 httpOnly、path `/api/v1/auth`；刷新后 `/auth/me` 正常；令牌仅存哈希。

### ✅ Phase 2 — 工作空间、资源与配额（已完成）

| 交付 | 说明 |
| --- | --- |
| 数据模型 | `workspaces` / `workspace_members` / `workspace_resource_grants` / `resource_groups` / `resource_specs` / `resource_group_specs` / `runtime_limits` / `resource_group_limits` / `compute_nodes` / `gpu_devices` / `quotas`，迁移 `4392f95350e1` |
| 租户边界 | 工作空间 ≡ K8s 命名空间 `wl-{group}-{name}`；命名冲突时截断至 63 字符并附 8 位哈希 |
| K8s 下发 | `ensure_namespace` / `apply_resource_quota` / `apply_limit_range` / `apply_network_policy`，幂等，可由 `/reconcile` 重放 |
| 配额 | 由已授权资源组的 `ResourceGroupLimit` 汇总；GPU 按型号取各池**最大**规格（规格是互斥选项，求和会虚增额度） |
| 一人一空间 | `workspace_members.user_id` **单列唯一**，数据库层兜底；迁移需显式 `moveFromExisting` |
| 资源可见性 | 目录（资源组/规格/时长/节点）对全体登录用户可读，**授权**才是边界 |
| API | `/workspaces`（含 `/mine`、`/reconcile`、成员、资源授权）、`/resource-groups`、`/resource-specs`、`/runtime-limits`、`/nodes`、`/quotas` |
| 测试 | 新增 61 项（36 项 API 验收 + 25 项命名/数量解析单测），全量 **183 项通过** |
| 前端 | 我的工作空间（配额进度、网络档位、可用资源池）、工作空间管理（建/停/删、成员、资源授权、重新下发）、资源与配额（资源池/规格/时长/节点四个页签） |

**Phase 2 验收结果**：
- 质量门禁：`ruff check` + `ruff format --check` + `mypy --strict`（70 文件）+ `pytest`（183 项）全绿；
  前端 `vue-tsc --noEmit` 无错误、`vite build` 成功。
- 真实进程联调：`scripts/smoke_phase2.py` 对 uvicorn 实例执行 **33 项检查全部通过**
  （资源池与规格 CRUD、命名空间生成、工作空间重名拦截、一人一空间需显式迁移、
  普通用户越权返回 404、配额汇总、被占用资源池拒删、重新下发幂等、删除后从列表消失但记录保留）。
  服务端未开启集群（`K8S_ENABLED` 未设），K8s 网关回退到 `FakeK8sGateway` —— 这些检查必须在无集群的机器上跑通。
  （注意：回退条件在 Phase 3 已收紧，见 §13「本轮修复的既有缺陷」第 1 条。）
- 前端联调：Vite dev server 下三个新页面与三个新模块均编译为 200，`/api` 代理登录返回 200。
- 下发实测（`FakeK8sGateway`）：建空间后命名空间标签含 `wl-managed=true` / `wl-group` / `wl-workspace`；
  ResourceQuota 为 `requests.cpu=8`、`requests.memory=16384Mi`、`count/deployments.apps=2`；
  LimitRange 默认 4 核；NetworkPolicy 取所授权池中**最严格**的网络档位。
- 隔离实测：跨租户读取工作空间返回 404；组管理员列表仅含本组空间；普通用户仅见自己的空间；
  组管理员不能建资源组、不能删工作空间、不能改配额（均 403）。
- 失败可见性：集群下发失败时工作空间以 `error` 状态**持久化**（刻意在请求内提交），
  可被列表看到并经 `/reconcile` 重试，而不是随事务回滚消失。
- 无授权即封禁：未授权任何资源组的工作空间，其 ResourceQuota 为 `pods: 0`——
  缺失配额会被 K8s 解读为「无限」，因此必须显式封零。

**本轮修复的既有缺陷**：
1. `pydantic` 的 `to_camel` 把 `k8s_namespace` 转成 `k8SNamespace`。已改为自定义生成器，
   线格式统一为 `k8sNamespace`（仅影响本期新增的 `k8s_*` 字段，Phase 1 契约不变）。
2. `ensure_object_access` 未纳入工作空间成员维度：普通用户能列出自己的空间却按 id 取不到（404）。
   已为 `DataScope` 增加 `workspace_ids`（由 `User.workspace_memberships` 预加载），
   对象级与列表级规则自此一致。
3. `K8sError` 继承 `RuntimeError`，集群故障对外表现为不透明的 500。已改为继承 `KubernetesError`，
   返回 502 `kubernetes_error` 并携带操作名。
4. 默认列表未过滤 `is_active`：已删除的空间仍出现在列表中。现在默认只列 `is_active=true`。

### ✅ Phase 3 — 容器实例端到端（SSH / 终端 / IDE，已完成）

| 交付 | 说明 |
| --- | --- |
| 数据模型 | `instances`，迁移 `64a763f3755e`。资源形态**快照**在行上（改规格不影响已建实例）；`gpu_type` 为 `VARCHAR(32)`；`expires_at` 可空表示「无期限」；`UNIQUE(workspace_id, name)` |
| 命名 | 实例对象名 `wl-{name}-{id}`，超长截断并附哈希；`k8s_ssh_service` 与 `k8s_ide_service` **分列**——哪个是 NodePort 是安全事实，且逗号拼接会撑爆 `VARCHAR(63)` |
| 对象集 | Deployment（`Recreate` 策略，避免两份开发环境争抢同一个 home 卷）+ **两个 Service**（仅 SSH 为 NodePort，IDE 恒为 ClusterIP）+ Ingress `/x/{id}/` + PVC + `authorized_keys` Secret |
| SSH | 用户自助登记公钥，指纹按 OpenSSH 格式（`SHA256:` + SHA-256(blob)）本地计算，不依赖 `ssh-keygen`；未登记则**不建 Secret 也不挂卷**，容器内 sshd 不接受任何密钥 |
| 终端 | `WS /ws/terminal/{id}`：二进制帧＝按键，文本帧＝控制（`resize`）。按类型而非内容分帧——嗅探 JSON 会在用户粘贴 JSON 时误判。容器侧阻塞套接字由工作线程驱动 |
| IDE 票据 | `GET /instances/{id}/ide-access` 签发 `typ=ide` 的短时 JWT，锁定 `instance` 与 `namespace`；与访问令牌**类型互斥**，无法互相冒用 |
| 配额 | 独立内核：实例数 / CPU / 内存 / 存储 / GPU(按型号) + 资源组公平上限（并发数、单实例卡数）。停止**不释放**配额——否则重启会因自己占用的容量而失败 |
| 生命周期 | create / start / stop / delete / reconcile / logs（含 `previous=true` 看崩溃前日志）；`expire_overdue` 在服务端强制超时，UI 关掉也拦得住 |
| API | `/instances`（9 个端点）+ `/ssh-keys`（3 个），OpenAPI 由 49 增至 **58** 个路径 |
| 前端 | 实例页（列表/筛选/创建，管理员多出工作空间与归属人列）、详情抽屉（概览/连接/终端）、xterm.js 终端组件、个人设置页（SSH 公钥自助管理）。终端按需加载，xterm 独立分包 |
| 测试 | 新增 40 项，全量 **223 项通过** |

**Phase 3 验收结果**：
- 质量门禁：`ruff check` + `ruff format --check` + `mypy --strict`（77 文件）+ `pytest`（223 项）全绿；
  前端 `vue-tsc --noEmit` 无错误、`vite build` 成功（实例页 24.5 kB，xterm 独立 336 kB 分包按需加载）。
- 真实进程联调：`scripts/smoke_phase3.py` 对 uvicorn 实例执行 **49 项检查全部通过**
  （资源组授权、配额累加、SSH 公钥与指纹、实例 CRUD 与启停、重新下发、日志、IDE 票据、
  跨工作空间隔离、未授权资源组拦截、删除后记录保留）。
- 终端协议实测：对 `WS /ws/terminal/{id}` 做裸握手，未带 token 与非法 token 均先收到
  `{"type":"error",...}` 文本帧、再以 **1008** 关闭，不会挂起也不会 500。
- 前端联调：Vite dev server 下 5 个新模块均编译为 200，`/api` 代理登录 200。
- 对象集实测（`FakeK8sGateway`）：一次创建产出 1 Deployment / 2 Service / 1 Ingress / 1 PVC；
  Jupyter 命令含 `--ServerApp.base_url=/x/{id}/`（否则经 Ingress 后所有静态资源 404）；
  SSH Service 端口名 `ssh`、IDE 为 `ide` 且类型分别为 `NodePort` / `ClusterIP`。
- 配额实测：4 核配额下第 3 个 2 核实例返回 409 且 `detail.resource=cpu`；
  资源组公平上限单独生效（并发数满时即使 CPU 有余也 409）；停止一个实例**不**腾出配额。
- 越权实测：跨工作空间读取/停止均 404（而非 403，避免泄漏存在性）；组管理员仅见本组；
  全局管理员全见；观察者可见不可建（403）。
- 失败可见性：集群拒绝 Deployment 时返回 502 `kubernetes_error`，且实例以 `error` 状态**持久化**，
  可在列表中看到 `statusMessage`，避免遗留无人认领的对象。
- 迁移往返：`upgrade → downgrade base → upgrade` 通过，24 张表。
- 集群零污染：全部测试与冒烟跑完后 `kubectl get ns -l wl-managed=true` 为空。

**本轮修复的既有缺陷（含一次真实事故）**：

1. **`get_gateway()` 把「存在 kubeconfig」当成「可以下发」** —— 本机 `~/.kube/config` 指向真实
   `dev` 集群，导致 Phase 2 冒烟与 Phase 3 测试在真集群上创建了 3 个命名空间
   （`wl-lab-a-alpha` / `wl-lab-b-beta` / `wl-smoke-lab-smoke-ws-2`）。已全部删除，并加装两道闸：
   - 新增 `K8S_ENABLED`（默认 `false`），只有 `K8S_IN_CLUSTER` 或 `K8S_ENABLED` 为真才构造真实网关；
     仅有 kubeconfig 一律回退 `FakeK8sGateway`。回归测试：`test_a_kubeconfig_alone_does_not_enable_a_real_cluster`。
   - `tests/conftest.py` 的 `gateway` fixture 改为 **autouse**：此前「忘记显式请求」的测试会走真实网关，
     失败是静默的、破坏是真实的，安全默认必须是自动的。
2. **`Workspace.group` 是 `lazy="joined"`，新建对象永远没有这个属性** ——
   查询加载时才会连表取回，而 `Workspace(...)` 刚构造出来时该属性是未加载状态。
   于是「创建带资源授权的工作空间」在**真实进程**下必然 500 `MissingGreenlet`
   （`to_summary` 渲染 `workspace.group`）。测试没发现，是因为测试路径里该对象恰好先被查询加载过。
   修复：`apply_cluster_state` 已经把 Group 取出来了，顺手赋回 `workspace.group`。
   经验：`lazy="joined"` 的关系**不能靠惰性加载兜底**，任何会读它的路径都必须显式准备好。
3. **合法的 OpenSSH 公钥被当成非法** —— OpenSSH 会去掉 base64 的 `=` 填充，
   `b64decode(validate=True)` 对未填充输入直接抛错，于是「能否登记」取决于密钥长度。
   已按 `-len % 4` 补回填充，并对三种余数类各加一条回归测试。
4. **停止实例后配额语义与文档自相矛盾** —— 代码里 `LIVE_STATUSES` 不含 `stopped`，
   即停止即释放计算配额；而接口文档写的是「停止保留配额」。二者只能留一个：
   保留配额才是对的 —— 命名空间的 `ResourceQuota` 只统计运行中的 Pod，
   一旦释放，重启会被平台放行、然后卡在 Pending 且无从解释。现已把 `stopped` 计入占用，
   并把测试改成「填满配额 → 停一个 → 仍拒绝第三个」以真正验证该语义。
5. 路由层直接调用 `get_settings()` 而非注入 `Depends(get_settings)`，会绕过依赖覆盖拿到进程级单例。
   已全部改为 `AppSettings` 注入，否则测试与按请求配置都会被静默忽略。
6. `WorkspaceUpdate` 缺 `status` 字段：管理员能建、能删，却无法**暂停**工作空间，
   而暂停正是 `create_instance` 校验的状态。已补 `Literal["active","suspended"]`
   （其余状态由 provisioner 驱动，手写会让行与集群脱节）。
7. `/reconcile` 对**已停止**实例会经 `apply_cluster_state` 将其翻回 `creating` —— 修复一个对象不该把它悄悄启动。
   现在按状态传入 `replicas`，停止态重建为 0 副本且状态保持 `stopped`。
8. `decode_access_token` 的三条错误信息是英文（`Invalid token` 等），而全项目其余面向用户的文案都是中文。
   由 WebSocket 拒绝路径暴露。已统一为中文。

### ✅ Phase 4 — 应用程序与自定义任务（已完成）

**Phase 4 后端交付**：

| 维度 | 内容 |
| --- | --- |
| 数据模型 | `applications`（可发布的可复用实例形态 + 介绍信息）、`tasks`（K8s Job）、`workload_events`（工作负载时间线）；`resource_group_limits` 新增 `max_concurrent_tasks` |
| 迁移 | `f818f0e87ceb`，往返通过，27 张表 |
| K8s | 网关新增 `apply_job` / `read_job` / `delete_job`（`batch/v1`）；`build_task_objects()` |
| 服务 | `applications.py`（可见性、启动、编辑权）、`tasks.py`（提交/取消/删除/对齐/日志）、`workload_events.py`、`quota_guard.py`（实例与任务共用配额） |
| 接口 | `/applications`（列表/详情/创建/修改/下架/启动/时间线）、`/tasks`（列表/详情/提交/修改/取消/对齐/日志/删除/时间线）；OpenAPI **58 → 68 条路径** |
| 定时任务 | `app/workers/tasks/reconcile.py` 接入 Celery beat（30 秒一次）；`python -m app.cli reconcile` 支持手动对齐 |
| 前端 | `ApplicationsView.vue`（应用目录 / 发布 / 启动）、`TasksView.vue`（任务列表 / 提交 / 日志 / 时间线）、`types/application.ts`、`types/task.ts`、`api/applications.ts`、`api/tasks.ts` |
| 测试 | 新增 51 项，全量 **274 项通过** |

**Phase 4 验收结果**：
- 质量门禁：`ruff check` + `ruff format --check` + `mypy --strict`（91 文件）+ `pytest`（274 项）全绿；
  前端 `vue-tsc --noEmit` 与 `vite build` 通过。
- 真实进程联调（同一 uvicorn 实例，干净数据库）：
  Phase 1 **30/30**、Phase 2 **33/33**、Phase 3 **49/49**、Phase 4 **61/61**，共 173 项。
- 类型一致性：`scripts/check-frontend-types.py` 核对 39 个接口（含全部请求体）
  与 OpenAPI schema，零漂移。
- 集群零污染：跑完后 `kubectl get ns -l wl-managed=true` 为空。

**关键设计决定**：
1. **`app_templates` 不单独建表**。计划 §5.6 列出了它，但「只有管理员能写的模板」就是
   `is_curated=true` + `visibility=global` 的 application。两张几乎相同的表会各自漂移，
   而且每次列表查询都要 union。已并入 `applications`。
2. **可见性不是行级数据范围**。应用生来就是要被共享的（校级 / 组级 / 工作空间级），
   所以 applications 不走 `policy.scope_filter`，而是把规则集中写在
   `applications.visible_clause()` 一处。**越权访问返回 404 而非 403**：403 等于确认
   「另一个实验室确实有这个应用」。
3. **任务里的 shell 是刻意开的唯一口子**。平台自身的 argv 永远不做字符串插值；
   自定义任务存在的意义就是跑 shell，所以用户脚本作为
   `("/bin/sh", "-lc", <script>)` 的第三个元素传入 —— 三个固定字符串，平台不拼接任何
   用户输入，shell 在用户自己的命名空间和配额里执行。
4. **任务与实例共享同一份额度**。两者消耗同一个 namespace，受同一个 `ResourceQuota`
   约束；分开计算会各自正确而总和超限，症状是 Pod 卡在 Pending 且没有任何报错。
5. **终态任务立即释放配额**（与「停止的实例仍占用」相反）：已结束的 Job 不会再消耗任何东西。
6. **超时由 Kubernetes 执行**（`activeDeadlineSeconds`），不是平台定时器 ——
   后端宕机也不会有任务超期运行。
7. **`TIMEOUT` 与 `FAILED` 分开**：撞上自己的运行时上限，和代码返回非零，是两回事，
   处理方式也不同。

**本轮修复的既有缺陷**：

1. **Phase 3 的三处多词查询参数漏了 camelCase 别名**（`include_inactive` / `page_size` 等）。
   FastAPI 按名字匹配查询参数，**不认识的参数直接忽略、不报错**，所以
   `?includeInactive=true` 并不会 400，而是把过滤条件悄悄丢掉 —— 列表多返回数据，
   没有任何症状。Phase 1/2 的路由一直是 `Query(alias="pageSize")` 这种写法，是 Phase 3/4 写偏了。
   已全部补上，并新增结构性回归测试
   `test_every_multi_word_query_parameter_is_camel_case`：直接读 OpenAPI schema，
   任何含下划线的 query 参数都会让测试失败。
2. **任务从未记录开始时间**。`started_at` 原本只在对齐时观察到 Running 才写入，
   于是「两次巡检之间就跑完的任务」会得到 `durationSeconds: null`。
   现在在 Job 提交成功时打点 —— 用户想知道的「这个任务花了我多久」包含排队时间。
3. **`/users` 用 `groupIds`（复数）**，脚本里写成 `groupId` 时被静默忽略，用户根本没有进组。
   这是第 1 条同一个病根。

**根治：请求体不再静默丢弃未知字段**（`ApiModel` 设 `extra="forbid"`）。

前面两条都只是症状。真正的病因是 Pydantic 默认**忽略**未声明字段：拼错一个字段名，
接口返回 200、数据只写了一半，调用方和审计日志都看不出任何异常。
改成拒绝之后，一次性翻出三处一直存在的问题：

1. **`DataScopeResponse` 漏了 `workspace_ids`** —— `DataScope.to_dict()` 一直在返回它，
   Pydantic 一直在丢掉它。数据范围里「我能操作哪些工作空间」这一项从未到达前端。
   已补进 schema，并透传到前端 `usePermissions().scopeWorkspaceIds`。
2. **`PATCH /users/me` 会静默忽略 `isSuperuser`** —— 提权尝试拿到 200，什么都没发生。
   现在返回 422 并点名该字段，且**整个请求都不生效**（不做「一半照做」），
   提权尝试因此会留在审计日志里。
3. **三个冒烟脚本一直在发不存在的字段**：`displayName`（`User.display_name` 是派生属性，
   由 `full_name` 推导）、`groupId`（应为 `groupIds`）、`role`（应为 `roleCodes`）。
   它们此前「通过」是因为账号确实建成了、组关联压根没建、而 `roleCodes` 的默认值恰好
   也是 `["user"]` —— 断言的是从未发生的事。已全部修正。

### ⏳ Phase 5–8（待开始）

| 阶段 | 内容 | 周期 |
| --- | --- | --- |
| Phase 5 | 镜像与存储 | 1.5 周 |
| Phase 6 | 用量中心 | 1.5 周 |
| Phase 7 | Grafana 监控集成 | 1.5 周 |
| Phase 8 | API 开放、审计与上线 | 2 周 |

> 每期必须可运行并**通过验收**后才进入下一期。

### 有意推迟（非遗漏）

- **OIDC** 仅预留扩展点：`OidcProvider.exchange_code()` 明确抛错说明。大学的统一身份是
  FreeIPA，接入 OIDC 需要校方先提供客户端注册；在拿到之前不写不可验证的代码。
- `app/workers/celery_app.py` 的 `beat_schedule` 为空，任务随各阶段接入。
- `frontend/nginx.conf` 尚未包含 `/grafana/` 反代，Phase 7 添加。
- `deploy/helm/` 与 `deploy/grafana/dashboards/` 分别属于 Phase 8 与 Phase 7。
- `/auth/login` 的**登录限流**尚未实现，Phase 8 与审计告警一并落地。

---

## 14. 已知问题与陷阱

### 14.1 安全（最高优先级）

1. **历史凭证已泄漏**：`legacy/backend/app/common/k8s/config/*.yaml` 曾提交明文 kubeconfig token，
   `legacy/backend/config.py` 曾硬编码 `SECRET_KEY`。文件已在归档时清除，但
   **凭证本身必须在集群侧吊销并轮换**。
2. 旧 `k8sclient.py` / `test.py` 硬编码 Windows 绝对路径 `D:\vscodeProject\...`（已在 `legacy/`，勿复制）。

### 14.2 旧前端缺陷（仅供参考，不要再犯）

端口硬编码 `localhost:5000`、`Login.vue` 为死代码、路由重复定义、
Vuex `namespace: true` 拼写错误、`Admin/User.vue` 为 0 字节、
`Admin/Group.vue` 使用未安装的 Element UI、侧栏链接指向不存在的路由、无任何角色控制。
**新版前端已全部规避，回归这些模式视为缺陷。**

### 14.3 新版注意点

- **异步下访问未加载的属性会炸**：SQLAlchemy 在 UPDATE 后会过期服务端生成的列，
  关系默认惰性加载——在 async session 里再读就是 `MissingGreenlet`，而不是像同步代码那样悄悄补一次查询。
  已通过 `Base.__mapper_args__ = {"eager_defaults": True}`（INSERT/UPDATE 用 RETURNING 取回）
  与 `UserGroupLink.group` / `.user` 的 `lazy="joined"` 解决。
  **新增模型时：任何渲染时会读到的关系都要显式指定加载策略，任何 `flush()` 之后会读的字段都要确认已加载。**
- **失败的请求不会回滚审计**：刷新令牌重放要吊销整个令牌族，若与导致 401 的事务共用 session，
  回滚会把吊销一起撤销，等于没吊销。`auth/service._revoke_family()` 因此在**独立事务**中提交。
- **pydantic 的 `to_camel` 会拆数字边界**：`k8s_namespace` 会变成 `k8SNamespace`，而不是前端会写的
  `k8sNamespace`。`app/schemas/base.py` 因此自带一个生成器（首段整体保留）。
  **新增含数字的字段名时，先确认它在 `openapi/schema.json` 里的实际拼写。**
- **缺省的 K8s 配额等于「无限」**：`ResourceQuota` 里没有列出的资源不受约束。
  未授权任何资源组的工作空间必须显式下发 `pods: "0"`，否则它可以在共享集群上无限调度。
- **集群下发失败必须显式落库**：处理函数抛异常时请求事务会整体回滚，
  于是「失败的工作空间」会被撤销，而集群里半成品命名空间还在。
  `services/workspaces._persist_failure()` 因此在抛出前 `commit()`——这是该服务唯一在请求中途提交的地方，
  且是有意为之。
- **异常类型决定对外状态码**：`K8sError` 曾继承 `RuntimeError`，导致集群故障对外是 500 traceback；
  现继承 `KubernetesError`（502 `kubernetes_error`）。**新增外部依赖的异常时，让它成为 `AppError` 的子类。**
- **查询参数拼错不会报错，只会被忽略**：FastAPI 按名字匹配，多余的 query 参数既不报错
  也不进函数。多词参数一律 `Query(alias="camelCase")`，并有结构性测试兜底
  （`test_every_multi_word_query_parameter_is_camel_case`）。
- **请求体已禁止未知字段**（`ApiModel` 的 `extra="forbid"`）：拼错字段名会得到 422 并点名
  出错字段，而不是 200 + 数据只写一半。新增请求体字段时前端要同步改，否则会在联调时立刻暴露
  ——这正是想要的效果。`scripts/check-frontend-types.py` 会在提交前做这个核对。
- **前端类型是手写的，会与后端漂移**：`scripts/check-frontend-types.py` 解析 `src/types/*.ts`
  的接口（含 `extends` 继承与 `...Payload` → `...Create` 的别名映射），要求每个字段都能在
  OpenAPI schema 里找到。字段被改名时 TypeScript 不会报错，只会在运行时变成 `undefined`。
- **`lazy="joined"` 的关系不参与惰性加载**：它只在**查询加载**时连表取回，因此
  `Model(...)` 刚构造出来的对象上该属性是未加载的，任何读它的地方都会 `MissingGreenlet`。
  新增此类关系时，要么在服务层显式赋值（如 `workspace.group = group`），
  要么在读取前用一次查询加载。**测试里「恰好先查询过」会掩盖这个问题，真实进程不会。**
- **「存在 kubeconfig」≠「可以下发」**（真实事故）：本机 `~/.kube/config` 指向真实 `dev` 集群，
  早期 `get_gateway()` 见到 kubeconfig 就构造真实网关，于是**单元测试与冒烟脚本在真集群上建了 3 个命名空间**。
  现在只有 `K8S_IN_CLUSTER` / `K8S_ENABLED` 为真才连集群，且 `tests/conftest.py` 的 `gateway` fixture 是 autouse。
  **写任何会访问外部系统的网关时，默认必须是「不访问」，配置只能收窄而不能放大。**
  同理：测试里 `get_settings()` 必须走 `Depends` 注入，直接调用会绕过依赖覆盖拿到进程级单例。
- **实例停止后重建对象不得把它启动**：`apply_cluster_state` 既是创建路径也是 `/reconcile` 修复路径，
  若不带 `replicas` 就会把用户刻意停止的实例翻回 `creating`。**幂等的「修复」必须保留被修复对象的既有意图状态。**
- **一个实例不止一个 Service**：SSH 是 `NodePort`、IDE 是 `ClusterIP`（只经后端鉴权的 Ingress 进入）。
  把两个端口放进同一个 NodePort Service，等于在校内每个节点地址上发布一个**免鉴权的 IDE**。
- **审计动作名是稳定接口**：`action` 表示*尝试的操作*，`status` 表示结果。
  服务层成功路径写 `user:create` 这类域名动作，中间件兜底路径经 `canonical_resource()` 归一化，
  所以同一种操作无论成败都只有一个 action 字符串。不要把结果编进动作名（`*_failed` 已废弃）。
- **Vite 8 / Rolldown**：`manualChunks` 只接受函数。
- **Starlette ≥1.0** 重命名了 422 常量；`app/core/errors.py` 用 `getattr` 兼容两种命名，
  新增错误类型时请复用该常量，不要直接引用 `status.HTTP_422_UNPROCESSABLE_ENTITY`。
- **structlog**：`add_logger_name` 必须搭配 `structlog.stdlib.LoggerFactory()`；
  与 `PrintLoggerFactory` 组合会抛 `AttributeError`。
- **`ldap3` 2.9.1 会发 `pyasn1` 弃用告警**：在 `filterwarnings = ["error"]` 下直接 fatal。
  已通过固定 `pyasn1>=0.5.1,<0.6` 解决（不要改成抑制告警）。
- **pytest `filterwarnings = ["error"]`**：任何 DeprecationWarning 都会让测试失败，
  引入新依赖时注意其告警。
- **测试夹具改动数据后必须 `commit()`**：HTTP 层走独立连接，未提交的改动它看不到。
- **`/tmp` 在沙箱中跨进程不可靠**：冒烟测试的数据库放仓库内 `.smoke/`（已 gitignore），
  用 `scripts/smoke-env.sh` 一键准备。
- **`tsconfig.node.json`**：`composite: true` 与 `noEmit: true` 互斥，
  当前用 `emitDeclarationOnly` + `outDir` 指向 `node_modules/.tmp`。

---

## 15. 快速定位索引

| 需求 | 关键文件 |
| --- | --- |
| 应用工厂 / 中间件 / 路由挂载 | `backend/app/main.py` |
| 配置项全集 | `backend/app/core/config.py`、`backend/.env.example` |
| 错误信封与异常类型 | `backend/app/core/errors.py` |
| 日志 | `backend/app/core/logging.py` |
| DB 引擎 / 会话依赖 | `backend/app/db/session.py` |
| ORM 基类与加载策略 | `backend/app/db/base.py` |
| 迁移 | `backend/alembic/env.py`、`./scripts/migrate.sh` |
| **权限判定 / 数据范围** | `backend/app/auth/policy.py` |
| 权限码与角色目录 | `backend/app/auth/permissions.py` |
| 认证服务（开户 / 令牌轮换） | `backend/app/auth/service.py` |
| 认证 Provider | `backend/app/auth/providers/` |
| 审计写入 | `backend/app/core/audit.py`、`backend/app/middleware/audit.py` |
| 运维命令（首个账户） | `backend/app/cli.py` |
| 业务服务层 | `backend/app/services/` |
| **工作空间生命周期与集群下发** | `backend/app/services/workspaces.py` |
| 资源组 / 规格 / 节点 / 配额 | `backend/app/services/resources.py` |
| **K8s 抽象与假实现** | `backend/app/k8s/gateway.py`（`FakeK8sGateway` 供测试） |
| K8s 真实客户端 | `backend/app/k8s/client.py`（唯一 import `kubernetes` 的模块） |
| 命名空间 / 标签命名规则 | `backend/app/k8s/naming.py` |
| 工作空间与资源 API | `backend/app/api/v1/workspaces.py`、`backend/app/api/v1/resources.py` |
| Celery | `backend/app/workers/celery_app.py` |
| 前端主题 | `frontend/src/plugins/vuetify.ts` |
| 设计令牌 | `frontend/src/styles/tokens.scss` |
| 全局样式覆盖 | `frontend/src/styles/overrides.scss` |
| **HTTP 客户端（令牌 / 刷新 / 错误）** | `frontend/src/api/client.ts` |
| API 模块 | `frontend/src/api/` |
| 侧栏 / 角色菜单 | `frontend/src/config/navigation.ts` |
| 路由与守卫 | `frontend/src/router/index.ts` |
| 权限组合式函数 | `frontend/src/composables/usePermissions.ts` |
| 身份状态 | `frontend/src/stores/auth.ts` |
| 全局消息 | `frontend/src/stores/notifications.ts`、`frontend/src/components/AppSnackbar.vue` |
| 通用组件 | `frontend/src/components/common/` |
| API 契约 | `frontend/openapi/schema.json`（由 `./scripts/gen-openapi.sh` 生成） |
| 工作空间与资源前端 | `frontend/src/views/MyWorkspaceView.vue`、`frontend/src/views/admin/WorkspacesView.vue`、`frontend/src/views/admin/ResourcesView.vue` |
| 前端资源/工作空间类型与 API | `frontend/src/types/resource.ts`、`frontend/src/api/workspaces.ts`、`frontend/src/api/resources.ts` |
| **部署冒烟验收** | `scripts/smoke-env.sh` + `scripts/smoke_phase1.py` + `scripts/smoke_phase2.py` |
| 旧实现参考 | `legacy/README.md` |
