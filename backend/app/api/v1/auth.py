"""Authentication endpoints: login, refresh, logout, identity and permissions."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.auth import policy, rate_limit
from app.auth import service as auth_service
from app.auth.cookies import REFRESH_COOKIE_NAME, clear_refresh_cookie, set_refresh_cookie
from app.auth.providers import build_providers
from app.core.audit import record_audit
from app.core.context import get_client_ip
from app.core.deps import AppSettings, CurrentUser, DbSession, OptionalUser
from app.core.errors import UnauthorizedError
from app.schemas.auth import (
    CurrentUserResponse,
    DataScopeResponse,
    LoginOptionsResponse,
    LoginRequest,
    PermissionsResponse,
    ProviderInfo,
    RefreshRequest,
    TokenResponse,
)
from app.schemas.base import ApiMessage

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    user_agent = request.headers.get("user-agent")
    return (user_agent[:255] if user_agent else None), get_client_ip()


def _token_response(issued: auth_service.IssuedSession, settings: AppSettings) -> TokenResponse:
    return TokenResponse(
        access_token=issued.access_token,
        token_type="bearer",
        expires_in=settings.access_token_expire_minutes * 60,
        expires_at=issued.access_expires_at,
        user=CurrentUserResponse.from_user(issued.user),
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in and start a session",
    responses={
        401: {"description": "Invalid credentials"},
        503: {"description": "Directory unreachable"},
    },
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: DbSession,
    settings: AppSettings,
) -> TokenResponse:
    """Verify credentials against the configured providers and issue a session.

    The access token is returned in the body; the refresh token is set as an
    httpOnly cookie and never appears in the response payload.

    The attempt is rate limited *before* the credentials are checked — see
    `app.auth.rate_limit` — so a locked-out attempt costs no directory round trip
    and cannot be used to make the LDAP server do work.
    """
    user_agent, ip_address = _client_meta(request)

    await rate_limit.enforce_login_rate_limit(
        session, username=payload.username, ip_address=ip_address, settings=settings
    )

    try:
        user, provider = await auth_service.authenticate(
            session, payload.username, payload.password, settings=settings
        )
    except UnauthorizedError as exc:
        await rate_limit.record_login_failure(
            request=request, username=payload.username, error_message=exc.message
        )
        raise

    issued = await auth_service.issue_session(
        session,
        user,
        provider=provider,
        user_agent=user_agent,
        ip_address=ip_address,
        settings=settings,
    )
    set_refresh_cookie(response, issued.refresh_token, issued.refresh_expires_at, settings)

    record_audit(
        session,
        action="auth:login",
        actor=user,
        object_type="user",
        object_id=user.id,
        detail={"provider": provider},
        request=request,
    )

    return _token_response(issued, settings)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate the refresh token and issue a new access token",
    responses={401: {"description": "Refresh token missing, expired or already used"}},
)
async def refresh(
    request: Request,
    response: Response,
    session: DbSession,
    settings: AppSettings,
    payload: RefreshRequest | None = None,
) -> TokenResponse:
    """Exchange the refresh cookie for a new session.

    Rotation is unconditional: the presented token is revoked and replaced. If a
    token that was already rotated is presented again, the whole family is
    revoked — that is the standard defence against a stolen refresh token.
    """
    raw_token = (payload.refresh_token if payload else None) or request.cookies.get(
        REFRESH_COOKIE_NAME
    )
    if not raw_token:
        raise UnauthorizedError("缺少刷新令牌", code="missing_refresh_token")

    user_agent, ip_address = _client_meta(request)
    issued = await auth_service.rotate_refresh_token(
        session,
        raw_token,
        user_agent=user_agent,
        ip_address=ip_address,
        settings=settings,
    )
    set_refresh_cookie(response, issued.refresh_token, issued.refresh_expires_at, settings)

    return _token_response(issued, settings)


@router.post(
    "/logout",
    response_model=ApiMessage,
    summary="End the current session",
)
async def logout(
    request: Request,
    response: Response,
    session: DbSession,
    settings: AppSettings,
    user: OptionalUser = None,
) -> ApiMessage:
    """Revoke the presented refresh token and clear the cookie.

    Deliberately does not require a valid access token: a session whose access
    token has already expired must still be able to log out cleanly.
    """
    raw_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_token:
        await auth_service.revoke_refresh_token(session, raw_token)

    clear_refresh_cookie(response, settings)

    record_audit(
        session,
        action="auth:logout",
        actor=user,
        object_type="user",
        object_id=user.id if user else None,
        actor_username=user.username if user else "anonymous",
        request=request,
    )
    return ApiMessage(message="已退出登录")


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="The authenticated user",
)
async def me(user: CurrentUser) -> CurrentUserResponse:
    return CurrentUserResponse.from_user(user)


@router.get(
    "/permissions",
    response_model=PermissionsResponse,
    summary="The caller's effective permissions and data scope",
)
async def permissions(user: CurrentUser) -> PermissionsResponse:
    """Everything the frontend needs to decide what to render.

    The frontend treats this as advisory only — every request is re-checked
    server-side.
    """
    scope = policy.resolve_scope(user)
    return PermissionsResponse(
        permissions=sorted(policy.effective_permissions(user)),
        roles=policy.effective_role_codes(user),
        scope=DataScopeResponse(**scope.to_dict()),
        is_global_admin=policy.is_global_admin(user),
    )


@router.get(
    "/login-options",
    response_model=LoginOptionsResponse,
    summary="Which login methods the UI should offer",
)
async def login_options(settings: AppSettings) -> LoginOptionsResponse:
    """Public endpoint used by the login screen before any session exists."""
    providers = build_providers(settings)
    return LoginOptionsResponse(
        providers=[
            ProviderInfo(name=provider.name, description=provider.describe())
            for provider in providers
        ],
        ldap_enabled=any(provider.name == "ldap" for provider in providers),
        oidc_enabled=any(provider.name == "oidc" for provider in providers),
    )
