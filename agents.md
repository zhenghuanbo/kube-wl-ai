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
| **容器实例** | 选镜像 + 资源组 + 规格 + 存储 → K8s Deployment/Service/PVC，支持 SSH、终端、Jupyter、VSCode；IDE 由**平台后端反代**（`/x/{用户名}/{实例 id}/`）提供，**不建 Ingress** |
| **应用程序** | 带介绍信息的可复用实例形态 |
| **自定义任务** | 直接运行 shell 命令，结束即退出（K8s Job） |
| **存储** | **一个存储目录 = 一个存储卷**。静态 `hostPath` PV + 显式绑定的 PVC；三种粒度：用户卷 / 组共享 / 全局共享。增删改查只给全局管理员，普通用户只读 |
| **镜像** | 对接 Harbor；一个用户组一个 project，个人镜像用用户名作仓库前缀；区分个人 / 官方 / 共享 |
| **用量中心** | 统计用量（**不计费**），管理员可跨用户/组查询导出 |
| **监控中心** | **只集成**集群侧既有的 Prometheus + Grafana |

### 角色模型

| 角色 | 作用域 | 权限 |
| --- | --- | --- |
| `observer` 观察用户 | 全局 | 只读，**零写权限**，不能提交任务 |
| `user` 普通用户 | 全局 | 管理自己的实例/任务/镜像/存储 |
| `group_admin` 组管理员 | 本组 | 普通用户权限 + 建工作空间、分配用户、查本组用量并导出 |
| `global_admin` 全局管理员 | 全局 | 超级管理员，含用户管理 / 用户组管理 / 用量重算 |

权限条目数（`app/auth/permissions.py`）：observer **10** / user **32** / group_admin **45** /
global_admin **69**（目录共 **69** 条）。**这个数字在每期验收里都要跟着更新**，
它同时也是"某个角色是不是误拿了新权限"最快的信号。

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
├── agents.md  README.md  .gitignore  .pre-commit-config.yaml
├── .github/workflows/ci.yml
├── design/                       # 原型截图（功能/字段基线，非视觉基线）
├── docs/                         # 设计与运维文档
├── legacy/                       # 重写前旧代码，只读留档（未删除，见 14.4）
├── scripts/
│   ├── gen-openapi.sh            # 导出 OpenAPI → frontend/openapi/schema.json
│   ├── migrate.sh                # Alembic 包装脚本
│   ├── helm-gen-keys.sh          # 生成 RS256 密钥对并写入 Secret（私钥不落盘）
│   ├── check-frontend-types.py   # 前端类型 ↔ OpenAPI schema 比对
│   ├── smoke_phaseN.py           # 每期一份，对着真实后端跑
│   └── smoke_frontend_views.mjs  # 递归挂载全部视图，带「必须不出现」断言
├── deploy/
│   ├── docker-compose.dev.yml
│   ├── grafana/dashboards/       # Phase 7
│   └── helm/kube-wl-ai/          # Phase 8：应用本体，不含监控栈
├── backend/
│   ├── Dockerfile  pyproject.toml  alembic.ini
│   ├── requirements.txt  requirements-dev.txt  .env.example
│   ├── alembic/                  # env.py + versions/
│   ├── app/
│   │   ├── main.py               # create_app()，应用工厂
│   │   ├── core/                 # config / logging / errors / deps
│   │   │                         # + version.py（版本单一来源，Phase 9）
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
        ├── layouts/  components/  views/   # 全部为真实页面，已无占位页
        └── (Phase 9 删除 PlaceholderView.vue)
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

`resource_groups(kind=public|private, is_default, k8s_label_selector, network_type,
max_runtime_seconds)` · `resource_specs(cpu_milli, memory_mb, gpu_type, gpu_count)` ·
`resource_group_specs` · `runtime_limits` · `resource_group_limits` ·
`compute_nodes(resource_group_id, ...)` · `gpu_devices` ·
`quota_profiles(kind=user|workspace, ...)` · `quotas(scope_type=workspace, ...)`

> `gpu_type` 是**字符串枚举**（如 `v100` / `a100` / `h100`），**不是**整数。

#### 算力配额是三层，各自独立生效，取最严

一个「能不能建」的判定要同时满足三层上限，任一层不满足就 409，报文里的 `detail.layer`
说明是哪一层拒的（`user` / `pool` / `workspace`），`detail.used`/`detail.limit` 是该层的
**实测**用量与该层上限：

| 层 | 数据来源 | 作用域 | 维度 |
| --- | --- | --- | --- |
| ① 用户 | `users.quota_profile_id` → `quota_profiles(kind=user)` | **跨全部工作空间**一个人一行 | cpu / 内存 / 加速卡 + 实例数 / 应用数 / 自定义业务数 |
| ② 资源池 | `resource_group_limits` | 该用户**在该池内** | cpu 核 / 内存 GB / 加速卡张数（每用户） |
| ③ 工作空间 | `workspaces.quota_profile_id` → `quota_profiles(kind=workspace)` | 该命名空间 | cpu / 内存 / 加速卡 + Pod / Deployment / Service / Job 计数 |

三条要点：

1. **数量只查第 ① 层**。「能建几个」是人的属性，放到池上等于给同一空间的每个人各发一份
   相同额度，放到工作空间上又答不了「我还能建几个」。工作空间的对象计数是**兜底**（
   与命名空间的 `ResourceQuota` 同源），个人通常先撞到自己的那一层。
2. **模板上的 `gpu` 是单一数字**（所有型号合计），而第 ①、② 层按型号比较：命名空间配额
   是整个租户的总量，而「2 张 A100 但没有 H100」是关于一个人能申领什么的规定。
   工作空间那层因此按裸 `gpu` 行查表、按各型号求和比较（曾因按型号查表而 fail closed，
   把**每一个** GPU 请求都拒掉——没有 GPU 的集群走不到这条分支，测试全绿）。
3. **`-1` 表示不校验**，`0` 是真实上限（完全不允许），两者相反。`-1` 传到 Kubernetes 的
   忠实翻译是**省略该 key**：`apply_resource_quota` 整体替换 `hard`，而 k8s 把缺失读作
   「无限制」。

数量口径是**现存未删除的对象数**：`is_active` 为真就算，运行中与已停止都占名额，删除后
释放；自定义任务只算未结束的（终态 Job 不再消耗任何东西）。计算用量的口径不同：
开发实例按存活状态（**含已停止**，否则重启会撞上自己占用的容量），任务只算非终态。

#### 用量是算出来的，不是存下来的

`quotas` 表只存**上限**（`limit_value`），没有 `used_value`：那一列从来没有代码写过，
所有读者看到的「已用」恒为 0，比没有计数器更糟——它看起来是权威的。用量由
`app/services/quota_guard.py` 从工作负载表实时算出，用的就是准入校验那一个函数，
所以「用了多少」与「这次能不能过」不可能互相矛盾，也不会漂移。

`quotas` 的行与下发给命名空间的对象**用的是同一组数字**（`_desired_quota`），因此接口报的
和集群上的不可能不一致。`PATCH /quotas/{id}` 已删除：它改的是**派生**值，下一次 reconcile
会悄悄改回去；写入口只有配额模板（`quota_profile:update`，全局管理员）。

`resource_group_limits` 存 cpu 核/内存 GB/加速卡张/存储 GB（**不是** milli/MiB——那是 k8s
的线上格式，转换只发生在拼 manifest 的地方）。

#### 节点归属：数据库是事实，`wl-pool` 标签是物化视图

资源池是**调度目标**，不只是配额单位。这条链路有三段，缺一段任务就会落到别的机器上：

| 环节 | 载体 | 谁写 |
| --- | --- | --- |
| 事实 | `compute_nodes.resource_group_id` | 平台（同步时推导、管理员改派） |
| 物化 | 节点标签 `wl-pool: <资源池 name>` | 平台（同步/改派时 patch） |
| 读取 | Pod 的 `nodeSelector` | `pool_node_selector()`，下发工作负载时烘焙 |

**`resource_group_id` 是唯一权威**，标签只是它的副本。`resolve_node_pool_id()` 的收敛规则：

| 数据库 | 节点标签 | 结果 |
| --- | --- | --- |
| 有池 P | ≠ P | 以 P 为准，patch 标签为 P |
| 有池 P | = P | 不动（稳态下同步**不产生任何写请求**） |
| 空 | 指向真实存在的池 | 采纳该池（运维预打标 / 引导 / 资源池删除后重新归位） |
| 空 | 缺失或指向已删除的池 | 归入默认池并 patch 标签；若无默认池则计入 `nodesUnpooled` |

> **为什么不是反向（标签为权威）**：标签是集群里的、可被任何有权限的人改，而且改标签
> 不会驱逐已在运行的 Pod（`requiredDuringSchedulingIgnoredDuringExecution` 只作用于调度时刻）。
> 把它当权威等于把"哪台机器属于哪个池"交给集群侧的临时状态。

> **为什么单值 key 很关键**：`wl-pool` 是**单值**键，"一个节点恰好属于一个池"由构造保证，
> 不需要额外校验；改派也就是 patch 一个键，是原子的——不存在"先摘除、后加入"之间的空窗。

> **失败方向是安全的**：`nodeSelector` 是硬性要求，所以数据库与标签分歧只会导致 Pod
> `Pending` + `FailedScheduling` 事件，**不会**把任务放到错误的机器上。因此这套收敛机制
> 保护的是可用性而不是隔离性，它的义务只是最终一致。这也是不引入 taint 的原因：硬隔离
> 会让"标签短暂落后"变成"任务卡住"，而收益只是防住一个本来就不该发生的情况。

**默认池（`is_default`）** 是让归属保持**完备**的兜底：没有被任何具名池认领的节点仍然有家，
平台就不需要表达"这台机器不属于任何池"——那是一种任何工作负载都到不了的状态。

- 第一个创建的资源池自动成为默认池（`create_resource_group` 里 `payload.is_default or not has_default`）；
  显式指定会顶掉上一个（`_demote_other_group_defaults`），因为"默认池"是定冠词，有两个就有歧义。
- 默认池的**节点范围是推导出来的**（所有具名池的补集），所以
  `PUT /resource-groups/{id}/nodes` 对它是拒绝的（`default_group_nodes_are_derived`），
  手工维护的列表和算出来的补集会是同一个问题的两个答案。
- `PUT /resource-groups/{id}/nodes` 的语义是**替换整个集合**：被取消勾选的节点移入默认池，
  而不是变成无归属；若此时没有默认池则整笔拒绝（`no_default_resource_group`），不做一半。
- 删除资源池时，若仍有节点属于它则拒绝（`resource_group_has_nodes`）。外键是
  `ON DELETE SET NULL`，放任删除会留下任何工作负载都到不了的硬件——这种故障没有报错，且是永久的。

**引导顺序**：升级后必须先确保存在一个默认池（建任意一个池即可），再跑一次
`POST /nodes/sync` 让节点拿到 `wl-pool` 标签；否则新下发的工作负载带着 `wl-pool: <池>`
选择器却匹配不到任何节点，会一直 `Pending`。**已存在的** Deployment/Job 不受影响：
它们的 `nodeSelector` 在下发时就已烘焙（空选择器仍然是空选择器），reconcile 只读状态。

> 选择器只在**创建**和 `set_replicas` 时下发，之后不再改写——这也意味着**改派节点不会
> 迁移已经在跑的工作负载**，它们仍在原机器上跑完。这是刻意的：任务的 `backoff_limit = 0`，
> 驱逐一个任务 Pod 等于永久杀死它。

`node_sync_interval_seconds`（默认 120s）驱动 `app.workers.tasks.nodes.sync_nodes`。

### 5.4 存储

`storages(kind=user_volume|group_share|global_share, host_path, dir_name, capacity_gb, used_gb,
access_mode, pvc_name, k8s_namespace, source, status, status_message, checked_at, workspace_id,
group_id, owner_user_id, created_by, is_active)`

**卷的同一性就是目录**，不是分配出来的空间。`host_path` 是唯一键（部分唯一索引，只约束
`is_active` 的行），所以"一个目录只能有一个卷"是数据库层面的事实，而不是服务层的约定；
退休的行保留自己的路径，不挡住重新登记。

目录全部由 `Settings.storage_root`（默认 `/storage/kube-wl`）推导，不接受手填：

| 类型 | 目录 | 绑定 |
| --- | --- | --- |
| 用户存储卷 | `{root}/{组}/{用户名}` | `owner_user_id` 必填，组由该用户推导 |
| 组共享卷（指定目录名） | `{root}/{组}/{dir_name}` | `group_id` 必填 |
| 组本级卷（`dir_name` 为空） | `{root}/{组}` | 同上；整个组目录，**只有组管理员能挂载** |
| 全局共享卷 | `{root}/_global/{name}` | 两者都不绑，`workspace_id` 为 NULL |

**顺序要求：组 → 工作空间 → 存储卷。** 卷的 claim 必须落在某个命名空间的 PVC 里，而命名空间来自
工作空间，所以 `_workspace_for_group()` 要求该组**恰好有一个**工作空间：零个是
`group_has_no_workspace`，两个及以上是 `group_has_multiple_workspaces`。全局共享卷不绑组，因此
**没有**这条要求。用户卷看的是**属主**的组（`owner.primary_group`），不是请求里传的组 —— 目录是
`{root}/{组}/{用户名}`，让调用方自己指定组会把一个组的用户放进另一个组的目录。建卷页面据此
**不提供**注定失败的选项（见「存储卷重构（第三步）」）。

`instance_storages(instance_id, storage_id, mount_path, is_read_only)` —— 挂载关系是显式的
多对多，因为一个卷可以同时被多个实例挂载，而挂载点随实例而变。

> 集群侧是**静态** `hostPath` PV + 显式绑定的 PVC（`storageClassName: ""` + `volumeName`），
> 回收策略 `Retain`，容量**只是账面数字** —— `hostPath` 和 `local` 都不强制容量，PVC 也
> **不能扩容**（`only dynamically provisioned pvc can be resized`）。所以 `resize_storage`
> 只改记录，不调集群。
>
> hostPath 的 type 是 **`DirectoryOrCreate`**：登记一个卷就够了，目录由 kubelet 在第一次挂载
> 时创建，不需要单独的供给步骤。**代价是写错路径也会"成功"** —— kubelet 会把错的目录建出来，
> 容器带着一个空目录起来，用户看到的是"我的数据没了"而不是一个错误。`type="Directory"` 会把
> 它变成一次响亮的挂载失败。这是**有意识地选了前者**（用户明确确认）；缓解手段是只有管理员
> 能登记卷（`storage:manage`），路径是手写一次而不是由租户能控制的东西推导出来的。
>
> **目录必须落在节点本地**：`hostPath` 是节点路径，不是网络存储。所以这套模型只在单节点或
> 每个节点都有同一份目录时才成立；等 GPFS/Lustre 阶段到来时，`storage_root` 指向它们的挂载点，
> 而 PV 类型要从 `hostPath` 换成对应的 CSI/local 卷 —— 那时才需要考虑 StorageClass 与配额。
>
> `access_mode` 对 `group_share`/`global_share` 默认 `ReadWriteMany`，`user_volume` 默认
> `ReadWriteOnce`（它是单用户的）。
>
> 全局共享卷**没有 PVC**：claim 是命名空间级的，一个全局卷在每个挂载它的命名空间各需要一个，
> 因此按需创建（名字里带命名空间后缀）。这也是它 `pvc_name` 为空、PV 停在 `Available` 的原因。
>
> 挂载点：用户卷 → `/home/{用户名}`（所有工作负载**隐式**挂载）；组共享 → `/mnt/group_shared`；
> 全局共享 → `/mnt/global_shared`。共享卷**默认不挂**，由用户在创建工作负载时选择；同类多选时
> 第一个占基础路径，后续的挂在 `{基础路径}/{卷名}` 下。

**工作负载不自带磁盘（第二步已落地）。** 实例与任务都只挂属主的用户卷，`HOME` 指向它，
容器里就是 `/home/{用户名}`。由此有三条硬性规则：

1. **用户卷不存在 = 拒绝创建，而不是"少挂一个卷"。** `storages.require_user_volume()` 在实例、
   应用、任务三条创建路径的最前面调用，抛 400 `user_volume_missing`。若改成静默跳过，容器会带着
   一个空 `HOME` 起来，用户读到的是"我的文件没了"，然后往容器可写层写的新文件在负载停止时一起消失
   —— 这是最坏的失败方式，所以宁可拒绝并说清怎么办。
2. **实例/任务都不再创建自己的 PVC**，`instance.k8s_pvc` 因此恒为 `None`（老行仍保留该列，
   删除老实例时还要靠它回收旧临时盘）。**绝不能把用户卷的 claim 记到这里**：删除路径会删掉
   `k8s_pvc` 指向的 claim，而 PV 是 `Retain`，claim 一旦消失 PV 变 `Released` 且不可重绑，
   用户的存储会**永久无法挂载**。
3. **`WorkloadSpec.mount_path` / `JobSpec.mount_path` 没有默认值。** 原来的 `/home/dev` 只是个
   占位路径，与其让调用方漏传后静默挂到一个没人拥有的目录，不如让它成为必填参数。

**全局共享卷的按命名空间 claim** 由 `storages.ensure_attachment_claims()` 在挂载时补齐：
`claim_name_for(storage, workspace)` 是纯函数（全局卷 → 带命名空间后缀的名字，其余 → `storage.pvc_name`），
所以 pod spec 随时可以从零重建而不会与集群漂移。校验（可见性、种类、组本级权限）留在
`resolve_attachments()`，它不碰集群；建对象需要 gateway，所以单独一步。

**工作空间的存储用量来自卷，不来自工作负载。** `quota_guard.workspace_usage()` 汇总
`Storage.capacity_gb`（同工作空间、`is_active`），不再累加 `instance.storage_gb`/`task.storage_gb`
（两列已删）。同理 `check_quota()` 只在请求**自身消耗**该资源时才检查存储：注册卷时检查，
启动工作负载时不检查 —— 实例只是挂一个已经计过费的卷，因为存储到顶就不让启动，是拿已经在盘上的
数据卡人。

**每用户存储上限（`ResourceGroupLimit.max_storage_gb_per_user`）现在校验的是存储卷容量**，
在 `create_storage()` 里由 `_check_capacity_against_pools()` 执行。一个工作空间可以被授予多个资源组，
而卷不属于其中任何一个（用户每次起负载时选），所以取**已授予资源组里最小的那个上限** ——
只看最大的（或第一个）会登记出一个在更严格的池里用不了的卷，而且直到有人真去用才会发现。
卷没有工作空间时无从比较，此时**记一条 warning 而不是静默放行**："没上限"和"通过了上限"在日志里
必须长得不一样。共享卷不做这个校验：它不是个人主目录，按每用户上限去卡没有意义。

### 5.5 镜像

`images(registry, project, repository, tag, digest, kind=personal|official|shared, visibility,
size_bytes, layer_count, display_name, description, is_pinned, is_active, usage_count, synced_at,
sync_error, workspace_id, group_id, owner_user_id, created_by)`

> 唯一约束是 `(registry, project, repository, tag)`。
> Python 属性名为 `registry_host`（`registry` 与 `DeclarativeBase.registry` 冲突），
> 数据库列名仍是 `registry`。
>
> 镜像**没有** framework / category 一类的分类字段：同一个 `pytorch:2.4-cuda12`
> 既跑 notebook 也跑训练任务，那套分类属于工作负载而不是镜像。真正区分镜像的只有
> `kind` 与 `visibility`，而且都由引用路径**严格**推导 —— `library/*` → official、
> `{组}/share/*` → shared、`{组}/{用户名}/*` → personal，其余不属于任何一类
> （见 5.5.1），不靠人工填写。

#### 5.5.1 Harbor 项目结构：一个组一个 project

```
harbor.edu.cn/
├── library/                       官方镜像（安装级，全员可拉）
│   └── pytorch:2.4-cuda12
├── lab-vision/                    ← 一个用户组 = 一个 Harbor project
│   ├── share/cuda-toolkit:12.4   组内约定共用
│   └── zhangsan/my-env:v3         个人镜像，用户名作第一段
└── lab-nlp/
    └── wangwu/nlp-env:v2
```

**project 名 = `Group.name`，不是 `directory_name`。** Harbor 项目名只允许
`[a-z0-9][a-z0-9._-]*`，目录里的 `cn` 可能是 `Lab_Vision` 或中文，注册表不收。
`Group.name` 本来就是小写 ASCII slug（它同时是 K8s 命名空间的一段），约束正好够用。

三条规则在 `register_image` 里强制：

| 规则 | 错误码 |
| --- | --- |
| project 必须是某个 `Group.name` 或 `library` | `image_project_unknown` |
| 个人镜像的仓库名必须以本人用户名开头 | `image_prefix_mismatch` |
| 项目所属组与显式 `groupId` 必须一致 | `image_project_group_mismatch` |

> **为什么平台要管这个**：Harbor 的 RBAC **只到 project** —— 官方权限表里
> `Create repositories` 是 `Developer` 就有的，且是 project 范围。没有任何配置能
> 表达"这个成员可以推 `lab-vision/zhangsan/*` 但不能推 `lab-vision/lisi/*`"。
> 所以**组内没有真正的隔离**，前缀只是平台能在自己这层提供的归属约定，也为将来
> 发限定前缀的 robot 留出余地。它挡不住直接 `docker push`。

**解析规则**：project 取 reference 的**第一段**，repository 取**其余全部**（可嵌套）。

```
harbor.edu.cn/lab-vision/zhangsan/my-env:v3
  → project=lab-vision  repository=zhangsan/my-env  tag=v3
```

> 早期实现取的是**最后一段**做 repository，于是 project 变成
> `lab-vision/zhangsan`。这不只是名字难看：同步路径是按 Harbor 报告的
> `(project, repository)` 建行的，两条路径对同一张镜像写出**两行不同的记录**
> （唯一键 `(registry, project, repository, tag)` 因此不冲突），目录里镜像翻倍、
> 用量统计被拆散。

**Harbor API 的 URL 编码**：repository 含斜杠时必须**双重** URL 编码
（`zhangsan/my-env` → `zhangsan%252Fmy-env`），因为 Harbor 的路由解一次、
handler 再解一次。用原始斜杠会被当成额外路径段返回 404，导致嵌套 repository 的
tag 列表、校验、删除全部失败。见 `harbor.py::_repository_path`。

**同步时的归组**：分类是**引用路径的纯函数**，约定只有三种形状，别的都不算：

| 路径 | kind / visibility | 谁能看到 |
| --- | --- | --- |
| `library/<镜像>` | `official` / `global` | 所有登录用户 |
| `{组}/share/<镜像>` | `shared` / `group` | 该组成员 |
| `{组}/{用户名}/<镜像>` | `personal` / `private` | 仅该用户 |

`{组}` 必须是平台上真实存在的组，`{用户名}` 必须是真实账号 —— 只有平台能解析这个
名字时，路径才被当成对该组/该人的声明。其余一律**不属于任何一类**：没人认领的
Harbor 项目、直接丢进组项目的仓库（如 `hpc/harbor-portal`）、首段不是账号的路径
（如 `{组}/team-cache/x`）。这些行**照样进目录**（目录的职责就是说清楚"有什么"），
但落为 `shared`+`private` 且无 owner，`visible_condition` 的任何一个分支都不匹配，
**只给管理员看，不进任何人的选择器**。

> 宽松规则（"组项目就是组的空间"）曾把 `hpc/harbor-portal` —— 某个人顺手推的一次
> —— 变成每个 hpc 成员都能拉的镜像。严格才是重点。
>
> 不做归组的话，有人往 `lab-vision/zhangsan/secret-model` 推的镜像会以
> `shared`+`global` 落进目录，出现在**每个实验室**的镜像选择器里 —— 与"一个组一个
> 私有 project"的目的正好相反。

**同步会重算已有行的分类**：`kind`/`visibility`/`group_id`/`owner_user_id` 既然由路径
推导，一行如果按它现在的路径不再符合当初归入的形状，那就是**过时**而不是"人工策展"，
由同步改正。**唯一例外是 `is_pinned` 的行** —— 管理员 pin 一行就是接管了它（这正是
pin 的用途），同步既不回收它、也不重算它的分类。

#### 5.5.2 选择器只显示三类镜像

创建任务/实例要选镜像，这是整个项目结构对用户唯一可见的地方，因此
`visible_condition` 就是契约：

| 类别 | 判定 | 谁看得见 |
| --- | --- | --- |
| **集群内置** | `kind == official` | 所有登录用户 |
| **组共享** | `visibility == group` 且组在本人（或所辖）组内 | 该组成员 |
| **个人** | `owner_user_id == 本人` | 仅本人 |
| 显式全局 | `visibility == global` | 所有登录用户（需 `image:share`，管理员主动标） |
| 工作空间共享 | `visibility == workspace` 且在本人工作空间内 | 该空间成员 |
| 全局管理员 | `scope.all_access` | 全部 |

后两项是有人主动做的动作（普通用户只能发 `private`/`workspace`，`group`/`global`
需要 `image:share`），所以不会被静默取消；但它们不在"三类"之内。

> **别人的个人镜像不会出现在我的选择器里**：同步发现的个人镜像是 `private`，
> 手动登记时前缀校验又挡住 `{组}/{他人用户名}/...`。唯一例外是管理员显式把某张
> 镜像标成 `group` —— 那是主动共享，不是泄漏。
>
> **对照实例**：`zhenghuanbo` 属于 `hpc` 组，他看到的就是 `library/*`（官方）+
> `hpc/zhenghuanbo/*`（个人）+ `hpc/share/*`（组共享）。`hpc/harbor-portal` 不在
> 其中 —— 它不在 `share/` 下，就不属于这个组。

### 5.6 工作负载

`app_templates` · `applications` · `instances` · `tasks` · `workload_events` · `usage_daily`

`usage_daily(day, user_id, group_id, workspace_id, workload_kind=instance|task, gpu_type,
runtime_seconds, gpu_seconds, cpu_milli_seconds, memory_mb_seconds, workload_count)`

> 唯一约束 `(day, user_id, group_id, workspace_id, workload_kind, gpu_type)`。
> **参与唯一键的列全部 NOT NULL** —— `gpu_type` 用 `"none"` 而不是 NULL，因为
> NULL 在 SQLite 和 PostgreSQL 里都互不相等，带 NULL 的唯一约束等于没有约束，
> 重算就会变成翻倍累加而不是幂等替换。
>
> `group_id` 是**快照**：工作空间改挂到别的组、用户离开组，都不应该改写上一季度的用量。
> `resource_group_id` 刻意**不在**表里 —— 它可以为空（资源组删除时 `SET NULL`），
> 拿它做唯一键的一部分就要引入哨兵值；而"用了哪种卡"由 `gpu_type` 回答，
> "用了哪个池"是可从授权关系推出来的，不值得为它破坏幂等性。

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
6. **`effective_permissions` 不区分 `UserRole.scope`** —— 组范围的角色**不会**让权限变少，
   只会让数据范围变窄。因此"哪些角色可以按组下发"必须显式限制（见下）。

### 组范围的角色只有 `group_admin`（`GROUP_SCOPED_ROLES`）

`RoleScope.GROUP` 的语义是"**在哪个组里**有这个角色"，**不是"这个角色只有组内的权限"**。
`effective_permissions` 会把所有 `UserRole` 的权限**全量并入**，不看 scope：

```python
for link in user.role_links:            # ← 无论 link.scope 是什么
    codes.update(grant.permission.code for grant in link.role.permission_links)
```

所以一个"组范围的 `global_admin`"会拿到**全部 70 个权限**，而
`system:config` / `user:create` / `node:sync` / `group:delete` 这些**根本没有"组"这个维度**，
scope 限不住它们。更糟的是 `effective_role_codes` 只对 `group_admin` 认组范围的角色
（`global_role_codes` 过滤 `scope == GLOBAL`），于是前端会认为这个人**没有任何角色**、
隐藏所有管理入口——而他可以用 API 做任何事。**UI 和 API 对同一个用户给出相反结论。**

因此两条路径都强制同一个不变量：

| 路径 | 强制方式 |
| --- | --- |
| 创建用户 / 设置组（`plan_role_assignments`） | 组范围只取 `GROUP_SCOPED_ROLES` 里的角色 |
| 单独授权（`grant_role`，`POST /users/{id}/roles`） | 组范围且角色不在 `GROUP_SCOPED_ROLES` → 400 `role_not_group_scoped` |

**新增按范围下发的角色时，除了改 `GROUP_SCOPED_ROLES`，还要确认该角色的每个权限码在
"范围只剩一个组"时仍然合理**——也就是确认它要么真的与组相关，要么是只读的目录类权限。
`group_admin` 之所以安全，是因为它在组资源之外只持有只读权限
（`user:read` / `node:read` / `role:read` / `permission:read` / `resource_group:read` /
`resource_spec:read`），没有任何全局写权限。

### 用量中心的权限（范围由数据权限决定，不由权限码分档）

用量的**读**只有一个权限码：`usage:read_self`。`usage:export` / `usage:manage` 是另外两件事
（带走文件、重算聚合），不是"更大范围的读"。

"能看到多少"完全由 `scope_filter` 决定，三档都从 `resolve_scope` 派生：

| 角色 | 读用量的权限码 | 数据范围（唯一的分档来源） |
| --- | --- | --- |
| observer / user | `usage:read_self` | 仅本人 |
| group_admin | `usage:read_self` | 本组 + 本人（scope 带来的） |
| global_admin | `usage:read_self` | 全部（`all_access`） |

**这里曾经有 `usage:read_group` / `usage:read_all` 两个码，现已删除**，因为它们不被任何
端点检查——`/usage/*` 的读接口 gate 在 `read_self` 上，分档由 scope 完成。作为权限码它们
描述了一种不存在的能力：给普通用户授 `usage:read_all` 什么也不会发生（scope 仍只给自己），
而只持 `read_self` 的组管理员照样看到整组。**一个不会被检查、却出现在权限清单和授权界面里的
码，比没有这个码更糟**——授权的人会以为它生效了。

监控中心保留了三个码（`monitor:read_self/group/all`），因为它们**真的**在分档：仪表盘目录
按码过滤（`monitoring/grafana.py`），打开某个仪表盘时再校验一次（`api/v1/monitor.py`）。
用量只有一个页面，所以"breadth"属于 scope，不属于权限码。

两个刻意的选择：

1. **用量比它所描述的负载更窄。** `UsageDaily` 的 `ScopeRule` **不含 `member_workspace`**：
   同处一个命名空间足以让你看到同事的容器，但不足以让你看到同事占用了多少张卡。
2. **`usage:manage` 只给全局管理员。** 重算会一次性重写所有租户的行，
   这件事没有"只管本组"的版本。

**教训（适用于新增模块）**：权限码回答"能不能做"，范围回答"对谁做"。如果发现自己想加一个
形如 `xxx:read_group` 的码来表达"范围更大"，那多半说明分档应该交给 `ScopeRule`；只有当
**不同的档位对应不同的事物**（不同仪表盘、不同端点、不同动作）时，才应该新增权限码。

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
- `WS /ws/terminal/{id}` 从 `token` 查询参数读**同一个 access token**，按普通 HTTP 请求的方式校验；
  **每个请求重新读一次角色**，所以撤掉 `instance:exec` 会立刻关掉已建立的入口。这条路径**没有**独立
  的握手票据机制——本文档早先写的"WebSocket 握手使用短期一次性 ticket"与代码不符，已改正。
- **IDE 反代路由 `GET /x/{用户名}/{实例 id}/…`（含 websocket）目前不鉴权。** 知道用户名与自增的
  实例 id 就能打开别人的 IDE。这是**已知且有意推迟**的缺口（用户明确表示"暂时先不实现访问服务的
  鉴权凭证（票据）"），见 §14.1 与"IDE 暴露改为平台后端反代"一节。——**不要在任何地方把它描述成
  已鉴权，也不要说票据在保护它。**
- 首次登录 JIT 开户；FreeIPA 组映射本地实验室组；命中约定 IPA 组者自动获得 `observer`。
- **LDAP 不可用时明确报错，绝不隐式放行。**

---

## 8. API 约定

- 前缀 `/api/v1`；健康检查在根路径（`/healthz`、`/readyz`、`/metrics`）。
- **`/readyz` 会校验数据库的迁移版本，不只是连通性**（`app/db/schema.py`）。这一点很重要：
  一个落后于模型的库 `SELECT 1` 照样成功，所以只测连通性的探针会一直报 `ok`，直到第一个
  碰到新列的查询在 ORM 预加载里炸出 `no such column` —— 表现为一个带两百行堆栈的 500，
  而真正的原因只是**忘了跑一条命令**。现在它会直接给出：

  ```json
  {"status":"degraded","checks":{
     "database":"ok",
     "schema":"error: database at 3f0dbd1b4617, code expects b7c41a9e5d02; run `alembic upgrade head`"}}
  ```

  三种判定：版本一致 → `ok`；对不上（含库未打标 `unversioned`）→ 503 并给出修复命令；
  **读不到迁移脚本则报 `unknown` 且不算失败** —— 镜像里没打包迁移目录时无从得知"应该是哪个版本"，
  为这个让 Pod 不健康会误伤那些在别处统一做迁移的部署。Helm 用 `/readyz` 作 readinessProbe，
  所以滚动升级时"新代码 + 旧库"的 Pod 不会被接流量（chart 本来就有 pre-upgrade 迁移 hook，
  这是 `migrations.enabled=false` 时的兜底）。
- **统一错误信封**（`app/core/errors.py` 已实现）：

  ```json
  {"error": {"code": "not_found", "message": "Resource not found", "detail": {}}}
  ```

- **统一分页信封**（`app/schemas/common.py`）：`{"items": [...], "meta": {"page","page_size","total","pages"}}`
- 业务错误一律抛 `AppError` 子类，**不要直接返回 `JSONResponse` 或裸 `HTTPException`**（后者会丢掉信封）。
- 列表接口的分页参数是 `page` / `pageSize`（≤200）。
- **多词查询参数一律 camelCase**，由 `Query(alias=...)` 声明。snake_case 拼写
  **不会报错，会被静默忽略** —— 请求返回 200 与一个不受筛选影响的结果集，
  看起来像"没有数据"。这条在 Phase 3/5/6 各踩过一次，`usage` 冒烟里有一条专门盯它。
- 变更 OpenAPI 后必须重新运行 `./scripts/gen-openapi.sh` 并提交 `frontend/openapi/schema.json`。

---

## 9. Kubernetes 编排

- 每个工作空间一个 namespace，标签 `wl-managed=true`；配套 `ResourceQuota` + `LimitRange` +
  默认拒绝 `NetworkPolicy`。
- **实例** → `Deployment(replicas=1)` + **两个 Service**（SSH `NodePort`、IDE `ClusterIP`）+ PVC。
  **不再创建 Ingress**：IDE 由平台后端自己的反代（`/x/{用户名}/{实例 id}/`）访问那个 `ClusterIP`。
- **任务** → `batch/v1 Job`（`backoffLimit=0`、`ttlSecondsAfterFinished`）。
- **GPU** → `resources.limits["nvidia.com/gpu"]`（整卡），`nodeSelector` 走资源组与加速卡型号标签 + tolerations。
- **SSH（唯一方案）**：实例内运行 `sshd` + `Service type=NodePort`；用户公钥存 `ssh_keys`，
  以 Secret 挂载为 `authorized_keys`。
- **浏览器终端**：`WS /ws/terminal/{id}` → K8s `exec`。
- **IDE**：`ide_type=none|jupyter|vscode|webssh`，由平台后端反代暴露在 `/x/{用户名}/{实例 id}/`
  （Jupyter 用 `…/lab`，vscode 与 webssh 用根路径）。**这条路由不做任何鉴权**，IDE 自身也以关闭认证
  的方式启动（`--ServerApp.token=` / `code-server --auth none` / `ttyd --writable`），
  所以**拿到 URL 就等于拿到该用户的 shell**。
- **协调**：周期性 reconcile，**两个调用者共用同一份代码**
  （`app/services/reconcile.py::sweep_all()`）：Web 进程内的 sweeper
  （`RECONCILE_INTERVAL_SECONDS`，默认 5 秒）和 Celery beat 的 `reconcile-workloads`（30 秒）；
  多 worker 部署把前者设成 `0`，让 beat 当唯一的对账者。**没有 K8s watch，没有 Redis
  pub/sub，没有 SSE 推送，也没有指数退避。** 状态靠轮询观测，前端只在列表里还有未安定
  行时每 3 秒拉一次。
  （这一行原先写的是「beat 周期 reconcile（指数退避）+ K8s watcher → Redis pub/sub → SSE
  推送」—— 四件里有三件从未存在，第四件的退避也没有实现。）
- **命令注入防护**：Job 命令必须用 `command` / `args` 数组，**禁止 shell 字符串拼接**。
- **测试隔离**：单元测试一律使用 `K8sGateway` 的 Fake 实现，不接触真实集群。

---

## 10. 外部集成

| 服务 | 方式 |
| --- | --- |
| **FreeIPA** | LDAPS，`uid={username},cn=users,cn=accounts,{base_dn}`；验证口令用本人 bind、读属性用服务账号；配置见 [`deploy/ldap/README.md`](./deploy/ldap/README.md) |
| **Harbor** | API v2.0；一个用户组一个 project（`library` 例外），个人镜像用用户名作仓库前缀；平台侧 1 个系统级 robot（全权限）读 API；节点侧 1 个**只读** robot 供 kubelet 拉镜像。详见 `deploy/harbor/README.md` |
| **Prometheus** | 既有实例，用于用量聚合（GPU 秒 / CPU 核秒 / 内存 GB 秒） |
| **Grafana** | 既有实例；开启 `GF_AUTH_JWT_ENABLED`，JWKS 指向后端 `/.well-known/jwks.json`；后端签发 5 分钟 JWT 并**强制锁定** `var-namespace` / `var-pod` |

**用量口径**：底层按 **GPU 秒** 累计，展示折算为**卡时（GPU·h）**，**不做计费**。

**Grafana 隔离三重保险**：① 目录 `AI-Admin` / `AI-User`；② 后端代签 URL 时锁定变量；
③ `/monitor/*` 接口同样走 `scope_filter`。未配置 Grafana 时降级提示，不影响其他模块。

### 10.1 目录组与平台组的对应关系（`Group.directory_name`）

**目录不会自动创建用户组**，这是刻意的：平台的「组」不是标签，而是
`workspaces` / `applications` / `tasks` / `usage` 的 `NOT NULL` 归属根，
工作空间的命名空间还由 `wl-{group}-{workspace}` 派生。自动建组等于让目录侧的
任何改动在平台上开出承载命名空间与配额的地盘，绕过平台的配额与审批流程。
所以流程是：目录里建组 → 平台里建组 → 同步成员。

**关联是隐式的，靠名字相等**，没有映射表。原先这个「名字」只能是
`Group.name`，而它被约束成 `^[a-z0-9][a-z0-9-]*$`（因为它要进 K8s 命名空间，
且正因为已是小写 ASCII，`slug(name) == name`，命名空间才不会在派生时碰撞或变空）。
目录的 `cn` 则是任意 UTF-8。两者相撞的后果是一个**死结**：

```
FreeIPA: cn=Lab_Vision          →  平台不允许创建 name=Lab_Vision（正则拒绝）
平台:     name=lab-vision       →  按 name 匹配永远匹配不到 "Lab_Vision"
```

`directory_name` 就是解开它的字段：

- 匹配键统一为 `Group.directory_key == directory_name or name`（模型上的属性，
  **一处定义**，保证全量同步与每次登录的同步不会对「同一个组」有不同理解）。
- `NULL` 表示「按 `name` 匹配」，也就是这一列出现之前的行为 —— 因此**存量行无需回填**。
- 唯一性分两处：数据库唯一索引管 `directory_name` 自身重复；跨列的那种
  （某组的 `directory_name` 撞上另一组的 `name`）由
  `_ensure_directory_key_free()` 按 `coalesce(directory_name, name)` 检查。
- 两条同步路径都**拒绝猜**：一个目录名被两个平台组认领时，两边都跳过并报错
  （全量同步进 `errors`，登录路径记 warning）。不这样做的话，用未加保护的
  dict 构建会「保留先查到的那一行」，**成员会随查询计划落到不同的组**。

`GroupSyncResult.unmatched_groups` 是给管理员看的那一半：它列出「目录里有、
平台上还没有」的组。只报 `memberships_added: 0` 而不说为什么，正是
「我在目录里建了实验室，怎么什么都没发生」的来源。

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

> **想拿副本试迁移时注意**：`alembic/env.py` 的 URL 来自 `get_settings().database_url`，
> **不读 `-x db_url=...`** —— 那个参数会被静默忽略，于是"在 `.smoke/` 的副本上先试一下"
> 实际动的是 `backend/app.db`（工作目录下的真实开发库）。正确做法是把 `DATABASE_URL`
> 环境变量指到副本：
>
> ```bash
> cd backend
> DATABASE_URL=sqlite+aiosqlite:///./.smoke/try.db .venv/bin/alembic upgrade head
> ```
>
> `app.db` 已被 gitignore，误操作不会进提交，但里面可能是别人正在用的开发数据。

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
| 配额 | 由已授权资源组的 `ResourceGroupLimit` 汇总；GPU 按型号取各池**最大**规格（规格是互斥选项，求和会虚增额度）。*（**已被取代**：工作空间的命名空间配额改由 `quota_profiles` 驱动，资源池不再汇总进工作空间上限，见 §5.3）* |
| 一人一空间 | `workspace_members.user_id` **单列唯一**，数据库层兜底；迁移需显式 `moveFromExisting` |
| 资源可见性 | 目录（资源组/规格/时长/节点）对全体登录用户可读，**授权**才是边界 |
| API | `/workspaces`（含 `/mine`、`/reconcile`、成员、资源授权）、`/resource-groups`（含 `/nodes`、`/specs`、`/limits`）、`/resource-specs`、`/runtime-limits`、`/nodes`（含 `/sync`）、`/quotas` |
| 列表搜索 | 所有列表接口都接受 `search`（按 `name` / `display_name`）。`/resource-specs` 原先漏了它，Phase 6 补上，见下方"静默忽略的参数" |
| 测试 | 新增 61 项（36 项 API 验收 + 25 项命名/数量解析单测），全量 **183 项通过** |
| 前端 | 我的工作空间（配额进度、网络档位、可用资源池）、工作空间管理（建/停/删、成员、资源授权、重新下发）、资源与配额（资源池/规格/时长/节点四个页签） |
| 节点归属 | 见 §5.3「节点归属」：数据库 `compute_nodes.resource_group_id` 为权威，`wl-pool` 标签由同步物化，Pod 用 `nodeSelector` 读取；`is_default` 兜底保证归属完备，迁移 `e5b2a7c9d1f3` |

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
| 对象集 | Deployment（`Recreate` 策略，避免两份开发环境争抢同一个 home 卷）+ **两个 Service**（仅 SSH 为 NodePort，IDE 恒为 ClusterIP）+ PVC + `authorized_keys` Secret。*（本期还额外建一个 Ingress `/x/{id}/`；后已取消，见"IDE 暴露改为平台后端反代"）* |
| SSH | 用户自助登记公钥，指纹按 OpenSSH 格式（`SHA256:` + SHA-256(blob)）本地计算，不依赖 `ssh-keygen`；未登记则**不建 Secret 也不挂卷**，容器内 sshd 不接受任何密钥 |
| 终端 | `WS /ws/terminal/{id}`：二进制帧＝按键，文本帧＝控制（`resize`）。按类型而非内容分帧——嗅探 JSON 会在用户粘贴 JSON 时误判。容器侧阻塞套接字由工作线程驱动 |
| IDE 票据 | `GET /instances/{id}/ide-access` 签发 `typ=ide` 的短时 JWT，锁定 `instance` 与 `namespace`；与访问令牌**类型互斥**，无法互相冒用。**但入口处从未校验过它**——`decode_ide_ticket` 至今没有生产调用点，所以这张票据当时也没有真正保护任何东西。后来 IDE 改走平台后端反代，该接口不再签发票据、只返回 `/x/{用户名}/{id}/lab`；`create_ide_ticket` / `decode_ide_ticket` 保留且有测试覆盖，但没有调用者 |
| 配额 | 独立内核：实例数 / CPU / 内存 / 存储 / GPU(按型号) + 资源组公平上限（并发数、单实例卡数）。停止**不释放**配额——否则重启会因自己占用的容量而失败。*（**已被取代**：实例/应用/任务数量改由用户配额模板承担，资源池的四列改为 cpu 核 / 内存 GB / 加速卡张 / 存储 GB，见 §5.3）* |
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
- 对象集实测（`FakeK8sGateway`）：一次创建产出 1 Deployment / 2 Service / 1 Ingress / 1 PVC
  （其中的 Ingress 后已不再创建）；Jupyter 命令含 `--ServerApp.base_url=/x/{id}/`，当时的理由是
  "否则经 Ingress 后所有静态资源 404"——**"入口路径前缀必须与 IDE 的 base path 一致"这条结论仍然成立**，
  只是入口从 Ingress 换成了平台反代，`base_url` 也随之变成 `/x/{用户名}/{id}/`；
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
| 数据模型 | `applications`（可发布的可复用实例形态 + 介绍信息）、`tasks`（K8s Job）、`workload_events`（工作负载时间线）；`resource_group_limits` 新增 `max_concurrent_tasks`。*（该列后已删除，数量归用户配额模板，见 §5.3）* |
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

### ✅ Phase 5 — 镜像与存储（已完成）

**交付（后端）**

| 交付物 | 位置 |
| --- | --- |
| 存储/镜像枚举 | `app/models/enums.py`（`StorageKind`、`StorageStatus`、`LIVE_STORAGE_STATUSES`、`ImageKind`、`ImageVisibility`） |
| 数据模型 | `app/models/storage.py`（`Storage`、`InstanceStorage`）、`app/models/image.py` |
| 迁移 | `alembic/versions/d0d900699fb4_*.py`（新增 3 张表，head 共 **30** 张，`downgrade base` 可往返） |
| K8s 抽象 | `VolumeMountSpec`、`PvcInfo`/`PvInfo`、`read_pvc`、`apply_pv`/`read_pv`/`delete_pv`/`list_pvs`、`parse_storage_gb`、`build_storage_objects` |
| 服务 | `app/services/storages.py`、`app/services/images.py` |
| Schema | `app/schemas/storage.py`、`app/schemas/image.py` |
| 镜像仓库客户端 | `app/registries/`（`registry.py` 协议 + `harbor.py` 真实实现 + `fake.py`，与 K8s gateway 同构） |
| API | `app/api/v1/storages.py`（7 个端点）、`app/api/v1/images.py`（7 个端点） |
| 权限 | `storage:read/create/resize/delete/manage`、`image:read/create/share/delete`；本期新增 `storage:manage`，并把 `image:share` 从普通用户上收到组管理员（组管理员 **44** 项，普通用户 **32** 项） |
| 实例挂载 | `InstanceCreate.storageIds`、`TaskCreate.storageIds` → `build_instance_objects`/`build_task_objects(extra_mounts=...)`；属主用户卷隐式挂到 `/home/{用户名}` |
| 验收测试 | `tests/test_acceptance_phase5.py`（**33** 项）、`tests/test_acceptance_phase5_images.py`（**36** 项） |
| 冒烟脚本 | `scripts/smoke_phase5.py`（**65** 项，可重复执行） |

> **注**：本表是 Phase 5 当时的交付记录。其中**存储**部分此后被
> 「后续增量 — 存储卷重构（第一步）」重写：`StorageKind` 由五种收敛为三种，
> `resize_pvc` 与 `storage:create/resize/delete` 已删除，卷的身份改由 `host_path` 承担。
> 以 §5.4 与那个条目为准；这里保留原文是为了让"当时决定了什么"仍然可查。

**交付（前端）**

| 交付物 | 位置 |
| --- | --- |
| 类型 | `src/types/storage.ts`、`src/types/image.ts`（含 `STORAGE_KINDS` 元数据、`IMAGE_KIND_META`、`IMAGE_VISIBILITY_META`、`SELF_SERVICE_VISIBILITIES`） |
| API 客户端 | `src/api/storages.ts`（9 个函数）、`src/api/images.ts`（9 个函数） |
| 页面 | `src/views/StorageView.vue`、`src/views/ImagesView.vue`（已接入 `/storage`、`/images` 路由，不再是占位页） |
| 复用组件 | `src/components/image/ImagePicker.vue` |
| 表单接入 | `InstancesView`（镜像选择器 + 挂载存储多选）、`TasksView`、`ApplicationsView`（镜像选择器） |
| 状态词表 | `StatusChip` 补齐 `bound`/`resizing`/`released`/`timeout` |
| 契约校验 | `scripts/check-frontend-types.py` 覆盖接口由 **39** 增至 **51** |
| 视图冒烟 | `scripts/smoke_frontend_views.mjs`（**11/11** 视图挂载并渲染出数据） |

**验收证据**：`ruff check` + `ruff format --check` + `mypy app`（strict）全绿；
后端全量 **343** 项测试通过；Phase 1–5 冒烟（30/33/49/61/65）无回归，Phase 5 冒烟连续两次
执行均全通过；`kubectl get ns,pvc -l wl-managed=true` 为空（未触碰真实集群）。
前端 `vue-tsc --noEmit` 与 `vite build` 均通过；`scripts/check-frontend-types.py`
校验 **51** 个接口全部吻合（并已用植入错误反向确认它**真的**会失败，退出码 1）；
`scripts/smoke_frontend_views.mjs` 把 **11** 个视图逐个挂载进 `happy-dom`、等数据到位、
再断言渲染结果（同样用植入错误反向确认过）；
两个新页面调用的全部端点已对着运行中的后端逐条走通，`StorageDetail`/`ImageDetail`
的每个抽屉字段、分页信封 `{items, meta{page,pageSize,total,pages}}`、
`StorageUsage`/`ImageUsage`/`RegistryStatus` 的字段集合都已断言。

**关键设计决策**

1. **存储不再从属于实例**。`Storage` 是独立实体，PVC 名 `wl-vol-{name}-{id}`
   **不含实例信息** —— 否则删除那个恰好挂了它的实例就会把数据一起带走。
2. **可见性按 *类型* 分叉**，这是 `visible_condition()` 返回裸条件而非
   `policy.scope_filter()` 的原因：用户卷只有本人可见，而 `group_share`/`global_share`
   的可见范围不同 —— 同一张表上三类行的可见规则不同，行级 scope 表达不了。
3. **附加存储不重复计配额**。容量在**创建存储时**结算一次；实例挂载它时不再计入配额，
   否则同一个 GB 会被收两次费，工作空间会莫名撞上配额上限。第二步干脆把实例的
   `storage_gb` 整个删掉了 —— 一个"只是挂载已计费卷"的字段，留着就一定会有人再次把它加进
   用量汇总里。
4. **失败的操作 ≠ 失败的卷**。核对失败时容量与**状态都保持原样**，不标记 `FAILED` ——
   卷仍然可用，把它标红会让用户去追一个不存在的问题。
5. **只读同时落在 volume 和 mount 两处**，否则「只读」只是 API 响应里的一句话，
   训练任务照样能写穿。
6. **先查可见性，再描述对象**。`resolve_attachments()` 把可见性检查放在最前面：
   「该存储属于其他工作空间」「该存储已停用」这类消息都在确认对象存在，
   而看不见它的人不该知道它存在（对普通用户是 404，对管理员才是解释性的 400）。
   组本级卷是这条规则最尖锐的例子：它对普通成员**完全不可见**，正是因为它挂着每个人的
   个人目录 —— 报 403 就等于确认了它存在。
7. **`usage_by_owner` 统计的是「你被收费的」，不是「你能看见的」**。二者必须分开：
   一个组共享卷对五十个成员都可见、却不属于任何人，按可见性统计会让每个人都
   被告知自己占用了那 100 GB。全局管理员拿到的是全站总量（个人总量对他们没有意义）。

8. **镜像目录是 Harbor 的缓存，不是真相**。同步可以回收仓库不再报告的条目 ——
   但 `is_pinned` 除外：一个被精选的镜像从列表里消失，通常意味着 project 配错了，
   而不是决定不再提供它，静默下架会打断每一个引用了它的已保存应用。
   同步还**不会**动个人条目，因为它们本来就不属于共享目录。
9. **回收的判断依据是「这一趟是否成功」，而不是「这一趟是否看见了东西」**。
   仓库确实一个 repository 都没有并不算失败，用 `if not seen: return 0` 挡着会让
   真正的清理永远做不成；但一次出过错的同步看到的是残缺画面，按残缺画面删东西才是
   真正危险的。所以判断条件改成了 `if result.errors: return 0`。
10. **`image:share` 从普通用户上收到组管理员**。共享一个镜像等于替所有实验室背书
   他们将要拉取的东西，这不该是每个用户都能做的事。发布 `official` 或 `global`
   同样需要它。
11. **未命名用户组的 `group` 共享**：单组管理员可以直接推断出其用户组（答案没有歧义，
   再问一遍是纯摩擦）；但全局管理员不属于任何组，此时拒绝而不是猜 ——
   猜错会把镜像发到一个没人要求的地方。

**前端验证：为什么需要一个挂载型冒烟**

`vue-tsc` 证明模板类型正确，`vite build` 证明它能编译，但**两者都不会运行 `setup()`**。
一个视图可以同时通过这两关，然后在第一次绘制时崩掉：读了接口不发的字段、用不存在的键
去索引映射、把接口返回的形状搞错。这些正是会到达用户面前的缺陷，而编译器看不见它们。

`scripts/smoke_frontend_views.mjs` 因此把视图**真正挂载**起来：用 `vite build` 出一份
浏览器构建（不是 SSR —— 见下），在 `happy-dom` 里挂载，喂给桩化的 API 与预置的 store，
等数据落地，然后断言「夹具里的文本确实出现在 DOM 里」。最后一步是关键：
「没抛异常」是个很弱的结论，因为**数据没到的视图会渲染出一个完全合法的空状态**。

两条走不通的路（都试过，记下来省得再试）：

- **`renderToString`**：SSR 不执行 `onMounted`，而这里的每个视图都在 `onMounted` 里取数；
  重新渲染又会新建组件实例，所以取到的状态永远活不到第二遍。
- **`vite.ssrLoadModule` + DOM**：模块是按**服务端**编译返回的（`ssrRender`、
  `useSSRContext`），Vuetify 的客户端组件会因为缺少 SSR 上下文而失败。
  视图必须按浏览器构建。

它当场就抓到了一个真问题：`StorageView` 的表格**没有传 `:headers`**，
于是 `v-data-table` 按第一行的每个字段自动生成列 —— 页面上出现了 `pvcName`、
`k8sNamespace`、`workspaceId` 这些内部字段，而我写的 `capacity`、`owner` 两个插槽
**根本没有对应的列可渲染**，表格一直在显示原始值。类型检查和构建都发现不了这个。

同时它也暴露了一类断言陷阱：桩里的 `150 GB` 让期望值 `50 GB` **子串命中**，
植入的显示缺陷因此溜了过去。所以脚本额外维护一份「绝不能出现」的清单
（`undefined`、`NaN`、`[object Object]`），它不依赖夹具、也不依赖有没有人记得写期望值，
并且夹具取值刻意互不包含。

它还顺带纠正了脚本自身的一个假设：「列表类接口都返回分页信封」是错的
（`listWorkspaceGrants` 返回数组、`listWorkspaces` 返回 `Page`），
所以桩的默认形状改为**从其 `Promise<...>` 声明推导**，新端点不会再默默拿到错误的形状。

**前端设计决策**

12. **镜像选择器允许越过目录直接输入**。它的职责是让常见情况变成「选一下」、
   让少见情况变成「显式输入」，**不是**禁止少见情况。把输入锁死在目录里，挡住的
   恰好是那些在造目录里还没有的东西的人 —— 而他们正是最需要建实例的人。
   目录取不到时更是如此：选择器降级成文本框，表单不会因为一个 GET 失败而不可用。
13. **容量只能涨，所以扩容对话框的初值是 `当前值 + 10`**，且小于等于当前值的输入
   在校验层就被拒绝。预填当前值等于提供一个必然失败的默认值
   —— Kubernetes 无法缩小 PVC，接口拒绝它是对的，界面不该先邀请再拒绝。
14. **存储名创建后不可改**。它已经是集群里 PVC 名的一部分，改这里要么什么都不改，
   要么让记录和 PVC 对不上 —— 而一个名字与实体不一致的卷，比一个名字难看的卷糟得多。
15. **「已释放」的卷保留记录**。删除的是集群里的卷，不是表里的行：谁在什么时候
   释放了多少容量，是下一次有人问「我的盘去哪了」时唯一的答案。
    因此列表默认隐藏、`includeInactive=true` 可查，详情抽屉里的操作记录永不删除。
16. **删除被拒绝时，把实例名一起显示**。接口返回 409 并点名仍挂载它的实例，
   界面只显示「使用中」就把唯一有用的信息丢掉了 —— 用户需要知道的是**该去关哪个**。
17. **两个页面的统计卡都是「我被收费的」，不是「我能看见的」**（后端已如此实现，
    前端如实呈现）：组共享卷对五十个成员都可见却不属于任何人。

**本期踩到的坑**

- **`MissingGreenlet` 复发（与 Phase 3 的 `Workspace.group` 同类）**：`Storage.workspace`
  等关系是 `lazy="joined"`，但 `joined` 只对**从查询返回的**行生效；刚 `session.add()`
  的行关系是空的，序列化时触碰它就触发同步上下文的懒加载并抛错。已在创建路径显式
  赋值 `storage.workspace = workspace` 等，并写明原因。
- **`run_gateway()` 包错了对象**：它用 `asyncio.to_thread` 跑**同步**函数，包住一个
  `async def` 服务函数会返回协程而不执行 —— 扩容/删除会「成功」但什么都没发生。
  正确做法是服务内部**只**把阻塞的 gateway 调用丢进 `run_gateway`。
  （`mypy` 的 `unused-coroutine` 抓到了它。）
- `PvcSpec.medium` 从来就不该存在：PVC 没有 medium 字段（那是 emptyDir 的），已移除。
  同一次收敛里，`VolumeMountSpec.sub_path` 也删了 —— 「一个目录一个卷」之后挂载不再需要
  切分，留着它只会让两条路径可以表达同一件事（见「后续增量 — 存储卷重构」）。
- **自定义校验器会让请求变成 500**：Pydantic 在 `ctx` 里挂的是原始异常对象，
  `ValueError` 无法 JSON 序列化，于是**任何** `field_validator` 拒绝取值时都会 500 ——
  而这恰恰是最需要一条清晰 422 的场合。现已在 `_handle_validation_error` 里把异常
  换成它的 message 并做编码兜底。
- **`DataScope.group_ids` 是「我管理的用户组」，不是「我所属的用户组」**。
  用它来实现「共享给我的组」会让组共享**只对组管理员可见、对普通成员不可见**，
  也就是让这个功能对它真正服务的人失效。存储与镜像的 `visible_condition` 都已改为
  以 `actor.group_ids`（成员关系）为准，并并入管理范围。**排查时优先怀疑这里。**
- **唯一约束才是真正的仲裁者**：预先查重挡不住并发（两个请求都能通过检查，只有一个
  能插入成功），只有把 `IntegrityError` 翻译成 409 才能避免 500。存储创建已如此处理。
- 冒烟脚本原先不可重复执行（存储名唯一、个人主目录每用户唯一、镜像 reference 全局唯一）。
  现已改用每次运行的随机后缀 + 冲突时复用既有条目；删除后仍被占用的 reference 需要
  带 `includeInactive=true` 才能查到，再 PATCH 复活。

---

### ✅ Phase 6 — 用量中心（已完成）

**交付（后端）**

| 交付物 | 位置 |
| --- | --- |
| 枚举 | `app/models/enums.py`（`UsageKind`，刻意比 `WorkloadKind` 窄） |
| 数据模型 | `app/models/usage.py`（`UsageDaily`） |
| 迁移 | `alembic/versions/3f0dbd1b4617_*.py`（head 共 **31** 张表，`downgrade base` 可往返） |
| 服务 | `app/services/usage.py`（跨日切分、幂等重算、汇总/时序/分组/CSV） |
| Schema | `app/schemas/usage.py` |
| API | `app/api/v1/usage.py`（6 个端点：`summary`、`series`、`breakdown`、`meta`、`export`、`rollup`） |
| 权限 | 新增 `usage:manage`；`usage:export` 下发给组管理员（组管理员 **45** 项，普通用户 **32** 项，全局管理员 **69** 项） |
| 定时任务 | `app/workers/tasks/usage.py` + beat `rollup-usage`（每 20 分钟，回看 3 天） |
| 审计 | 中间件 `ACTION_OVERRIDES` 增加 `usage:rollup`；`usage:export` 由处理函数显式记录 |
| 验收测试 | `tests/test_acceptance_phase6.py`（**39** 项）、Phase 2 新增 1 项 |
| 冒烟脚本 | `scripts/smoke_phase6.py`（**61** 项，可重复执行） |

**交付（前端）**

| 交付物 | 位置 |
| --- | --- |
| 类型 | `src/types/usage.ts`（含 `USAGE_RANGES`、`rangeStart`/`todayUtc`、`formatGpuHours`、`formatRuntime`） |
| API 客户端 | `src/api/usage.ts`（6 个函数，含 `downloadUsageCsv`） |
| 图表宿主 | `src/components/common/EChart.vue`（生命周期 / resize / 降级） |
| 页面 | `src/views/UsageView.vue`（已接入 `/usage` 路由，不再是占位页） |
| 契约校验 | `scripts/check-frontend-types.py` 覆盖接口由 **51** 增至 **60** |
| 视图冒烟 | `scripts/smoke_frontend_views.mjs`（**12/12**，新增 `EXPECTS_ABSENT` 与 canvas 桩） |

**验收证据**：`ruff check` + `ruff format --check` + `mypy app`（strict）全绿；
后端全量 **386** 项测试通过；Phase 1–6 冒烟（30/33/49/61/65/61）无回归，Phase 5/6 冒烟连续两次全通过；
`kubectl get ns,pvc -l wl-managed=true` 为空。
前端 `vue-tsc --noEmit` 与 `vite build` 通过；契约校验 **60** 个接口吻合
（植入错误反向确认退出码 1）；视图冒烟 **12/12**（`UsageView` 的图表在 canvas 桩下
真的走完了 `setOption`，以「降级提示不出现」断言）。

**关键设计决策**

1. **计量的是「被占用」的时间，不是「在算」的时间。** 一个实例占着 A100 八小时，
   无论内核是否满载，这八小时里别人都拿不到这张卡 —— 这才是集群主人能用来规划的
   数字，也才是调度器能精确记账的数字。卡用得多满由 Prometheus 回答，
   那是监控集成的问题，不是这里的问题。
2. **按 UTC 日切分，而不是把整段算给开始那天。** 22:00 到 02:00 的作业是两天各两小时。
   这是这张表按天存在的**全部理由**：把整段算给开始日，会让「昨天用了多少」
   每天早上都是错的；而本地时区的切分会在一年里多算或漏算两个小时，
   出现在某一个月报表里，然后被归咎于数据。
3. **所有计量都是精确整数。** 浮点会累积误差，而一个"与分项之和差一点"的合计
   足以摧毁整份报告的可信度。所以列是 `gpu_seconds` / `cpu_milli_seconds` /
   `memory_mb_seconds`，展示单位（GPU·h、核·h、GB·h）在 API 层折算，从不入库。
4. **重算是替换，不是累加。** 删除窗口内的行再整体重写，没有增量计数器可以漂移。
   这既是「今天的数据每 20 分钟重算一次」的前提（今天还在长），也是「修好 bug 之后
   可以回填整个季度」的前提。`test_recomputing_a_day_does_not_double_it` 盯着它。
5. **统计口径是平台自己的记录，不是指标系统。** 调度器发出去的东西是已知的、
   不需要监控栈在线、也没法争辩；指标是个更好的**测量**，却是个更差的**记录**。
   所以 Prometheus 记账是**后续叠加**，不是替换（见 Phase 7）。
6. **进行中的负载按「到现在为止」计算。** 于是今天的行是临时的、会增长 ——
   这是预期行为，不是 bug；也因此 `covered` 要区分「没算过」和「算了但是零」。
7. **只有全局管理员能触发重算**（`usage:manage`）。它一次性重写所有租户的行，
   没有"只管本组"的版本。
8. **导出是独立权限，且单独审计。** 在页面上看到一个数字，和带着一份包含每位同事
   每日消耗的文件离开，是两件事；而且导出是这里唯一会离开浏览器的响应，
   中间件只记录变更类请求，所以由处理函数显式写审计。
9. **`group_id` 是快照。** 工作空间改挂用户组，不该把上一季度的用量一起搬走。
10. **空白天数补零返回。** 图表若把空闲的一周直接连成直线，读起来是「用量平稳」，
    与事实正好相反。补零在服务端做，任何消费者都不必记得这件事。
11. **删除的用户仍出现在报表里。** 已经发生的消耗不该因为账号注销而消失；
    标签退化为用户名或 `用户 #id`。
12. **导出 CSV 带 UTF-8 BOM。** Windows 上的 Excel 会把没有 BOM 的 UTF-8 CSV
    按系统代码页读，然后一份满是乱码的报表会被当成「坏了」退回来。

**前端设计决策**

13. **图表宿主是通用组件，不是用量页的一部分。** `EChart.vue` 负责 dispose、
    resize 与 option 变更三件事 —— 一个只 `echarts.init` 的封装会在每次路由切换时
    漏掉一个 canvas、一个 observer 和一条全局注册项，症状是"用得越久越卡"，
    而不是一个能指认的报错。
14. **Canvas 不可用时降级，而不是让页面崩掉。** 老浏览器、加固配置、无头渲染都可能
    拿不到 2D 上下文；图表画不出来时给出说明，而页面上其他位置的数值不受影响 ——
    这同时也是用量页能在 `happy-dom` 里被挂载的原因。
15. **`covered` 为假时在页面顶部明说。** 「没算过」和「算了但是零」在空图上长得一模一样，
    而一条横穿整月却从未被测量过的直线，会被读成「用量平稳」。
16. **管理员的「其中你自己」单独成卡。** 「这总量里有多少是我」是管理员看组报表的
    第一个问题，而一旦行被聚合过，这个答案在客户端就再也算不出来了 ——
    所以由服务端在 `selfTotals` 里单独返回；普通用户的 scope 本身就是自己，
    接口返回 `null`，页面不重复画一遍相同的数字。
17. **时间范围用固定档位（7/30/90 天），不用日期选择器。** 日期选择器会邀请出
    两年的区间，而那是会在演示现场把数据库压垮的查询；这三档又恰好是所有人真正会问的，
    所以这个约束不花任何代价。
18. **明细表显式声明 `:headers`。** 这是 Phase 5 那个 bug 的直接后果：
    不传 `headers` 时 `v-data-table` 会按第一行的每个字段自动建列，
    内部字段全露出来、而自定义插槽因为没有对应列而永不触发。

**本期踩到的坑**

- **`POST /users` 给组管理员的角色是全局范围、且不带用户组**（**Phase 1 起就存在的缺陷**）。
  结果是这个账号拿到了全部 **45** 项组管理员权限，而数据范围塌缩成 `self` ——
  成员、工作空间、配额、用量，每一个列表都是空的，响应里没有任何东西解释为什么。
  「有权限、没数据」读起来像产品坏了，而不是像配置错了。
  已在 `plan_role_assignments()` 修正：组范围角色按用户组**逐组**写入，
  并同步 `UserGroupLink.role_in_group`；不指定用户组则直接 400
  （`group_admin_requires_group`）—— 静默放宽成全局会给出远超请求范围的权限。
  **这个 bug 是被 `scripts/smoke_phase6.py` 抓到的，pytest 全绿也没发现它**：
  测试夹具一直用工厂函数（同时写了两种记录），只有走 HTTP 建号的路径才暴露。
- **同一条不变量当时只覆盖了「建号」一条路径**，其余四条改 `role_in_group` 或组范围
  角色的路径各自只动一种记录，于是它以镜像形式残留，而且两个方向都出现过：
  - 成员管理页 `PATCH /groups/{id}/members/{uid}` 设 `role_in_group=group_admin`
    只写了标记、没写角色 —— **看得到、做不了**，正是上一个缺陷的镜像。
  - 目录同步（`sync_directory` 与每次登录的 `sync_directory_groups`）删除成员时
    只删成员、留下组范围角色 —— 账号对**已经退出的组**继续持有组管理员权限，
    而成员列表里已经查不到这个人，属于更危险的方向。
  现收拢到 `app/services/group_admin.py` 一处，由四条路径共同调用：
  `role_in_group` 与组范围 `group_admin` 角色同增同减。只有 `group_admin`
  这一个角色会碰这个标记 —— 在组范围授予别的角色与「谁管理这个组」无关。
  两个调用端点（`user:assign_role`、`group:manage_member`）都只有 `global_admin`
  持有，所以双向同步不会被用来取得调用者本来没有的权限。
  回归测试在 `tests/test_group_admin_sync.py`。
- **`happy-dom` 的 `getContext('2d')` 返回 `null`**，ECharts 因此在 `init` 时抛错，
  整个用量页挂不起来。已给 `HTMLCanvasElement.prototype` 加一层极薄的 2D 桩：
  它的目的是让 `setOption` **跑起来**（这能抓出畸形 option），
  它对像素一无所知，也不假装知道。
- **`str.replace` 在已被格式化过的文件上会静默失配**。改 `smoke_phase6.py` 时
  两次替换没生效（一次是工作空间名没带上 run token，一次是响应头大小写），
  两次的表现都是"脚本行为不对但看不出哪一步没改"。**批量替换后必须 grep 确认**。
- **`dict(urllib` 的响应头是小写的**：`dict(response.headers)` 归一化成
  `content-type`，用 `headers["Content-Type"]` 取是取不到的 —— 与产品无关，
  但与"检查到底在测什么"有关。
- **24/7 运行的任务会让冒烟脚本第二次失败**：配额是按**用户跨工作空间**统计的，
  上一轮提交的任务因为没有 reconciler 一直处于 running。夹具名一律带上 run token
  （用户、工作空间、任务），只有用户组保持不变 —— 组不消耗配额。
- **`task.group_id` 是 NOT NULL**（`instances` 没有这一列，它的组在重算时才推导）。
  直接构造 `Task` 的测试必须显式给出。
- **`GET /resource-specs` 曾静默忽略 `search`**。它是唯一一个不支持搜索的列表接口，
  而**未知查询参数是被丢弃而不是被拒绝的** —— 请求返回 200 和一份未经过滤的完整列表。
  冒烟脚本因此读到 `items[0]`，以为自己拿到了刚建的「单卡 A100」规格，
  实际拿到的是一个 CPU-only 规格，然后在下游的 GPU 计量等式上失败。
  已补上 `search`（按 `name` / `display_name`，与 `list_resource_groups` 一致）并加测试；
  冒烟脚本也不再信任「第一行」，改为按名字**精确匹配**（`find_by_name`）。
  **教训：一个静默忽略的参数，比一个会报错的参数危险得多。**
- **模板表达式里不能写带类型标注的箭头函数**。`@click:row="(_e: unknown, row: {item: T}) => f(row.item)"`
  会被模板编译器当成语法错误（`TS1005`），vue-tsc 报的位置还落在属性行上，看不出是表达式的问题。
  改成脚本里的处理函数即可；顺带用 `unknown` + 收窄来兼容 `v-data-table` 各版本传参形状的差异。
- **`v-data-table` 不传 `:headers` 时按数据字段自动建列**，看起来「表格能出来」，
  实际上内部字段全都露在外面、而你写的 `#item.<key>` 插槽只要 key 不是真实字段名就永远
  不触发。任务/实例/存储三张表都显式声明了 `headers`，新增表格时务必照做。
- **`check-frontend-types.py` 的 `PAYLOAD_ALIASES` 静默漏检**：它的规则是
  `XxxPayload → XxxCreate/XxxUpdate`，而存储与镜像是按**动作**命名的
  （`StorageCreate`、`ImageRegister`、`ImageUpdate`），于是这几个请求体
  **一行都没被校验**，却在输出里只是安静地列进「未检查」。已补映射，
  并把 `RegistryStatus → RegistryStatusRead` 一并补上。
  **新增请求体类型后一定要看这一行计数有没有涨** —— 它不涨就说明映射没生效。

### ✅ Phase 7 — Grafana 监控集成（已完成）

**交付（后端）**

| 交付物 | 位置 |
| --- | --- |
| 配置 | `app/core/config.py`（`grafana_public_url` / `grafana_url` / `grafana_org_id` / `grafana_jwt_audience` / `grafana_jwt_ttl_seconds` / `grafana_datasource_uid`，`grafana_enabled` 属性） |
| 面板编目与签名 | `app/monitoring/grafana.py`（`DashboardSpec`、`DASHBOARDS`、`MonitorScope`、`dashboard_variables`、`build_grafana_token`、`build_embed_url`、`dashboards_for`） |
| 范围计算 | `app/services/monitor.py`（`watched_instances`、`resolve_scope`、`inventory`） |
| 读模型 | `app/schemas/monitor.py` |
| 接口 | `app/api/v1/monitor.py`：`GET /monitor/overview`、`POST /monitor/dashboards/{key}/embed` |

**交付（前端）**

| 交付物 | 位置 |
| --- | --- |
| 类型 | `frontend/src/types/monitor.ts`（含 `EMBED_REFRESH_MARGIN_SECONDS` 与三个标签函数） |
| API | `frontend/src/api/monitor.ts` |
| 页面 | `frontend/src/views/MonitorView.vue`（`/monitor` 已从 `PlaceholderView` 接出） |

**交付（部署）**

| 交付物 | 位置 |
| --- | --- |
| 面板生成器 | `deploy/grafana/build_dashboards.py`（**含越权自检，见下**） |
| 面板 JSON | `deploy/grafana/dashboards/{user,admin}/*.json`（四个 uid 与编目一致，有测试保证不漂移） |
| Provisioning | `deploy/grafana/dashboards.yaml`、`deploy/grafana/datasources.yaml` |
| 部署说明 | `deploy/grafana/README.md` |
| 同源反代 | `frontend/nginx.conf` 的 `location /grafana/` |

**验收证据**

| 项目 | 结果 |
| --- | --- |
| `pytest tests/test_acceptance_phase7.py` | 32 passed |
| `scripts/smoke_phase7.py`（Grafana 关闭） | **44/44** |
| `scripts/smoke_phase7.py`（Grafana 开启） | **53/53** |
| `ruff check` / `ruff format --check` / `mypy app` | 通过 / 通过 / 113 文件无问题 |
| `check-frontend-types.py` | 69 个接口比对 schema |
| `smoke_frontend_views.mjs` | **13/13** 视图挂载 |
| `nginx -t` | syntax ok（含 `/grafana/`） |
| 集群 | 未触碰（`kubectl get ns,pvc -l wl-managed=true` 为空） |

**关键设计决策**

1. **两层信息，地位不等。** 平台自己的 `instances` / `ComputeNode` / `GpuDevice` 记录是第一层，
   **没有 Grafana 也能用**；Grafana 只回答「此刻在干什么」，不回答「用掉了多少」。
   后者是 Phase 6 的 `usage_daily` 的职责。这条分工意味着 Prometheus 挂了、标签缺失、
   Grafana 根本没部署，页面都还有内容。
2. **Grafana 未配置是一种受支持的状态，不是故障。** `grafana_enabled` 就是
   `bool(grafana_public_url)`。关闭时 `enabled: false` + `disabledReason`，页面照常渲染清单与统计，
   并且**不下发任何面板**——给一个打不开的磁贴比不给更糟，它看起来像监控坏了。
3. **范围同时写进签名令牌和 `var-*` 参数。** 两处都要有：令牌是 Grafana 侧无法绕过的约束，
   `var-*` 是让面板一打开就落在正确范围上。只有令牌没有参数，面板会先显示全局再被过滤；
   只有参数没有令牌，改一下地址栏就绕过去了。
4. **管理员拿到的是「没有参数」，不是 `$__all`。** `$__all` 只对多值变量有意义，
   传给单值变量会静默地选中「空」。少一个参数让面板回落到它自己的 All 默认值，
   才是管理员要的效果。这个区别在测试里有单独一条断言。
5. **空范围发送字面量 `none`，绝不省略参数。** 工作空间就是命名空间，一个组共用一个。
   用户的面板之所以是「我的」，靠的就是 `var-pod`。省略它不会显示空，会显示整个实验室。
   没有已调度的 Pod 时发 `none`（匹配不到任何 Pod），失败方向是安全的。
6. **未知 key 与无权 key 都返回 404 `dashboard_not_found`。** 403 等于确认「这个面板存在」。
   报错文案只回显调用方自己传进来的 key，不泄露标题、uid 或任何编目信息（有测试断言）。
7. **`POST` 签发地址。** 它创建的是一份凭证，不是读取资源；`POST` 的语义让缓存和
   自动重试都不会意外地把它当幂等读操作处理。
8. **五个面板编目，四个面板文件。** `my-containers` 要 `monitor:read_self`，
   `lab-overview` 要 `monitor:read_group`，`gpu-fleet` / `node-health` 要 `monitor:read_all`。
   编目按权限过滤后**根本不出现在响应里**，前端不需要判断能不能点。
9. **`/monitor/overview` 是一个响应而不是四个请求。** 四个并发请求会在四个不同时刻被应答，
   拼出来的页面描述的是一个从未存在过的状态。
10. **`MonitorScope` 用两个独立布尔表达「无限制」与「空」。** 管理员和「没有任何容器的新用户」
    都得到空列表；把这两者合并，就会把全平台的面板发给一个刚注册的人。
11. **面板生成器拒绝写出会越权的面板。** `build_dashboards.py` 的 `verify()` 强制每条 `expr`
    含 `namespace=~"$namespace"`，有 `pod` 变量时还必须含 `pod=~"$pod"`，且这两个变量必须是
    `multi: True`（后端发的是逗号拼接的单值，只有多值变量才会被 Grafana 拆开）。
    **违规时在任何文件落盘之前就抛错**，不会留下半更新的目录。已用植入缺陷验证会退出 1。
12. **nginx 用变量而非 `upstream` 块反代 Grafana。** `upstream { server grafana:3000; }` 在 nginx
    启动时解析，名字不存在会**让 nginx 拒绝启动**，于是没部署 Grafana 的安装连 SPA 和 API
    都没有了。变量形式把解析推迟到请求期，坏掉的只有 `/grafana/` 这一个 location。
13. **前端在令牌到期前 60 秒重新签发当前面板。** Grafana 不会告诉 iframe 令牌过期了，
    它只会变成一个登录页，读起来就是「监控坏了」。只刷新当前面板，不为没人看的地址造凭证。

**本期踩到的坑**

- **`monkeypatch.setattr(settings, "jwt_private_key", "…")` 必须包 `SecretStr`。**
  直接塞 `str` 不会在设置时报错，而是在下一次登录时以
  `AttributeError: 'str' object has no attribute 'get_secret_value'` 的形式炸在
  `auth/tokens.py` 里，离现场很远。
- **`SecretStr` 的字段不能用普通字符串赋值**，`Settings` 不是 frozen 的，
  所以 `monkeypatch` 能改，但改的类型要对。
- **`workspace_members.user_id` 有 UNIQUE 约束** —— 这是「一个用户只属于一个工作空间」
  这条需求在数据库层的落实。写多命名空间范围的测试时不能给普通用户加第二个工作空间，
  要改用**组管理员**：他管理的组里可以有很多工作空间，这才是多命名空间范围的真实来源。
- **冒烟脚本断言「等于」在共享数据库上必然失败。** 前几期留下的实例仍归同一个管理员所有，
  所以「管理员看到 2 个容器」应写成「**包含**这 2 个」，而真正有分量的是
  **排除**断言：「组管理员的范围里不含乙组命名空间」。
- **冒烟环境跑在 `K8S_ENABLED=false` 上，所以没有 sweeper，实例会停在 `creating`
  且 `pod_name` 为 NULL。** 冒烟脚本用的是假网关，而进程内对账器只在
  `k8s_enabled` / `k8s_in_cluster` 为真时启动（否则它会把假网关编出来的状态写进真行）。
  所以断言要落在「`pods` 是列表」而不是「`pods` 非空」，Pod 锁定的行为放到 pytest 里
  用确定的数据覆盖。
  ⚠️ 别把这句话读成「停在 `creating` 是正常的」——在真正连着集群的部署里它曾经**就是**缺陷
  （对账的写入被回滚，见「后续增量 — 状态永远停在「创建中」」）。区别在于谁在跑、以及
  它写不写得进去。
- **`GET /resource-specs` 返回裸列表、`/resource-groups` 返回信封** —— 两个列表接口的形状不同，
  冒烟脚本的 `rows_of()` 两种都要能处理。

### ✅ Phase 8 — API 开放、审计与上线（已完成）

**交付（后端）**

| 交付物 | 位置 |
| --- | --- |
| 时间工具 | `app/core/time.py`（`utc_now` / `as_aware` / `is_past`，见「本期踩到的坑」） |
| 审计查询 | `app/services/audit.py`（`AuditFilters`、`_base_query`、`list_audit_logs`、`export_rows`、`to_csv`、`distinct_values`） |
| 审计读模型 | `app/schemas/audit.py` |
| 审计接口 | `app/api/v1/audit.py`：`GET /audit-logs`、`GET /audit-logs/meta`、`GET /audit-logs/export`（**只有 GET**） |
| 令牌服务 | `app/services/api_tokens.py`（`create_token` / `list_tokens` / `revoke_token` / `resolve_token` / `touch`） |
| 令牌接口 | `app/api/v1/api_tokens.py`：`GET` / `POST` / `DELETE /api-tokens[/{id}]` |
| 登录限流 | `app/auth/rate_limit.py`（`enforce_login_rate_limit`、`record_login_failure`） |
| 权限上限 | `app/auth/policy.py` 的 `permission_ceiling()` / `set_permission_ceiling()` / `current_permission_ceiling()` |
| 三角色守卫 | `app/core/deps.py` 的 `_resolve_api_token`，`request.state.auth_method="api_token"` |
| 新权限码 | `api_token:read` / `api_token:create` / `api_token:delete`（归入 observer 基线，见下） |
| 错误类型 | `app/core/errors.py` 新增 `TooManyRequestsError`（429 / `rate_limited`） |

**交付（前端）**

| 交付物 | 位置 |
| --- | --- |
| 令牌类型与 API | `frontend/src/types/apiToken.ts`、`frontend/src/api/apiTokens.ts` |
| 令牌组件 | `frontend/src/components/profile/ApiTokensCard.vue`（挂在 `/profile` 下） |
| 审计类型与 API | `frontend/src/types/audit.ts`、`frontend/src/api/audit.ts` |
| 审计页面 | `frontend/src/views/admin/AuditView.vue`（`/admin/audit` 已从 `PlaceholderView` 接出） |
| 动作标签 | `ACTION_LABELS`：47 个常用动作码的中文名，未收录的回落到原码 |

**交付（部署）**

| 交付物 | 位置 |
| --- | --- |
| Helm chart | `deploy/helm/kube-wl-ai/`（Chart/values/README/`values-dev.yaml`/`values-prod.example.yaml`） |
| 工作负载 | `templates/deployment-{api,worker,beat,web}.yaml`、`templates/migrate.yaml`（pre-upgrade hook） |
| RBAC | `templates/rbac.yaml`（**含本期最重要的一段说明，见下**） |
| 准入策略 | `templates/admission-note.yaml`（以 ConfigMap 内注释形式给出，不自动安装） |
| 网络策略 | `templates/networkpolicy.yaml`（默认拒绝 + 三条放行） |
| 密钥生成 | `scripts/helm-gen-keys.sh`（RS256，私钥不落盘） |
| 冒烟脚本 | `scripts/smoke_phase8.py`（**60 项**） |

**验收证据**

| 项目 | 结果 |
| --- | --- |
| `pytest`（全量） | **461 passed** in 664.68s（其中 Phase 8 新增 43） |
| `scripts/smoke_phase8.py` | **60/60**，连跑两次均通过（可重复） |
| `ruff check` / `ruff format --check` / `mypy app` | 通过 / 通过 / 121 文件无问题 |
| `check-frontend-types.py` | **74** 个接口比对 schema |
| `smoke_frontend_views.mjs` | **18/18** 视图（此前只覆盖顶层视图，见「本期踩到的坑」） |
| `vue-tsc --noEmit` / `vite build` | 通过 / 通过 |
| `helm lint` | 1 chart, 0 failed |
| `kubectl apply --dry-run=server` | 全部对象通过（真实 1.28 API Server） |
| 集群 | 未触碰（只做 `--dry-run=server`） |

**关键设计决策**

1. **登录限流的计数器就是审计表。** 不引入 Redis：`auth:login` 且 `status=denied` 的行数
   就是失败次数。这样限流**重启后仍然有效**，且每一次计数都能在审计页面里指给用户看
   ——「你被限流了，这是那 10 次失败」是可以回答的。用 Redis 计数器，这个能力就没了。
2. **成功登录把窗口重新计时。** 窗口下界取 `max(上次成功时间, 当前时间 - 窗口长度)`。
   否则一个人正常登录后，前 14 分钟里的 3 次手误会在第 15 分钟突然凑成 429。
3. **`record_login_failure` 自带会话并立即提交。** `get_session` 在任何异常时回滚，
   而 `UnauthorizedError` 正是要抛出给中间的。若沿用请求会话，这条审计行会被回滚掉，
   于是用户名永远为空、按账号的计数永远是 0——**限流会静默地只按 IP 生效**。
   这个 bug 真的出现过，症状是「怎么也到不了 429」。
4. **限额触顶后才写审计的 429 行。** 未触顶的失败由处理函数写、并置
   `request.state.audit_recorded`，让 `AuditMiddleware` 跳过重复写入。一次尝试只有一行。
5. **限流先于 `authenticate()`。** 否则每次被限流的请求仍会去打一次 LDAP，
   把对目录服务的暴力破解放大成对目录服务的 DoS。
6. **令牌的权限是「与当前角色取交集」，每次请求都算。** 令牌不存权限快照
   （除非显式声明了 scopes）。因此**撤掉一个人的角色，他手里的令牌立刻变弱**，
   不需要去吊销令牌。这是「令牌不能成为权限的避难所」这条要求的落地方式。
7. **令牌创建令牌时，把可委派集合显式写下来。** 一个自身受限的令牌不能再造出
   「继承我」的子令牌——否则子令牌会以父令牌**所有者**的完整角色去解析，把父令牌的
   限制洗掉。有冒烟断言：父令牌 `["api_token:create", "instance:read"]` 造出的子令牌
   恰好是这两个，且读不了存储。
8. **观察者也能持有令牌，这不违背「只读」。** 令牌自身不带权限，它只是个身份。
   观察者的令牌依旧是只读的（冒烟里两条断言）。收回这个能力不会让人更安全，
   只会把人逼去把人类密码贴进脚本。
9. **密钥只在创建响应里出现一次，审计里只有前缀。** 列表接口根本不返回明文
   （`ApiTokenRead` 与 `ApiTokenIssued` 是两个类型，前端要拿 `token` 字段就必须显式声明）。
   前端据此把创建对话框做成两态：签发成功后**不关闭**，直到用户复制过才允许离开。
10. **令牌的作用域永远不改变数据范围。** `scopes` 决定「能不能做这个动作」，
    `DataScope` 决定「能看见哪些行」，两者是正交的。一个带 `audit:read` 的令牌
    不会因为持证人恰好是全局管理员就看见全平台——有测试与冒烟各一条断言。
11. **吊销是 404 而不是 403。** 别人的令牌 id 与不存在的 id 应当无法区分。
12. **审计表按构造不可写。** `/audit-logs` 只有 GET 路由，没有删除、没有清空、没有补录。
    这不是「前端没放按钮」，是路由表里就没有。
13. **未认证的审计行只有全局管理员看得见。** 失败登录行的 `actor_user_id` 是 NULL，
    用户名是**尝试者填写的**、未经证实的字符串。让组管理员按它过滤，等于让任何人
    猜一个名字就能把记录塞进别的组。代价是组管理员看不到针对自己成员的爆破，
    必须向全局管理员要——这个取舍写进了测试的 docstring。
14. **审计导出与列表共用同一条带范围的语句。** `_base_query` 只有一个实现，
    导出不可能包含列表里看不见的行。导出上限 `MAX_EXPORT_ROWS = 50_000`，
    截断时**在 CSV 末尾追加一行说明**，而不是安静地少给几行。
15. **CSV 前置 UTF-8 BOM。** 不加的话 Excel 会把中文显示成乱码，
    而「导出的表在 Excel 里是乱的」会被当成数据问题去查。
16. **Helm 的 `_validate.tpl` 在渲染期就拒绝半成品生产安装。** `helm template` 也跑这些检查，
    所以缺 `secretKey`、缺数据库口令、缺 JWT 密钥、LDAP 开着却没填 URI——
    都在装到集群之前失败。一个「看起来装好了」但用着占位口令的系统不会自己报警。
17. **迁移用 hook 而不是 initContainer。** initContainer 每个副本各跑一次，
    两个副本会去抢同一个 Alembic revision；失败时表现为应用 CrashLoop，
    而不是一次失败的发布。hook 只跑一次，且非零退出**会阻塞升级**。
    迁移任务里还有 `python -m app.cli sync-rbac`——**新增权限只存在于代码里**，
    不同步到数据库就没有任何角色持有它。
18. **`beat` 用 `Recreate` 且只有一个副本。** 两个 beat 会各自看到同一份计划并各自触发。
    滚动更新期间宁可短暂没有 beat（下一个 tick 会补上），也不要同时有两个。
19. **内置 PostgreSQL 是便利，不是生产方案。** 单副本、无备份、无故障转移。
    `secrets.databaseUrl` 才是正经用法，`values-prod.example.yaml` 里默认就把它关掉了。
20. **`helm uninstall` 不删数据。** Secret（数据库口令与 JWT 私钥）和两个 PVC 都带
    `helm.sh/resource-policy: keep`。一次误卸载不该带走一学期的用量记录。
21. **工作空间命名空间的 RBAC 是本 chart 唯一无法兼顾「安全」与「可移植」的地方。**
    详见下一条。

**本期踩到的坑**

- **SQLite 返回不带时区的 `datetime`，PostgreSQL 返回带时区的。** 两者相减直接
  `TypeError: can't subtract offset-naive and offset-aware datetimes`。这不是测试环境
  的怪癖，而是「开发用 SQLite、生产用 PG」这条常规路线上的真实分歧。修法是
  `app/core/time.py` 的 `utc_now()` / `as_aware()` / `is_past()`，所有时间比较都走它。
- **`export_rows` 的上限原本是默认参数**（`limit: int = MAX_EXPORT_ROWS`），
  于是 `monkeypatch.setattr(module, "MAX_EXPORT_ROWS", 1)` 完全不起作用——
  默认参数在函数定义时求值。改成 `limit: int | None = None` 并在调用时读模块全局。
- **`client.headers["Authorization"] = None` 不会清除请求头。** httpx 会把 `None`
  序列化成字符串 `"None"`，于是请求带着 `Authorization: None` 发出去，
  测试验证的是另一回事。要用 `client.headers.pop("Authorization", None)`。
- **`test_observer_is_read_only` 抓住了真实的设计冲突。** 它断言 observer 不含任何
  `:create/:update/:delete`，而 Phase 8 有意给了所有角色 `api_token:create|delete`。
  处理方式不是删断言，而是把它改写成**表达原本意图**：列出被豁免的两个码，
  并断言「除这两个之外没有任何会导致变更的码」——将来误授第三个码仍然会失败。
- **`smoke_frontend_views.mjs` 的视图发现是「不递归的 `readdirSync`」，`views/admin/` 下
  五个页面从未被挂载过**，而脚本仍然报告「13/13 视图挂载成功」。
  一个悄悄缩小了自己范围的检查比没有检查更糟：两种情况都报成功，所以没人去看。
  改成递归后发现 `UsersView` 的夹具一直带着 API 从未发送过的 `groups` 数组、
  又缺少 `fullName`，姓名列渲染成「—」。夹具与断言一起修正，现在是 **18/18**。
- **`concat` 遇到 nil 会 panic。** `concat .Values.image.pullSecrets .Values.imagePullSecrets`
  在 `web.image.pullSecrets` 未定义时直接
  `error calling concat: invalid memory address or nil pointer dereference`。
  用 `concat (default (list) …) (default (list) …)`。
- **NetworkPolicy 的 `podSelector` 缩进少了两格**，`include | nindent 4` 把标签打到了
  `matchLabels` 同一级。渲染出来是合法 YAML、看着也对，**只有 `--dry-run=server`
  的严格解码才会报 `unknown field "spec.podSelector.app.kubernetes.io/name"`**。
  这正是服务端 dry-run 值得跑的原因。
- **`subjects[].namespaceSelector` 在 Kubernetes 1.29 才出现（alpha），1.31 才默认开启。**
  本集群是 **1.28**，API Server 直接
  `strict decoding error: unknown field "subjects[0].namespaceSelector"` 拒绝整个绑定
  ——不是优雅降级，是整个对象不可用。原先的无条件写法让 chart **根本装不上目标集群**。
  现在按 `.Capabilities.KubeVersion` 分支：≥1.31 用标签选择器；<1.31 必须显式
  `rbac.allowClusterWideWorkspaceAccess=true`，并在 `NOTES.txt` 里把后果说清楚。
  **`helm template` 不带 `--kube-version` 时用的是 Helm 二进制内置的版本，不是集群的**，
  所以本地渲染验证两者都要跑。
- **`randAlphaNum` 每次求值都重新生成。** 数据库口令若被两处 `include` 各自生成一次，
  Secret 里的 URL 和 PostgreSQL 的 `POSTGRES_PASSWORD` 会是两个不同的值。
  现在在 `secret.yaml` 里解析一次，并用 `lookup` 复用已存在的 Secret
  （`initdb` 时写进数据目录的口令不能变）。

**工作空间 RBAC：本 chart 唯一无法兼顾「安全」与「可移植」的地方**

目标是一个「只能在自己创建的那些命名空间里活动」的 ServiceAccount。Kubernetes 有两种表达方式：

1. **subjects 上的 `namespaceSelector`（≥ 1.31）**：一条绑定，按 `wl-managed=true` 标签匹配。
   语义完全正确，不需要任何按命名空间的对象。**≥1.31 时自动使用这条路径。**
2. **在每个工作空间里写一条 RoleBinding**：在创建命名空间的同一时刻创建。任何版本都能用，
   但意味着**应用自己去写 RBAC 对象**——那是产品改动，不是 chart 改动。本 chart 没有采用。

于是 <1.31 时只能集群范围授予，且必须显式打开开关。后果是实打实的：
平台的服务账号可以读取、进入**所有**命名空间里的 Pod。这不是被忽略的细节，
它写在 `templates/rbac.yaml` 的块注释里，也会在每次安装的 `NOTES.txt` 里打印出来。

另有两条规则**任何版本都无法用 RBAC 限定**：`namespaces` 是集群作用域对象，
subject 选择器对它无从匹配。因此 `-namespace-lifecycle`（含 `delete`）与
`-workspace-namespaced`（<1.31 时）被单独拆成独立的 ClusterRole，
让 `kubectl get clusterrole` 一眼能看见这份权力，而不是埋在四十条规则里。
补上这个缺口的是 `templates/admission-note.yaml` 里那份
ValidatingAdmissionPolicy——它**不自动安装**，因为它是集群级生效的，
一次 `helm install` 不该顺手改掉整集群其他租户的命名空间删除行为。

> 每期必须可运行并**通过验收**后才进入下一期。

### ✅ Phase 9 — 收尾：最后两块占位页面（已完成）

Phase 8 结束时，`config/navigation.ts` 里还有两个入口指向 `PlaceholderView`。
它们不是「尚未排期」，而是**已经排期但会出错**，所以必须处理掉：

- **`/compute`（计算资源）** 被 `roles: WRITE_ROLES` 挡住，但页面的内容
  （节点清单）需要 `node:read`，而 `user` 没有这个权限——普通用户点进去只会看到占位符，
  一旦接上真实接口就是 403。同时它和 `/admin/resources` 的「集群节点」页签完全重复。
- **`/admin/settings`（系统设置）** 在后端**根本没有对应接口**，却挂在一个真实权限码
  `system:config` 下面。

**交付（后端）**

| 交付物 | 位置 |
| --- | --- |
| 版本单一来源 | `app/core/version.py`（`VERSION` / `APP_NAME` / `build_info()`） |
| 系统信息服务 | `app/services/system.py`（`_features` / `_warnings` / `build_info`） |
| 系统信息读模型 | `app/schemas/system.py`（`SystemInfo` / `SystemFeature` / `SystemWarning` / `SystemCounts`） |
| 系统信息接口 | `app/api/v1/system.py`：`GET /system/info`（**只有读，没有写**，见「关键设计决策」） |
| 验收测试 | `tests/test_acceptance_phase9.py`（**20 项**） |
| 冒烟脚本 | `scripts/smoke_phase9.py`（**23 项**，可重复） |

**交付（前端）**

| 交付物 | 位置 |
| --- | --- |
| 计算资源页面 | `frontend/src/views/ComputeView.vue`（`/compute`，不再有角色门槛） |
| 系统设置页面 | `frontend/src/views/admin/SystemView.vue`（`/admin/settings`，`global_admin`） |
| 系统信息类型与 API | `frontend/src/types/system.ts`、`frontend/src/api/system.ts` |
| 占位页删除 | `frontend/src/views/PlaceholderView.vue` **已删除**（不再有任何路由引用） |
| 死代码清理 | `WRITE_ROLES`、`RouteMeta.phase` 一并移除（全站已无占位页） |

**验收证据**

| 项目 | 结果 |
| --- | --- |
| `pytest`（全量） | **481 passed**（Phase 9 新增 20） |
| `scripts/smoke_phase1..9.py` | 干净库上按序 **9/9 全绿**，共 428 项 |
| `ruff check` / `ruff format --check` / `mypy app` | 通过 / 通过 / 125 文件无问题 |
| `check-frontend-types.py` | **78** 个接口比对 schema（Phase 9 新增 4） |
| `smoke_frontend_views.mjs` | **19/19** 视图，已无占位页 |
| `vue-tsc --noEmit` | 通过 |

**关键设计决策**

1. **系统设置页只读，且这是设计而不是没做完。** 运行时配置来自环境变量，在启动时读一次。
   一个能编辑它的表单只有两种可能：改了一个进程已经缓存的值（页面在说谎），
   或者需要一个它没有能力触发的重启。真正的接口是 ConfigMap 和 chart，
   这个页面的职责是告诉管理员该往里写什么——所以每条告警都带 `remedy` 字段。
2. **settings 对象绝不整体序列化。** `app/services/system.py` 逐字段拷贝，
   于是「新增一个 Secret 配置项」不会自动出现在页面上——它要出现必须有人写一行代码，
   而那是一个决定。验收测试用**金丝雀值**（`CANARIES`）扫描整个响应体，
   而不是逐个字段断言：真正的泄漏往往发生在没人想到的地方（比如某个 feature 的
   `detail` 里），逐字段检查只覆盖写测试的人当时想到的那些。
3. **告警列表才是这个页面存在的理由。** 打印配置的页面没人看；说「
   `AUTH_PROVIDERS=ldap` 但 `LDAP_URI` 为空，于是所有目录账号都会登录失败，
   而错误看起来像密码错误」的页面回答了管理员真正带着的那个问题。
   因此每条告警三个字段：`detail` 说后果，`remedy` 说改哪里。
4. **不写「生产环境 SECRET_KEY 过短」这条告警。** `Settings._guard_production_secrets`
   已经让进程在生产环境用弱密钥时**直接启动失败**。为一个进程根本无法处于的状态写告警，
   是读起来像活控件、实际上是死代码的东西。
5. **`/compute` 不加权限，而是分两段渲染。** 资源池所有人都看得到（`resource_group:read`
   在 user 基线里），节点清单只对持有 `node:read` 的人渲染。给普通用户补 `node:read`
   也能让页面「看起来正常」，但那是在为一个导航项扩大权限面。页面明说自己展示了什么、
   为什么——而不是让读者猜某个页签是不是加载失败了。
6. **规格用展开行懒加载，而不是链到管理页。** 列表接口只带 `specCount`，
   展开时再取详情——每行都预取就是每次进页面都为没人展开的东西发一轮请求。
   这里原本写的是「查看规格 →」指向 `/admin/resources`，而那是个**管理员页面**：
   普通用户点进去会被拦住，正是本次要修的那类 bug。

**本期踩到的坑**

- **Phase 1 冒烟用 `len(permissions) == 45` 断言组管理员权限数**，
  Phase 4/5/6/8 各让它失败一次，每次的修法都是把数字改大。
  **一个每期都要改的魔数在教错误的反射**：让测试变绿最快的方法就是改期望值，
  而这恰好是放行一次意外扩权的习惯。改成结构性断言：`组管理员 ⊋ 普通用户`、
  `⊆ 全局管理员`、`⊊ 全局管理员`，外加一份**全局专属权限黑名单**。
  普通用户基线从 `GET /roles/user` 读，而不是新建一个一次性账号——
  为了读一份服务器已经公布的静态映射而制造状态，是自找麻烦。
- **`audit:read` 一度被写进那份黑名单**，而组管理员**本来就该有**它
  （只能看本组的审计行，由 DataScope 收窄）。断言差点反过来把设计钉死。
  黑名单现在只列真正的全局专属权限，并在注释里写明为什么 `audit:read` 不在里面。
- **`smoke_frontend_views.mjs` 的 `RESOURCE_GROUP` 夹具是过期的**：
  它带着 `nodeSelector` 和一套编造的 `limits` 字段名（`maxGpuCountPerUser` 等），
  与 `ResourceGroupSummary` 完全不符。没有报错，因为没有任何视图的断言碰到那些字段
  ——**这正是问题所在**：夹具与 API 不一致，就是一个抓不到「视图读了服务器从不发送的字段」
  的桩。已按 `types/resource.ts` 重建。
- **夹具会掩盖「角色门槛」这类缺陷。** 挂载时只从一份基线播种权限，
  于是每个页面都以同一个身份渲染，`node:read` 分支根本测不到。
  给 `smoke_frontend_views.mjs` 加了 `VIEW_PERMISSIONS`（按视图追加权限码），
  这项检查才第一次能覆盖角色分支。
- **`SecretStr` 在赋值时不校验。** 把 `settings.jwt_private_key` 直接赋成 `str`，
  要到第一次 `.get_secret_value()` 才炸，而那时堆栈在令牌签名里，与测试毫无关系。
  另外**不要为了造金丝雀去改 `secret_key`**：它给访问令牌签名，
  改掉之后 `admin_client` 手上的令牌立刻失效，测试会以一个和泄漏无关的 401 失败。
  改成断言**真实的**签名密钥不出现在响应里——这本来就是更强的检查。
- **同一份夹具里不要改 `database_url`。** `engine` fixture 用它建测试库，
  改成 Postgres DSN 会让整套测试指向一个真实数据库。
- **`test_the_version_matches_the_health_probe` 顺手抓到一个真实重复**：
  `VERSION = "0.1.0"` 同时写在 `app/main.py` 和 `app/api/v1/health.py` 里。
  已收敛到 `app/core/version.py`——一个能和自己不一致的版本号比没有版本号更糟。

### ✅ 后续增量 — 节点归属与资源池分区（第一步，已完成）

把「资源池」从**配额单位**变成**调度目标**。此前 `ResourceGroup` 与节点之间没有任何关联，
`k8s_label_selector` 由管理员手填且默认是 `{}`，所以每个资源池实际上匹配整个集群 ——
"任务跑在哪个池"只是一种说法，不是集群层面的事实。

**语义**（详见 §5.3）：

- `compute_nodes.resource_group_id` 是唯一权威；节点标签 `wl-pool` 是它的物化副本；
  Pod 的 `nodeSelector` 读取标签。`nodeSelector` 是硬性要求，所以两者分歧只会让 Pod
  `Pending`，**不会**把任务放到错误的机器上 —— 失败方向是安全的。
- `resource_groups.is_default` 标记兜底池，第一个创建的资源池自动成为默认池，
  保证"每个节点都有归属"这条不变式（归属是**完备**的，不存在"不属于任何池"的节点）。
- 同步只在标签与数据库不一致时才写（稳态下零写请求）。收敛规则见 §5.3 的表格。
- 节点改派走 `PUT /resource-groups/{id}/nodes`（替换整个集合语义），被移除的节点移入默认池。
- 工作负载下发时由 `pool_node_selector()` 注入 `wl-pool`，且**池自己的选择器不能覆盖它** ——
  否则一个池可以把自己指到别的池的机器上，分区就漏了。

**交付物**：迁移 `e5b2a7c9d1f3`（`resource_groups.is_default` +
`compute_nodes.resource_group_id` + 索引 + `ON DELETE SET NULL` 外键）·
`K8sGateway.patch_node_labels()`（真实实现走 strategic-merge patch）·
`app/workers/tasks/nodes.py` + beat `sync-nodes`（`NODE_SYNC_INTERVAL_SECONDS`，默认 120s）·
`tests/test_node_pool_membership.py`（20 项）· 前端资源池页签的节点列/默认池开关/节点改派弹窗。

**本轮修复的既有缺陷**（都是新测试翻出来的，不是新引入的）：

1. **`group_to_summary()` 里的 `group.group.name` 是一次未预期的懒加载**。
   `ResourceGroup.group` 是 `lazy="joined"`，查询出来的对象没问题；但
   `create_resource_group()` 新建的对象上这个关系从未被赋值，读它就触发一次 IO。
   异步 SQLAlchemy 下这会抛 `MissingGreenlet` —— **除非**父组恰好在同一个 session 的
   identity map 里（`_ensure_group_exists()` 用 `session.get()` 会把它放进去），
   此时懒加载走内存、什么都不会发生。也就是说这个 bug 只在缓存没被焐热时出现：
   现场表现为**时有时无的 500**。已改为赋值关系而非外键（`_resolve_owning_group()`）。
2. **`update_resource_group()` 会留下过期的父组**。它用 `setattr(group, "group_id", ...)`
   改外键，而该对象是查询出来的、`group.group` 已经指向**旧的**父组，于是改挂用户组之后
   响应里返回的仍是旧组名。这是第 1 条的反面：一个是读不到（报错），一个是读到错的
   （静默错误，更糟）。同样改为赋值关系，并让 `groupId: null` 真正表示"改成全体可用的池"。

> 第 1 条在改动前就存在（`git show HEAD` 可见 `group_name=group.group.name` 原样），
> 属于**被发现的**旧缺陷。它和第 2 条是同一个设计错误的两面：把关系当外键用。

**尚未做（第二步，用户明确要求先不做）**：节点移除流程（`cordon → drain → 重新归池`，
复用一直只读不写的 `compute_nodes.schedulable`）、`FailedScheduling` 事件的前端呈现。
另外**不引入 taint**：硬隔离会把"标签短暂落后"从"任务 Pending 但可自愈"变成"卡死"，
而它防住的情况本来就不该发生。

### ✅ 后续增量 — 存储卷重构（第一步，已完成）

把存储从「按需分配的空间」改成「**登记一个已经存在的目录**」。

**为什么是重写而不是补丁**：需求是每个组一个存储目录、每人在其下一个子目录、组另有共享目录，
即 `/storage/kube-wl/{组}/{用户名}` 与 `/storage/kube-wl/{组}/{共享名}`。原实现里 `Storage`
是一块平台**动态**申请的 PVC（`storage_class` + `medium` + `size_bytes`），**没有任何字段能
表达"这个卷对应哪个目录"** —— 系统里并不存在"用户的存储目录"这个概念。这不是实现差异，是
模型错误。

**新模型**（详见 §5.4）：

- 三种粒度：`user_volume` / `group_share` / `global_share`。旧的 `personal_home` /
  `personal_data` / `dataset` / `object` 删除 —— 它们的区别是**用途**，而用途不是存储的属性。
- `host_path` 是身份，`dir_name` 区分「组共享目录」与「组本级目录」（`dir_name IS NULL`）。
- 目录由 `Settings.storage_root` 推导，**不接受手填**：手填意味着两行可以指向同一个目录，
  而"两行同一个目录"在集群里表现出来就是 Pod 挂到别人的数据上。
- 集群侧是**静态** `hostPath` PV + 显式绑定的 PVC（`storageClassName: ""` + `volumeName`），
  回收策略 `Retain`。已在真实集群验证：PV 与 PVC **立即** `Bound`。
- 容量**不再承诺**：`hostPath` 不强制容量，静态 PVC 也**不能扩容**
  （实测报 `only dynamically provisioned pvc can be resized`）。所以 `resize_storage`
  只改账面，并且界面上这么写。

**权限收敛**：`storage:create|resize|delete` 三个码删除，只留 `storage:read` 与
`storage:manage`，后者**只给全局管理员**。普通用户（含组管理员）只有读 —— 目录布局是安装级的
决定，而且写错目录不会被自动纠正，"谁能用哪个目录"不是组管理员能决定的事。角色 × 权限矩阵
已在测试里钉死。

前端也跟着收：存储页的"操作"列对读者**整列不渲染**（不是禁用按钮），并且视图冒烟脚本新增
`VIEW_PERMISSIONS_ABSENT` —— 它以**读者**身份再挂载一次这个页面，断言"新建存储"与"操作"
都不在 DOM 里。只测管理员形态会漏掉这类缺陷：界面上的禁用按钮和会 403 的按钮，对用户是同一
件事。

**顺带修掉的一个既有缺陷**：`seed_rbac()` 过去只**新增和更新**权限行，从不删除。于是删掉的
权限码会永远留在 `permissions` 表里，而**两处都读这张表** —— `GET /rbac/permissions` 用它
渲染管理员的权限矩阵，`effective_permissions()` 用它解析用户实际持有的权限码。也就是说一个
已废弃的码仍然可以被授予、被持有，而**没有任何端点再要求它**：看着像一个能力，实际什么都不
给。现在会删除目录里已经不存在的权限行（`Permission` 只由 `seed_rbac` 创建，`/rbac/permissions`
是只读的，所以这不是谁的手工扩展），并计数进 `SyncResult.permissions_removed`。存储这次
实测清掉了 3 行（`storage:create/resize/delete`），权限总数 70 → 67。

**踩到的坑**（都是新测试翻出来的，不是新引入的）：

1. **PV 的 `Bound` 早于 PVC 的 `Bound`**。volume controller 先把 PV 标 Bound，claim 自己的
   `.status.phase` 随后才追上。单次读会看到 `PV=Bound` + `PVC=Pending` 并误判成失败，必须轮询。
2. **一个 PV 只能绑一个 PVC，而 claim 是命名空间级的** —— 所以**全局共享卷需要每个命名空间
   一对 PV/PVC**。没有引入子表：PV 名按 `storage_pv_name(..., namespace=ns)` 确定性推导，
   配 `wl-managed` 标签即可枚举；多一张表只会多一份可能与集群不一致的真相。
3. **退休的卷会永久占用自己的目录**。最初 `host_path` 是普通唯一约束，于是删掉的卷仍然挡着
   同一目录重新登记 —— 而"释放后重建同一个用户卷"是最正常的操作。改为**部分**唯一索引
   （只约束 `is_active` 的行）。
4. **`resize_pvc` 从来没有调用者，而且只可能失败**（静态卷不允许扩容）。删掉，而不是留着。
5. **`str.format` 会把中文占位符当成字段名**：自检信息里的布局写成
   `"…{root}/{组}…".format(root=…)`，`{组}` 被解析成字段 → `/api/v1/system/info` 直接 500。
   改成 f-string 并把字面花括号写成 `{{组}}`。**这条是 pytest 抓出来的**（Phase 9 全红）。
6. **「组管理员」有两个来源，只用了一个**。`resolve_scope()` 的 docstring 写得很清楚：组管理员
   权限来自**两处** —— 成员关系上的 `role_in_group` 标志，以及 group 作用域的 `group_admin`
   **角色授予** —— 并且有意取**并集**。可见性规则（`visible_condition`）用的是它，而我写的
   挂载规则 `_administers()` 只查了 `actor.admin_group_ids`（仅成员标志）。后果很具体：
   靠角色授予当上组管理员的账号**能在列表里看见**组本级卷，点挂载却 400，而消息里没有一个字
   解释这个差别。现已改为两条规则都读 `resolve_scope()` —— "组管理员"在可见性和挂载里必须是
   同一个集合。**这条是子代理审查时发现的**，我的测试只覆盖了成员标志那条路径。

**交付物**：迁移 `a1c4e7f90b23`（加 `host_path` + `dir_name`，`workspace_id` 改可空，
删 `storage_class`/`medium`/`size_bytes` 与 `instance_storages.sub_path`，建两个部分唯一索引；
**表里已有行时拒绝执行**并打印导出指引 —— 旧行无法推断目录，硬凑只会指向错误的数据）·
`app/services/storage_paths.py`（唯一的目录推导处）· `K8sGateway.apply_pv/read_pv/delete_pv/list_pvs` ·
`PvSpec`/`PvInfo` · 前端存储页（目录成为独立一列、表单按类型收窄、只有 `storage:manage`
才显示写操作）。

**曾经"尚未做"的四项，已在第二步全部完成**（见下面「后续增量 — 存储卷重构（第二步）」）：
用户卷默认挂进三类工作负载（`/home/{用户名}`）、实例与任务自身临时 PVC 取消、全局共享卷按命名
空间补齐 claim、**用户存储卷不存在时硬失败**。最后一条当时是这一轮唯一明确"没做完"的地方：
缺卷会静默返回空挂载列表，而"看不到自己的数据"不该表现为一个空目录。真实集群 E2E 里那两项
`PENDING` 也已转为通过。

**两处当时已知的不一致，以及一处环境坑**：

1. ~~**`GET /storages/attachable` 会向所有人推荐全局共享卷，但挂载必然 400**。~~ **已解决
   （第二步）**：全局共享卷的按命名空间 claim 现在会在挂载时创建，所以"推荐"和"能用"一致了。
   旧用例 `test_a_global_share_cannot_be_mounted_until_it_has_a_claim` 被改写成断言相反的
   事实：推荐它，就能挂上它。
2. **`storage_group_level_requires_admin` 这个分支当前不可达**（`_administers` 收窄到
   `scope.group_ids` 之后，它和可见性读的是同一个集合，能过可见性的必定能过它）。按"防御性
   第二道防线"保留，并在代码里写明了它不可达、以及**如果你放宽了 `visible_condition` 就是它
   救你**。见 `resolve_attachments` 里那段注释。
3. **`smoke_phase5.py` 的镜像段会崩，卡在 `KeyError: 'id'`**。根因是环境：Harbor 的 `library`
   项目里只有 `library/python`，`library/base-notebook:latest` 不存在 → `image_not_found_upstream`，
   脚本随后直接取 `['id']` 就抛 `KeyError`。**这个坑以前被掩盖着** —— 存储段一失败就
   `return 1`，根本走不到镜像段，所以存储重构反而把它暴露了出来。它不在存储范围内，本轮没动；
   真正的修法是**把官方基础镜像推到 `library/`**（这是早就记在案的待办），顺带让脚本的失败
   路径不崩、而是报出上游缺镜像。

**质量门禁**（本轮实测，全部在 `backend/` 下按 CI 口径跑）：

- `ruff check .` → All checks passed；`ruff format --check .` → **159 files already formatted**；
  `mypy app` → **Success: no issues found in 131 source files**。
- `pytest -q` → **686 passed, 0 failed, 0 errors**（15:19）。基线是 640 例（592 passed + 48 failed），
  本轮 phase5 从 33 例扩到 78 例、`test_cli` 加 1 例，640 + 45 + 1 = 686。
- `alembic heads` → `a1c4e7f90b23 (head)`；`alembic check` → No new upgrade operations detected；
  10 个迁移文件 `ruff format --check --no-force-exclude` 全过（`alembic/versions` 被 CI 排除，
  所以这条得单独跑）。
- 真实集群存储 E2E：**51 PASS / 0 FAIL / 2 PENDING**（2 项 PENDING 就是第二步的缺卷硬失败）。
- 前端：`vue-tsc --noEmit` 与 `vite build` 均 0 退出、`smoke_frontend_views.mjs` **19/19** 视图
  真实挂载并断言到内容（其中 StorageView 以**读者**身份再挂一次，证明"新建存储"与"操作"不在
  DOM 里）、`check-frontend-types.py` 报 "OK — every declared field exists in the API schema"、
  `openapi/schema.json` 重新生成后**无漂移**。

> **跑测试时的环境坑（这个沙箱特有，值得记一笔）**：`/tmp` 是**每次 bash 调用一个全新的
> tmpfs** —— 上一次调用里 pytest 建的 `/tmp/pytest-of-root`，下一次调用就已经不存在。
> 于是**两个 pytest 同时跑**会互相删对方的 `tmp_path`，SQLite 报
> `attempt to write a readonly database`，并连带产生假的 FAILED / ERROR。
> 实测：并发跑得到 `604 passed, 2 failed, 2 errors`，把受影响的 4 个用例单独重跑
> **4 passed**；换成给 pytest 一个独占的 `--basetemp` 后，同样的代码
> **686 passed, 0 failed, 0 errors**。所以在本机验证时：**一次只跑一个 pytest**，
> 并显式给 basetemp，例如
> `pytest -q --basetemp=/root/project/kube-wl-ai/.smoke/bt`。

### ✅ 后续增量 — 存储卷重构（第二步：工作负载不自带磁盘，已完成）

第一步把「目录」变成了权威，但仍留着三处没做完：工作负载还在要 `storageGb` 并各自建临时 PVC、
用户卷还没默认挂进容器、全局共享卷在每个命名空间的 claim 还没建。这一步就是关掉这三处，让
§5.4 里写下的那套模型**成为唯一事实**。

**改了什么**

| 改动 | 位置 |
| --- | --- |
| 删除 `instances.storage_gb`、`tasks.storage_gb`、`applications.default_storage_gb` | 迁移 `b7d2f4a8c915`（三张表各一批 `batch_alter_table`） |
| 实例与任务只挂属主用户卷，`HOME=/home/{用户名}` | `build_instance_objects`/`build_task_objects` 的 `home_pvc_name`/`home_mount_path` |
| `WorkloadSpec.mount_path`/`JobSpec.mount_path` 改为**必填**，删掉 `HOME_MOUNT_PATH = "/home/dev"` | `app/k8s/gateway.py`、`app/k8s/workloads.py` |
| 实例/任务都不再建 PVC；`InstanceObjects.pvc`、`TaskObjects.pvc`、`TASK_MOUNT_PATH` 删除 | `app/services/instances.py`、`app/services/tasks.py` |
| 缺用户卷 → 400 `user_volume_missing` | `storages.require_user_volume()`（实例/应用/任务三条创建路径都会先调它） |
| 全局共享卷按命名空间补 claim | `storages.ensure_attachment_claims()` + `claim_name_for()` |
| `TaskCreate.storage_ids` 新增（任务也能挂共享卷） | `app/schemas/task.py`、`build_task_objects(extra_mounts=...)` |
| 每用户存储上限改在**建卷**时校验 | `storages._check_capacity_against_pools()`，取已授予资源组里**最小**的上限 |
| 工作空间存储用量只从**卷**汇总 | `quota_guard.workspace_usage()`；`check_quota()` 只在请求自身消耗存储时才查 |
| `STORAGE_CLASS_*` 不再被读取，系统页改为 info | `app/services/system.py`（原来那条 warning 已经变成假话：没有任何东西会因此失败） |
| 退休共享卷时按 `wl-storage-id` 清扫跨命名空间的 PV/PVC | `K8sGateway.list_pvs`、`storages._delete_namespaced_claims()` |

**为什么删列而不是迁移数据**：`storage_gb` 描述的是「这个实例自己的那块盘」，而这块盘正是被取消
的东西。把它换算成某个用户卷的容量是**编造**：一个实例的 10 GB 并不等于属主目录只有 10 GB，
而属主目录的容量早在登记卷时就定下了。宁愿丢掉这个数字，也不要留下一个看起来有意义、实际与
真实容量无关的列。老实例仍然可以正常删除 —— `instances.k8s_pvc` 这一列**特意保留**，它记的是
升级前那些临时盘，删除路径靠它回收。（**千万不要**把用户卷的 claim 写进去：PV 是 `Retain`，
claim 一消失 PV 就 `Released` 且不可重绑，用户的存储会永久挂不上。）

**任务的 `storage_ids` 不落库**。实例有 `instance_storages` 关联表，任务没有，而这一步也不打算
为它加一张：`apply_job` **只在 `create_task` 里被调用一次**，没有重试、没有重跑、没有"按当前
配置重新对齐"的入口，所以挂载集合在 Job 创建后就再也不需要读了。加一张表只会得到一份没有读者
的真相。**如果以后加了任务重跑或对账，这里必须一起补上** —— 到那时 `_apply` 就从"一次性提交"
变成了"可重复收敛"，而它的输入不能再只来自调用栈。

**`ensure_attachment_claims` 为什么单独一步**：可见性、种类、组本级权限这些校验都在
`resolve_attachments()` 里，它是**纯数据库**的、可单测的；而建 PV/PVC 需要 gateway。
把两者混在一起会让"能不能挂"这个问题必须先有一个假集群才能回答，并且会把集群调用塞进一条
本该只读的校验路径里。现在 `resolve_attachments` 不再检查 `storage.k8s_namespace` 与
`workspace.k8s_namespace` 是否相等 —— 那个检查在"按需建 claim"之后已经**不成立**了：
全局共享卷的 `k8s_namespace` 就是 `None`，而它恰恰是必须能挂的那一类。`claim_name_for()` 是
**纯函数**，所以 pod spec 任何时候都能从零重建而不与集群漂移。

**顺带删掉 `to_mounts()`**：它没有生产调用者，而且计算出的 `pvc_name` 对全局共享卷是**错的**
（直接用 `storage.pvc_name`，对全局卷恒为空）。留着一个只有在被使用时才会暴露错误的辅助函数，
比没有它更危险。

**踩到的坑**

1. **`mypy` 是唯一挡住"漏传 `mount_path`"的东西。** 把 `HOME_MOUNT_PATH` 从一个共享常量改成
   必填参数后，所有调用点立刻变红 —— 这正是想要的：一个默认值会让某个忘了传的地方安静地挂到
   `/home/dev`，而那个目录不属于任何人。
2. **删 `TaskCreate.storage_gb` 之后 `storage_ids` 必须先存在**，否则 `mypy` 报
   `"TaskCreate" has no attribute "storage_ids"` —— 它把"字段换了名字但没加新的"挡住了。
3. **`check_quota` 的 `storage_gb` 要变成可选**（`= 0`，且只在非零时进检查列表）。否则实例/任务
   创建路径会继续传一个已经不存在的量，等于把刚删掉的概念从后门放回来。
4. **迁移必须 `--no-force-exclude` 才检查得到**：`alembic/versions` 被 CI 的 ruff 配置排除，
   `ruff format --check .` 永远会说它"已格式化"。这一步的迁移文件单独跑过一次。
5. **`alembic -x db_url=…` 是空操作**（`alembic/env.py` 用 `get_settings().database_url` 覆盖了
   命令行值），要用 `DATABASE_URL=` 环境变量。往返验证就是这样在副本上做的。

**质量门禁**（本轮实测，全部在 `backend/` 下按 CI 口径跑）：

- `ruff check .` → All checks passed；`ruff format --check .` → **159 files already formatted**；
  `mypy app` → **Success: no issues found in 131 source files**。
- `alembic upgrade head` → `b7d2f4a8c915 (head)`，`alembic check` → No new upgrade operations detected；
  在副本上 `downgrade -1` 再 `upgrade head` 往返一次，三列正确地回来又消失，`integrity_check` ok。
- `pytest -q` → **690 passed**（第一步 686，本步净增 4 条：缺家目录硬失败、任务挂载集合、
  每用户上限在建卷时生效、退休共享卷按标签清扫跨命名空间 claim）；`openapi/schema.json`
  重新生成后，`InstanceCreate`/`TaskCreate` 有 `storageIds`、一切 `storageGb` 全部消失，
  `check-frontend-types.py` 报 "OK — every declared field exists in the API schema"。
- 前端 `vue-tsc --noEmit` 0 退出、`vite build` 2.47s 成功、`smoke_frontend_views.mjs`
  **19/19** 视图挂载并渲染出数据；`InstancesView`/`TasksView`/`ApplicationsView` 里的
  `storageGb`/`defaultStorageGb` 全部清除，任务的"临时存储（GB）"换成与实例一致的
  **挂载存储多选**。
- 真集群 E2E（`.smoke/e2e_storage.py`，`K8S_ENABLED` 环境下的 `RealK8sGateway`）：
  **全部通过，0 失败，无 PENDING**。第七组"删除后无遗留 PV/PVC"是本步从 **FAIL 修到 PASS**
  的 —— 见下面第 6 条。
- 真实后端冒烟（独立端口 + 一次性 SQLite，跑完即销毁）：**7 个阶段共 377 项通过 / 1 项失败**。
  `phase3` 50/50、`phase4` 62/62、`phase5` 75/1、`phase6` 62/62、`phase7` 45/45、`phase8` 60/60、
  `phase9` 23/23（`phase9` 除了 `SMOKE_BASE` 还要 `SMOKE_HEALTH_URL` 指向同一端口）。唯一的失败是
  **本轮明确推迟的镜像问题**（`library/base-notebook:latest` 不在 Harbor 的 `library` 项目里 →
  `image_not_found_upstream`，随后脚本读 `body["id"]` 崩掉）。存储相关全过，包括本步新增的三条：
  `user_volume_missing` 硬失败、建卷时校验每用户上限、共享卷不受该上限约束。

**踩到的第 6 个坑：按需创建的跨命名空间 claim 没人回收。** 真集群 E2E 一开始报
`集群中无遗留的托管 PV — 残留 ['wl-vol-vol3-4-wl-e2e6']`：`ensure_attachment_claims()` 给每个
挂载过全局共享卷的命名空间各建了一对 PV+PVC，而数据库**刻意**不记录它们（名字可由
`storage_pv_name(id, name, namespace=…)` 推导），`delete_storage()` 却只知道卷自己的那个名字。
于是每挂过一个命名空间就留下一块 `Retain` PV：界面看不见、任何代码路径都不会删它，只有
`kubectl` 手工能清。**"可推导所以不落库"是对的，"可推导所以不必找"是错的** —— 删除时必须
反过来用标签去找它们。修法是给网关加 `list_pvs(label_selector)`（真实现
`list_persistent_volume`，假实现复用 `_parse_selector`/`_labels_match`），`delete_storage()`
在删掉卷自己的 PV 之后按 `wl-storage-id` 清扫一遍，先删 claim 再删 PV（顺序同前，理由同前）。
顺带把 `read_pv()` 里内联的 `PvInfo` 转换提成 `_pv_info()`，让"按名字读"和"从列表读"不可能
给出不同答案 —— 清扫信任列表的结论。

**踩到的第 7 个坑：一个组有两个工作空间，这个组就永远不能有存储。** 改 `smoke_phase3.py` 时它在
登记主目录那一步返回 `group_has_multiple_workspaces`：`_workspace_for_group()` 要求组恰好有**一个**
工作空间，因为 claim 得放在某个命名空间里；而脚本为了测"未授权"故意在同一个组下建了第二个工作空间。
脚本已改成用**第二个组**放那个未授权工作空间（这才是它本来的意图）。

但这暴露的是产品层面的死路，不只是脚本问题：**工作负载必须挂属主的主目录，而主目录必须登记，
登记又要求组只有一个工作空间** —— 所以任何有两个工作空间的组，其成员**一个实例都起不来**，
而且只有走到登记那一步才会看到 409。规则本身（不猜）是对的，缺的是"指定是哪一个"。
`Storage.workspace_id` 这一列和 `create_storage(workspace_id=…)` 参数**都已经存在**，
`_workspace_for_group()` 只是没把它们用起来。建议的改法：允许调用方显式指定 `workspace_id`
（校验它确实属于该组），只有在**没指定**时才退回"必须唯一"。这一步不动它 —— 那是第一步已经
评审并写进验收用例 `test_a_group_with_two_workspaces_is_refused_rather_than_guessed_at`
的行为，属于需要单独决策的改动，不该混在这次提交里。

**踩到的第 8 个坑：冒烟脚本自己也会过期。** 上面这套"全绿"是补了 4 个脚本才得到的，每一个都是
工作负载的**前置条件**变了：
`phase3`/`phase4`/`phase6`/`phase7` 都要先为自己的用户登记主目录卷（否则第一次创建实例/应用/任务
就 409 `user_volume_missing`），`phase4` 的应用创建体里还留着 `defaultStorageGb: 10` ——
`ApplicationCreate` 是 `extra="forbid"`，所以那是一次 **422**，脚本会停在"创建应用程序"。
`smoke_frontend_views.mjs` 的应用桩数据里也有同样的字段。

这印证了一件事：**"字段删干净了"不能靠 grep 源码来确认，要靠把每个消费者真的跑一遍。** grep 只
覆盖了 `backend/app`、`frontend/src` 和测试，`scripts/` 下的 7 个冒烟脚本是独立的一套调用方，它们
和前端一样会 422。留下的 `storageGb` 命中项都已逐条核对过，全部是**同名不同物**，不是漏网：
`ResourceGroupLimit.max_storage_gb_per_user`（资源池上限，本步改为在建卷时校验）、
`QuotaResource.STORAGE` / `usage.storage_gb`（工作空间的存储**配额维度**，本步改为从卷汇总）、
`parse_storage_gb()`（解析 K8s 容量字符串）、`workspaces.py` 里的命名空间 `requests.storage`
ResourceQuota，以及若干断言"这个字段已被拒绝/不再回报"的测试与注释。

**同一个 E2E 里还有一条是断言太严，不是产品问题**：`该 claim 已经 Bound` 一开始 FAIL。卷控制器
先把 **PV** 标成 `Bound`，claim 自己的 `.status.phase` 落后一拍，所以紧跟
`ensure_attachment_claims()` 的一次读会看到"其实已经绑好、却还写着 `Pending`"。这与真正的
provisioning 失败无法区分，单读一次必然偶发误报。harness 里本来就有轮询（对第一步的 claim），
这一步把轮询提成 `wait_bound()` 并给按需 claim 也用上。**产品侧不需要等** —— pod 提交后多等
一拍是正常的 Kubernetes 行为。

### ✅ 后续增量 — 存储卷重构（第三步：建卷页不再提供走不通的选择，已完成）

现场反馈：「在页面用管理员创建用户存储，点击创建，没有反应。」后端日志是 `POST /api/v1/storages` →
**400**，审计 `error_message` 为 `用户组 hpc 还没有工作空间，存储卷的声明无处存放`
（`group_has_no_workspace`），操作者 `admin`。

**这不是后端的问题，后端拒绝得对。** 演示库里 `workspaces` 是**空表**，而一个存储卷的 claim 必须落在某个
命名空间的 PVC 里 —— 组没有工作空间，这个卷建出来也没有任何 pod 能挂上它。在隔离库上按线上同形复现，
逐字得到同一条消息：

| 请求 | 结果 |
| --- | --- |
| `user_volume`（属主在 `hpc`） | 400 `group_has_no_workspace` |
| `group_share`（`hpc`） | 400 `group_has_no_workspace` |
| `global_share` | **201**，`/storage/kube-wl/_global/public-data` |
| 先给 `hpc` 建一个工作空间，再重发前两条 | **201**，`/storage/kube-wl/hpc/zhangsan` 与 `/storage/kube-wl/hpc/share1` |

所以平台有一条**顺序要求**：**先有组，再有工作空间，才能有存储卷**。这条要求此前只存在于后端，
页面上一个字都没有 —— 建卷对话框把**所有**组都列出来，管理员挑一个必然失败的，点创建，得到 400。

**改的是「不要提供走不通的选择」，不是放宽后端的规则**（`frontend/src/views/StorageView.vue`）：

| 改动 | 理由 |
| --- | --- |
| `groupWorkspaceCount` / `groupOptions` | 用页面**已经加载**的 `workspaces` 算出每个组有几个工作空间；恰好一个才可选 |
| `userOptions` | 用户卷的组来自**属主**（`owner.primary_group`），所以属主的组不满足时该用户也不可选 |
| 下拉项 `:disabled` + `:subtitle` 写原因 | 「还没有工作空间 —— 存储卷的声明无处存放」比一个 400 有用 |
| 一个都不可选时弹 `v-alert` | 明确说出顺序要求，并指向「工作空间」页面 |
| `openCreate()` 默认选**第一个可选的** | 原来默认 `groups[0]`，等于每次创建都从一个必失败的组开始 |
| `formValid` 再查一次可选性 | 按钮可用但请求必然失败，比按钮禁用更糟 |
| `save()` 不再 `throw` 非 `ApiError` | 原来非 `ApiError` 会**静默**抛出：对话框不动、按钮恢复，用户什么也看不到 |

**边界没变**：`_workspace_for_group()` 仍然要求组**恰好一个**工作空间，两个工作空间依旧
`group_has_multiple_workspaces`（第 7 个坑记的那条死路依然存在，仍待单独决策）。这一步只是让页面
**提前**说出后端早就会说的话。

**顺带补上了冒烟脚本缺的一种能力。** `smoke_frontend_views.mjs` 此前只做「挂载 + 首屏断言」，
所以**点开对话框之后**的分支从来没被测过 —— 而这正是这次出问题的地方。新增 `INTERACTIONS`：
挂载后可以点一个按钮、再断言其后的 DOM。为它顺带修了两件事：

- **`visualViewport` 桩。** happy-dom 没实现它，而 Vuetify 的 overlay 在**卸载**时会读它 —— 于是
  「打开过对话框」的视图会在 `app.unmount()` 抛 `visualViewport is not defined`，看上去像视图的 bug。
- **`EXPECTS_ABSENT` 改判首屏。** StorageView 是**故意**以只读者身份挂载的
  （`VIEW_PERMISSIONS_ABSENT.StorageView = ['storage:manage']`，用来证明只读页面不显示变更入口）；
  而交互会**主动授予** `storage:manage` 并打开那个入口。两者必须分开判断，否则交互会把只读断言撞掉。

冒烟脚本挂载时不带管理员角色，所以 `loadWorkspaces()` 走 `getMyWorkspace()` 分支、`workspaces` 为空 ——
**这恰好就是线上的状态**，于是新增的断言 `还没有用户具备存储条件` 直接对应现场。把新增的告警
`v-if` 改成 `false` 复跑，冒烟立即以 `rendered, but without: "还没有用户具备存储条件"` 失败，
恢复后 19/19 通过 —— 这条断言确实在测东西。

### ✅ 后续增量 — 实例状态不再把崩溃的容器说成「运行中」（已完成）

现场反馈：「创建开发实例之后，页面一直显示创建中。」`kubectl describe` 显示容器
`CrashLoopBackOff`、`exec: "jupyter": executable file not found in $PATH`（exit 127）。

查下来是**三件事**叠在一起，只有第 2、3 件属于平台要修的问题：

**1. 镜像里没有 `jupyter`（不是代码问题，是前置条件缺失）。** `ide_command()` 把
`jupyter lab …` 写死在代码里，等于假设**每个镜像都自带 IDE 二进制**。用户选的
`library/python:3.12` 是官方纯 Python 镜像，没有 Jupyter。而当时 Harbor 的 `library`
项目下**只有 `library/python`** —— 也就是说**选任何镜像都起不来 Jupyter**。这解释了为什么
它一直推迟的「往 `library/` 推官方基础镜像」其实是前置条件，不是可选项。同一个假设对
`vscode`（要 `code-server`）和 `webssh`（要 `ttyd`）一样成立。

**2. 页面一直「创建中」，因为对账器根本没在跑。** 实例状态只由 `reconcile_instance()` 推进，
触发它的只有 Celery beat 的 `reconcile-workloads`（30 秒）、CLI 和手动
`POST /instances/{id}/reconcile` —— **页面读取不触发对账**（这是 `create_instance` 的设计：
先报 `CREATING`，等对账提升）。决定性证据是实例记录里 `podName` 一直是 `None`，而 pod 已经
存在 12 分钟。

⚠️ **本条当时写的是「本次没有改」，后来发现真相比这更糟：对账器即使跑起来也没用**，
因为它的写入会被回滚，而且本机连 broker 都没有。已在
「后续增量 — 状态永远停在「创建中」：一次被回滚的提交」里连根修掉（进程内 sweeper +
补上提交 + 前端自适应轮询）。

**3. 对账一跑，它会把崩溃的容器报成「运行中」——这次修的就是它。**

原因是 **pod 的 phase 在整个 crash-loop 期间一直是 `Running`**：phase 描述的是 pod，pod 本身
没问题，只有容器在崩。而旧代码是：

```python
if pod.phase.lower() == "running" and pod.ready:   # 崩的时候 ready 是 False
    ...RUNNING
else:
    instance.status = _PHASE_TO_STATUS.get(pod.phase.lower(), CREATING)  # "running" → RUNNING
```

`ready` 在第一支里用了，到 else 里**被丢掉了**，于是 `running` 又映射回 `RUNNING`。结果比
「卡在创建中」更糟：页面显示**运行中**，消息里却写着 CrashLoopBackOff，IDE 链接必然 502。

改法是把这段判断提成纯函数 `status_from_pod()`，并让 **`ready` 优先于 phase**：

| 观测 | 状态 |
| --- | --- |
| `Running` + Ready | `running` |
| 容器 reason 属于 `FATAL_POD_REASONS`（CrashLoopBackOff / ImagePullBackOff / ContainerCannotRun …） | `error`，保留 message |
| `Running` 但不 Ready，且没有失败容器（就绪探针还没过） | `creating` |
| 其它 phase | 仍按 `_PHASE_TO_STATUS` |

两个刻意的选择。**`FATAL_POD_REASONS` 用「失败清单」而不是「可接受清单」**：没预料到的 reason
应该让实例看起来仍在启动（可恢复，且对慢镜像拉取是真的），而不是替平台宣布一个它证明不了的
结论。**`Running` 但不 Ready 且无失败容器一律算 `creating`** —— 否则就绪探针慢一点的实例都会
变成「异常」。

为此 `PodInfo` 新增了 `reason` 字段（未格式化的容器 reason）。不能拿现成的 `message` 去比对：
它是给人看的散文（`CrashLoopBackOff: back-off 5m0s restarting failed container=main pod=…`），
措辞一变匹配就失效。`_pod_info()` 顺带补上了**两次重启之间**的窗口 —— 那时容器不是 `waiting`
而是 `terminated`，带着杀死它的退出码；不看这一支，实例会在每个循环里闪回「还在启动」。

**最值得记的一条：那个测出这个 bug 的测试是假的。** `tests/test_acceptance_phase3.py` 里早就有
`test_reconcile_reports_a_crash_looping_container`，名字完全对得上，但它 spawn 的 pod 是
`phase="Pending"` —— 而崩溃时 phase 是 `Running`。它测的是 `Pending → creating`，**从来没复现过
这个 bug 的形状**，所以它一路绿着。已改成真正的形状（`Running` + `ready=False` +
`reason="CrashLoopBackOff"`），并新增 `tests/test_instance_status.py` 覆盖整个映射矩阵，
含「未预料的 reason 不得被判成失败」和「每个 `FATAL_POD_REASONS` 成员都真的可达」。
把新判定关掉复跑：**6 项失败**（5 项单元 + 1 项验收），恢复后全绿 —— 这些测试确实在测东西。

**顺带修了一处名实不符的文案（但理由当时写错了）。** 实例菜单的「重新下发」和提示
「已重新下发」被改成了「刷新状态」。**改名是对的，给的理由是错的**：这里原先写着
`POST /instances/{id}/reconcile` 「只读回 pod 观测状态，**不对集群下发任何东西**」——
而该端点第一件事就是 `apply_cluster_state()`（已停止的实例用 `replicas=0` 重建，免得
「修对象」把实例悄悄启动起来），然后才 `reconcile_instance()` 读回观测状态。

所以准确的说法是**两件事都做**：它**能**修好一个被删除或驱逐的工作负载（重新下发），
**不能**修好一个正在崩溃的容器（重新下发只会重演同一次崩溃）。工作空间那个「重新下发」
则一直是名副其实的。前端里同一句错话也已一并改正
（`InstancesView.vue` 的 `reconcile()` 注释）。

验证

- **在真实集群的真 pod 上**跑新映射（只读）：
  `phase='Running' ready=False reason='CrashLoopBackOff'` → `status=error`，
  message 携带完整原因。旧代码在同一输入上给的是 `running`。
- `pytest -q` **704 passed**（原 690 + 14 项新增）；`ruff check .`、`ruff format --check .`
  （160 文件）、`mypy app`（131 文件）全部无问题。
- 前端 `vue-tsc --noEmit` exit 0、`vite build` 2.45s、视图冒烟 19/19。
- 线上后端带 `--reload`，已加载新代码；该实例现在正确显示**异常**。

### ✅ 后续增量 — 工作空间配额算错，以及失败实例留下的孤儿对象（已完成）

现场症状两条，看起来像两个问题，其实是同一个事故的两半。

**一、第二个实例建不出来**

在工作空间里建第二个开发实例，前端报：

```
下发失败：create service wl-jupyter-test-2-ssh failed: services "wl-jupyter-test-2-ssh"
is forbidden: exceeded quota: wl-quota, requested: count/services=1, used: count/services=2,
limited: count/services=2
```

而集群里那个 Pod 明明是 `1/1 Running`——**新镜像没问题，被拒的是 Service**。

根因在 `workspaces.py` 的配额公式：`hard["count/services"] = str(instances)`，
但**每个实例创建 2 个 Service**（`-ssh` 的 NodePort + `-ide` 的 ClusterIP）。
`max_concurrent_instances=2` 于是只给 2 个 Service 名额，第一个实例的两个 Service
就吃光了，第二个实例一个都建不出来。**这不是权限问题，也与 CPU/GPU 无关**：只要公式是
`instances`，同一个组里就永远只能有 1 个带 SSH+IDE 的实例。

修法：把"每个实例几个 Service"变成**一个常量**，放在创建 Service 的那段代码旁边
（`app/k8s/workloads.py` 的 `SERVICES_PER_INSTANCE`），由配额侧引用。数量必须待在
创建侧，否则两边会各自漂移；配额只是引用它。

**二、失败之后留下的东西，平台自己删不掉**

更严重的一半。下发顺序是 deployment 在前、Service 在后，而**对象名是在所有 apply 都
成功之后才写回数据库的**（原 `_apply` 末尾）。异常在 Service 那步抛出 → 写回的代码
永不执行 → 线上实测该实例行的 `k8s_deployment` / `k8s_ssh_service` / `k8s_ide_service`
**全是 NULL**，而集群里的 Deployment 正在跑。

`delete_instance` 是**从这些列拼出要删的对象名**的，所以：删掉这个实例，集群里什么都
不会被删；Deployment 永久残留；`count/deployments.apps` 与内存配额被它一直占着（实测
连排查用的临时 Pod 都被配额拒绝），**整个工作空间彻底卡死**，重试还会再产生一个孤儿。

讽刺的是 `_persist_failure` 的文档字符串写明了它为什么要把 `error` 行提交下来：
"losing the `error` row would leave a half-built workload in the namespace with nothing
referencing it"——**它想要的正是"能引用到半成品"，而 refs 全 NULL 的行什么都引用不到**。
注释描述的目标被代码本身破坏了。接口文档里"a rejected request never leaves a partial
workload behind"同理：对**平台侧校验**成立，对**集群侧拒绝**（命名空间 ResourceQuota）
不成立。

修法：**在下发每个对象之前就把它的名字记到行上**，而不是等全部成功。
- 记录"成功的"会丢对象，"先记名字"不会：删除一个从未创建的对象是空操作，
  而 `delete_workload` 本来就容忍对象不存在。
- 它还顺带覆盖了第二种丢对象的路径：**apply 在 API server 上已生效、但回程报错**
  （超时、连接断开），这时"它失败了"和"它存在"同时为真。
- 代价是行上可能记着一个集群里并不存在的对象——这是无害的那个方向。

第二、三条是同一类问题：**配额公式算的是"能跑起来多少"，而实际消耗的是"建了多少个对象"**。

**三、顺带修正的两处**

1. **`count/persistentvolumeclaims` 算错了整个维度**。原公式 `instances + tasks`：
   实例**不建任何 PVC**（它的 home 是用户的*卷*），任务的临时盘也已移除。真正落在这个
   命名空间里的是**每个已注册的卷一个**（用户卷/组共享卷）以及**每个全局共享卷按命名空间
   一个** —— 与能跑几个实例毫无关系。它同时**错在两个方向**：组里卷一多就会在*注册卷*时
   撞配额，而它从来没有真正按 claim 数限住任何东西。已**移除该键**（字节由下面的
   `requests.storage` 限）。
2. **`requests.storage` 之前只在授权非零时才设置**，而**缺失的配额被 K8s 解读为"无限"**
   —— 这恰好违反了同一函数上方注释自己声明的原则（"Kubernetes reads an absent resource
   as *unlimited*"）。存储这一维是唯一没有被封零的。现在无条件设置（未授权即 `0Gi`）。
   这条不是洁癖：命名空间配额是**组存储总量的唯一上限**，`max_storage_gb_per_user` 只在
   注册卷时核对单卷大小；而无池工作空间连那个上限都读不到（`_refuse_oversized_user_volume`
   在 `not rows` 时只记一条 `storage_capacity_unchecked` 就放行）。因此同步更新了
   `test_workspace_without_a_pool_gets_a_usable_but_empty_namespace` 的断言（它原本只断言
   `{"pods": "0"}`，其注释所写的原则正是这条，只是早于存储维度）。

验证

- **逐个证明新测试能抓住对应的 bug**（临时改回旧写法，跑完即还原）：
  `count/services` 改回 `instances` → `assert 2 == (2 * 2)`；
  `requests.storage` 改回 `if storage_gb:` → `KeyError: 'requests.storage'`；
  refs 改回"全部成功后才写" → `assert None is not None`（正是线上那个 NULL）。
- **真实集群端到端**（后端 `K8S_ENABLED=true` 且带 `--reload`，改动已生效）：
  - `POST /workspaces/1/reconcile` 后，线上 ResourceQuota 变为 `count/services: 4`、
    `requests.storage: 100Gi`，`count/persistentvolumeclaims` 消失。
  - **同一命名空间建起两个 `ideType=jupyter` + `sshEnabled` 的实例**（用
    `library/ubuntu-dev:24.04`）：两个 Deployment/SVC/Ingress 全部创建成功，
    `count/services used=4/4`，两个 Pod 都 `1/1 Running` 且 `RESTARTS=0`。
  - 两个实例各自的 ClusterIP 上 `/x/{id}/lab` 都返回 **200 `<title>JupyterLab</title>`**；
    两个 NodePort 都回 `SSH-2.0-OpenSSH_9.6p1` 横幅。
  - `/reconcile` 后平台状态均为 `running`、message 为空；删除两个实例后
    Deployment/Service/Ingress/Pod **全部清空**、配额归零。
- `pytest -q` **709 passed**（原 704 + 5 项新增；另有 1 项既有断言因上述存储封零而更新）；
  `ruff check .`、`ruff format --check .`（160 文件）、`mypy app`（131 文件）全部无问题。

**本轮暴露但未修的问题（另行处理）**

- **平台接受一个它明知拉不下来的镜像引用**。Pod 里的 `image` 是用户输入**原样**透传的
  （`instance.image` → `build_instance_objects(image=…)`），没有补 registry host。
  填 `library/ubuntu-dev:24.04` 会去 **Docker Hub** 拉并超时（实测
  `Get "https://registry-1.docker.io/v2/": Client.Timeout`），而目录里存着 `registry_host`
  字段、平台完全知道正确的完整引用应该是什么（`10.28.0.194:30002/...`）。
  失败形态是最难查的一种：Pod 一直 `ErrImagePull`，而创建接口返回 201。
- **已删除的实例永久占用名字**：唯一性检查没有过滤 `is_active`
  （`instances.py` 的 `instance_name_taken` 查询），所以 `is_active=false` 的行虽然
  "从所有列表里消失"，名字却再也用不了。
- **旧 bug 留下的一块残骸**：`Secret wl-jupyter-test-2-ssh` 仍在 `wl-hpc-hpc` 里——
  那一行的 `k8s_secret` 当年就是 NULL，因此没有任何东西能引用并清理它。属于本次修复前
  造成的既成事实，手工删掉即可。

### ✅ 后续增量 — IDE 暴露改为平台后端反代（已完成）

实例的 IDE 过去是"建一个 `Ingress` 指向 IDE 的 `ClusterIP`，再把 `/x/{id}/` 这个地址发给用户"。
这条路解决了"怎么把流量送进集群"，但它**给不出一个用户真的点得开的地址**，而且方向从一开始就偏了。

**为什么 Ingress 在这个场景里是错的选择**

1. **它会逼出一个新主机名。** Ingress 是集群边缘的入口，用户拿到的地址必然带着它的 host
   （`https://<某个集群域名>/x/8/…`），于是**这个地址离开了平台自己的源**。而平台已经有一个用户能登录、
   能被信任的源；为 IDE 再引入一个域名意味着多一份证书、多一条 DNS、多一套 CORS/Cookie 边界，
   而且用户要在两个源之间被来回搬运 —— 票据和 Cookie 都过不去。反代让 IDE 落在**平台自己的源**上，
   `/x/…` 只是一个普通的同源相对路径，前端 `window.open()` 直接就能用，不需要知道任何主机名。
2. **它无法按请求决定。** Ingress 规则是声明式的、长期存在的：路由此刻"给谁"在创建时就被写死了。
   而"这个请求该不该放行"必须**每次请求**重新判断（用户可能已被移出工作空间、实例可能已被停掉）。
   反代天然位于请求路径上，这里就能判断；Ingress 只能在它前面再接一层（`auth-url`），最终还是需要
   一个后端端点 —— 那不如直接反代。
3. **它要求集群先有一个 ingress controller，而线上并没有。** 实测 `kubectl get ingressclass` 为空，
   `kubectl get ingress -n wl-hpc-hpc` 里那条规则的 `CLASS=<none>`、`ADDRESS=` 为空 ——
   对象建出来了，却**没有任何东西会去实现它**。这是一个"创建成功、永不生效"的失败形态。

现在的形态：**平台后端自己反代**（`backend/app/api/ide/proxy.py`），router 注册在**根路径**
（`app/main.py` 的 `app.include_router(ide_router)`）而不是 `/api/v1` 下 —— 因为它服务的不是平台的
JSON API，而是别人的 HTML / JS / websocket。三条路径：`GET /x/{用户名}/{实例 id}`
（307 跳到带斜杠的形式）、`/x/{用户名}/{实例 id}/{rest:path}`（所有 HTTP 方法，外加一条 websocket 路由），
全部 `include_in_schema=False`。

路径里那一段是**用户名而不是实例 id**，地址栏因此可读。要说清楚的是：**这不构成任何保护** ——
用户名本来就是可见的，实例 id 又是自增整数，所以它只是"看起来更友好"，不是一个安全边界。

**每个 IDE 对前缀的期望不一样：`IDE_STRIPS_PREFIX`**

反代把请求转给实例的 `ClusterIP` 时，前缀是留还是剥，取决于上游是谁：

| IDE | 交给用户的 URL | 前缀 | 原因 |
| --- | --- | --- | --- |
| jupyter | `/x/{用户名}/{id}/lab` | **保留** | `--ServerApp.base_url=/x/{用户名}/{id}/`，它的路由表本身就长在前缀下 |
| webssh | `/x/{用户名}/{id}/` | **保留** | ttyd 的 `--base-path`，由 `ide_command()` 显式传入（所以现有镜像不用改） |
| vscode | `/x/{用户名}/{id}/` | **剥掉** | code-server 没有 base path 参数 |

code-server 是唯一的例外，也是唯一需要**回程改写**的：剥掉前缀后它的响应里会出现根相对的
`Location`（重定向），回程必须把前缀加回去，否则浏览器会被甩到平台根下的无关路径。
`IDE_STRIPS_PREFIX = {JUPYTER: False, VSCODE: True, WEBSSH: False}` 就是这张表。

**真实集群端到端验证**

三个实例，镜像都是 `10.28.0.194:30002/library/ubuntu-dev:24.04`：

- **实例 8（`jupyter`）**：`GET /x/zhenghuanbo/8/lab` → 200 且 `<title>JupyterLab</title>`；
  `/api/contents/` 返回真实目录列表；一个页面静态资源 200 / 237731 字节；
  **并经由反代真正跑起了一个内核** —— `POST /x/zhenghuanbo/8/api/kernels` 返回 **201**，
  再通过反代的那条 kernel websocket 执行了一个 cell，拿回 `PROXY_WS_OK 42`，
  `execute_reply` 的 `status=ok`。这一步才是关键：**它证明的不是"页面能打开"，而是"代码能跑"。**
- **实例 9（`webssh`）**：ttyd 的 HTML 200；它的 websocket 经反代协商出 `tty` 子协议，
  并返回了真实的 shell 横幅与提示符。
- **实例 10（`vscode`）**：code-server 的 HTML 200，HTML 里引用的 **8 个资源全部 200**；
  workbench 的 socket **既经后端（8000）也经 Vite dev server（5181）** 连上，
  并吐出了真实的 VS Code 协议帧。

三个实例事后全部删除；命名空间配额归零，且**集群里不存在任何 Ingress**。

**只有真实集群才会暴露的 bug：websocket 握手头被转发了两次**

反代最初把调用方的 websocket 握手头（`Sec-WebSocket-Key`、`Sec-WebSocket-Version`…）原样转给上游，
但 `websockets` 客户端**自己会生成一套握手**，于是上游收到重复的握手头，tornado 直接回 HTTP 400，
而反代把它当作 403 抛给了浏览器。

症状极容易误判：**页面能打开、内核能创建，但执行 cell 没有反应。** 修法是在 `_forward_headers` 里
过滤掉 `sec-websocket-*`，并用一个针对该函数的单元测试钉住。

**教训要记下来：用 `httpx` mock 出来的测试套件看不见 websocket 握手 bug。** 这类问题只在真实握手、
真实 tornado 上出现 —— HTTP 层面的 mock 永远是绿的。

**第二个 bug：一个 `api_route` 声明多个方法会撞 operation id**

把多个 HTTP 方法写进同一个 `api_route` 时，FastAPI 的 `generate_unique_id` 只用
`list(route.methods)[0]` 生成 id，于是每个方法拿到**同一个** operation id；OpenAPI 生成时告警，
并且 `/api/v1/openapi.json`（连带 `/docs`）**返回 500**。

用 `include_in_schema=False` 解决 —— 这同时也是语义上正确的：这条路由说的是别人的协议
（ttyd / Jupyter / code-server 的 HTML、JS、websocket），**不吐平台的统一错误信封**，
本来就不该出现在平台的 OpenAPI 契约里。

**两个副产品**

- `apply_ingress` **已删除**（gateway 的 Protocol、Fake 与真实 client 三处一并去掉，连同 client 里
  随之无用的 `IngressSpec` 导入）。留着它的理由是"将来改回 Ingress 不必重写"，但那个将来并不存在：
  这里不适合用 Ingress 的三个原因（新主机名、无法按请求决定、线上没有 controller）都不是临时状况。
  与其留一条能建对象却没人调用的路径，不如让"平台不建 Ingress"在代码里也没有例外 —— 想改回去时
  重新写这三十行，比留着一个会腐烂的分支便宜。**`IngressSpec` 类型与 Fake 的 `ingresses` 存储保留**，
  因为后者是"历史 Ingress 被删掉了"这件事唯一可观测的地方（见下一条）。
- `instance.k8s_ingress` 这一列**刻意保留**：`delete_instance` 仍然从这一列拼出要删的对象清单，
  所以**本次改动之前创建的 Ingress 依然能被删掉**。删掉这一列会让那些历史对象永远无法被平台清理。
  保留的是一个**字符串列**和一个删除路径，不是一条创建路径 —— 这两件事必须分开看：前者是清理历史，
  后者是继续制造需要清理的东西。

**同时去掉/加上**：`Settings.instance_ingress_host` 整个删除；`build_instance_objects()` 增加
`owner_username` 参数、去掉 `ingress_host`；`create_instance` / `apply_cluster_state` 不再需要
`settings` 参数。前端的 `vite.config.ts` 与 `nginx.conf` 都补了 `/x` 代理规则（nginx 为此需要一条
新的 `map $http_upgrade $connection_upgrade`，否则 websocket 过不去）。`requirements.txt` 现在
直接声明 `httpx>=0.28.1` 与 `websockets>=14.0`（此前只是传递依赖，既然直接用就该直接声明）。
`GET /instances/{id}/ide-access` **不再签发票据**，只返回该实例的 IDE 地址
（Jupyter 是 `/x/{用户名}/{id}/lab`，vscode/webssh 是 `/x/{用户名}/{id}/`）；
`IdeAccess.expires_in` 从后端 schema 与前端类型中一并移除。`create_ide_ticket` / `decode_ide_ticket`
保留且仍有测试，但 `decode_ide_ticket` **没有生产调用者**。

**没有解决的那件事：这条路由不鉴权**

必须写清楚，因为这是本次改动最重要的属性：**`/x/` 对任何能访问平台的人都是一扇敞开的门。**
用户名是可见的、实例 id 是自增的，所以打开别人的 IDE 只需要改两个数字。而三个 IDE 又都以关闭认证的
方式启动（`--ServerApp.token=`、`code-server --auth none`、`ttyd --writable`），理由同样是
"前面会有一层鉴权"——**而那一层从来没有存在过**（票据签发过，却从未被校验）。

也就是说：**系统现在处于"两层同时为空"的状态。** 本次改动既没有改善也没有恶化这一点，
它只是把入口从"一个从不生效的 Ingress"换成了"一条真实存在、但不鉴权的反代"。
用户明确表示**暂时先不实现访问服务的鉴权凭证（票据）**，所以这是**已知、有意、经用户同意**的缺口，
不是疏漏 —— 但任何读过本文档的人都必须知道它，因此 §14.1 里也单列了一条。

补鉴权的位置**只有一处**：`_resolve_target`。**真正提供内容的两条路由**（HTTP 反代与 websocket）
都先经过它，在这里加一次判断就能覆盖全部。第三条（`GET /x/{用户名}/{id}` 的 307 跳转）**刻意不解析
实例** —— 它只是把地址补成带斜杠的形式，不做任何转发，也因此不需要判断；它指向的目标会再走一遍
上面那两条，所以"跳转本身不鉴权"不会漏掉任何东西（但它也**不是**一个可以顺手加检查的地方：在那儿
加检查只会让重定向失败，内容照样由后面的路由提供）。它现在对"没这个实例 / 不是你的 / 这个实例没有 IDE"
**一律返回 `NOT_FOUND`**，刻意不区分 —— 如果对"不是你的"回 403、对"不存在"回 404，就能靠状态码把
"谁有哪些实例"枚举出来。只有 `NOT_RUNNING` 是**单独**的：对一个自己刚点过"访问"、只是已停止的实例
回答"没有这个实例"，那是在撒谎，用户会以为实例被删了。

### 有意推迟（非遗漏）

- **OIDC** 仅预留扩展点：`OidcProvider.exchange_code()` 明确抛错说明。大学的统一身份是
  FreeIPA，接入 OIDC 需要校方先提供客户端注册；在拿到之前不写不可验证的代码。
- **实例状态依赖 Celery beat 是否在跑，页面读取不会就地推进。** 这是上一条的必然结果：
  `reconcile-workloads` 是 30 秒一次的唯一自动途径，所以 beat 没起来时，创建完成的实例会
  **一直停在「创建中」**（`podName` 也是 `None`，是判断这个问题最快的证据）。改法有两条——让
  列表/详情读取时就地推进一次，或至少在对账器停摆时明确提示——都还没做，因为前者会把一次
  K8s 调用放进读路径，是需要单独权衡的改动。
- **`library/` 里还没有带 IDE 的镜像（镜像已交付，待推送）。** `ide_command()` 假设镜像自带
  `jupyter`/`code-server`/`ttyd`，但 Harbor 的 `library` 项目下只有 `library/python`。在推入
  一个真正装了 Jupyter 的基础镜像之前，**任何 Jupyter 实例都会 `CrashLoopBackOff`**，且这与
  CPU/GPU 无关。镜像已写好并逐条验证：`deploy/images/ubuntu-dev/`（该目录的 README 记录了
  平台对实例镜像的完整契约、以及只有跑起来才会暴露的四个坑）。**剩下的是人工动作**：用
  `PUSH=1 deploy/images/ubuntu-dev/build.sh` 推到 `library/`。
- ~~**`vscode` 走不通子路径（平台侧缺口）**~~ **这条判断是错的，已实测推翻。** 当初的理由是
  "code-server 不支持 base path，没有等价参数，所以 VS Code 实例的静态资源会 404，修它需要
  `rewrite-target`"。实测：code-server 的 HTML 里 **8 个资源引用全部是相对路径**
  （`./_static/…`、`./manifest.json`、`stable-<hash>/static/…`），经反代后 8 个全部返回 200；
  workbench 的 websocket 用的就是页面自身的路径，也能连上并吐出真实的 VS Code 协议帧。
  **结论：code-server 不需要 base path —— 它的 HTML 只用相对引用，所以在反代里把前缀剥掉就够了。**
  真正需要保留的是"各 IDE 对前缀的期望不同"这个事实：Jupyter（`--ServerApp.base_url`）与
  ttyd（`--base-path`）要**收到完整前缀**，只有 code-server 要**剥掉前缀**（并在回程给根相对的
  `Location` 加回前缀）——这就是 `IDE_STRIPS_PREFIX` 的全部内容。
  **诚实的边界**：以上是在 HTTP 状态码 / 静态资源 / websocket 协议帧这一层验证的，
  **没有用真实浏览器打开过页面**，渲染层的最后一公里未经人工确认。
- `app/workers/celery_app.py` 的 `beat_schedule` 已接入 `reconcile-workloads`（30 秒）、
  `rollup-usage`（20 分钟）与 `sync-images`（默认每天一次，`IMAGE_SYNC_INTERVAL_SECONDS`
  可调）。最后一个是镜像从「推送到 Harbor」变成「选择器里可见」的唯一自动途径。
- **Prometheus 记账不会接入，这是有意为之**。Phase 6 的用量来自平台自身的记录；
  Phase 7 的 Grafana 只做「此刻在干什么」的展示。两者不交叉：把计费建立在 Prometheus 上
  会让采集中断直接变成少计费。**唯一的前置依赖是既有的 Prometheus 是否带
  `namespace` / `pod` 标签** —— 缺了它 GPU 利用率面板会是空的，而用量统计不受影响
  （见 14.4，以及 `deploy/grafana/README.md` 末尾的确认命令）。
- ~~`deploy/helm/`~~ 已随 Phase 8 交付（`deploy/helm/kube-wl-ai/`）。
- ~~`/auth/login` 的登录限流~~ 已随 Phase 8 交付（`app/auth/rate_limit.py`），未做的是**告警**：限流会 429，但不会主动通知任何人。
- ~~审计日志的读取接口~~ 已随 Phase 8 交付（`app/api/v1/audit.py` + `/admin/audit`）。
  仍然没有的是**审计的实时告警**：写入完整、可查询、可导出，但没人盯着，所以
  「有人正在爆破」这件事只有事后翻日志才知道。
- ~~`/compute` 与 `/admin/settings` 两块占位页~~ 已随 Phase 9 交付；
  前端**已无任何 `PlaceholderView`**，`config/navigation.ts` 的每个入口都通向真实页面。
- **未在真实集群上部署验证。** Helm chart 过了 `helm lint`、两条版本分支的渲染，
  以及针对真实 1.28 API Server 的 `kubectl apply --dry-run=server`，
  但**从未真正 `helm install` 过**。镜像仓库地址、StorageClass 名称、
  LDAP 服务账号口令都还是占位值。
- **`legacy/` 目录未删除。** 计划里 Phase 8 要删，但没有执行：
  它包含旧代码里泄漏的 kubeconfig 与硬编码 `SECRET_KEY`，删掉有利，
  但也是一份只有一次的存档。**先确认集群侧的凭证已轮换**，再决定要不要删。

---

### ✅ 后续增量 — 状态永远停在「创建中」：一次被回滚的提交（已完成）

用户报告：**前端提交成功、K8s 创建成功、数据库记录也在，但页面状态一直停在「创建中」，
也不会出现打开 IDE 的链接。** 查下来不是一个 bug，是四个；而最要命的那个让整套状态同步
机制**从来没有真正生效过一次**。

#### ① 提交被回滚：对账写下的状态从来没进过数据库

`app/workers/tasks/reconcile.py` 原来是三个裸的

```python
async with factory() as session:
    await instance_service.reconcile_all(session, gateway)
```

`reconcile_instance()` 只 `flush()`，**从不 `commit()`**。而 SQLAlchemy 的
`AsyncSession.__aexit__` 是 `close()` —— **close 会回滚未提交的事务**。于是每一轮对账都
正确读到了集群状态，写进会话，然后在退出时全部丢掉。

拿 `app.db` 的副本直接实测（用合法枚举值当哨兵）：

```
初始 status = creating
块内 flush() 成功（SQL 已发出）
块退出后 status = creating   pod_name = None      ← 改动被回滚
```

**为什么它藏了这么久**：HTTP 路径的 `get_session` 依赖在 handler 返回后 `commit()`，所以
`POST /instances/{id}/reconcile` 一直是好的，而**所有既有的对账测试都走那个门**——
`test_acceptance_phase3.py::test_reconcile_promotes_a_ready_pod_to_running` 就是，
它从头到尾都是绿的。

**这是疏漏，不是设计**，两条对照证据：

| 文件 | `factory()` 块 | `commit()` |
| --- | --- | --- |
| `workers/tasks/reconcile.py` | 3 | **0** |
| `workers/tasks/usage.py` | 1 | 1 |
| `workers/tasks/images.py` | 1 | 1 |
| `workers/tasks/nodes.py` | 1 | 1 |

更直接的一条：**`task_service.reconcile_all()` 自己 commit（`tasks.py:19`），
`instances.reconcile_all()` 不 commit** —— 所以任务状态一直会同步，实例状态永远不会。

连带后果比「状态不动」更糟。`expire_overdue()` 也只有 `flush()`，于是**最长运行时长这条
上限从来没有真正执行过一次**；而且停止动作把 Deployment 真的缩到了 0（集群侧生效），
回滚之后行里还写着 `running`，下一轮对账看到没有 pod，就判定「还在启动」—— 实例于是
既停不下来，也永不过期。

改法：**commit 归批处理入口自己所有**。`instances.reconcile_all()` 与 `expire_overdue()`
末尾各自 `await session.commit()`（照抄 `task_service.reconcile_all()` 已有的先例），
并在 docstring 里写明这不是细节、而是这个函数存在的理由。`reconcile_instance()` 保持
flush-only，因为它还要被 HTTP 端点组合进请求事务。

**第三个调用者，一模一样的 bug。** `app/cli.py` 的 `reconcile` 命令（运维手动救急会敲的
那一个）是同一份三连裸 `async with factory()`，同样没有任何 `commit()`，而且它还会打印
`对齐完成：实例 N 个状态变化，超时回收 M 个，任务 K 个状态变化。` 这句话撒了两个谎：
改动**全部被回滚**，而 N 数的是「检查了几个」不是「变了几个」（`task_service.reconcile_all`
数的却是变化数 —— 两边语义还不一致）。

现在 CLI 也改为调用 `sweep_all()`，并且 `instances.reconcile_all()` 与任务侧统一成
**数变化数**，于是那行摘要第一次是真话。真集群实测（先把实例 11 直接改回 `creating`）：

```
$ .venv/bin/python -m app.cli reconcile
instance_status_changed  current=running instance=test previous=creating
对齐完成：实例 1 个状态变化，超时回收 0 个，任务 0 个状态变化。
$ sqlite3 app.db "SELECT status,pod_name FROM instances WHERE id=11"
running|wl-test-11-5894b9bd87-87rjt        ← 真的落库了
```

计数的语义也顺带成了日志质量的支点：**只有在真有东西移动时才打 info**。若按「检查了几个」
计数，只要有实例存活，5 秒一次的 sweeper 就会永远重复打印同一行 —— 一条有用的日志就此
变成没人看的噪音。

#### ② 没有任何东西在跑对账：进程内 sweeper

原先唯一的自动触发者是 Celery beat，而本机 `REDIS_URL=redis://redis:6379/0` 里的 `redis`
主机名根本解析不了（那是 docker-compose 的服务名）—— **没有 broker 就没有 beat**。

⚠️ **一条要作废的推理**：早先「`ps` 里没有 celery 进程」这条证据是**无效的**。我的 bash
跑在 `bwrap --unshare-pid` 的私有 PID 命名空间里，**看不到宿主机进程**（`ss -ltnp` 也拿不到
pid）。结论成立靠的是另外两条独立证据：Redis 主机名解析失败，以及实例卡住 17 分钟、
`pod_name` 为 NULL 的真实观测。

新增 `app/workers/sweeper.py`：在 FastAPI 的 lifespan 里起一个 asyncio 后台任务，按
`RECONCILE_INTERVAL_SECONDS`（默认 **5 秒**，设 `0` 关闭）调一次对账；每个 tick 独立
try/except，一次集群抖动不会杀死循环；关闭时 `cancel()` 并 await（保证没有 tick 在引擎被
拆掉的同时还在开会话）；集群调用都走 `run_gateway`（即 `asyncio.to_thread`），**不阻塞
事件循环**。仅在 `k8s_enabled` / `k8s_in_cluster` 为真时启动 —— 否则它会把假网关编出来的
状态写进真行。

对账逻辑本身被提到 `app/services/reconcile.py::sweep_all()`，**Celery 任务和进程内 sweeper
调用同一份代码**，两者不可能再各自漂移。**引擎管理留在调用方**：Celery 是同步进程，必须
自己建/拆引擎；sweeper 跑在 Web 进程里，每 5 秒 dispose 一次等于把 API 的数据库从底下抽走。

多 worker 部署应设 `RECONCILE_INTERVAL_SECONDS=0`，让 beat 当唯一的对账者。两者同时跑是
浪费但无害：所有状态都从集群推导而来，不是计数器，两个 sweeper 抢着跑会到达同一状态。

#### ③ 前端列表是快照

`InstancesView.vue` 原先只在 `onMounted` 和每次操作之后 `load()` 一次，没有轮询。改为
**自适应轮询**：列表里只要还有未安定状态（`pending` / `creating` / `stopping` /
`deleting`）就每 **3 秒**刷一次，全部安定后自动停表（一个没人启停的列表产生**零**额外流量）；
另外在 `visibilitychange` 回到前台时立刻刷一次（后台标签页的定时器会被节流到分钟级，
否则切回来的瞬间看到的可能是几分钟前的数据）。后台轮询失败**不弹 toast**：下一次 tick
大概率就成功了，而每 3 秒重复同一句错误对用户没有帮助。

**没有做 SSE**：它能在用户盯 8 秒的界面上省下 1~2 秒，代价却是一个流式端点、nginx 的
`proxy_buffering off`、因为 `EventSource` 发不了 header 而不得不塞进 query string 的 token，
以及一条必须写对的重连路径。

#### ④ IDE 按钮被状态闸门锁死 —— 这是「打不开 IDE」的直接原因

`INTERACTIVE_STATUSES = ['running']`，模板里 `:disabled="!isInteractive(item)"`。但后端
**早就在 `creating` 状态下给出完全可用的 IDE**：

```
实例 11：status='creating'  idePath='/x/zhenghuanbo/11/lab'
GET /api/v1/instances/11/ide-access -> 200 {"url":"/x/zhenghuanbo/11/lab"}
GET /x/zhenghuanbo/11/lab           -> 200  <title>JupyterLab</title>
```

**IDE 一直在正常工作，只是 UI 不让人点。** 改为**有 IDE 就能点**（只剩
`v-if="item.ideType !== 'none'"`），让反代当权威。`isInteractive` 保留给终端按钮和
「终端」标签页——shell 确实需要容器活着。

这个选择是安全的，因为反代的 503 本来就已经把两种情况分开了：实例没在运行 →
`该实例未在运行，请先启动它。`；连不上 IDE → `IDE 暂不可用 / 无法连接实例的 IDE，
它可能正在重启。实例可能仍在创建中，稍后刷新本页即可。`

顺带删掉了详情页那句过期文案（「IDE 链接由后端签发一次性短时票据…几分钟后失效」）——
票据已随反代方案停用，现在写明**地址里不含任何凭证**、拿到链接的人都能打开。

#### 顺带查出来的一个事实：`running` 并不等于 IDE 在服务

Deployment **没有任何 `readinessProbe`**（`readinessProbe = None`，liveness 也没有）。
k8s 的 `Ready` 因此只表示容器进程起来了，而 JupyterLab 还要几十秒才监听 8888。实测：

```
 t      status      IDE
 0.7s  creating    503  "IDE 暂不可用 …实例可能仍在创建中，稍后刷新本页即可。"
 4.3s  running     200
```

所以 `running` 之后仍可能短暂 503。**没有顺手加就绪探针**：那会把 `running` 推迟到 Jupyter
真正监听之后（对慢镜像可能是几十秒），同时拖慢 SSH，并改变配额语义（未就绪的 pod 照样占
配额）。这是语义变更，应当单独决定。

#### 验证

- 新增 `tests/test_reconcile_sweeper.py`（8 项）。**关键是它确实在测东西**：临时抽掉
  `reconcile_all()` 的 `commit()` 复跑，`test_a_sweep_persists_what_it_observed` 与
  `test_the_session_shape_that_caused_the_bug_now_persists` 精确失败在
  `assert creating is running`，恢复后全绿。断言一律**换一个新会话来读**——用同一个会话读
  什么也证明不了，identity map 会把刚改过的对象原样还给你。
- **真集群、无人干预**：把实例 11 直接改回 `creating` 并清空 `pod_name` / `node_name`，
  然后完全不碰它：
  ```
  t= 4s  status=creating  podName=None
  t= 8s  status=running   podName=wl-test-11-5894b9bd87-87rjt nodeName=10.28.0.194
  ```
- **真集群、全新创建**：`POST /instances` → 201 `creating`，**3.4 秒后自己变成 `running`**，
  全程没有人刷新页面或点任何按钮。
- 门禁：`ruff check .`、`ruff format --check .`（165 文件）、`mypy app`（135 文件）全部通过；
  `pytest` **738 passed**（原 730 + 新增 8）；前端 `vue-tsc --noEmit` 与 `vite build` 通过。
- 顺手修掉两处名实不符的注释：`instances.py` 里指向反代的路径写成
  `app/api/x/proxy.py`（应为 `app/api/ide/proxy.py`）；`IdeType` 的 docstring 仍说
  「通过 ingress 暴露」。

#### ⑤ 一次 sweep 每个实例发一次 LIST → 改为每个命名空间一次

**动机**：`reconcile_all()` 原来对每个实例调一次 `reconcile_instance()`，也就发一次
`list_namespaced_pod(ns, "wl-instance={name}")`，成本随「有多少实例在跑」线性增长。改为
**按命名空间分组、每个命名空间一次 `wl-managed=true` LIST**，再用 `wl-instance` 标签把
pod 分派回各自的行；成本变成随「有多少命名空间在用」增长——一个更小、增长更慢的量。

**为什么标签是唯一可靠的分派键**：pod 名是 `{deployment}-{replicaset hash}-{random}`
（`wl-test-11-5894b9bd87-87rjt`），而且 k8s 会截断过长的名字，所以解析 pod 名会**恰好
在名字最长的实例上静默分派出错**。`wl-instance` 在 pod 模板的 labels 里（真实 pod 实测：
`pod-template-hash=5894b9bd87,wl-instance-id=11,wl-instance=test,wl-managed=true,wl-workspace=hpc`），
而 `Workspace.k8s_namespace` 是 `unique=True`（namespace ⇔ workspace 一一对应）、实例名
在 workspace 内唯一，所以标签在这个命名空间里是无歧义的键。

**顺带：为什么必须忽略「没有 `wl-instance` 的 managed pod」**。任务 Job 的 pod 同样带
`wl-managed=true`（`build_task_objects()` 的标签），但它们属于另一张表的行。忽略它们是
**一段活代码，不是防御性写法**——所以它有专门的测试。

改动形状：`reconcile_instance()` 拆成「判据 + LIST + `_absorb_observation()`」，批处理路径
把同一次 LIST 的结果喂给同一个 `_absorb_observation()`，两条路径因此不可能对「一个 pod
意味着什么」产生分歧（正是 `tasks.py` 里 `reconcile_task` / `_absorb` 早就用的形状）。
命名空间 LIST 失败**只影响该命名空间**，其余实例照常观测。

**验证（真集群、2 个实例、同一命名空间）**——把旧形状（逐实例调 `reconcile_instance`）与
新形状跑在同一个真实集群上：

```
OLD SHAPE listings for 2 instances in ONE namespace: 2
  wl-hpc-hpc  wl-instance=dev
  wl-hpc-hpc  wl-instance=dev2
```

新形状：**1 次** `list_pods('wl-hpc-hpc', 'wl-managed=true')`，返回 2 个 pod，
`instance_name='test'` / `'probe2'` 各归其主。测试断言的是**调用参数本身**
（`== [(ns, MANAGED_LABEL_SELECTOR)]`）加上两行各自 `running` 且各自对应自己的 pod——
只断言「调用一次」的话，「只发一次但把同一个 pod 发给两行」也能骗过去。

#### ⚠️ 测量这个改动时我犯的两个方法论错误（值得记住）

两次 A/B 都是错的，而且两次都给出了「看起来像结论」的数字：

1. 第一次：新形状的计时包含它自己的行 SELECT，而我手写的旧形状把 SELECT 放在计时区外
   → 得出「新的慢 4.5 倍」。
2. 第二次修正了 SELECT，但旧形状结尾 `rollback()`、新形状 `commit()` → 又得出
   「commit 是瓶颈」。

逐阶段插桩后才看到真相：

```
SELECT live rows                   131.94 ms
group by namespace                   0.02 ms
one LIST per namespace              17.20 ms
group pods by label                  0.01 ms
_absorb_observation(test)            3.35 ms
_absorb_observation(probe2)          2.56 ms
commit                               1.48 ms
```

**commit 只要 1.48 ms，SELECT 要 131.94 ms。** 在 2 个实例上这次合并每 tick 只省下约
9 ms（一次 LIST 的边际成本）——它的收益随实例数增长，在 2 个实例上量不出来。

#### 🔴 由此查出的真问题：加载一个 `Instance` 实体要 ~130 ms（**未修**）

顺着那个 131.94 ms 查下去，分层定位（同一份数据、同一个查询）：

| 路径 | 中位耗时 |
| --- | --- |
| 裸 `sqlite3`（开+查+关） | 1.38 ms |
| `engine.connect()` + `text()` | 0.62 ms |
| 引擎 + ORM `select(Instance)` | 1.22 ms |
| **AsyncSession + `select(Instance)`** | **126.75 ms** |
| AsyncSession + `text("SELECT 1")` | 0.38 ms |
| AsyncSession + `select(Instance.id)` | 0.57 ms |

不是锁竞争（**孤立副本一样慢**，中位 136 ms）、不是异步包装（`SELECT 1` 0.38 ms）、
不是 SQL（编译出的语句只有 6 个 JOIN，返回 2 行、**1.79 ms**）。

真因是**级联的 `selectin` 预加载**：`Instance` 的 4 个 `lazy="joined"` 关系指向
`Workspace` / `User` / `ResourceGroup` / `ResourceSpec`，而这些实体各自又带一批 `selectin`
关系（`User.group_links` / `role_links` / `ssh_keys` / `api_tokens` / `workspace_memberships`、
`Workspace.members` / `resource_grants`、`ResourceGroup.specs` / `limits`）。**加载一行实例
会连带发出约 10 条额外的 `SELECT … IN (…)`。**

用户可见的代价（本机实测，8 次取样中位）：

```
GET /healthz                    1.1 ms
GET /instances                170.7 ms
GET /instances/11             159.7 ms    ← 单个实例也要这么久
GET /users                     76.4 ms
GET /workspaces                85.3 ms
```

**当时没有一并修**：把那些 `selectin` 改成按需加载会改变整个 API 的加载语义（哪些序列化器
依赖它们要逐个审），而且它与「减少集群 LIST」是两件独立的事。**建议的下一步**是在调用点
显式收窄（`reconcile_all()` 只需要 `instance.workspace.k8s_namespace` 与行自身的列），而不是
改模型默认值 —— 这一步随后就做了，见 ⑥。

**本轮的其余验证**：门禁 `ruff check .` / `ruff format --check .`（166 文件）/ `mypy app`
（135 文件）全部通过；`pytest` **741 passed**（上轮 738 + 新增 3）；真集群上第二个实例经
新的批量对账**在无人干预下到达 `running`**，随后已删除，集群只剩实例 11、无残留工作负载。

#### ⑥ 收窄实例加载：一个查询从 72 条语句降到 1 条

**做法**：在每个查询点声明它真正读的东西，其余一律拒绝。新增两个选项元组：`_SWEEP_LOAD`
（对账用，只放行 `workspace`）与 `_READ_LOAD`（序列化用，放行 `workspace` / `owner` /
`resource_group`），两者都以 `raiseload("*")` 开头，再显式 `joinedload()` 放行需要的那个。
应用到四处：`list_instances`、`get_instance`（`session.get(..., options=...)`）、
`reconcile_all`、`expire_overdue`。

**为什么不改模型默认值**：那些集合在别处是**有意**用的（SSH 密钥接口就读 `user.ssh_keys`）。
把 `selectin` 改成惰性，会把每一处这样的使用变成 async 惰性加载失败。`raiseload` 顺带成了
告警机制：谁开始读一个他没声明的属性，会拿到 `InvalidRequestError`，而不是静默的 `None`。

**踩到的两个坑**（都靠实测才发现，读代码看不出来）：

1. **`raiseload` 链式调用会把路径移到子级**。`joinedload(Instance.owner).raiseload(User.ssh_keys)
   .raiseload(User.api_tokens)` 直接报 `"User.api_tokens" does not link from relationship
   "User.ssh_keys"` —— 同级多个选项必须写成 `.options(raiseload(a), raiseload(b), ...)`。
2. **逐条 `raiseload` 只部分生效**。按 9 个集合逐个写，语句数只从 72 降到 14
   （`user_groups` / `user_roles` / `ssh_keys` / `api_tokens` 仍在发查询）；换成
   `raiseload("*")` 通配 + 显式放行后**降到 1 条**。所以最终形状里没有任何逐条的 `raiseload`。

**真实函数前后对比**（同一份数据、直接调用服务函数，语句数由 `before_cursor_execute` 计数）：

| 调用 | 语句数 | 中位耗时 | 倍数 |
| --- | --- | --- | --- |
| `reconcile_all()` 旧 | 74 | 151.00 ms | |
| `reconcile_all()` 新 | **3** | **18.99 ms** | **8.0×** |
| `list_instances()` 旧 | 73 | 123.60 ms | |
| `list_instances()` 新 | **2** | **2.13 ms** | **58×** |

HTTP 层（本机 10 次取样中位；`/healthz` 与两个未改动的接口作对照）：

```
GET /healthz                    1.1 ->   1.1 ms   (  1.0x)   对照：无 ORM
GET /instances                170.7 ->  15.7 ms   ( 10.9x)
GET /instances/11             159.7 ->  14.2 ms   ( 11.2x)
GET /users                     76.4 ->  72.9 ms   (  1.0x)   未动
GET /workspaces                85.3 ->  84.0 ms   (  1.0x)   未动
```

详情字段抽查确认收窄没有削掉序列化需要的数据：`workspaceName='wl-hpc-hpc'`
`ownerUsername='zhenghuanbo'` `resourceGroupName='cpu'` `idePath='/x/zhenghuanbo/11/lab'`。
（`resourceGroupName` 取的是 `resource_group.display_name` —— 所以 `resource_group` 必须留下。）

**顺带修掉 batch 路径里按实例重复的那次级联**：`_absorb_observation()` 的
`session.refresh(instance)` 走的是**映射器默认**策略，于是每个实例都要再来一遍级联（单实例
就 18 条语句，且随实例数线性放大）。改成 `refresh(instance, attribute_names=[...])`，只重载
该函数真正写的那几列，`reconcile_all()` 因此从 36.94 ms 再降到 18.99 ms。**这一步很关键**：
收窄了 SELECT 却不收窄 `refresh`，等于把级联从「每 tick 一次」变成「每实例一次」，规模越大
越糟。

**`get_instance()` 在进程内测不出来**：单独测它是 0 条语句 / 0.05 ms —— 因为同一个会话里
前一次 `list_instances()` 已经把它放进 identity map 了。又一个「同一个会话证明不了什么」的
例子，它只能从 HTTP 层测（159.7 → 14.2 ms）。

**还没收窄的同类加载点**（本次按约定只做 A+B）：`api/ide/proxy.py:285`（每次 IDE 请求都走）、
`services/quota_guard.py:70`、`services/storages.py:841`、`services/usage.py:295`，以及
`create_instance()` 里的重名检查。它们各自要读的字段不同，需要分别核实后再各自声明。

**验证**：门禁全绿（`ruff check` / `ruff format --check` 166 文件 / `mypy app` 135 文件）；
`pytest` **745 passed**（741 + 新增 4）。新增的 `tests/test_instance_load_shape.py` 是回归守卫：
它数语句而不是测时间，上限设 8（收窄后实测 2 和 3，回退后是 73）。**并且验证过这个守卫
不是装饰**：临时把 `_READ_LOAD` / `_SWEEP_LOAD` 换成空元组复跑，两项分别报出
`statements without _READ_LOAD: 73 (ceiling 8)` 与 `73`，即真的会失败；验证用的临时文件已删除。
另有 `test_the_read_shape_still_carries_what_the_detail_page_shows`（防止收窄过头）与
`test_the_sweep_shape_refuses_an_attribute_it_never_declared`（钉住「误读要响亮失败」这一属性）。

---

### ✅ 后续增量 — 比「最小规格」大的实例永远创建不出来（已完成）

**现象**：用户在网页创建了一个 4 CPU 的 VS Code 实例（id 16），页面一直「创建中 / 等待容器创建」，
但集群里**一个 pod 都没有**，Deployment 停在 `0/1`。

**排查的关键在「没有 pod」本身**：镜像拉不动、PVC 不存在、节点调度不上去，都会留下一个
Pending / ContainerCreating 的 pod —— 可见的东西。**完全没有 pod，说明创建 pod 的请求被 API server
拒绝了**，而准入控制发生在对象落库之前。决定性证据在 ReplicaSet 的事件里：

```
Warning FailedCreate  Error creating: pods "wl-vscode-test-16-76dbc87745-hfbtx" is forbidden:
                      maximum cpu usage per Container is 2, but limit is 4
```

#### 缺陷 A：上限取了「最小规格」

`services/workspaces.py` 的 `_effective_limit_spec()` 用**同一个** `smallest` 规格同时决定了
`default_cpu` / `default_request_cpu` **和 `max_cpu`**（memory 同理）。它原来的 docstring 写着
「Using the smallest spec keeps an un-annotated pod from defaulting to a shape the user never asked
for」——这个理由对 `default*` 完全成立，对 `max` 则正好相反：**上限等于最小规格，等于目录里所有更大
的规格永远无法调度**。而 LimitRange 是准入控制，所以失败方式是最难查的那种：pod 根本不存在，
没有任何东西留给对账去看。本池子提供 `2cpu-8g` 与 `4cpu-8g` 两档，于是较大的那档**必定失败**。

而且平台的配额模型里**根本没有「每实例 CPU 上限」这个概念**（`resource_group_limits` 只有
`max_cpu_milli_per_user` / `max_memory_mb_per_user` / `max_gpu_per_instance` /
`max_concurrent_instances`）。按策略 4 CPU 完全合法，唯一说不的就是这个 LimitRange —— 平台一边在
目录里提供它，一边禁止它。*（列名是当时的：这四个里后三个已删除，单位也改为核/GB，见 §5.3；
结论不变。）*

**更糟的推论**：取的是「所有已授予资源组里最小的规格」。以后往池子里加一个更小的规格（例如
500m/1Gi），下一次工作空间重下发就会把 `max.cpu` **降低**，于是**已经在跑的实例将无法再启动**
（`start_instance` 只是把 ReplicaSet 扩到 1，重建 pod 同样被拒）。

**修法**：一次往返同时取两端（`func.min` / `func.max`）。`default*` 取最小，`max_cpu` /
`max_memory` 取最大，并且**两个维度各自独立**取最大——最大的 CPU 与最大的内存未必来自同一个规格，
这一点由测试刻意交叉两个规格来钉住。

#### 缺陷 B：失败了却永远显示「创建中」

sweep 唯一的输入是 pod，而「还没起来」和「永远起不来」产生**逐字节相同**的观测，于是走
`_absorb_observation` 的空分支 → `creating` + 「等待容器启动」，永不升级、永不报错。原因是平台
**从不读 Deployment 的状态、条件或事件**：gateway 只有 `read_pod` / `read_job` / `read_service` /
`read_pvc` / `read_pv`，那条 `ReplicaFailure` 一直摆在那里没人问。

**修法**：
- gateway 增加 `read_deployment_status()`（Protocol / 真实 client / Fake 三处），读 `ReplicaFailure`
  条件的 `message`；
- `_absorb_observation()` 多接一个 `deployment_failure`：没有 pod **且** Deployment 有抱怨时落
  `status=error` + `集群拒绝创建容器：<集群原话>`；
- **只在没有 pod 时才去问**。健康实例永不付这笔钱，`test_a_healthy_instance_is_never_asked_about_its_deployment`
  钉住了这一点（它断言 `gateway.calls` 里没有 `read_deployment_status`）。
- `error` 本来就在 `LIVE_INSTANCE_STATUSES` 里，所以 sweep 会继续观察一个已被判失败的实例，
  **pod 一出现就自行转 `running`**，旧消息被清掉——修好的因必须是可观测的。

**为什么不是「每命名空间一次 LIST」**：那要先给 Deployment 打上 `wl-instance` 标签再批量列，
为了一个「只在已经出错时才存在」的场景加一个常数因子不划算。取舍写在了 `_deployment_failure()` 的
docstring 里，包括最坏情况：一个命名空间里**全部**实例都没有 pod 时（一个坏掉的上限会同时打断所有
实例），代价是每实例一次 GET，而不是每命名空间一次。

#### 验证

- 门禁全绿；`pytest` **751 passed**（745 + 新增 6）。
- **两个守卫都验证过不是装饰**：A 把 `max_*` 改回取最小 → 新测试报 `assert '4' == '8'`；B 让
  `_deployment_failure()` 恒返回 `None` → 3 个测试分别报 `assert 0 == 1`、
  `creating is not InstanceStatus.ERROR`、`'creating' == 'error'`。两处临时改动都已还原并复查。
- **真集群、就是那个故障实例 16**：修前 `kubectl create --dry-run=server` 一个 4 CPU 的 pod →
  `Forbidden: ... maximum cpu usage per Container is 2, but limit is 4`（精确复现）；执行
  `POST /api/v1/workspaces/1/reconcile` 重下发后 `max.cpu` 由 2 变 4、而 `default.cpu` 仍是 2 →
  同一个 pod 变成 `created (server dry run)`。**用服务端 dry-run 是有意的**：它走完整准入链
  （含 LimitRange）但不落库，所以能在不删除、不改动任何既有对象的前提下证明准入结果。
- 随后实例 16 的 ReplicaSet 重试成功，实例**自行**由 `error` 变 `running`（sweep 在一个 tick 内
  观测到 pod），`GET /x/zhenghuanbo/16/` 返回 200 —— 缺陷 A 与 B、以及用户最初报的现象，在同一个
  实例上一并闭环。
- 一处**方法误差认领**：`podName` / `lastSeenAt` 在**列表**接口里恒为 `None`（二者只存在于
  `InstanceDetail`），所以一度看起来像「running 却没有 pod」。是接口字段问题，不是 bug；记录在此
  以免下次再被同一件事误导。

---

## 14. 已知问题与陷阱

### 14.1 安全（最高优先级）

1. **历史凭证已泄漏**：`legacy/backend/app/common/k8s/config/*.yaml` 曾提交明文 kubeconfig token，
   `legacy/backend/config.py` 曾硬编码 `SECRET_KEY`。文件已在归档时清除，但
   **凭证本身必须在集群侧吊销并轮换**。
2. 旧 `k8sclient.py` / `test.py` 硬编码 Windows 绝对路径 `D:\vscodeProject\...`（已在 `legacy/`，勿复制）。
3. **IDE 反代路由不鉴权（当前最大的已知风险，且是有意推迟的）。**
   `GET /x/{用户名}/{实例 id}` 这条路由（以及它下面的全部 HTTP 方法与 websocket）**不校验任何身份**：
   用户名可见，实例 id 是自增整数，所以**猜出别人的 URL 就能打开别人的 IDE**。而三个 IDE 都按
   "前面会有一层鉴权"的假设**关闭了自身认证**（`--ServerApp.token=`、`code-server --auth none`、
   `ttyd --writable`），本该做这件事的票据又从未被校验 —— 即**两层同时为空**。
   实际后果不是"看到别人的代码"而是"**以那个用户的身份执行任意代码**"：容器挂着他的 home 卷、
   能用他的算力、并且能访问集群内部（实例的 NetworkPolicy 入站全放行、出站覆盖所有命名空间）。
   **在补上鉴权之前不要把它暴露到平台可达范围之外**（例如直接给实例开 NodePort、或加装 Ingress
   而不加 `auth-url`）。补的位置只有一处：`app/api/ide/proxy.py` 的 `_resolve_target` ——
   它现在对"没这个实例 / 不是你的 / 这个实例没有 IDE"一律返回 `NOT_FOUND`（不泄漏存在性），
   另外单独返回 `NOT_RUNNING`（对自己刚点过"访问"的已停止实例回答"没有这个实例"是撒谎）。
   详见"IDE 暴露改为平台后端反代"一节。

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
- **一个实例不止一个 Service**：SSH 是 `NodePort`、IDE 是 `ClusterIP`（**只经平台后端反代进入**，
  不建 Ingress）。把两个端口放进同一个 NodePort Service，等于在校内每个节点地址上发布一个**免鉴权的
  IDE** —— 而反代这条路**今天同样免鉴权**（见 §14.1 第 3 条），所以这条注意事项保住的是
  "不要再多开一个入口"，**不是"IDE 已经安全了"**。
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

### 14.4 待外部确认（阻塞后续阶段，不是缺陷）

这几项都不是代码问题，但**都必须先有答案再写代码**，否则写出来的是不可验证的实现：

1. **既有的 Prometheus 是否带 `namespace` / `pod` 标签？** 没有这两个标签就无法把指标
   归到租户上，"用量按 Prometheus 记账"就无从谈起。这是 Phase 7 的前置问题（详见 §10）。
2. ~~**集群里是否存在 RWX 的 StorageClass？**~~ **已彻底不再是问题**。托管存储卷用静态
   `hostPath` PV（`storageClassName: ""` + `volumeName`），完全不经过 StorageClass，而
   `hostPath` 本来就可以声明 `ReadWriteMany` —— 已在真实集群上验证组共享/全局共享卷能 `Bound`。
   原先"仍然相关"的那半句（实例与任务的临时盘走 `STORAGE_CLASS_HOME/DATA/SHARE`）也随第二步
   消失了：**工作负载不再有自己的盘**，只挂已经登记好的用户卷。于是 `STORAGE_CLASS_*` 这三个
   设置项目前**没有任何消费者**，系统页把它们从 warning 降为 info —— 保留它们是为了将来换
   GPFS/Lustre 时接 CSI 的 StorageClass，而不是因为它们现在还有用。
3. **LDAP 服务账号凭据**：目录同步需要一个可绑定账号；没有它，`group:sync` 只能跑假实现。
4. **kubeconfig token 轮换**：`legacy/` 里泄漏的那个（见 14.1）必须在集群侧吊销。

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
| **节点 ↔ 资源池归属** | `backend/app/services/resources.py`（`resolve_node_pool_id` / `sync_nodes` / `set_group_nodes`）、`backend/app/services/workload_common.py`（`pool_node_selector`） |
| **K8s 抽象与假实现** | `backend/app/k8s/gateway.py`（`FakeK8sGateway` 供测试） |
| K8s 真实客户端 | `backend/app/k8s/client.py`（唯一 import `kubernetes` 的模块） |
| 命名空间 / 标签命名规则 | `backend/app/k8s/naming.py` |
| 工作空间与资源 API | `backend/app/api/v1/workspaces.py`、`backend/app/api/v1/resources.py` |
| Celery | `backend/app/workers/celery_app.py` |
| **状态同步 / 对账（唯一把观测状态写回数据库的路径）** | `backend/app/services/reconcile.py`（`sweep_all`，两个调用者共用）、`backend/app/workers/sweeper.py`（进程内，`RECONCILE_INTERVAL_SECONDS` 默认 5 秒）、`backend/app/workers/tasks/reconcile.py`（Celery beat，30 秒）、`backend/app/services/instances.py`（`reconcile_all` / `reconcile_instance` / `expire_overdue`，**批处理入口自己 commit**） |
| 实例状态实时刷新（前端） | `frontend/src/views/InstancesView.vue`（未安定状态时 3 秒自适应轮询 + `visibilitychange`） |
| **实例加载策略（收窄 eager 级联：一个查询 72 条语句 → 1 条）** | `backend/app/services/instances.py`（`_SWEEP_LOAD` / `_READ_LOAD`，都以 `raiseload("*")` 开头再显式放行）、回归守卫 `backend/tests/test_instance_load_shape.py` |
| **实例规格上限（比最小规格大的实例永远创建不出来）** | `backend/app/services/workspaces.py`（`_effective_limit_spec`：`default*` 取最小规格、`max_cpu`/`max_memory` 取最大，且两个维度各自独立）、`backend/app/services/instances.py`（`_deployment_failure` + `_absorb_observation` 的 `error` 升级与自愈）、`backend/app/k8s/gateway.py`（`DeploymentStatus` / `read_deployment_status`，Fake 用 `deployment_failures` 脚本化）、回归守卫 `backend/tests/test_reconcile_sweeper.py` 与 `backend/tests/test_acceptance_phase2.py` |
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
| **持久化存储（后端）** | `backend/app/services/storages.py`、`backend/app/services/storage_paths.py`（目录推导的唯一来源）、`backend/app/api/v1/storages.py` |
| **镜像目录（后端）** | `backend/app/services/images.py`、`backend/app/api/v1/images.py` |
| **镜像仓库客户端** | `backend/app/registries/`（`registry.py` 协议 / `harbor.py` 真实 / `fake.py` 测试） |
| 存储与镜像前端 | `frontend/src/views/StorageView.vue`、`frontend/src/views/ImagesView.vue` |
| 镜像选择器（实例/任务/应用表单复用） | `frontend/src/components/image/ImagePicker.vue` |
| 存储与镜像类型 / API | `frontend/src/types/storage.ts`、`frontend/src/types/image.ts`、`frontend/src/api/storages.ts`、`frontend/src/api/images.ts` |
| **用量聚合与查询（后端）** | `backend/app/services/usage.py`、`backend/app/api/v1/usage.py` |
| 用量日汇总表 | `backend/app/models/usage.py`（`UsageDaily`） |
| 用量定时重算 | `backend/app/workers/tasks/usage.py`、`backend/app/workers/celery_app.py` |
| 用量中心前端 | `frontend/src/views/UsageView.vue`、`frontend/src/api/usage.ts`、`frontend/src/types/usage.ts` |
| 图表宿主（ECharts 生命周期/resize/降级） | `frontend/src/components/common/EChart.vue` |
| **监控范围与清单（后端）** | `backend/app/services/monitor.py`、`backend/app/api/v1/monitor.py` |
| Grafana 面板编目与签名令牌 | `backend/app/monitoring/grafana.py` |
| Grafana 部署（生成器/面板/provisioning/说明） | `deploy/grafana/`（`build_dashboards.py` 带越权自检） |
| 监控中心前端 | `frontend/src/views/MonitorView.vue`、`frontend/src/api/monitor.ts`、`frontend/src/types/monitor.ts` |
| 版本单一来源 | `backend/app/core/version.py`（`VERSION` / `APP_NAME` / `build_info()`） |
| **系统信息与配置体检（后端）** | `backend/app/services/system.py`、`backend/app/api/v1/system.py` |
| 系统信息读模型 | `backend/app/schemas/system.py`（金丝雀测试见 `tests/test_acceptance_phase9.py`） |
| 系统设置前端 | `frontend/src/views/admin/SystemView.vue`、`frontend/src/api/system.ts`、`frontend/src/types/system.ts` |
| 计算资源前端（全员可读） | `frontend/src/views/ComputeView.vue`（节点段按 `node:read` 条件渲染） |
| **部署冒烟验收** | `scripts/smoke-env.sh` + `scripts/smoke_phase1.py` … `scripts/smoke_phase9.py` |
| **前端契约校验** | `scripts/check-frontend-types.py`（新请求体类型记得同步 `PAYLOAD_ALIASES`） |
| **前端视图冒烟** | `scripts/smoke_frontend_views.mjs`（`happy-dom` 里真实挂载 + 内容断言 + `EXPECTS_ABSENT` 反向断言 + `VIEW_PERMISSIONS` 按视图改角色） |
| 旧实现参考 | `legacy/README.md` |
