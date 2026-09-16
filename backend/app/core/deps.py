"""FastAPI dependencies for authentication and authorisation.

Thin wrappers around `app.auth.policy`; keeping the FastAPI imports here means
the policy engine stays unit-testable as plain functions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.auth.policy import DataScope
from app.auth.tokens import decode_access_token
from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError, PermissionDeniedError, UnauthorizedError
from app.db.session import get_session
from app.models.user import User
from app.services import api_tokens as api_token_service

#: Personal access tokens are recognisable by prefix, which is what lets one
#: ``Authorization: Bearer`` header carry two credential types without either
#: implementation knowing about the other. A JWT is three base64 segments joined
#: by dots and never begins with this.
API_TOKEN_PREFIX = "wl_"

#: ``auto_error=False`` so we can raise our own uniform error envelope.
_bearer = HTTPBearer(auto_error=False, description="Access token issued by /auth/login")


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    """Resolve the authenticated user from the bearer token.

    The user is always re-read from the database, so deactivating an account or
    changing its roles takes effect immediately rather than when the token
    happens to expire.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("请先登录", code="missing_token")

    presented = credentials.credentials

    if presented.startswith(API_TOKEN_PREFIX):
        user, ceiling = await _resolve_api_token(request, session, presented)
    else:
        decoded = decode_access_token(presented, settings)
        user = await _load_user(session, decoded.user_id)
        ceiling = None

    # Always written, including as `None` for a session token. Setting it only
    # when a token is present would let a request inherit whatever ceiling the
    # previous request in the same context left behind — a token restriction that
    # outlives the token is precisely the failure this mechanism exists to avoid.
    policy.set_permission_ceiling(ceiling)

    if not user.is_active:
        raise ForbiddenError("账户已被停用", code="account_disabled")
    if user.is_expired:
        raise ForbiddenError("账户已过期", code="account_expired")

    # Read by the audit middleware; kept on scope["state"], which every
    # middleware in the chain shares regardless of task boundaries.
    request.state.user_id = user.id
    request.state.username = user.username

    return user


async def _load_user(session: AsyncSession, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise UnauthorizedError("用户不存在", code="invalid_token")
    return user


async def _resolve_api_token(
    request: Request, session: AsyncSession, presented: str
) -> tuple[User, frozenset[str] | None]:
    """Authenticate a personal access token.

    Returns the owner and the ceiling its scopes impose. An empty scope string
    yields ``None``, meaning "track the owner's roles" — the token then gains and
    loses permissions exactly as the account does, which is what makes revoking a
    role an immediate revocation of every token that role was carrying.
    """
    token = await api_token_service.resolve_token(session, presented)
    if token is None:
        # Same message for "no such token" and "expired": the caller learns
        # nothing from the difference, and an operator reads it from the log.
        raise UnauthorizedError("API 令牌无效或已过期", code="invalid_token")

    user = await _load_user(session, token.user_id)

    # The audit trail records *which kind of credential* acted, because "who did
    # this" and "what did this" are different questions once automation is in
    # play, and the second one is usually the one being asked.
    request.state.auth_method = "api_token"
    request.state.api_token_id = token.id

    # Deferred to the response, not awaited here: the session commits once at the
    # end of the request, so a read-only call still records the use without a
    # second round trip of its own.
    await api_token_service.touch(session, token)

    scopes = api_token_service.parse_scopes(token.scopes)
    return user, (scopes or None)


async def get_optional_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User | None:
    """Like :func:`get_current_user` but returns ``None`` instead of raising."""
    if credentials is None or not credentials.credentials:
        return None
    try:
        return await get_current_user(request, credentials, session, settings)
    except (UnauthorizedError, ForbiddenError):
        return None


#: Convenient annotated types for router signatures.
CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def require(code: str) -> Callable[[User], Awaitable[User]]:
    """Dependency factory enforcing a single permission code.

    Usage::

        @router.post("/instances", dependencies=[Depends(require("instance:create"))])
    """

    async def _checker(user: CurrentUser) -> User:
        policy.require_permission(user, code)
        return user

    return _checker


def require_any(*codes: str) -> Callable[[User], Awaitable[User]]:
    """Dependency factory enforcing at least one of ``codes``."""

    async def _checker(user: CurrentUser) -> User:
        if not any(policy.has_permission(user, code) for code in codes):
            raise PermissionDeniedError(
                f"缺少权限：{' 或 '.join(codes)}",
                detail={"required_any": list(codes)},
            )
        return user

    return _checker


def current_scope(user: CurrentUser) -> DataScope:
    """Convenience dependency exposing the resolved data scope."""
    return policy.resolve_scope(user)
