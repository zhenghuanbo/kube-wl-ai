"""System information for the administration page.

The whole point of this module is the **warning list**. A page that prints the
configuration is a page nobody reads; a page that says *"SSH is configured but
``SSH_HOST`` is empty, so the SSH command shown to users will name a
cluster-internal address"* is a page that answers the question the administrator
actually arrived with.

Two rules govern what may appear here, and both are enforced by construction
rather than by review:

1. **No credentials.** ``Settings`` is never serialised. Every value is copied
   across by hand, so a new secret setting cannot appear on this page by
   accident — it appears only if somebody adds a line, which is a decision.
2. **A boolean is not enough.** ``enabled: false`` on its own sends the reader to
   the source to find out why. Every feature carries a ``detail`` and every
   warning carries a ``remedy``.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.version import VERSION
from app.models.enums import InstanceStatus
from app.models.group import Group
from app.models.instance import Instance
from app.models.resource import ComputeNode, GpuDevice
from app.models.user import User
from app.models.workspace import Workspace
from app.schemas.system import (
    SystemCounts,
    SystemFeature,
    SystemInfo,
    SystemWarning,
    WarningSeverity,
)


def _feature(key: str, title: str, enabled: bool, detail: str, config_key: str) -> SystemFeature:
    return SystemFeature(
        key=key, title=title, enabled=enabled, detail=detail, config_key=config_key
    )


def _features(settings: Settings) -> list[SystemFeature]:
    providers = settings.auth_providers
    has_ldap = "ldap" in providers

    return [
        _feature(
            "ldap",
            "LDAP / FreeIPA 目录",
            has_ldap and bool(settings.ldap_uri),
            (
                f"绑定 {settings.ldap_uri}，基准 DN {settings.ldap_base_dn}"
                if has_ldap and settings.ldap_uri
                else "未启用，用户只能使用本地账号"
            ),
            "LDAP_URI",
        ),
        _feature(
            "local",
            "本地兜底账号",
            "local" in providers,
            (
                f"应急管理员 {settings.local_admin_username}"
                if "local" in providers
                else "已关闭。目录不可用将无人能登录"
            ),
            "AUTH_PROVIDERS / LOCAL_ADMIN_USERNAME",
        ),
        _feature(
            "oidc",
            "OIDC 统一身份",
            "oidc" in providers,
            (
                f"发行方 {settings.oidc_issuer}"
                if "oidc" in providers and settings.oidc_issuer
                else "未接入。大学统一身份是 FreeIPA，OIDC 需要校方先注册客户端"
            ),
            "AUTH_PROVIDERS / OIDC_ISSUER",
        ),
        _feature(
            "kubernetes",
            "Kubernetes 集群",
            settings.k8s_enabled,
            (
                "以 in-cluster 身份访问"
                if settings.k8s_enabled and settings.k8s_in_cluster
                else (
                    f"使用 kubeconfig {settings.kubeconfig}"
                    if settings.k8s_enabled and settings.kubeconfig
                    else "已关闭，实例与任务接口不可用"
                )
            ),
            "K8S_ENABLED / K8S_IN_CLUSTER",
        ),
        _feature(
            "grafana",
            "Grafana 监控",
            settings.grafana_enabled,
            (
                f"面板挂载在同源路径 {settings.grafana_public_url}"
                if settings.grafana_enabled
                else "未配置，监控中心只显示平台自身统计"
            ),
            "GRAFANA_PUBLIC_URL",
        ),
        _feature(
            "prometheus",
            "Prometheus",
            bool(settings.prometheus_url),
            settings.prometheus_url or "未配置。用量统计不依赖它，不受影响",
            "PROMETHEUS_URL",
        ),
        _feature(
            "harbor",
            "Harbor 镜像仓库",
            bool(settings.harbor_url),
            (
                f"{settings.harbor_url}，机器人账户 {settings.harbor_robot_name}"
                if settings.harbor_url
                else "未配置，镜像只能使用公开地址"
            ),
            "HARBOR_URL",
        ),
    ]


def _warnings(settings: Settings, counts: SystemCounts) -> list[SystemWarning]:
    """Concrete misconfigurations, worst first.

    Each check here corresponds to a failure that has actually been observed or
    that the code would produce silently. Speculative warnings are not added —
    a page that cries wolf gets ignored, which costs more than it saves.
    """
    warnings: list[SystemWarning] = []

    def add(code: str, severity: WarningSeverity, title: str, detail: str, remedy: str) -> None:
        warnings.append(
            SystemWarning(
                code=code,
                severity=severity,
                title=title,
                detail=detail,
                remedy=remedy,
            )
        )

    # --- security ---------------------------------------------------------
    if settings.app_env == "production" and settings.debug:
        add(
            "debug_in_production",
            "critical",
            "生产环境开启了 DEBUG",
            "调试模式会输出更详细的错误信息，其中可能包含数据库语句与内部路径。",
            "设置 DEBUG=false",
        )

    # No `SECRET_KEY` check here on purpose: `Settings._guard_production_secrets`
    # already refuses to start in production with a weak key, placeholder admin
    # password or empty CORS list. A warning for a state the process cannot be
    # running in is dead code that reads like a live control.

    if not settings.login_rate_limit_enabled:
        add(
            "rate_limit_disabled",
            "warning",
            "登录限流已关闭",
            "任何人都可以无限制地尝试口令，审计日志只记录、不阻断。",
            "设置 LOGIN_RATE_LIMIT_ENABLED=true",
        )

    # --- authentication ---------------------------------------------------
    providers = settings.auth_providers
    if "ldap" in providers and not settings.ldap_uri:
        add(
            "ldap_without_uri",
            "critical",
            "启用了 LDAP 但没有配置地址",
            "所有目录账号都无法登录，且失败信息会指向认证失败而不是配置缺失。",
            "设置 LDAP_URI，或从 AUTH_PROVIDERS 中移除 ldap",
        )

    if (
        providers
        and "local" not in providers
        and not any(p in providers and getattr(settings, f"{p}_uri", "") for p in providers)
    ):
        add(
            "no_usable_provider",
            "critical",
            "没有任何可用的认证方式",
            "目录未配置且本地账号已关闭，没有人能登录，包括管理员。",
            "在 AUTH_PROVIDERS 中保留 local 作为兜底",
        )

    # --- runtime ----------------------------------------------------------
    if not settings.k8s_enabled:
        add(
            "kubernetes_disabled",
            "warning",
            "Kubernetes 已关闭",
            "开发实例、应用程序与自定义任务接口会返回不可用，只有用户、组、配额等元数据可管理。",
            "设置 K8S_ENABLED=true 并确认服务账号权限",
        )

    missing_classes = [
        name
        for name, value in (
            ("HOME", settings.storage_class_home),
            ("DATA", settings.storage_class_data),
            ("SHARE", settings.storage_class_share),
        )
        if not value
    ]
    if missing_classes:
        add(
            "storage_class_missing",
            "warning",
            "有存储类型没有配置 StorageClass",
            "对应的「创建数据盘」会在运行时才失败，报错是集群侧的 ProvisioningFailed，"
            "看不出是配置缺失。",
            "设置 " + "、".join(f"STORAGE_CLASS_{name}" for name in missing_classes),
        )

    # `ssh_mode` is a single-member Literal today (nodeport), so this warns about
    # the address rather than about the mode. When a second mode is added, the
    # branch belongs here — and the check below is what a lab forgets, because
    # the default (the node's own hostname) works on a single-node cluster and
    # silently fails everywhere else.
    if not settings.instance_ssh_host:
        add(
            "ssh_host_missing",
            "warning",
            "没有配置 SSH 对外地址",
            "用户在实例详情页看到的 SSH 命令会指向集群内部地址，从校园网连不上。"
            "单节点集群恰好能用，所以这个配置缺失通常到多节点扩容后才暴露。",
            "设置 INSTANCE_SSH_HOST 为集群的对外地址",
        )

    if not settings.grafana_enabled and settings.prometheus_url:
        add(
            "grafana_without_public_url",
            "info",
            "已接入 Prometheus 但未配置 Grafana",
            "监控中心只显示平台自身的容器清单，不提供面板。",
            "设置 GRAFANA_PUBLIC_URL 后把面板目录导入 Grafana",
        )

    # --- data -------------------------------------------------------------
    if counts.active_users == 0:
        add(
            "no_active_users",
            "info",
            "没有启用中的用户",
            "除了应急管理员以外没有任何账号。",
            "在「用户管理」中创建账号，或确认目录同步是否正常",
        )

    if counts.workspaces == 0:
        add(
            "no_workspaces",
            "info",
            "还没有工作空间",
            "工作空间对应一个 Kubernetes 命名空间；没有它，用户无法创建实例。",
            "在「工作空间管理」中创建",
        )

    if counts.nodes == 0 and settings.k8s_enabled:
        add(
            "no_nodes",
            "info",
            "还没有同步过集群节点",
            "算力池的资源规格是按节点容量校验的，节点表为空时配额计算会偏保守。",
            "在「资源与配额 → 集群节点」执行一次同步",
        )

    return warnings


async def build_info(session: AsyncSession, settings: Settings) -> SystemInfo:
    """Assemble the page payload.

    The counts are a handful of ``COUNT(*)`` queries rather than one clever
    aggregate: this endpoint is opened by hand, by an administrator, when
    something is wrong, and a query that is obviously correct is worth more here
    than one that is fast.
    """
    users = await session.scalar(select(func.count()).select_from(User)) or 0
    active_users = (
        await session.scalar(select(func.count()).select_from(User).where(User.is_active.is_(True)))
        or 0
    )
    groups = await session.scalar(select(func.count()).select_from(Group)) or 0
    workspaces = await session.scalar(select(func.count()).select_from(Workspace)) or 0
    nodes = await session.scalar(select(func.count()).select_from(ComputeNode)) or 0
    gpus = await session.scalar(select(func.count()).select_from(GpuDevice)) or 0
    instances = await session.scalar(select(func.count()).select_from(Instance)) or 0
    running = (
        await session.scalar(
            select(func.count())
            .select_from(Instance)
            .where(Instance.status == InstanceStatus.RUNNING)
        )
        or 0
    )

    counts = SystemCounts(
        users=users,
        active_users=active_users,
        groups=groups,
        workspaces=workspaces,
        nodes=nodes,
        gpus=gpus,
        instances=instances,
        running_instances=running,
    )

    return SystemInfo(
        app_name="kube-wl-ai",
        app_version=VERSION,
        environment=settings.app_env,
        debug=settings.debug,
        auth_providers=list(settings.auth_providers),
        features=_features(settings),
        warnings=_warnings(settings, counts),
        access_token_minutes=settings.access_token_expire_minutes,
        refresh_token_days=settings.refresh_token_expire_days,
        login_rate_limit_enabled=settings.login_rate_limit_enabled,
        login_rate_limit_window_seconds=settings.login_rate_limit_window_seconds,
        login_rate_limit_attempts_per_ip=settings.login_rate_limit_attempts_per_ip,
        login_rate_limit_attempts_per_username=(settings.login_rate_limit_attempts_per_username),
        counts=counts,
        ssh_mode=settings.ssh_mode,
        ssh_host=settings.instance_ssh_host or None,
        storage_classes={
            "home": settings.storage_class_home or None,
            "data": settings.storage_class_data or None,
            "share": settings.storage_class_share or None,
        },
    )
