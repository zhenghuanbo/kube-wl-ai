# 代码说明（供人工审核）

> 目的：让评审者在**不读全部 3.5 万行代码**的前提下，知道这个系统在做什么、代码放在哪、
> 关键约束在哪里，以及**哪些地方值得重点看**。
>
> 另见：`README.md`（怎么跑、怎么验收）、`agents.md`（每个阶段的交付记录与设计决策）。

---

## 1. 这是什么

一个面向高校 AI 集群的 **Kubernetes GPU 调度与管理系统**。它把「实验室 / 学生 / 显卡」
这组现实关系，映射成 K8s 的命名空间、ResourceQuota、Deployment/Job，并对外提供 Web 界面。

一句话概括业务闭环：

```
管理员建房(资源组/资源规格) → 建组(实验室) → 建空间(工作空间=命名空间，自动配额度与网络策略)
    → 用户拿容器实例 / 提交任务(自动带配额校验与 GPU 分配) → 用量汇总 → 审计留痕
```

**核心设计前提（多数设计争议都可以回到这几条）：**

| 前提 | 含义 |
|---|---|
| 工作空间 = K8s 命名空间 | 一个组可以有多个空间；空间是配额、网络、成员的最小隔离单位 |
| 一个用户只属于一个工作空间 | `workspace_members.user_id` 是 **UNIQUE**，前端**没有空间切换器** |
| 资源组绑定工作空间 | `PUBLIC` 全员可用，`PRIVATE` 必须显式授权 —— 这是租户与硬件池之间**唯一**的绑定 |
| 平台不信任自己的元数据 | 集群侧才是事实来源，本地表是缓存，由 reconcile 循环校正 |
| 权限三层合一 | RBAC（能不能做）→ 数据范围（能对谁做）→ 对象/配额约束（做到什么程度） |

---

## 2. 技术栈

| 层 | 选型 |
|---|---|
| 后端 | FastAPI ≥0.115 + Uvicorn；SQLAlchemy 2.0 async + asyncpg + Alembic；Pydantic v2 |
| 异步 | Celery 5 + Redis 7（另有两个周期任务） |
| 认证 | ldap3（FreeIPA）、authlib（OIDC）、PyJWT、argon2-cffi |
| K8s | 官方 `kubernetes` 客户端 36.0.3 |
| 可观测 | structlog、prometheus-client |
| 前端 | Vue 3.5 + Vuetify 4 + Vite 8(Rolldown) + Pinia 4 + vue-router 5 + ECharts 6 |
| 终端 | `@xterm/xterm` 6（在浏览器里直连 Pod 的 SSH/exec） |

---

## 3. 目录结构

### 3.1 仓库根

```
kube-wl-ai/
├── backend/         FastAPI 服务（api / worker / beat 共用同一镜像）
├── frontend/        Vue 3 SPA（nginx 托管并反代）
├── deploy/
│   ├── docker-compose.dev.yml   开发环境一键起（含 Postgres/Redis）
│   ├── grafana/                 面板生成器 + provisioning
│   └── helm/kube-wl-ai/         应用本体 Chart（不含监控栈）
├── design/          原型截图 —— 功能与字段的基线，**不是视觉基线**
├── legacy/          重写前的旧代码，只读留档（内含历史泄漏凭证，删除前需先轮换集群凭证）
├── scripts/         验收与工具脚本（详见 §8）
├── agents.md        分阶段实施记录（设计决策、踩坑、验收证据）
└── README.md        运行 / 验收 / 配置说明
```

### 3.2 后端 `backend/app/`（约 24,700 行）

分层严格单向依赖：`api → services → models`，跨层能力（权限、K8s、审计）走 `core/auth/k8s`。

```
app/
├── main.py             应用工厂：日志 → 引擎 → 路由 → 中间件 → 异常处理器
├── api/
│   ├── v1/             19 个路由模块（§5）
│   ├── ws/             终端 WebSocket（/ws/terminal/{id}）
│   └── wellknown.py    JWKS 端点，供 Grafana 校验令牌
├── auth/               ★ 权限体系（§4）
│   ├── permissions.py  72 个权限点的目录 + 四个角色的权限集合
│   ├── policy.py       ★ 公开入口：has_permission / scope_filter / ensure_* 系列
│   ├── tokens.py       签发与校验 JWT
│   ├── rate_limit.py   登录限流
│   └── providers/      local / ldap / oidc 三种身份来源
├── models/             17 个 ORM 模块（业务对象见 §5.1）
│   └── enums.py        ★ 所有状态机枚举，先读这个能快速建立领域概念
├── schemas/            19 个 Pydantic 模块，请求/响应体
│   └── base.py         ApiModel：camelCase 别名 + extra="forbid"
├── services/           18 个业务服务（§5.2）
├── k8s/
│   ├── gateway.py      ★ 集群的唯一出口（协议 + 真实实现）
│   ├── client.py       真实集群客户端
│   ├── naming.py       ★ 命名与标签规则（namespace / workload / label）
│   └── workloads.py    Deployment / Job 的规格构造
├── middleware/
│   ├── request_context.py  注入 request-id，贯穿日志
│   └── audit.py            自动补写审计行（服务层已写则跳过）
├── core/               config / deps / errors / logging / time / cookies / version
├── workers/
│   ├── celery_app.py   Celery 配置
│   └── tasks/          reconcile.py（对账集群）、usage.py（用量汇总）
├── registries/         镜像仓库抽象：harbor 实现 + fake（测试用）
└── monitoring/grafana.py  面板与嵌入链接生成
```

### 3.3 前端 `frontend/src/`（约 17,900 行）

```
src/
├── api/          17 个模块，与后端路由一一对应；client.ts 统一封装 axios
├── views/        19 个页面（admin/ 下 6 个为管理员专属）
├── components/   13 个组件：通用件(common/) + 领域件(instance/image/profile)
├── stores/       auth（会话与权限）、notifications、ui
├── composables/  usePermissions.ts —— ★ 前端权限判定，但只是 UX
├── config/       navigation.ts —— 侧边栏定义，同时是页面的角色可见性来源
├── router/       路由表 + 守卫
├── layouts/      主框架布局
├── styles/       设计令牌（tokens.scss）
└── types/        手写 TS 类型（由 check-frontend-types.py 与后端 schema 比对）
```

---

## 4. ★ 权限体系（这是最该看的部分）

**三层过滤，缺一不可：**

| 层 | 回答的问题 | 实现在哪 | 形式 |
|---|---|---|---|
| ① RBAC | 这个人**能不能**做这个动作 | `auth/permissions.py` | `resource:action`，共 **72** 个权限点 |
| ② 数据范围 | 这个动作**能作用在谁身上** | `auth/policy.py::scope_filter` | 自动往 SQL 里注入 `WHERE` |
| ③ 对象/配额约束 | 做到什么**程度** | `services/quota_guard.py` 等 | 额度、并发数、单实例卡数 |

### 四个角色与权限规模

| 角色 | 权限数 | 说明 |
|---|---|---|
| `observer` | 13 | 只读观察者 |
| `user` | 35 | 普通用户 |
| `group_admin` | 48 | 组管理员，比 `user` 多出 `node:read` 等 |
| `global_admin` | 72 | 平台管理员，等于**全部权限点**（`ALL_PERMISSION_CODES`） |

角色是**累积**定义的（`_USER = _OBSERVER | {...}`，见 `permissions.py:167-211`），
所以「某人有什么权限」= 其所有角色集合的并集。
`global_admin` 独有 25 个权限点，包括 `user:*`、`group:*`、`resource_group:*`、
`node:sync`、`system:config`、`workspace:delete`、`monitor:read_all`、`usage:read_all` 等。

### 关键机制

- **角色有作用域**：`RoleScope.GROUP` 的分配带 `group_id`，这正是「组管理员天然只管一个组」的实现方式。
- **`DataScope` 表示「能看到哪些行」**，`ScopeRule` 描述每个模型按什么字段过滤
  （`owner` / `group` / `workspace` / `via_user` / `member_of` / `member_workspace` / `public`）。
- **容易踩错的一点**：`DataScope.group_ids` 是**该用户管理的组**，不是**他所属的组**。
  要判断成员关系请用 `actor.group_ids`，两者语义不同。
- **API 令牌有一层额外的「权限天花板」**（`permission_ceiling`）：令牌只能行使**签发者**
  权限的子集，且可进一步收窄 scope。所以令牌永远不会成为提权途径。

> ⚠️ 审核提示：前端的 `usePermissions()` **只是 UX**（决定按钮显不显示）。
> 真正的拦截全部在后端。任何一个接口都应能在 `app/api/v1/` 里找到 `require(...)` 或
> `ensure_*_access(...)` 调用 —— **如果某个接口没有，那就是缺陷**。

---

## 5. 业务模块

### 5.1 核心对象模型

```
Group(实验室/公共组)
 └── Workspace(工作空间) ──1:1──> K8s Namespace (wl-{group}-{name})
      ├── WorkspaceMember  (user_id UNIQUE —— 一人只在一个空间)
      ├── ResourceGrant    (PRIVATE 资源组必须在此显式授权)
      ├── Quota            (CPU/内存/GPU 上限)
      ├── Instance         (容器实例 → Deployment + Service/NodePort)
      ├── Task             (自定义任务 → Job)
      ├── StorageVolume    (持久卷 → PVC)
      └── WorkloadEvent    (状态变更时间线)

ResourceGroup(硬件池) ──> ResourceSpec(规格模板: 卡数/CPU/内存/镜像)
ComputeNode / GpuDevice   (从集群同步，只读缓存)

User ──> RoleAssignment(可带 group_id) / SshKey / ApiToken
AuditLog                   (操作留痕)
UsageDaily                 (按天汇总用量)
Image                      (镜像目录，来自 Harbor 或手工登记)
```

### 5.2 19 个路由按业务分组

| 业务域 | 路由模块 | 主要能力 |
|---|---|---|
| 身份与权限 | `auth` `users` `groups` `rbac` | 登录/刷新/登出、用户与组 CRUD、角色分配、权限查询 |
| 租户与资源 | `workspaces` `resources` | 空间开通/成员/配额、资源组与规格目录、授权 |
| 算力负载 | `instances` `tasks` `applications` | 实例生命周期、任务提交与取消、应用目录 |
| 存储与镜像 | `storages` `images` | 持久卷、镜像目录与拉取凭证 |
| 计量与运维 | `usage` `monitor` `audit` `system` `health` | 用量汇总与导出、监控面板、审计查询、系统信息、健康检查 |
| 开放接口 | `api_tokens` | 令牌签发与吊销（供程序调用） |

### 5.3 状态机（`models/enums.py`）

- `WorkspaceStatus`：`pending → provisioning → active`，异常落 `error` 并由 reconcile 重试
- `InstanceStatus`：`pending / creating / running / stopping / stopped / failed`
- `TaskStatus`：提交 → 运行 → 成功/失败/取消
- **工作空间只有在命名空间、配额、网络策略三者都建成后才算 `active`** —— 半成品不会被当成可用。

---

## 6. 一次请求的完整路径

以「创建一个容器实例」为例：

```
浏览器
  POST /api/v1/instances            (camelCase JSON；未知字段直接 422)
   │
   ▼ 中间件（由外到内）
  CORS → RequestContextMiddleware(注入 request-id) → AuditMiddleware
   │
   ▼ 路由层  api/v1/instances.py
  依赖注入：当前用户(client) + 数据范围(scope)
  权限判定：require("instance:create")          ← 第①层
   │
   ▼ 服务层  services/instances.py
  数据范围：scope_filter 注入 WHERE            ← 第②层
  配额校验：services/quota_guard.py            ← 第③层
  落库：实例记录 status=pending
   │
   ▼ K8s 层  k8s/gateway.py（唯一出口）
  构造 Deployment/Service 规格 → 应用到命名空间
   │
   ▼ 返回
  201 + camelCase 响应体；审计行已写入
```

**几个贯穿全局的约定（审核时容易误判为 bug）：**

- **请求/响应体是 camelCase**，由 `schemas/base.py::ApiModel` 自动转换，且 `extra="forbid"` ——
  多传一个字段会 422，这是**有意的**，防止前端笔误被静默忽略。
- **查询参数也是 camelCase**（后端用 `Query(alias=...)` 声明）。写成 snake_case 会被**静默忽略**，
  这是排查「筛选没生效」类问题的常见原因。
- **分页**：请求 `page`/`pageSize`；响应外层是 `{"items": [...], "meta": {...}}`。
  但有 **18 个接口返回裸数组**（`response_model=list[...]`），审核时容易误判为不一致。
  规律是：**有界或天然从属的集合返回裸数组**（`/ssh-keys`、`/{id}/members`、`/nodes/{id}/gpus`、
  `/resource-specs`、`/api-tokens`、`/*/events` 等），**需要分页的列表才用信封**。
  这是历史遗留的不统一，功能上没问题，但如果要收口，应统一成信封并同步改前端 17 个 api 模块。
- **时间**：SQLite 返回 naive、PostgreSQL 返回 aware，统一由 `core/time.py` 归一化。
- **`get_session` 遇异常一律回滚**，所以服务层不需要自己写 rollback。
- **审计**：`core/audit.py::record_audit` 是同步函数；传 `request=` 后会置位
  `request.state.audit_recorded`，让中间件跳过重复写入。

---

## 7. Kubernetes 交互层

**所有集群操作都从 `k8s/gateway.py` 走**，它是一个 Protocol：真实实现连集群，
测试用 `FakeK8sGateway` 内存实现，**因此测试永远不会碰到真实集群**。

命名与标签规则集中在 `k8s/naming.py`，审核时看这一处即可：

| 规则 | 值 |
|---|---|
| 命名空间 | `wl-{group}-{workspace}` |
| 托管标记 | 标签 `wl-managed=true`（创建时自动打上） |
| 归属标签 | `wl-group` / `wl-workspace` / `wl-owner` / `wl-instance-id` / `wl-task-id` |

> **安全护栏**：删除命名空间前必须通过 `is_managed_namespace(name)` 检查
> （`k8s/client.py:311`）。注意它检查的是**名字前缀** `wl-`（`naming.py:133`），
> 而不是托管标签 —— 这是有意设计的保护，避免误删非本平台创建的命名空间，
> 不要简化掉。审核时可以思考一下：前缀检查是否足够严格？

---

## 8. `scripts/` 目录

**验收脚本**（对着真实运行的 uvicorn 发 HTTP，与前端调用顺序一致）：

| 脚本 | 覆盖 | 项数 |
|---|---|---|
| `smoke-env.sh` | 建一次性 SQLite 库 + 迁移 + 管理员，并导出环境变量 | — |
| `smoke_phase1.py` | 身份 / RBAC / 用户组 | 33 |
| `smoke_phase2.py` | 工作空间租户边界 / 配额 | 33 |
| `smoke_phase3.py` | 实例生命周期 / SSH / IDE 票据 | 49 |
| `smoke_phase4.py` | 应用目录 / 自定义任务 | 61 |
| `smoke_phase5.py` | 持久卷 | 65 |
| `smoke_phase6.py` | 用量汇总的恒等式 | 61 |
| `smoke_phase7.py` | 监控与 Grafana 交接（**两种模式都跑**） | 43 |
| `smoke_phase8.py` | 审计 / 令牌作用域 / 限流 | 60 |
| `smoke_phase9.py` | 系统信息接口 / 权限边界 | 23 |
| `smoke_frontend_views.mjs` | 在 happy-dom 里真实挂载全部 19 个视图 | 19 |

**工具脚本**：`gen-openapi.sh`（导出 schema）、`migrate.sh`（Alembic 包装）、
`helm-gen-keys.sh`（生成 RS256 密钥入 Secret，私钥不落盘）、
`check-frontend-types.py`（手写 TS 类型 ↔ OpenAPI 比对）。

> `smoke_phase1–4` 用固定夹具名，**不可重复执行**（重跑会 409）；5–9 可重复。

---

## 9. 建议的审核顺序

按「投入产出比」排序：

1. **`backend/app/models/enums.py`** —— 20 分钟建立全部领域概念。
2. **`backend/app/auth/permissions.py` + `auth/policy.py`** —— 权限是这套系统最核心的抽象，
   也是评审意见最集中的地方。重点看 `scope_filter` 怎么把权限翻译成 `WHERE`。
3. **`backend/app/services/quota_guard.py`** —— 配额校验是并发场景下最容易出错的地方，
   看它如何避免超额分配。
4. **`backend/app/k8s/naming.py` + `gateway.py` 的 Protocol 部分** —— 集群交互的全部契约与护栏。
5. **`backend/app/services/instances.py`** —— 把上面四者串起来的典型业务流。
6. **`frontend/src/composables/usePermissions.ts` + `config/navigation.ts`** —— 前端可见性规则。
7. 抽查任意 3 个路由模块，确认**每个接口都有权限判定**（见 §4 的警告）。

### 值得特别关注的风险点

| 关注点 | 位置 | 为什么 |
|---|---|---|
| 权限与数据范围是否**每个**接口都覆盖 | `api/v1/*.py` | 漏掉一个就是越权 |
| 配额校验的并发安全 | `services/quota_guard.py` | 并发提交可能绕过上限 |
| 命名空间删除的护栏 | `k8s/naming.py` | 误删影响面极大 |
| 凭证是否泄漏 | `core/config.py`、`deploy/` | 已用 canary 测试兜底（见 `tests/test_acceptance_phase9.py`） |
| `legacy/` 的旧凭证 | `legacy/` | **历史泄漏，删除前需先轮换集群凭证** |

---

## 10. 当前质量状态

| 项目 | 结果 |
|---|---|
| 后端测试 | `pytest` **481 passed** |
| 验收脚本 | 9 个阶段 **428 项**在干净库上按序全绿 |
| 前端视图 | **19/19** 挂载并渲染出预期数据 |
| 类型与构建 | `ruff` / `mypy`(strict) / `vue-tsc` / `vite build` 全干净 |
| 接口契约 | **78 个接口**的前端类型与 OpenAPI schema 一致 |
| Helm | `helm lint` 通过，两条 RBAC 分支均可渲染 28 个对象 |

**已知未完成**（均为集群侧配合事项，非代码缺陷）：Chart 未真正 `helm install`；
`values.yaml` 中镜像仓库、StorageClass、LDAP 口令仍为占位值；`legacy/` 尚未删除。
