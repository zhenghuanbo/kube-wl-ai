# 实例镜像：Ubuntu 24.04 + conda + 三个 IDE

这是 kube-wl-ai **实例容器**（`ide_type=jupyter|vscode|webssh`）使用的业务镜像。
它不是一个通用基础镜像：它实现的是平台对「实例镜像」的一组**硬性约定**，每一条都被
`backend/app/k8s/` 里的某段代码强制。改这个镜像之前请先读完第 2 节。

这份文档同时解释**为什么 `ide_command()` 里写死的那些假设必须由镜像来满足**——平台
只负责下发 argv，镜像负责让那个 argv 真的能跑起来。

```
deploy/images/ubuntu-dev/
├── Dockerfile          # 镜像本身
├── build.sh            # 构建 + 推到 Harbor
├── README.md           # 本文件：镜像与平台的契约
└── rootfs/             # 原样 COPY 进镜像的文件
    └── opt/wl/
        ├── runtime.sh      # 每次启动的准备：建账号、给 home、起 sshd
        ├── entrypoint.sh   # ENTRYPOINT（只有 ide_type=none 会走到）
        └── bin/
            ├── wl-run      # 起 runtime 后降权到实例账号
            ├── jupyter     # 包装器，见第 3 节
            ├── code-server
            └── ttyd
```

---

## 1. 为什么需要自制镜像

`ide_command()`（`backend/app/k8s/workloads.py:91`）把 IDE 的启动命令**写死在平台侧**：

| `ide_type` | 平台下发的 argv | 端口 |
| --- | --- | --- |
| `jupyter` | `jupyter lab --ip=0.0.0.0 --port=8888 --ServerApp.base_url=/x/{id}/ …` | 8888 |
| `vscode` | `code-server --bind-addr 0.0.0.0:8080 --auth none --disable-telemetry` | 8080 |
| `webssh` | `ttyd --port 7681 --writable bash` | 7681 |
| `none` | `None`（用镜像的 ENTRYPOINT） | — |

也就是说镜像里**必须**有名为 `jupyter` / `code-server` / `ttyd` 的可执行文件，并且它们
要能接受上面这些参数。官方的 `library/python:3.12` 是纯 Python 镜像，没有 Jupyter，
所以容器一起就 `exec: "jupyter": executable file not found in $PATH`（exit 127）→
`CrashLoopBackOff`。**这和 CPU/GPU 无关**，选任何显卡都一样。

## 2. 平台对镜像的硬性约定

| # | 约定 | 谁在强制 |
| --- | --- | --- |
| 1 | 提供 `jupyter` / `code-server` / `ttyd` 三个可执行文件，接受平台下发的参数 | `ide_command()`，`workloads.py:91` |
| 2 | **sshd 必须监听 22** | `SSH_PORT=22` + `ssh_service` 为 NodePort，`workloads.py:59,84` |
| 3 | sshd 读 `/etc/ssh/authorized_keys.d/authorized_keys`（**一个文件，不是每用户一份**） | `SecretSpec(data={"authorized_keys": …})` 以目录形式挂载，`workloads.py:283`、`client.py:12` |
| 4 | 存在一个**与用户名同名**的本地账号（`ssh -p {port} {username}@{host}`） | `ssh_command()`，`instances.py:793` |
| 5 | 该账号拥有 `$HOME`，且 `HOME == /home/{username}` | `build_instance_objects` 同时设置 `HOME` 与挂载点，`workloads.py:65-73` |
| 6 | 端口 8888 / 8080 / 7681 由 IDE 监听 | `IDE_PORTS`，`workloads.py:66` |
| 7 | 用户名**不能**在构建期写死——只有 pod 起来才知道 | 同上 |

第 4、5 条合起来决定了 `rootfs/opt/wl/runtime.sh` 的做法：平台**不传用户名**，只传
`HOME=/home/{username}`，所以镜像只能从 `basename "$HOME"` 推出账号名。这不是取巧，
而是必须如此——`ssh_command()` 拼出来的字符串和这个推导必须一致，SSH 才登得进去。

第 3 条容易被误以为要按用户分文件，实际不是：Secret 挂进来的是**一个叫
`authorized_keys` 的文件**，所以 sshd 配置里写的是固定路径而不是 `%u`。一个容器只服务
一个人，一个 key 文件就是全部事实。

### sshd 的几个刻意选择（`runtime.sh` 里有对应注释）

- **`UsePAM no`**：没有密码、没有值得强制的 session limit，而 `pam_loginuid.so` 在缺少
  相应 capability 的 pod 里会让**每次登录都失败**，且表现得像「我的 key 被拒了」。
- **host key 放在 `$HOME/.ssh/wl-hostkeys/`**，不放 `/etc/ssh`。`/etc/ssh` 不是卷，pod
  重建就没了，于是每次重启用户都会看到 `REMOTE HOST IDENTIFICATION HAS CHANGED` ——
  这正是把人训练成无视那条真正用于发现中间人的告警的方式。放在 home 卷里就跨重启稳定。
  按**属主**而非实例区分：同一个人的两个实例共用 host key 不泄露任何他不知道的信息。
- **`PasswordAuthentication no`**：平台不发也不轮换密码，密码通道等于一个永远改不掉的凭据。

## 3. 三个 IDE 的包装器：为什么不能直接用真身

**这是这个镜像最反直觉的地方，也是最容易在重构时被删掉的一行。**

Kubernetes 里容器的 `command` 会**完全取代 ENTRYPOINT**。平台把 IDE argv 设成
`command`（`client.py:517` `command=list(spec.command)`），因此：

- `ide_type=jupyter|vscode|webssh` → 镜像的 ENTRYPOINT **根本不会执行**；
- 那么**没有任何东西会去启动 sshd**，而平台照样建了 SSH 的 NodePort Service
  （`workloads.py:80`，只要 `ssh_enabled` 就建）。

结果就是 SSH 端口指向一个没人监听的端口。平台侧没有任何一处启动 sshd，所以这个洞只能
由镜像补：`/opt/wl/bin/{jupyter,code-server,ttyd}` 是**包装器**，先跑
`runtime.sh` 再 `exec` 真身。

- `/opt/wl/bin` 在 `PATH` **最前面**，所以包装器一定赢过真身。包装器不是覆盖安装（不动
  conda / dpkg 的文件），避免出现「我到底在跑哪个 jupyter」这种没人答得上来的问题。
- `runtime.sh` 是**幂等**的，所以 ENTRYPOINT 和包装器都调它不会互相打架。
- `ide_type=none` 时平台不设 `command`，走 ENTRYPOINT：起 sshd 后 `sleep infinity`。

> 以后如果平台改成把 IDE argv 放进 `args`（而不是 `command`），包装器就变成多余的，
> 但**不会**出错——ENTRYPOINT 会正常执行，包装器只在有人手动敲 `jupyter` 时才触发。

## 4. 构建与推送

```bash
# 只看构建
deploy/images/ubuntu-dev/build.sh

# 构建并推到 Harbor 的 library/ 项目
PUSH=1 deploy/images/ubuntu-dev/build.sh
```

`library/` 的 push 需要该项目的推送权限。**这是有意的人工动作**：所有人都能选的镜像
不应该能被一个 API 请求替换掉。

镜像被选到创建页面的途径是周期同步（`sync-images`，默认 24h），所以推完不是立刻可见，
可以手动触发一次；`build.sh` 结尾会打印具体命令。

### 换源

全部是构建参数，境外构建可以直接指回上游：

```bash
docker build \
  --build-arg APT_MIRROR=https://archive.ubuntu.com/ubuntu \
  --build-arg APT_BOOTSTRAP_MIRROR=http://archive.ubuntu.com/ubuntu \
  --build-arg PIP_INDEX=https://pypi.org/simple \
  --build-arg CONDA_MIRROR=https://repo.anaconda.com \
  deploy/images/ubuntu-dev
```

> `APT_BOOTSTRAP_MIRROR` **必须是没有 TLS 的**。`ubuntu:24.04` 里根本没有
> `ca-certificates`，`/etc/ssl/certs` 是空的，所以用 https 源做第一次 `apt-get update`
> 会以 `The certificate issuer is unknown` 失败，随后每个包都报
> `no installation candidate`。第一遍用 http 只为装上 `ca-certificates`，之后切换到
> https 源；apt 仍然用镜像里已有的 keyring 校验包签名，所以这一步不降低包的信任级别。

## 5. 镜像里装了什么

| 类别 | 内容 |
| --- | --- |
| 基础工具 | `git`(`git-lfs`)、`vim`/`nano`、`less`、`tmux`/`screen`、`tree`、`rsync`、`unzip`/`zip`、`jq`、`htop`、`lsof`、`file`、`curl`/`wget` |
| 网络诊断 | `net-tools`（**`netstat`**）、`iproute2`（`ss`/`ip`）、`ping`、`dig`/`nslookup`、`traceroute`、`telnet` |
| 编译 | `build-essential`、`pkg-config`、`cmake` |
| 图形依赖 | `libgl1`、`libglib2.0-0`、`libsm6`、`libxext6`、`libxrender1`——opencv / matplotlib / 大量科学计算 wheel 在 import 时 dlopen 它们，缺了的报错是深埋在 notebook 里的一句 `libGL.so.1: cannot open shared object file` |
| SSH | `openssh-server`、`openssh-client`、`gosu`、`sudo` |
| conda | Miniconda（`/opt/conda`，Python 3.12），`.condarc` 指向 tuna；`auto_activate_base: false` |
| Python | `jupyterlab`、`notebook`、`ipykernel`、`jupyterlab-language-pack-zh-CN`、`numpy`、`pandas`、`matplotlib` |
| IDE | JupyterLab（conda）、`code-server`（`/opt/code-server`，版本固定）、`ttyd`（apt） |

locale 生成了 `en_US.UTF-8` 与 `zh_CN.UTF-8`，默认 `LANG=C.UTF-8`（最不容易出问题）；
时区 `Asia/Shanghai`。

### 加自己的包

`Dockerfile` 里的 `PIP_PACKAGES` 是留给业务扩展的位置：

```bash
docker build --build-arg PIP_PACKAGES="jupyterlab numpy pandas pytorch lightning" …
```

需要系统级依赖（比如 CUDA、`ffmpeg`）就在它上面的 apt 段落里加。**注意 GPU 镜像要在
同一个镜像里带上 `nvidia.com/gpu` 所需的运行时**，平台只负责把整卡挂进来
（`resources.limits["nvidia.com/gpu"]`）。

## 6. 已知缺口（镜像修不了，属于平台侧）

### 6.1 `vscode` 走不通子路径

Ingress 把 IDE 暴露在 `/x/{id}/` 且**没有** `rewrite-target` 注解
（`build_instance_objects` 只设了三个 proxy 注解），所以上游收到的是**带前缀的完整路径**。

- Jupyter 没问题：平台用 `--ServerApp.base_url=/x/{id}/` 告诉了它。
- `ttyd` 也没问题——本镜像的包装器补了 `--base-path /x/${WL_INSTANCE_ID}/`（实例 id 从
  环境变量拿，平台已经设了 `WL_INSTANCE_ID`）。
- **`code-server` 不支持子路径**，没有等价参数。它会在 `/` 上应答，于是 `/x/{id}/` 下
  所有静态资源 404。修它需要平台为这个 `ide_type` 加
  `nginx.ingress.kubernetes.io/rewrite-target` —— 镜像无法发明一个二进制里没有的功能。

### 6.2 状态推进依赖 Celery beat

实例状态只由 `reconcile_instance()` 推进，而触发它的只有 Celery beat 的
`reconcile-workloads`（30 秒）、CLI 和手动 reconcile。**页面读取不触发对账**，所以 beat
没起来时实例会一直停在「创建中」。这与镜像无关，记录在此是因为它和「镜像对不对」很容易
混淆：`podName` 一直是 `None` 就是这个问题，而不是镜像问题。

## 7. 构建/运行这个镜像时踩到的坑

全部是**只有真正跑起来才会暴露**的，逐个记在这里，因为每一个的症状都指向错误的方向。

### 7.1 `ubuntu:24.04` 里没有 CA 证书 → 第一次 `apt-get update` 必失败

基础镜像里 `ca-certificates` 没装、`/etc/ssl/certs` 是空的。所以用 https 源做第一次
`apt-get update` 会报 `The certificate issuer is unknown`，随后**每个**包都变成
`no installation candidate`——看起来像源写错了，其实是没证书。第一遍必须走 http。

### 7.2 `ubuntu:24.04` 自带 uid 1000 的 `ubuntu` 账号 → home 属主错人

镜像里有 `ubuntu`（uid 1000）。`useradd --uid 1000` 因此**静默失败**，回退分支给账号
分配了 1001，而脚本按「打算用的」1000 去 `chown` home。结果是 sshd 正常、账号正常、
home 却属于一个不存在的人，IDE 死在
`PermissionError: [Errno 13] Permission denied: '/home/<user>/.local'`。

两处修：Dockerfile 删掉 `ubuntu` 账号让 1000 空出来；`runtime.sh` 用 `id -u` **读回真实
uid** 再 chown，不再相信自己请求的那个值（`useradd` 拿不到请求的 uid 时照样返回成功）。

### 7.3 `useradd` 会把账号锁上 → sshd 连公钥登录都拒绝

`useradd` 在 `/etc/shadow` 写 `!`，而 sshd **对锁定账号连公钥认证都不允许**：

```
User zhenghuanbo not allowed because account is locked
```

症状是 `Permission denied (publickey)`，会让人去查 key 和权限，而两者都没问题。
`runtime.sh` 把 `!` 开头的密码字段改成 `*`：`*` 不是任何密码的合法哈希，所以即使以后有人
打开 `PasswordAuthentication` 也进不来；清空字段也行（多数镜像这么做），但空字段在那种
情况下就是一个真实的空口令漏洞。只动被锁定的账号，用户自己设的密码不会被覆盖。

### 7.4 SSH 会话不继承容器的 `PATH` → 登录后 `conda`/`python` 全都找不到

`ENV PATH` 是**容器进程**的环境，sshd **不会**把它传给会话：SSH 会话拿到的是 sshd 自己
编译进去的默认值，不含 `/opt/conda/bin` 和 `/opt/wl/bin`。于是刚登录的用户敲 `conda`、
`python`、`jupyter` 全是 `command not found`——最糟糕的第一印象。

唯一能同时覆盖**非交互**命令（`ssh host 'which conda'`，它不读 profile 也不读 bashrc）的
通道是 PAM：`pam_env` 会读 `/etc/environment`。所以 `runtime.sh` 里的 `UsePAM yes` 是**承重
的**，不是没改的默认值——把它关掉会静默地让每个 SSH 会话丢掉 PATH。

PAM 在容器里通常的代价是 `pam_loginuid.so` 是 `required`，缺 `CAP_AUDIT_WRITE` 时**整个
登录失败**（同样伪装成「key 被拒了」）。Dockerfile 把**这一个模块**降级为 `optional`，
而不是把 PAM 整个关掉。

## 8. 怎么验证这个镜像

镜像自己先验证一遍，不涉及集群：

```bash
# 1. 构建
deploy/images/ubuntu-dev/build.sh

# 2. 模拟平台：HOME 指向一个像 home 卷的目录，command 用平台下发的那个 argv
docker run --rm -it --name wl-check \
  -e HOME=/home/zhenghuanbo -e WL_INSTANCE_ID=1 -e WL_WORKSPACE=hpc \
  -v /tmp/wlhome:/home/zhenghuanbo \
  kube-wl-ai-ubuntu-dev:24.04 \
  jupyter lab --ip=0.0.0.0 --port=8888 \
    --ServerApp.base_url=/x/1/ --ServerApp.token= --ServerApp.password= \
    --ServerApp.disable_check_xsrf=True --no-browser
```

在另一个终端里检查这几件事——它们分别对应平台依赖的每一条：

```bash
docker exec wl-check id zhenghuanbo          # 约定 4：同名账号存在（uid 必须是 1000）
docker exec wl-check ls -ld /home/zhenghuanbo # 约定 5：账号拥有 home
docker exec wl-check netstat -lntp           # 约定 2 + 6：22 与 8888 都在监听
docker exec wl-check curl -sI localhost:8888/x/1/ | head -1
docker exec wl-check conda --version && docker exec wl-check git --version
```

> 注意：`docker run <image> jupyter lab …` 这种写法**不会**覆盖 ENTRYPOINT（Docker 替换的是
> CMD，不是 ENTRYPOINT），所以那一次运行里 ENTRYPOINT 会先准备环境再降权，包装器又会被
> 调用一次。这条路径也是通的（`runtime.sh` 与 `wl-run` 都显式处理了非 root 的情况）。
> 要**严格**模拟 K8s 的 `command` 语义，加 `--entrypoint /opt/wl/bin/jupyter`，把 IDE 参数
> 放在镜像名之后。

真正端到端的验证是在集群里：把镜像推到 `library/`，建一个 `ide_type=jupyter` 的实例，
等它变「运行中」，然后确认 IDE 链接能打开、SSH 能登进去。**`library/` 里没有带 Jupyter
的镜像之前，任何 Jupyter 实例都会 CrashLoopBackOff**——这就是这个镜像要解决的问题。

本镜像在写完后已按上面这套逐条跑过：**19 项断言全过**，包括用挂载进来的公钥真的
`ssh -p <port> <username>@<host>` 登进去、在会话里 import numpy、以及在 `/x/{id}/` 下拿到
Jupyter 的 200。ttyd 的 `--base-path` 补偿也单独验证过（`/x/3/` → 200，`/` → 404）。
