"""System information endpoint.

Gated on `system:config`, which only a global administrator holds. Not because
the content is secret — it is not, and the module docstring on
`app.services.system` explains how that is enforced — but because it is a map of
the deployment's weak points. A normal user reading "LDAP is enabled but
unconfigured" learns something they can use, and gains nothing.

There is no write endpoint. Runtime configuration comes from the environment and
is read once at startup; a page that edited it would either lie (change a value
the process already cached) or require a restart it cannot perform. The chart and
the ConfigMap are the interface, and this page tells an administrator what to put
in them.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.core.deps import CurrentUser, DbSession, require
from app.schemas.system import SystemInfo
from app.services import system as system_service

router = APIRouter(prefix="/system", tags=["system"])

CanReadSystem = Depends(require("system:config"))


@router.get(
    "/info",
    response_model=SystemInfo,
    summary="系统信息与配置体检（全局管理员）",
)
async def info(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[None, CanReadSystem],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SystemInfo:
    """Runtime state, integration status, and concrete misconfiguration warnings."""
    del actor  # the permission dependency is the whole authorisation decision
    return await system_service.build_info(session, settings)
