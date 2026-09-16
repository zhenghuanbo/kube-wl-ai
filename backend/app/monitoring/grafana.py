"""Grafana integration: scoped, short-lived embed URLs and the dashboard catalogue.

The whole module exists to answer one question safely: *how does a user see a
Grafana dashboard covering exactly the containers they are allowed to see, without
Grafana knowing anything about our permission model?*

The answer is a signed JWT that Grafana accepts in place of its own login, plus a
URL that pre-fills the template variables the dashboard is built around. Three
things make that safe, and all three are load-bearing:

1. **Grafana never sees a wildcard from a normal user.** The claims carry the
   exact namespaces and pods the caller may watch, computed from the same data
   scope the REST API uses. A user with one workspace gets one namespace.
2. **The URL pre-fills those variables and the dashboard is built to require
   them.** A pre-filled variable the viewer can still edit is a suggestion, not a
   restriction — so the dashboards in ``deploy/grafana/dashboards`` declare
   ``"skipUrlSync": false`` on a locked query and every panel filters on it.
3. **The token lives five minutes.** It is in a URL, and URLs leak: into browser
   history, into a screenshot, into a colleague's chat message. A five-minute
   credential that only ever granted read access to what the holder could already
   read is a bounded loss.

What the module deliberately does **not** do is query Prometheus. The platform's
own records are the source of truth for consumption (see `app/services/usage.py`);
Grafana is here to answer the different question "what is it doing right now".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from app.auth.tokens import GRAFANA_TOKEN_TYPE, create_access_token
from app.core.config import Settings
from app.models import User

#: Marks the dashboards this platform owns, so an operator can see at a glance
#: which ones arrived with the app and which the lab added.
MANAGED_TAG = "kube-wl-ai"


@dataclass(frozen=True, slots=True)
class DashboardSpec:
    """One entry in the platform's dashboard catalogue.

    ``uid`` must match the ``uid`` in the provisioned JSON in
    ``deploy/grafana/dashboards`` — the URL addresses a dashboard by uid, not by
    name, precisely so renaming it in the Grafana UI does not break the link.
    """

    key: str
    uid: str
    title: str
    description: str
    #: Permission code required to open it.
    permission: str
    icon: str
    #: Template variables the dashboard needs, and how to fill them.
    #: ``namespace`` is always present; ``pod`` only for per-container views.
    scoped_to: tuple[str, ...]


#: The catalogue. Ordered, because the UI renders it in this order and the first
#: one is what a user lands on.
DASHBOARDS: tuple[DashboardSpec, ...] = (
    DashboardSpec(
        key="lab-overview",
        uid="wl-lab-overview",
        title="实验室总览",
        description="本组各命名空间的 GPU、CPU、内存使用与容器健康度。",
        permission="monitor:read_group",
        icon="mdi-view-dashboard-outline",
        scoped_to=("namespace",),
    ),
    DashboardSpec(
        key="my-containers",
        uid="wl-my-containers",
        title="我的容器",
        description="本人容器的资源曲线、重启次数与 GPU 占用。",
        permission="monitor:read_self",
        icon="mdi-cube-outline",
        scoped_to=("namespace", "pod"),
    ),
    DashboardSpec(
        key="gpu-fleet",
        uid="wl-gpu-fleet",
        title="GPU 集群",
        description="全体加速卡的在位、分配与利用率。",
        permission="monitor:read_all",
        icon="mdi-expansion-card",
        scoped_to=(),
    ),
    DashboardSpec(
        key="node-health",
        uid="wl-node-health",
        title="节点健康",
        description="节点状态、负载与显存压力。",
        permission="monitor:read_all",
        icon="mdi-server-network",
        scoped_to=(),
    ),
)

DASHBOARDS_BY_KEY: dict[str, DashboardSpec] = {spec.key: spec for spec in DASHBOARDS}


@dataclass(frozen=True, slots=True)
class MonitorScope:
    """
    The namespaces and pods a caller may watch, resolved from the data scope.

    An empty ``namespaces`` on a *restricted* caller means "nothing", which is a
    legitimate state — somebody with no running containers. It is kept distinct
    from ``unrestricted`` so the two never collapse into the same variable value:
    a dashboard filtered on an empty list shows zero, while an unfiltered one
    shows the whole institution.
    """

    namespaces: tuple[str, ...]
    pods: tuple[str, ...]
    unrestricted: bool

    @property
    def is_empty(self) -> bool:
        return not self.unrestricted and not self.namespaces


def dashboard_variables(scope: MonitorScope, spec: DashboardSpec) -> dict[str, str]:
    """
    The ``var-*`` query parameters for one dashboard.

    An unrestricted caller is *not* given a wildcard variable. Grafana's
    ``All`` value is spelled ``$__all`` and is only meaningful to a dashboard
    whose variable is defined as multi-value; passing it for a single-value
    variable silently selects nothing. Omitting the parameter instead lets the
    dashboard fall back to its own "All" default, which is what an administrator
    wants and what the provisioned JSON declares.
    """
    if scope.unrestricted:
        return {}

    variables: dict[str, str] = {}
    if "namespace" in spec.scoped_to:
        # Grafana reads a repeated parameter as a multi-value selection.
        variables["var-namespace"] = ",".join(scope.namespaces) if scope.namespaces else "none"
    if "pod" in spec.scoped_to:
        variables["var-pod"] = ",".join(scope.pods) if scope.pods else "none"
    return variables


def build_grafana_token(
    user: User,
    scope: MonitorScope,
    settings: Settings,
    *,
    spec: DashboardSpec | None = None,
) -> str:
    """
    Mint the JWT Grafana accepts in place of a session cookie.

    The claims are deliberately explicit rather than derived by Grafana from
    anything: ``wl_namespaces`` / ``wl_pods`` are the whole authorisation, and
    ``wl_unrestricted`` says whether they should be enforced. Grafana maps them
    to template variables through ``GF_AUTH_JWT_...`` configuration (see
    ``deploy/grafana/README.md``); it makes no decision of its own.

    Note the asymmetry with the access token: this one is *not* re-checked
    against the database on every use, because Grafana is doing the checking and
    does not know how to ask. That is the entire reason for the five-minute
    lifetime — it bounds how long a revoked permission keeps working.
    """
    claims: dict[str, Any] = {
        "aud": settings.grafana_jwt_audience,
        "orgId": settings.grafana_org_id,
        "wl_namespaces": list(scope.namespaces),
        "wl_pods": list(scope.pods),
        "wl_unrestricted": scope.unrestricted,
        "wl_dashboard": spec.key if spec else None,
    }
    token, _ = create_access_token(
        user_id=user.id,
        username=user.username,
        settings=settings,
        extra_claims=claims,
        token_type=GRAFANA_TOKEN_TYPE,
        expires_in=timedelta(seconds=settings.grafana_jwt_ttl_seconds),
    )
    return token


def build_embed_url(
    spec: DashboardSpec,
    scope: MonitorScope,
    token: str,
    settings: Settings,
    *,
    theme: str = "light",
    kiosk: bool = True,
) -> str:
    """
    Assemble the iframe ``src``.

    ``kiosk`` strips Grafana's own chrome, so the panel reads as part of this app
    rather than as a second application embedded in it. It is not a security
    measure — the scope is in the token and the variables — and should not be
    mistaken for one.
    """
    base = settings.grafana_public_url.rstrip("/")
    query = {"auth_token": token, "theme": theme, "orgId": str(settings.grafana_org_id)}
    if kiosk:
        query["kiosk"] = "tv"
    query.update(dashboard_variables(scope, spec))
    return f"{base}/d/{spec.uid}?{urlencode(query)}"


def dashboards_for(permissions: frozenset[str]) -> list[DashboardSpec]:
    """Catalogue entries the caller holds the permission for, in display order."""
    return [spec for spec in DASHBOARDS if spec.permission in permissions]
