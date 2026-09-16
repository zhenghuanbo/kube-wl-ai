"""Monitoring endpoints.

The design constraint that shaped this module: **the platform mints Grafana
credentials, so every one of them must already be scoped.** A user never receives
a Grafana URL they can widen by editing it — the scope is inside the signed token
and repeated in the URL's template variables, and the dashboards in
``deploy/grafana/dashboards`` are built to require those variables rather than
merely default to them.

Two consequences worth stating explicitly:

* **The embed URL is minted per request, not listed once.** It carries a
  five-minute credential, so a page that cached it would start showing Grafana's
  own login page after five minutes with nothing to explain why. The overview
  mints the first one and the page re-mints on demand.
* **A dashboard the caller cannot open is absent, not empty.** Returning the
  entry with a null URL would invite the frontend to render a disabled tile, and
  the catalog of what exists is itself information: it tells a normal user that
  a fleet-wide dashboard exists but is not theirs.

When Grafana is not configured (`GRAFANA_PUBLIC_URL` unset) the endpoints still
answer. `enabled` is false, `dashboards` is empty, and the reason is stated — the
platform's own inventory remains useful on its own, and a monitoring page that
500s because an optional integration is missing is worse than one that says so.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.auth import policy
from app.auth.permissions import ALL_PERMISSION_CODES
from app.core.config import Settings, get_settings
from app.core.deps import CurrentUser, DbSession, require
from app.core.errors import NotFoundError
from app.monitoring.grafana import (
    DASHBOARDS_BY_KEY,
    DashboardSpec,
    MonitorScope,
    build_embed_url,
    build_grafana_token,
    dashboards_for,
)
from app.schemas.monitor import (
    MonitorDashboardRead,
    MonitorEmbedRead,
    MonitorOverview,
)
from app.services import monitor as monitor_service

router = APIRouter(prefix="/monitor", tags=["monitor"])

#: Reading anything here requires *a* monitoring permission; which rows come back
#: is decided by the data scope, not by which of the three codes was checked.
CanRead = Depends(require("monitor:read_self"))


def _permissions(actor: object) -> frozenset[str]:
    """
    The caller's permission codes.

    A superuser short-circuits to the whole catalogue for the same reason
    `effective_permissions` does: the flag exists precisely to bypass the mapping,
    and re-deriving it here would make a superuser with no roles see no
    dashboards.
    """
    user = actor  # narrowed by the dependency; kept local for the type checker
    if getattr(user, "is_superuser", False):
        return ALL_PERMISSION_CODES
    return policy.effective_permissions(user)  # type: ignore[arg-type]


def _embed(
    spec: DashboardSpec, scope: MonitorScope, actor: object, settings: Settings
) -> MonitorEmbedRead:
    token = build_grafana_token(actor, scope, settings, spec=spec)  # type: ignore[arg-type]
    return MonitorEmbedRead(
        key=spec.key,
        url=build_embed_url(spec, scope, token, settings),
        expires_in=settings.grafana_jwt_ttl_seconds,
    )


@router.get(
    "/overview",
    response_model=MonitorOverview,
    summary="监控总览（含已签名的 Grafana 面板地址）",
)
async def overview(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanRead],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MonitorOverview:
    """Inventory, the caller's live containers, and signed panel URLs."""
    permissions = _permissions(actor)
    scope = await monitor_service.resolve_scope(session, actor)
    # With Grafana unconfigured the catalogue is withheld rather than listed
    # without URLs. A tile that cannot be opened is worse than no tile: it tells
    # the reader that monitoring is broken when the truth is that this
    # deployment never had it.
    catalog = dashboards_for(permissions) if settings.grafana_enabled else []

    embeds: dict[str, MonitorEmbedRead] = {}
    if settings.grafana_enabled:
        embeds = {spec.key: _embed(spec, scope, actor, settings) for spec in catalog}

    return MonitorOverview(
        enabled=settings.grafana_enabled,
        disabled_reason=None
        if settings.grafana_enabled
        else "未配置 Grafana（GRAFANA_PUBLIC_URL 为空），仅显示平台自身统计。",
        scope=await monitor_service.scope_read(session, actor),
        inventory=await monitor_service.inventory(session),
        containers=monitor_service.to_watched_read(
            await monitor_service.watched_instances(session, actor)
        ),
        dashboards=[
            MonitorDashboardRead(
                key=spec.key,
                uid=spec.uid,
                title=spec.title,
                description=spec.description,
                icon=spec.icon,
            )
            for spec in catalog
        ],
        embeds=embeds,
    )


@router.post(
    "/dashboards/{key}/embed",
    response_model=MonitorEmbedRead,
    summary="重新签发某个面板的短期地址",
)
async def mint_embed(
    key: str,
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanRead],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MonitorEmbedRead:
    """
    Mint a fresh URL for one dashboard.

    A `POST` because it creates a credential — it is not safe to retry blindly
    from a cache, and the method says so. The permission is checked against the
    *dashboard*, not merely against monitoring in general: holding
    `monitor:read_self` must not produce a fleet-wide URL by naming its key.
    """
    spec = DASHBOARDS_BY_KEY.get(key)
    # An unknown key and one the caller may not open answer identically. Telling
    # them apart would confirm that a dashboard named "GPU 集群" exists, which is
    # exactly the catalog disclosure the overview already declines to make.
    if spec is None or spec.permission not in _permissions(actor):
        raise NotFoundError(f"监控面板不存在：{key}", code="dashboard_not_found")

    if not settings.grafana_enabled:
        raise NotFoundError("未配置 Grafana，无法签发面板地址", code="monitoring_disabled")

    scope = await monitor_service.resolve_scope(session, actor)
    return _embed(spec, scope, actor, settings)
