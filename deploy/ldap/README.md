# LDAP / FreeIPA 对接

平台把 FreeIPA 当作**唯一身份源**：账号、口令、姓名、邮箱、用户组全部来自目录。
**目录账号的口令平台一个字节都不存**（只有本地应急账号 `source=local` 存 argon2
哈希，它不参与目录认证）。代码在 `backend/app/auth/providers/ldap.py`。

## 1. 平台用 LDAP 做两件事，用两个不同的身份

理解这一点就不会被后面所有的现象绕晕：

| 步骤 | 用谁的凭证 | 干什么 | 失败会怎样 |
|---|---|---|---|
| **1. 验证口令** | **用户本人**（`uid=alice,cn=users,…`） | 用他输入的密码 bind 一次 | 口令错 → 登录失败 |
| **2. 读属性与组** | **服务账号**（`LDAP_BIND_DN`） | 读邮箱、姓名、`member`/`memberUid` | 见下方「匿名绑定陷阱」 |

> **为什么读属性不能用用户本人？** 目录的读策略通常限制普通用户能看什么
> （尤其是别人的 `member` 列表）。用服务账号读，结果才与用户是谁无关。

**关键：第 2 步失败不会让登录失败。** 组查询失败只记一条 warning 然后返回空组
（`_read_groups`）。用户照样登录成功，只是**没有任何组、只有默认角色**。这是
「明明配好了，怎么大家都没有权限」这类问题的根源——它不报错。

## 2. 变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUTH_PROVIDERS` | `local` | 必须包含 `ldap` 才会启用。逗号分隔、**按顺序尝试**，如 `local,ldap`（顺序的含义见 §4.1） |
| `LDAP_URI` | 空 | `ldaps://ipa.example.edu`（推荐）或 `ldap://…` |
| `LDAP_BASE_DN` | 空 | `dc=example,dc=edu` |
| `LDAP_USER_DN_TEMPLATE` | `uid={username},cn=users,cn=accounts,{base_dn}` | FreeIPA 默认布局，通常不用改 |
| `LDAP_GROUP_BASE_DN` | `cn=groups,cn=accounts,{base_dn}` | `{base_dn}` 会自动代入 |
| `LDAP_BIND_DN` | 空 | 服务账号的完整 DN |
| `LDAP_BIND_PASSWORD` | 空 | 服务账号口令（Secret） |
| `LDAP_CA_CERT` | 空 | 私有 CA 的 PEM **文件路径**（容器内），不是内容 |
| `LDAP_OBSERVER_GROUP` | 空 | 该组成员首次登录自动获得**只读 observer** 角色 |

`{username}` 和 `{base_dn}` 是 Python `str.format` 占位符，两个模板都必须至少含
`{base_dn}`，否则 DN 拼不出来。

## 3. FreeIPA 侧要做什么

### 3.1 建服务账号

```bash
# 专用账号，不要用 admin
ipa user-add svc-kube-wl-ai --first=Service --last="kube-wl-ai" --shell=/sbin/nologin
ipa passwd svc-kube-wl-ai
```

它的完整 DN 就是 `LDAP_BIND_DN`：
`uid=svc-kube-wl-ai,cn=users,cn=accounts,dc=example,dc=edu`

**权限**：FreeIPA 默认允许已认证用户读取用户与组条目，所以**普通账号通常就够**
——先按下面的方式验证，别急着配权限。

如果验证发现读不到（§3.2 第 2 条返回空），才需要收紧前的授权。做法是建一个只读
权限并挂到角色上：

```bash
# 1. 先看自带权限的确切名字，别照抄——名字随版本可能不同
ipa permission-find --pkey-only | grep -i "read.*\(user\|group\)"

# 2. 建权限组、角色，把权限挂上去（--permissions 用第 1 步查到的名字）
ipa privilege-add "kube-wl-ai read"
ipa privilege-add-permission "kube-wl-ai read" \
  --permissions="System: Read User Attributes" \
  --permissions="System: Read Group Attributes"
ipa role-add "kube-wl-ai reader"
ipa role-add-privilege "kube-wl-ai reader" --privileges="kube-wl-ai read"

# 3. 把服务账号加进角色
ipa role-add-member "kube-wl-ai reader" --users=svc-kube-wl-ai
```

> **第 1 步不是可选的。** 上面两个权限名是 FreeIPA 常见版本的叫法，但不同版本/
> 发行版的自带权限集合不完全一样。照抄一个不存在的名字，`ipa privilege-add-permission`
> 会直接报错——那还是好的；换成拼写接近但不覆盖该属性的名字，就会变成「命令成功、
> 权限没生效」，回到 §3.2 那种「不报错的错误」。用 `--pkey-only` 查一遍再填。

服务账号**不需要**写权限，也不需要任何系统级权限。它只读。

### 3.2 先手工验证，再配平台

这两条命令能挡掉九成的配置问题，**在动平台之前跑**：

```bash
# 1. 服务账号能 bind 吗？
ldapwhoami -x -H ldaps://ipa.example.edu \
  -D 'uid=svc-kube-wl-ai,cn=users,cn=accounts,dc=example,dc=edu' -W
# 期望：dn:uid=svc-kube-wl-ai,cn=users,cn=accounts,dc=example,dc=edu

# 2. 能读到某个用户的属性和组吗？
ldapsearch -x -H ldaps://ipa.example.edu \
  -D 'uid=svc-kube-wl-ai,cn=users,cn=accounts,dc=example,dc=edu' -W \
  -b 'dc=example,dc=edu' '(uid=alice)' uid cn displayName mail

ldapsearch -x -H ldaps://ipa.example.edu \
  -D 'uid=svc-kube-wl-ai,cn=users,cn=accounts,dc=example,dc=edu' -W \
  -b 'cn=groups,cn=accounts,dc=example,dc=edu' \
  '(member=uid=alice,cn=users,cn=accounts,dc=example,dc=edu)' cn
```

第 2 条如果返回空但第 1 条成功 → 服务账号读不到，就是「只有默认角色」的原因。

私有 CA 时加 `-o TLS_CACERT=/path/to/ca.crt`；`ldaps://` 用 IP 地址会因主机名
不匹配失败，务必用 FQDN。

## 4. 配置方式

### 4.1 本地 / docker-compose（`.env`）

```env
AUTH_PROVIDERS=local,ldap
LDAP_URI=ldaps://ipa.example.edu
LDAP_BASE_DN=dc=example,dc=edu
LDAP_BIND_DN=uid=svc-kube-wl-ai,cn=users,cn=accounts,dc=example,dc=edu
LDAP_BIND_PASSWORD=...
# LDAP_OBSERVER_GROUP=ai-observer
```

**保留 `local` 作为应急后门**，否则目录一挂就没人能登录修了。本地管理员用
`python -m app.cli bootstrap-admin` 创建。

> ⚠️ **顺序很重要。** 提供方按 `AUTH_PROVIDERS` 的顺序尝试，但**基础设施故障会
> 直接中断整条链**：LDAP 抛 `AuthProviderError`（目录不可达、TLS 失败）时，
> `authenticate` 立刻抛 `503 auth_provider_unavailable`，**根本不会去试 `local`**。
> 只有「LDAP 正常但这个人不在目录里」（返回 `None`）才会继续往下试。
>
> 所以：
>
> | 配置 | 目录挂了时本地管理员能登录吗 |
> |---|---|
> | `ldap,local` | **不能**，503 |
> | `local,ldap` | **能** |
>
> 想要真正的应急后门，把 `local` 写在**前面**（`local,ldap`）。这样目录账号会先
> 在本地返回 `None`（本地提供方明确拒绝 `source != local` 的账号，见
> `providers/local.py`），随即落到 LDAP，**没有任何歧义**；而本地应急账号在第一
> 步就被认证，根本不会碰到 LDAP——目录挂了也进得去。代价只是目录账号多一次
> argon2 哈希（可忽略）。

### 4.2 Helm（生产）

```yaml
# values-prod.yaml
config:
  # local 放在最前 = 真正的应急后门：目录挂了本地管理员也能进（见 §4.1）
  authProviders: "local,ldap"
  ldap:
    uri: "ldaps://ipa.example.edu"
    baseDn: "dc=example,dc=edu"
    userDnTemplate: "uid={username},cn=users,cn=accounts,{base_dn}"
    groupBaseDn: "cn=groups,cn=accounts,{base_dn}"
    bindDn: "uid=svc-kube-wl-ai,cn=users,cn=accounts,dc=example,dc=edu"
    observerGroup: "ai-observer"
  # 私有 CA：先建好 Secret，再指向它
  ldapCaCertSecret: "ipa-ca"
  ldapCaCertKey: "ca.crt"

secrets:
  ldapBindPassword: "..."   # 或用 secrets.existingSecret
```

```bash
kubectl create secret generic ipa-ca -n kube-wl-ai --from-file=ca.crt=/etc/ipa/ca.crt
```

Chart 会把该 Secret 挂到 API Pod 的 `/etc/ssl/ldap/`，并把 `LDAP_CA_CERT` 指向
`/etc/ssl/ldap/{ldapCaCertKey}`。

### 4.3 OpenLDAP（osixia 镜像）——与 FreeIPA 的三处不同

FreeIPA 的默认值**不能**直接用在 OpenLDAP 上。以下已经在一套真实的
osixia/openldap（`dc=hpc,dc=westlake`，NodePort 30389）上端到端验证过。

现成配置见 [`openldap-test.env.example`](./openldap-test.env.example)。

| | FreeIPA | OpenLDAP |
|---|---|---|
| 用户在哪 | `cn=users,cn=accounts,<base>` | `ou=Users,<base>` |
| 组在哪 | `cn=groups,cn=accounts,<base>` | `ou=Groups,<base>` |
| 组成员属性 | `member`（完整 DN） | **`memberUid`（登录名）** |
| 用户 RDN | `uid=` | 用 phpLDAPadmin 建时**默认是 `cn=`** ← 坑 |

对应配置：

```env
LDAP_URI=ldap://<node-ip>:30389
LDAP_BASE_DN=dc=hpc,dc=westlake
LDAP_USER_DN_TEMPLATE=uid={username},ou=Users,{base_dn}
LDAP_GROUP_BASE_DN=ou=Groups,{base_dn}
LDAP_BIND_DN=cn=admin,dc=hpc,dc=westlake
LDAP_BIND_PASSWORD=...
```

> 明文 389/30389 不需要 `LDAP_CA_CERT`。平台跑在集群内时也可以用
> `openldap-service.openldap.svc.cluster.local:389`。
>
> 测试环境直接用了 `cn=admin` 当服务账号；**生产环境务必换成一个只读专用账号**
> （§3.1）。

#### 坑：用户条目的 RDN 必须是登录名

用 phpLDAPadmin 建用户时，RDN 默认取 `cn`，于是条目是
`cn=San Zhang,ou=Users,...`，而 `uid` 只是里面一个普通属性。

**登录时会 bind 失败**，因为平台按模板拼 `uid={username},ou=Users,...`，而
**这个 DN 根本不存在**——LDAP simple bind 要求用条目真实的 DN，不能按属性值 bind。

| 步骤 | 用的 DN | 结果 |
|---|---|---|
| 读属性 `(uid=zhangsan)` | 搜整个 `base_dn` | ✅ 找得到 |
| **bind 验证口令** | `uid=zhangsan,ou=Users,...` | ❌ **DN 不存在** |

改成 `cn={username}` 也不行：`San Zhang` 带空格，会被
`_SAFE_USERNAME = ^[A-Za-z0-9][A-Za-z0-9._-]{0,149}$` 拒掉（防 DN 注入，不能放宽）。

**修法**：把 RDN 改成登录名，`cn` 留作显示名。已存在的条目不用删了重建，
`modrdn` 就地改名即可（`deleteoldrdn=0` 会把旧的 `cn` 保留为普通属性）：

```bash
ldapmodrdn -x -H ldap://<node-ip>:30389 \
  -D 'cn=admin,dc=hpc,dc=westlake' -W \
  -r 'cn=San Zhang,ou=Users,dc=hpc,dc=westlake' 'uid=zhangsan'
```

等价的 LDIF：

```ldif
dn: cn=San Zhang,ou=Users,dc=hpc,dc=westlake
changetype: modrdn
newrdn: uid=zhangsan
deleteoldrdn: 0
```

新建用户就直接用 `uid=` 作 RDN：

```ldif
dn: uid=zhangsan,ou=Users,dc=hpc,dc=westlake
changetype: add
objectClass: inetOrgPerson
objectClass: posixAccount
objectClass: top
uid: zhangsan
cn: San Zhang
sn: Zhang
givenName: San
uidNumber: 1001
gidNumber: 500
homeDirectory: /home/users/zhangsan
loginShell: /bin/bash
mail: zhangsan@example.edu
userPassword: <口令>
```

> `mail` 建议填上。没有它登录也能成功，只是 `email` 是 `null`——平台不会报错。

#### 坑：`posixGroup` 用 `memberUid`

```ldif
dn: cn=hpc,ou=Groups,dc=hpc,dc=westlake
changetype: modify
add: memberUid
memberUid: zhangsan
memberUid: zhenghuanbo
```

> **历史缺陷（已修）**：登录路径曾经只搜 `(member=<用户 DN>)`，而批量同步一直
> 读 `member` + `memberUid`。于是同一份 `posixGroup` 数据，同步能映射出组、
> 用户自己登录却报「没有组」，只能靠管理员手动跑一次同步兜底。现在两条路径都读
> 两种写法。

## 5. 用户从哪来

**没有自助注册。** 第一次目录登录时 JIT 建本地账号（`provision_user`）：

- 用户名 = **登录时输入的那个字符串**，不是目录里 `uid` 属性的值。两者一致只是因为
  模板就是 `uid={username}`；若改成 `cn={username}`，平台的登录名就变成目录的 `cn`
- 邮箱 = `mail`，姓名 = `displayName` → `givenName + sn` → `cn` → 用户名（逐级回退）。
  属性读不到时**不报错**，对应字段留空（OpenLDAP 常见：`email` 是 `null`）
- 角色 = `LDAP_OBSERVER_GROUP` 成员是 `observer`，否则 `user`
- `source=ipa`（枚举值是 `UserSource.IPA`，区别于手工建的 `local` 账号和 `oidc`）

**归属标记在「成员关系」上，不在用户上。** 目录带来的成员关系打
`is_directory_managed=true`；目录同步只增删**自己打的**这些行，管理员手工授予的
成员关系绝不会被静默撤销。

## 6. 用户组映射

**目录不会在平台里创建用户组。** 两套机制都只把目录里已有的组名**对应**到平台上
**已经存在**的同名组，然后维护成员关系（`services/groups.py::sync_directory` 和
`auth/service.py::sync_directory_groups` 都明说 "never creates groups"）。

| 机制 | 触发 | 做什么 |
|---|---|---|
| 登录时映射 | 每次登录 | 按 `identity.groups` 增减该用户的成员关系 |
| 批量对齐 | `POST /groups/sync`（需 `group:update`） | 遍历全平台的组 × 全目录的组，对所有人做一次 |

**所以新建实验室的正确顺序是**：先在 FreeIPA 建组建人 → 再在平台上**手工建同名
组** → 成员关系自动接上（用户下次登录，或管理员跑一次批量对齐）。

### 6.1 先跑 dryRun

```bash
curl -X POST $BASE/api/v1/groups/sync \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"dryRun": true}' | jq
```

返回里最有用的字段是 **`unmatchedGroups`**：**目录里有、平台里没有的组名**。
它就是待办清单——每个名字都需要有人在平台上建组（或建一个
`directoryName` 指向它的组）。`dryRun` 不写任何东西，正式跑才传 `false`。

响应还带 `membershipsAdded` / `Removed` / `Updated` 和 `errors`。**`errors` 非空
时要看**：成员对不上的会在那里报出来（比如目录成员在平台上根本没有账号）。

### 6.2 名字对不上怎么办

匹配用 `directory_key`（`directory_name` 有值就用它，否则用 `name`）。FreeIPA 的
`cn` 可能是 `Lab_Vision` 或中文，而平台组名必须是 `^[a-z0-9][a-z0-9-]*$`。这种
情况就在平台组上设 `directory_name=Lab_Vision`——两边名字可以不同，靠这个字段挂钩。

> 两个平台组抢同一个 `directory_name` 时，**两边都不动**并记一条
> `directory_group_name_contested` warning。不做「谁先返回算谁的」——那是随机行为，
> 而且会把成员写进目录根本没提过的组。

## 7. 验证

```bash
# 1. 配置体检（最先看这个）
#    系统设置 → /admin/settings，或：
curl -s -H "Authorization: Bearer $TOKEN" $BASE/api/v1/system/info | jq '.warnings'

# 2. 目录账号能登录
curl -X POST $BASE/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"..."}'

# 3. 名字和组有没有读到（第 3 步失败时这里会是 null / []）
curl -s -H "Authorization: Bearer $TOKEN" $BASE/api/v1/auth/me \
  | jq '{username,email,fullName,roles,groups:(.groups|length)}'
# fullName 是 null / groups 是 0 → 服务账号没读到（§8）
```

`/admin/settings` 的**配置体检**会直接告诉你缺什么，每条都带「什么后果、改哪个
变量」。两条与 LDAP 相关：

- `ldap_without_uri`（critical）——启用了 ldap 但没配地址
- `ldap_without_service_account`（warning）——**没有服务账号**，登录能成功但
  读不到邮箱/姓名/组。这是最该看的一条。

## 8. 匿名绑定陷阱（最容易踩）

`LDAP_BIND_DN` 为空时 ldap3 会**匿名 bind**，而匿名 bind **通常会成功**。于是：

- 登录**正常**
- 每个账号都**没有邮箱、没有姓名、没有任何组**
- 只拿到默认角色
- **日志里什么错都没有**

典型现象是「所有人都登录得了，但都没有权限」，然后去查权限系统——方向全错。

平台对此的处理：

- **开发环境**：记一条 `ldap_service_account_not_configured` warning
- **生产环境**：`Settings._guard_production_secrets` 直接**拒绝启动**，报
  「LDAP_BIND_DN and LDAP_BIND_PASSWORD are required when 'ldap' is in
  AUTH_PROVIDERS; without them group membership cannot be read」

所以在生产环境这个坑踩不到——进程起不来，而不是静默降级。

## 9. 排障对照表

| 现象 | 原因 |
|---|---|
| 所有人都登不上，提示密码错误 | `LDAP_URI` 或 `LDAP_BASE_DN` 填错；`AUTH_PROVIDERS` 里没写 `ldap` |
| **OpenLDAP 上所有人都登不上**，但 admin bind 正常 | 用户条目 RDN 是 `cn=` 而非 `uid=`，模板拼出的 DN 不存在（§4.3） |
| **能登录，但所有人没有组、只有默认角色** | 服务账号没配或口令错（§8）；或服务账号读不到组 |
| 提示证书校验失败 | 私有 CA 没配 `LDAP_CA_CERT`；或 `ldaps://` 用了 IP 而非 FQDN |
| 个别人登不上，别人正常 | 该账号被锁 / 口令过期——`LDAPBindError` 不匹配 `invalidCredential` 时会抛 `AuthProviderError`（503），不是密码错误 |
| 改了 FreeIPA 组，平台没反应 | 登录时的映射只认已存在的平台组（§6）；需要先建组或跑 `POST /groups/sync` |
| 用户上次登录后组变了，平台还是旧的 | 组成员关系在**每次登录**时刷新。用户不重新登录就不会更新 |
| 一直 503，且**本地管理员也登录不了** | 目录不可达，且 `AUTH_PROVIDERS` 里 `ldap` 在 `local` 前面（§4.1）。把 `local` 提到最前 |
| `invalidCredentials` 之外一直 503 | 目录不可达。`LDAP_URI` 拼写、防火墙、CA |

## 10. 平台侧的已知边界

- **用户名只允许 `^[A-Za-z0-9][A-Za-z0-9._-]{0,149}$`**。带其他字符（含 `@`
  ——有些目录用邮箱作 uid）会被拒并记 `ldap_unsafe_username_rejected`。这是防
  DN 注入，不是可以放宽的校验。
- **不读 `memberOf`。** 它是 overlay 维护的属性，裸目录不一定有，而且不限定在
  配置的组子树内。组一律在 `LDAP_GROUP_BASE_DN` 下按
  `(|(member=<用户 DN>)(memberUid=<登录名>))` 搜，两种写法都读（§4.3）。
- **只读直接成员，不递归嵌套组。** FreeIPA 的嵌套组不会自动展开。
- **没有周期性目录同步。** 组关系和姓名/邮箱在**每次登录**时刷新；账号删除后，其
  刷新令牌最多还能用 7 天（`REFRESH_TOKEN_EXPIRE_DAYS`）。
- **`observer` 角色只在账号第一次创建时判定，之后不再变。** 这是实测确认的行为，
  也是这套集成里最容易被误解的一点：

  | 改了什么 | 下次登录会生效吗 |
  |---|---|
  | 姓名、邮箱 | ✅ 会（`email`/`full_name` 每次登录比对更新） |
  | 平台组成员关系 | ✅ 会（`sync_directory_groups` 每次登录跑） |
  | **加入 / 移出 `LDAP_OBSERVER_GROUP`** | ❌ **不会** |

  后果：把一个人加进观察者组，他不会自动获得只读权限；把一个人移出，他也**不会
  失去**——角色一旦在首次登录时定下就固定了。要改现存账号的角色，得在平台里手工改
  （或删掉账号让他重新 JIT 一次，代价是历史归属会断）。

  实测：从 `hpc-test` 移除某用户后重新登录，`roles` 仍是 `['observer']`，而
  `groups` 已经正确更新。
- **`ldap3` 是同步库**，所有调用都走 `asyncio.to_thread`，不会阻塞事件循环；
  单次超时 10 秒。
