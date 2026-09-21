"""Authentication service: login orchestration, JIT provisioning and sessions.

This is the only place that turns an external identity into a platform user.
Providers verify credentials (`app.auth.providers`); everything else lives here:

* **JIT provisioning** — first successful directory login creates the local user.
* **Directory group sync** — LDAP group names are mapped onto local groups.
  Only rows this process created are ever removed (``is_directory_managed``),
  so memberships granted by hand survive a directory change.
* **Session issuance** — short-lived JWT plus a rotating opaque refresh token.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.providers import AuthProviderError, ExternalIdentity, build_providers
from app.auth.tokens import create_access_token, generate_refresh_token, hash_token
from app.core.config import Settings, get_settings
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    ServiceUnavailableError,
    UnauthorizedError,
)
from app.core.logging import get_logger
from app.core.time import as_aware
from app.db.session import get_session_factory
from app.models.enums import GroupRole, RoleCode, RoleScope, UserSource
from app.models.group import Group, UserGroupLink
from app.models.rbac import Role, UserRole
from app.models.token import RefreshToken
from app.models.user import User
from app.services import group_admin

logger = get_logger(__name__)

#: Provider name -> the identity source it produces.
_SOURCE_BY_PROVIDER: dict[str, UserSource] = {
    "ldap": UserSource.IPA,
    "oidc": UserSource.OIDC,
    "local": UserSource.LOCAL,
}


@dataclass(slots=True)
class IssuedSession:
    user: User
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    provider: str = "local"


def _aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; compare them in UTC.

    Kept as a thin alias so existing call sites read the same; the logic lives in
    `app.core.time` alongside the other datetime normalisation.
    """
    normalised = as_aware(value)
    assert normalised is not None
    return normalised


# --- Authentication ----------------------------------------------------------
async def authenticate(
    session: AsyncSession,
    username: str,
    password: str,
    *,
    settings: Settings | None = None,
) -> tuple[User, str]:
    """Verify credentials against the enabled providers.

    Returns ``(user, provider_name)``. Raises ``UnauthorizedError`` when the
    credentials are rejected, ``ServiceUnavailableError`` when the directory is
    unreachable (an outage must never look like a wrong password), and
    ``ForbiddenError`` when the account exists but is disabled or expired.
    """
    settings = settings or get_settings()
    providers = build_providers(settings)
    if not providers:
        raise ServiceUnavailableError(
            "没有可用的认证方式，请检查 AUTH_PROVIDERS 配置",
            code="no_auth_provider",
        )

    identity: ExternalIdentity | None = None
    used_provider = ""

    for provider in providers:
        try:
            identity = await provider.authenticate(username, password, session=session)
        except AuthProviderError as exc:
            # Infrastructure failure: surface it, do not fall through to the
            # next provider as if the password had simply been wrong.
            logger.error("auth_provider_failed", provider=provider.name, error=str(exc))
            raise ServiceUnavailableError(
                f"认证服务暂时不可用（{provider.name}）",
                code="auth_provider_unavailable",
            ) from exc

        if identity is not None:
            used_provider = provider.name
            break

    if identity is None:
        raise UnauthorizedError("用户名或密码错误", code="invalid_credentials")

    source = _SOURCE_BY_PROVIDER.get(used_provider, UserSource.LOCAL)
    user = await provision_user(session, identity, source=source, settings=settings)

    if not user.is_active:
        raise ForbiddenError("账户已被停用", code="account_disabled")
    if user.is_expired:
        raise ForbiddenError("账户已过期", code="account_expired")

    await sync_directory_groups(session, user, identity, source=source)

    user.last_login_at = datetime.now(UTC)
    await session.flush()
    return user, used_provider


# --- Provisioning ------------------------------------------------------------
async def provision_user(
    session: AsyncSession,
    identity: ExternalIdentity,
    *,
    source: UserSource,
    settings: Settings | None = None,
) -> User:
    """Find or create the local user for an authenticated external identity."""
    settings = settings or get_settings()

    user: User | None = None
    if identity.external_id:
        user = await session.scalar(
            select(User).where(User.source == source, User.external_id == identity.external_id)
        )
    if user is None:
        user = await session.scalar(select(User).where(User.username == identity.username))

    if user is not None and source != UserSource.LOCAL and user.password_hash:
        # An external identity may not take over a local account that happens to
        # share its username — that account could hold global_admin.
        raise ConflictError(
            "该用户名已被本地账户占用，请联系管理员重命名后重试",
            code="username_conflict",
        )

    if user is None:
        user = User(
            username=identity.username,
            email=identity.email,
            full_name=identity.full_name,
            source=source,
            external_id=identity.external_id,
            is_active=True,
        )
        session.add(user)
        await session.flush()

        is_observer = bool(
            settings.ldap_observer_group and settings.ldap_observer_group in identity.groups
        )
        await assign_default_role(session, user, is_observer=is_observer)
        logger.info(
            "user_provisioned",
            username=user.username,
            source=source.value,
            role="observer" if is_observer else "user",
        )
    else:
        if identity.email and user.email != identity.email:
            user.email = identity.email
        if identity.full_name and user.full_name != identity.full_name:
            user.full_name = identity.full_name
        if identity.external_id and user.external_id != identity.external_id:
            user.external_id = identity.external_id

    await session.flush()
    # Callers immediately need roles (the permission set) and groups (the data
    # scope). Loading them here keeps those reads inside async context instead
    # of tripping a lazy load later.
    await session.refresh(user, ["role_links", "group_links"])
    return user


async def assign_default_role(
    session: AsyncSession,
    user: User,
    *,
    is_observer: bool,
) -> None:
    """Grant the role a newly provisioned user starts with.

    Either ``observer`` (members of the configured directory group) or ``user``.
    ``group_admin`` is never auto-derived: it is granted explicitly by an
    administrator, because admin rights must be a deliberate act.
    """
    code = RoleCode.OBSERVER if is_observer else RoleCode.USER
    role = await session.scalar(select(Role).where(Role.code == code.value))
    if role is None:  # pragma: no cover - the migration seeds these
        raise ServiceUnavailableError(f"角色 {code.value} 尚未初始化", code="rbac_not_seeded")

    existing = await session.scalar(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
    )
    if existing is None:
        session.add(UserRole(user_id=user.id, role_id=role.id, scope=RoleScope.GLOBAL))
        await session.flush()


# --- Directory group sync ----------------------------------------------------
async def sync_directory_groups(
    session: AsyncSession,
    user: User,
    identity: ExternalIdentity,
    *,
    source: UserSource,
) -> None:
    """Map directory group names onto local groups.

    Only groups that already exist locally by name are mapped — the directory
    never creates groups in this system. Memberships are only added or removed
    for rows flagged ``is_directory_managed``.
    """
    if source == UserSource.LOCAL or not identity.groups:
        return

    # Match on the group's directory key, which is ``directory_name`` when set
    # and ``name`` otherwise. The two indexed INs keep this from being a full
    # scan, and the key is applied in Python so this agrees exactly with the bulk
    # reconcile about what "the same group" means.
    candidates = (
        await session.scalars(
            select(Group).where(
                or_(Group.directory_name.in_(identity.groups), Group.name.in_(identity.groups))
            )
        )
    ).all()
    local_groups: dict[str, Group] = {}
    contested: set[str] = set()
    for group in candidates:
        key = group.directory_key
        if key not in identity.groups:
            continue
        if key in local_groups:
            contested.add(key)
            continue
        local_groups[key] = group

    # Same rule as the bulk reconcile: two groups claiming one directory name is
    # not something to resolve by picking whichever row came back first, which is
    # what an unguarded dict build would do.
    for key in contested:
        local_groups.pop(key, None)
        logger.warning("directory_group_name_contested", group=key, username=user.username)

    target_group_ids = {group.id for group in local_groups.values()}

    links = (
        await session.scalars(select(UserGroupLink).where(UserGroupLink.user_id == user.id))
    ).all()
    by_group_id = {link.group_id: link for link in links}

    for group in local_groups.values():
        link = by_group_id.get(group.id)
        if link is None:
            session.add(
                UserGroupLink(
                    user_id=user.id,
                    group_id=group.id,
                    role_in_group=GroupRole.MEMBER,
                    is_primary=False,
                    is_directory_managed=True,
                )
            )
        else:
            link.is_directory_managed = True

    for group_id, link in by_group_id.items():
        if link.is_directory_managed and group_id not in target_group_ids:
            if link.role_in_group == GroupRole.GROUP_ADMIN:
                # Mirror the bulk reconcile: losing the directory membership also
                # has to lose the group-admin role, or the account keeps
                # group-admin authority over a group it no longer belongs to.
                await group_admin.set_group_scoped_role(
                    session, user_id=user.id, group_id=group_id, is_admin=False
                )
            await session.delete(link)

    await session.flush()
    await ensure_primary_group(session, user)


async def ensure_primary_group(session: AsyncSession, user: User) -> None:
    """Guarantee that a user has at most one primary group."""
    links = (
        await session.scalars(
            select(UserGroupLink)
            .where(UserGroupLink.user_id == user.id)
            .order_by(UserGroupLink.is_primary.desc(), UserGroupLink.id)
        )
    ).all()
    if not links:
        return

    keep = next((link for link in links if link.is_primary), links[0])
    for link in links:
        expected = link is keep
        if link.is_primary != expected:
            link.is_primary = expected
    await session.flush()


# --- Sessions ----------------------------------------------------------------
async def issue_session(
    session: AsyncSession,
    user: User,
    *,
    provider: str = "local",
    user_agent: str | None = None,
    ip_address: str | None = None,
    settings: Settings | None = None,
) -> IssuedSession:
    """Mint an access token and a fresh refresh token."""
    settings = settings or get_settings()

    access_token, access_expires_at = create_access_token(
        user_id=user.id,
        username=user.username,
        settings=settings,
    )

    raw_refresh, digest = generate_refresh_token()
    refresh_expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days)

    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=digest,
            expires_at=refresh_expires_at,
            user_agent=(user_agent or "")[:255] or None,
            ip_address=(ip_address or "")[:64] or None,
        )
    )
    await session.flush()

    return IssuedSession(
        user=user,
        access_token=access_token,
        access_expires_at=access_expires_at,
        refresh_token=raw_refresh,
        refresh_expires_at=refresh_expires_at,
        provider=provider,
    )


async def rotate_refresh_token(
    session: AsyncSession,
    raw_token: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
    settings: Settings | None = None,
) -> IssuedSession:
    """Exchange a refresh token for a new session, rotating the token.

    Replaying an already-rotated token is treated as theft: every refresh token
    for that user is revoked and the caller must log in again.
    """
    settings = settings or get_settings()

    token = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token))
    )
    if token is None:
        raise UnauthorizedError("刷新令牌无效", code="invalid_refresh_token")

    if token.revoked_at is not None:
        logger.warning("refresh_token_replay", user_id=token.user_id, token_id=token.id)
        await _revoke_family(session, token.user_id)
        raise UnauthorizedError("登录状态已失效，请重新登录", code="refresh_token_reused")

    if _aware(token.expires_at) <= datetime.now(UTC):
        raise UnauthorizedError("登录已过期，请重新登录", code="refresh_token_expired")

    user = await session.get(User, token.user_id)
    if user is None:
        raise UnauthorizedError("用户不存在", code="invalid_refresh_token")
    if not user.is_active:
        raise ForbiddenError("账户已被停用", code="account_disabled")
    if user.is_expired:
        raise ForbiddenError("账户已过期", code="account_expired")

    issued = await issue_session(
        session,
        user,
        user_agent=user_agent,
        ip_address=ip_address,
        settings=settings,
    )

    new_token = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(issued.refresh_token))
    )
    token.revoked_at = datetime.now(UTC)
    if new_token is not None:
        token.replaced_by_id = new_token.id
    await session.flush()

    return issued


async def _revoke_family(session: AsyncSession, user_id: int) -> None:
    """Revoke every refresh token for a user, in its own transaction.

    Called when a token that was already rotated is presented again — the
    standard signal that a refresh token was stolen. The request that detected
    the replay is about to fail with 401, and the session dependency rolls back
    on error, so a revocation staged on *that* session would be undone and the
    attacker's token would keep working. Committing independently is the whole
    point of this function.
    """
    try:
        factory = get_session_factory()
    except RuntimeError:
        # No process-wide engine (unit tests): stage it on the caller's session.
        await revoke_user_tokens(session, user_id)
        return

    async with factory() as independent:
        count = await revoke_user_tokens(independent, user_id)
        await independent.commit()
    logger.warning("refresh_token_family_revoked", user_id=user_id, revoked=count)


async def revoke_refresh_token(session: AsyncSession, raw_token: str) -> None:
    """Revoke a single refresh token (logout). Idempotent."""
    await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == hash_token(raw_token),
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )


async def revoke_user_tokens(session: AsyncSession, user_id: int) -> int:
    """Revoke every active refresh token for a user. Returns the count."""
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await session.flush()
    return int(getattr(result, "rowcount", 0) or 0)
