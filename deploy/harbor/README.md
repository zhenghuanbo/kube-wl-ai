# Harbor：项目结构、robot 账号与镜像拉取

这份文档描述平台与 Harbor 之间的约定。代码侧的实现在
`backend/app/services/images.py`（目录与校验）和
`backend/app/registries/harbor.py`（API 客户端）。

## 1. 项目结构：一个用户组一个 project

```
harbor.edu.cn/
├── library/                        官方镜像（安装级，全员可拉）
│   ├── pytorch:2.4-cuda12
│   └── ubuntu:22.04
│
├── lab-vision/                     ← 一个用户组 = 一个 Harbor project
│   ├── share/                      组内约定共用的镜像
│   │   └── cuda-toolkit:12.4
│   ├── zhangsan/                   个人镜像：用户名作第一段
│   │   ├── my-env:v3
│   │   └── debug-tools:v1
│   └── lisi/
│       └── my-env:v1
│
└── lab-nlp/
    ├── share/
    └── wangwu/
        └── nlp-env:v2
```

三条规则，全部由平台在登记镜像时强制：

| 规则 | 违反时的错误 |
|---|---|
| project 必须是某个 `Group.name`，或 `library` | `400 image_project_unknown` |
| 个人镜像（`kind=personal`）的仓库名必须以本人用户名开头 | `400 image_prefix_mismatch` |
| 项目所属的组与显式传入的 `groupId` 必须一致 | `400 image_project_group_mismatch` |

**project 名用 `Group.name` 而不是 `directory_name`。** Harbor 的项目名只允许
`[a-z0-9][a-z0-9._-]*`，而 `directory_name` 装的是目录里的 `cn`（可能是
`Lab_Vision` 或中文），注册表不接受。`Group.name` 本来就是小写 ASCII slug——
它同时是 K8s 命名空间的一段（`wl-{group}-{workspace}`），约束正好够用。

### 为什么平台要管这个

Harbor 的 RBAC **只到 project**，平台不试图在 Harbor 内部表达更细的权限。同一个
组的成员在 Harbor 里都是该 project 的 `Developer`：张三能推 `lab-vision/zhangsan/*`，
李四能推 `lab-vision/lisi/*`。

**命名约定由平台在登记镜像时校验，越界由组管理员处理。** 如果有人绕过平台直接
`docker push` 了一个不符合约定的仓库名（比如以李四的名义推 `lab-vision/lisi/x`），
平台的前缀校验会拒绝把它登记进目录，project 管理员则直接在 Harbor 里删掉这张镜像。

> 平台的前缀校验只管**目录登记**，挡不住 `docker push`。这是有意的：Harbor 的
> 权限粒度到此为止，再细的隔离只能靠收紧 Harbor 角色（那会让组员失去自助推镜像
> 的能力）。组内的信任边界由组管理员承担。

## 2. 需要哪些 robot 账号

| 用途 | 类型 | 权限 | 数量 |
|---|---|---|---|
| 平台调用 Harbor API | **系统级** robot | Cover all projects + 全部读写删 | 1 |
| 节点拉取镜像 | **系统级** robot | Cover all projects + **仅 List + Pull** | 1 |

### 平台 API 的 robot

平台只用它读 project/repository/tag、查 digest、删 tag。配到后端：

```
HARBOR_URL=https://harbor.edu.cn
HARBOR_ROBOT_NAME=robot$kube-wl-ai
HARBOR_ROBOT_SECRET=...
```

它从不接触计算节点，所以给全权限是合理的。

### 节点拉取的 robot

**权限只勾 `List Repository` + `Pull Repository`。** 这一条没有商量余地：
节点上跑的是不可信的用户代码，凭证就明文躺在那台机器上。

创建时的三个细节：

- **Cover all projects** 要勾——否则新加的组 project 立刻拉不动
- **Expiration 选 Never Expired**——默认 30 天，到期后全校镜像拉取集体失败，
  而且现象是 `ImagePullBackOff`，非常容易被误判成网络问题
- 密码只在创建时显示一次，Harbor 不保存

用 `scripts/harbor-node-pull-credential.sh` 生成节点配置：

```bash
HARBOR_HOST=harbor.edu.cn \
ROBOT_USERNAME='robot$node-pull' \
ROBOT_PASSWORD='...' \
  ./scripts/harbor-node-pull-credential.sh
```

先加 `DRY_RUN=true` 看输出，再按提示逐节点应用。

## 3. 拉取凭证的解析顺序

kubelet 按这个顺序找凭证，先命中先返回：

1. **Pod 的 `imagePullSecrets`**
2. **ServiceAccount 的 `imagePullSecrets`**（准入控制器在 Pod 创建时合并进去）
3. **节点 containerd 的 registry 配置** ← 当前方案
4. 匿名

平台目前只用第 3 层。它的两个直接后果：

- **私有 project 的镜像能拉了**（在此之前一个都拉不到）
- **组间的读隔离在 registry 层没有了**：节点不区分 Pod 属于谁，任何 Pod 都能拉
  该 robot 可读的所有 project。隔离从「拉不到」降级为「不知道名字」

这是可接受的，因为**用户能看到哪些镜像由平台保证**，不由 registry 保证。创建
任务/实例时的镜像选择器只列三类：

| 类别 | 判定 | 谁看得见 |
|---|---|---|
| **集群内置** | `kind == official`（`library` 里） | 所有登录用户 |
| **组共享** | `visibility == group` 且组在本人/所辖组内 | 该组成员 |
| **个人** | `owner_user_id == 本人` | 仅本人 |

别人的个人镜像不会出现在选择器里：同步发现的个人镜像是 `private`，手动登记时
前缀校验又挡住 `{组}/{他人用户名}/...`。实现见
`app/services/images.py::visible_condition`。

containerd 文档明确写着第 3 层「only be used when auth config is not specified
by Kubernetes via CRI」，所以将来加上第 1 层的 per-namespace Secret 时它自动
优先，节点配置会退化成兜底——这正是这个方案可以作为过渡的原因。

## 4. 升级路径：按组 robot + imagePullSecret

当需要恢复 registry 层读隔离时，把第 1 层补上：

1. 每个组建一个只读 project robot，授权限定在该组的 project
2. 平台在创建实例时生成 `kubernetes.io/dockerconfigjson` 类型的 Secret，
   放进该实例所在的 `wl-{group}-{workspace}` 命名空间
3. 把 Secret 名写进 Pod 的 `imagePullSecrets`

需要改的地方：`app/k8s/gateway.py` 的 `WorkloadSpec` 增加
`image_pull_secrets` 字段、`app/k8s/client.py` 的 `V1PodSpec` 带上它、
`app/services/instances.py` 在建实例时创建 Secret（`apply_secret` 已经具备）。

节点级配置可以保留作为兜底，也可以在所有命名空间都配好之后撤掉。

## 5. 平台侧的已知边界

- **组内在 Harbor 侧不隔离。** 同组成员都是 project 的 `Developer`，命名约定由平台在登记时校验，越界由组管理员在 Harbor 里删除。见 §1。
- **删除镜像后不会清理 registry 的 blob。** 平台删的是 tag；Harbor 的
  garbage collection 需要单独配定时任务。
- **`Developer` 能建 tag 保留策略**，而保留策略会自动删 tag——所以「Developer
  不能删镜像」有个间接通道。要堵得在 Harbor 系统设置里调整角色能力。
- **同步会归组但不覆盖人工设置。** `POST /images/sync` 对新行按项目归组
  （`library` → official/global；组 project → 该组，首段是用户名则归到那个人），
  已存在的行只刷新 digest/size，不碰 `kind`/`visibility`/`group_id`——同步是
  镜像仓库的镜子，不该撤销管理员在目录里做的决定。

## 6. 镜像目录与 Harbor 的一致性

`images` 表是 Harbor 的**缓存**，不是镜像。两边会不一致，这是设计如此。

### 两条写入路径

| 路径 | 触发 | 是否校验 Harbor |
|---|---|---|
| 平台登记 `POST /images` | 用户在页面上填地址 | **是**——先 `GET` 该 tag，不存在就 `400 image_not_found_upstream` |
| `POST /images/sync` | 管理员手动跑，或 beat 定时跑 | 读 Harbor 的 project/repository/tag 列表 |

### 定时同步

beat 里有一个 `sync-images` 任务，**默认每天跑一次**，间隔由
`IMAGE_SYNC_INTERVAL_SECONDS` 控制（默认 `86400`）。

> **用户直接 `docker push` 到 Harbor 的镜像，最多一天后自动可见。**
> 在那之前平台上任何人都看不到它，包括推送者本人——这不是故障。

在定时任务之前，唯一的途径是管理员记得点同步按钮，而按钮需要 `image:share`，
问「我的镜像怎么看不见」的人恰恰没有这个权限。所以定时任务解决的是这个错配。

小集群可以把间隔调小（`3600` = 每小时）：一次同步会走遍所有 project 的所有 tag，
集群越大越贵，所以这是个按规模调的旋钮，不是常量。管理员也可以随时手动触发
`POST /images/sync` 立即刷新。

**同步有互斥锁。** 定时任务和手动触发共用一把 Redis 锁（`kube-wl-ai:image-sync`），
避免两个同步同时读到同一个 tag、都发现目录里没有、都插入——唯一约束会让其中一个
回滚。手动触发撞上锁时返回 `409 image_sync_in_progress` 而不是 500。锁**失败开放**：
Redis 不可用时同步照常进行，因为同步是幂等的，而「因为缓存挂了就不刷新目录」是拿
真实故障换想象故障。

同步之后，这张镜像由**仓库名的第一段**决定归属：`{组}/{用户名}/…` 判为那个人的
个人镜像（`private`），`{组}/share/…` 判为组共享，`library/…` 判为官方。

### 不一致的三种情况

| 情况 | 下一次干净同步的行为 |
|---|---|
| Harbor 有、目录没有（直接 push） | **新增**。按项目/前缀归组；推的人立刻能看到自己的 |
| 目录有、Harbor 没有（Harbor 里删了） | **停用**（`is_active=false`），不删行。已固定（`is_pinned`）的除外 |
| 目录有、Harbor 不可达 | **不动**。`result.errors` 非空时 `_retire_missing` 直接返回 0 |
| project 数超过单次上限被截断 | **不动**。同上——截断会记为 error，避免误判为「已删除」 |

停用而非删除：`usage_count > 0` 的镜像被工作负载引用着，删行会让历史记录指向
空。用户在目录里勾选「包含已移除」仍能看到它们，页面上标为已移除。

**个人镜像不参与停用**：`_retire_missing` 跳过 `kind=personal`。个人镜像是用户自己
登记或推送的，注册表列不出来不代表它不该在目录里。

### 操作建议

- **调 `IMAGE_SYNC_INTERVAL_SECONDS`**：默认每天。想要「推完很快就可见」就调到
  `3600`（每小时），代价是每小时走一遍全部 project 和 tag。
- 同步是**全量**的：不带 `projects` 参数就遍历 Harbor 的所有 project。项目多时
  会慢，可以传 `{"projects": ["lab-vision"]}` 只同步一个组。
- **单次同步最多处理 500 个 project**（`MAX_PROJECTS_PER_SYNC`）。超过时会在
  `errors` 里明确报出来，并且**不执行停用**——因为一次被截断的同步看到的是不完整
  的镜像仓库，把没看过的 project 当成「已删除」会把它们的镜像全部下架。
- 同步的返回里有 `errors` 数组。**只要它非空，停用逻辑就不会执行**——所以在
  修好报错的 project 之前，Harbor 里已删除的镜像会一直留在目录里。
