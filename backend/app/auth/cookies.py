"""Refresh-token cookie handling.

The refresh token is the only credential stored in a cookie: it is httpOnly so
script cannot read it, same-site so a cross-site POST never carries it, and
path-scoped to the auth endpoints so it is not attached to ordinary API calls.
The access token stays in frontend memory and travels in the ``Authorization``
header, which a cross-site attacker cannot set — that is what keeps mutations
CSRF-safe without a separate token dance.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import Response

from app.core.config import Settings

REFRESH_COOKIE_NAME = "wl_refresh"


def refresh_cookie_path(settings: Settings) -> str:
    return f"{settings.api_v1_prefix}/auth"


def set_refresh_cookie(
    response: Response,
    token: str,
    expires_at: datetime,
    settings: Settings,
) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        expires=expires_at,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path=refresh_cookie_path(settings),
    )


def clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path=refresh_cookie_path(settings),
    )
