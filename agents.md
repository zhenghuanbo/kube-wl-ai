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
`quotas(scope_type=user|group|workspace, ...)`

> `gpu_type` 是**字符串枚举**（如 `v100` / `a100` / `h100`），**不是**整数。

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

`storages(kind=personal_home|personal_data|group_share|dataset|object, capacity_gb, used_gb,
storage_class, access_mode, medium, pvc_name, k8s_namespace, source, size_bytes, is_read_only,
status, status_message, checked_at, workspace_id, group_id, owner_user_id, created_by, is_active)`

`instance_storages(instance_id, storage_id, mount_path, is_read_only)` —— 挂载关系是显式的
多对多，因为一个卷可以同时被多个实例挂载，而挂载点随实例而变。

> `medium` **只是记录**，永远不发给集群：`PersistentVolumeClaim` 没有这个字段（那是
> `emptyDir` 的）。它留着是为了让界面能提示某个 StorageClass 是 tmpfs —— 那种卷重启即
> 清空，而且占用的是内存、不计入配额。`K8s` 层的 `PvcSpec` 上确实没有它。
>
> `access_mode` 对 `dataset`/`group_share` 默认 `ReadWriteMany`，其余默认 `ReadWriteOnce`。

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
- WebSocket 握手使用**短期一次性 ticket**，不复用 access token。
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

### ✅ Phase 5 — 镜像与存储（已完成）

**交付（后端）**

| 交付物 | 位置 |
| --- | --- |
| 存储/镜像枚举 | `app/models/enums.py`（`StorageKind`、`StorageStatus`、`LIVE_STORAGE_STATUSES`、`ImageKind`、`ImageVisibility`） |
| 数据模型 | `app/models/storage.py`（`Storage`、`InstanceStorage`）、`app/models/image.py` |
| 迁移 | `alembic/versions/d0d900699fb4_*.py`（新增 3 张表，head 共 **30** 张，`downgrade base` 可往返） |
| K8s 抽象 | `VolumeMountSpec`、`PvcInfo`、`read_pvc`、`resize_pvc`、`parse_storage_gb`、`build_storage_objects` |
| 服务 | `app/services/storages.py`、`app/services/images.py` |
| Schema | `app/schemas/storage.py`、`app/schemas/image.py` |
| 镜像仓库客户端 | `app/registries/`（`registry.py` 协议 + `harbor.py` 真实实现 + `fake.py`，与 K8s gateway 同构） |
| API | `app/api/v1/storages.py`（7 个端点）、`app/api/v1/images.py`（7 个端点） |
| 权限 | `storage:read/create/resize/delete/manage`、`image:read/create/share/delete`；本期新增 `storage:manage`，并把 `image:share` 从普通用户上收到组管理员（组管理员 **44** 项，普通用户 **32** 项） |
| 实例挂载 | `InstanceCreate.storageIds` → `build_instance_objects(extra_mounts=...)` |
| 验收测试 | `tests/test_acceptance_phase5.py`（**33** 项）、`tests/test_acceptance_phase5_images.py`（**36** 项） |
| 冒烟脚本 | `scripts/smoke_phase5.py`（**65** 项，可重复执行） |

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
   `policy.scope_filter()` 的原因：个人盘只有本人可见，而 `group_share`/`dataset`
   对全组可见 —— 同一张表上两类行的可见规则不同，行级 scope 表达不了。
3. **附加存储不重复计配额**。容量在**创建存储时**结算一次；实例挂载它时不再计入
   `storage_gb`，否则同一个 GB 会被收两次费，工作空间会莫名撞上配额上限。
4. **失败的操作 ≠ 失败的卷**。扩容被集群拒绝时，容量与**状态都保持原样**，
   不标记 `FAILED` —— 卷仍然是 Bound、仍然可用，把它标红会让用户去追一个不存在的问题。
5. **数据集强制只读，且只读落在挂载点上**。`read_only` 同时写在 volume 和 mount 两处，
   否则「只读」只是 API 响应里的一句话，训练任务照样能写穿。
   数据集还必须有 `source`（`dataset_source_required`）：没有来源，下一个人无法判断能不能用。
6. **先查可见性，再描述对象**。`resolve_attachments()` 把可见性检查放在最前面：
   「该存储属于其他工作空间」「该存储已停用」这类消息都在确认对象存在，
   而看不见它的人不该知道它存在（对普通用户是 404，对管理员才是解释性的 400）。
7. **`usage_by_owner` 统计的是「你被收费的」，不是「你能看见的」**。二者必须分开：
   一个组共享数据集对五十个成员都可见、却不属于任何人，按可见性统计会让每个人都
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
    前端如实呈现）：组共享数据集对五十个成员都可见却不属于任何人。

**本期踩到的坑**

- **`MissingGreenlet` 复发（与 Phase 3 的 `Workspace.group` 同类）**：`Storage.workspace`
  等关系是 `lazy="joined"`，但 `joined` 只对**从查询返回的**行生效；刚 `session.add()`
  的行关系是空的，序列化时触碰它就触发同步上下文的懒加载并抛错。已在创建路径显式
  赋值 `storage.workspace = workspace` 等，并写明原因。
- **`run_gateway()` 包错了对象**：它用 `asyncio.to_thread` 跑**同步**函数，包住一个
  `async def` 服务函数会返回协程而不执行 —— 扩容/删除会「成功」但什么都没发生。
  正确做法是服务内部**只**把阻塞的 gateway 调用丢进 `run_gateway`。
  （`mypy` 的 `unused-coroutine` 抓到了它。）
- `PvcSpec.medium` 不存在：PVC 没有 medium 字段（那是 emptyDir 的），已移除。
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
- **冒烟环境没有 reconciler，实例停在 `creating` 且 `pod_name` 为 NULL。**
  这不是缺陷而是一种真实状态（实例刚创建、或 reconciler 挂了），
  所以断言要落在「`pods` 是列表」而不是「`pods` 非空」，Pod 锁定的行为放到 pytest 里
  用确定的数据覆盖。
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

### 有意推迟（非遗漏）

- **OIDC** 仅预留扩展点：`OidcProvider.exchange_code()` 明确抛错说明。大学的统一身份是
  FreeIPA，接入 OIDC 需要校方先提供客户端注册；在拿到之前不写不可验证的代码。
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

### 14.4 待外部确认（阻塞后续阶段，不是缺陷）

这三项都不是代码问题，但**都必须先有答案再写代码**，否则写出来的是不可验证的实现：

1. **既有的 Prometheus 是否带 `namespace` / `pod` 标签？** 没有这两个标签就无法把指标
   归到租户上，"用量按 Prometheus 记账"就无从谈起。这是 Phase 7 的前置问题（详见 §10）。
2. **集群里是否存在 RWX 的 StorageClass？** `_default_access_mode()` 对
   `dataset` / `group_share` 返回 `ReadWriteMany`。若不存在，这两类卷会一直 Pending，
   而症状是"数据集创建成功但不能挂载"，排查起来离原因很远。
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
| **持久化存储（后端）** | `backend/app/services/storages.py`、`backend/app/api/v1/storages.py` |
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
