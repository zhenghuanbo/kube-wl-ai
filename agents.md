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

### ⏳ Phase 1–8（待开始）

| 阶段 | 内容 | 周期 |
| --- | --- | --- |
| Phase 1 | 身份、权限（RBAC + Data Scope）与用户/组管理 | 2.5 周 |
| Phase 2 | 工作空间、资源与配额 | 3 周 |
| Phase 3 | 容器实例端到端（SSH / 终端 / IDE） | 3 周 |
| Phase 4 | 应用程序与自定义任务 | 2 周 |
| Phase 5 | 镜像与存储 | 2 周 |
| Phase 6 | 用量中心 | 1.5 周 |
| Phase 7 | Grafana 监控集成 | 1.5 周 |
| Phase 8 | API 开放、审计与上线 | 2 周 |

> 每期必须可运行并**通过验收**后才进入下一期。

### Phase 0 遗留（有意推迟，非遗漏）

- `frontend/src/stores/auth.ts` 中的 `useDevPlaceholder()` 是**开发占位身份**，
  仅当 `import.meta.env.DEV` 时生效，Phase 1 接入真实 `/auth/me` 后删除。
- `app/workers/celery_app.py` 的 `beat_schedule` 为空，任务随各阶段接入。
- `frontend/nginx.conf` 尚未包含 `/grafana/` 反代，Phase 7 添加。
- `deploy/helm/` 与 `deploy/grafana/dashboards/` 分别属于 Phase 8 与 Phase 7。

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

- **Vite 8 / Rolldown**：`manualChunks` 只接受函数。
- **Starlette ≥1.0** 重命名了 422 常量；`app/core/errors.py` 用 `getattr` 兼容两种命名，
  新增错误类型时请复用该常量，不要直接引用 `status.HTTP_422_UNPROCESSABLE_ENTITY`。
- **structlog**：`add_logger_name` 必须搭配 `structlog.stdlib.LoggerFactory()`；
  与 `PrintLoggerFactory` 组合会抛 `AttributeError`。
- **pytest `filterwarnings = ["error"]`**：任何 DeprecationWarning 都会让测试失败，
  引入新依赖时注意其告警。
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
| 迁移 | `backend/alembic/env.py`、`./scripts/migrate.sh` |
| **权限判定（Phase 1）** | `backend/app/auth/policy.py` |
| 认证 Provider（Phase 1） | `backend/app/auth/providers/` |
| K8s 编排（Phase 2+） | `backend/app/k8s/` |
| Celery | `backend/app/workers/celery_app.py` |
| 前端主题 | `frontend/src/plugins/vuetify.ts` |
| 设计令牌 | `frontend/src/styles/tokens.scss` |
| 全局样式覆盖 | `frontend/src/styles/overrides.scss` |
| 侧栏 / 角色菜单 | `frontend/src/config/navigation.ts` |
| 路由与守卫 | `frontend/src/router/index.ts` |
| 权限组合式函数 | `frontend/src/composables/usePermissions.ts` |
| 身份状态 | `frontend/src/stores/auth.ts` |
| 通用组件 | `frontend/src/components/common/` |
| API 契约 | `frontend/openapi/schema.json`（由 `./scripts/gen-openapi.sh` 生成） |
| 旧实现参考 | `legacy/README.md` |
