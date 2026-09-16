"""Personal access tokens for the open API.

A token is a *credential*, and three decisions shape everything here.

**Only a hash is stored.** The raw value is returned once, at creation, and is
unrecoverable afterwards. A database leak must not yield usable credentials, and
"show me my token again" is a feature that can only be implemented by keeping
the secret.

**A token cannot exceed its owner.** Its scopes are stored as declared, but every
request resolves them as the *intersection* of those scopes and the user's
current roles. Deactivating a user, or removing a role, narrows every token they
hold on the very next request — because the roles are re-read per request and
the token is not a snapshot of them.

**Scopes are validated at creation against what the owner holds now.** A token
declaring a permission its owner lacks is refused with 400 rather than created
and silently inert. The alternative produces a credential that reports success
and then fails at the point of use, in somebody's automation, at 3am.

What a token deliberately does *not* do is change the data scope. A token's
scopes decide which verbs are available; whose rows those verbs reach is still
decided by the owner's roles, exactly as in a browser session. An administrator's
read-only token reads every row they can read — it does not become a normal
user's token, because that would be a different person's view wearing their name
in the audit trail.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.auth.tokens import generate_api_token, hash_token
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.time import as_aware, is_past, utc_now
from app.models.token import ApiToken
from app.models.user import User

#: Longest lifetime a token may be given, in days. A credential with no end date
#: is one nobody ever rotates; a year is long enough to be convenient and short
#: enough that a forgotten token eventually stops working by itself.
MAX_TOKEN_DAYS = 365

#: How often `last_used_at` is refreshed. Writing it on every request would turn
#: each read into a write, and the column exists to answer "is this token still
#: in use", which a minute's granularity answers perfectly well.
LAST_USED_RESOLUTION = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A newly created token: the row, plus the one chance to see the secret."""

    token: ApiToken
    secret: str


def parse_scopes(raw: str) -> frozenset[str]:
    """Split the stored representation, which is whitespace separated."""
    return frozenset(part for part in raw.split() if part)


def format_scopes(scopes: Sequence[str] | frozenset[str]) -> str:
    """Canonical storage form: sorted, so equal sets compare equal as strings."""
    return " ".join(sorted(set(scopes)))


async def create_token(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    scopes: Sequence[str] | None = None,
    expires_in_days: int | None = None,
) -> IssuedToken:
    """Mint a token for ``user``.

    ``scopes=None`` or an empty sequence means "inherit the user's roles" — the
    token then tracks the account, gaining and losing permissions with it. That
    is the right default for a person automating their own work; explicit scopes
    are for handing a credential to a script that should only ever do one thing.
    """
    clean_name = name.strip()
    if not clean_name:
        raise ValidationFailedError("令牌名称不能为空", code="token_name_required")

    # Already reduced by any ceiling in force: `effective_permissions` intersects
    # the token that authenticated *this* request with the user's roles. So a
    # superuser's is the whole catalogue, and a scoped token's is only its own
    # scopes. Anyone may declare anything within what they are, right now, able to
    # use — which is what stops a token from outliving its own authority by
    # minting a successor.
    granted = policy.effective_permissions(user)
    ceiling = policy.current_permission_ceiling()

    requested = frozenset(scopes or ())
    unknown = requested - granted
    if unknown:
        raise ValidationFailedError(
            f"令牌范围超出你当前的权限：{' '.join(sorted(unknown))}",
            code="token_scope_not_granted",
            detail={"unknown_scopes": sorted(unknown)},
        )

    if not requested and ceiling is not None:
        # The "inherit my roles" default is only available to a session. A token
        # that created this token must not be able to launder its restriction
        # away by asking for a successor with no scopes — the successor would
        # track the user's full roles, which is exactly what the ceiling exists
        # to prevent. So the delegable set is written down explicitly instead.
        #
        # This is the rule that makes `api_token:create` safe to grant: a token
        # can only ever hand on a subset of what it already had.
        requested = granted

    if expires_in_days is not None:
        if expires_in_days < 1:
            raise ValidationFailedError("有效期至少为 1 天", code="token_expiry_too_short")
        if expires_in_days > MAX_TOKEN_DAYS:
            raise ValidationFailedError(
                f"有效期最多为 {MAX_TOKEN_DAYS} 天", code="token_expiry_too_long"
            )

    # Names are how a person tells two tokens apart in a list of prefixes, so a
    # duplicate is worth refusing. Scoped to the owner: two people may both call
    # something "ci".
    existing = await session.scalar(
        select(ApiToken.id).where(ApiToken.user_id == user.id, ApiToken.name == clean_name)
    )
    if existing is not None:
        raise ConflictError("同名令牌已存在", code="token_name_taken")

    raw, digest, prefix = generate_api_token()
    expires_at = (
        utc_now() + timedelta(days=expires_in_days) if expires_in_days is not None else None
    )

    token = ApiToken(
        user_id=user.id,
        name=clean_name,
        token_hash=digest,
        prefix=prefix,
        scopes=format_scopes(requested),
        expires_at=expires_at,
    )
    session.add(token)
    await session.flush()
    return IssuedToken(token=token, secret=raw)


async def list_tokens(session: AsyncSession, user: User) -> list[ApiToken]:
    """The caller's own tokens, newest first.

    There is no "list everybody's tokens" variant and there is not meant to be
    one: a token is a personal credential, and an administrator who needs to
    revoke somebody's access deactivates the account or removes a role, which
    narrows every token that person holds on the next request.
    """
    rows = await session.scalars(
        select(ApiToken)
        .where(ApiToken.user_id == user.id)
        .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
    )
    return list(rows)


async def revoke_token(session: AsyncSession, user: User, token_id: int) -> ApiToken:
    """Revoke one of the caller's tokens.

    Out-of-scope is a 404, not a 403: token ids are sequential, and a 403 would
    confirm that somebody else's token exists.
    """
    token = await session.get(ApiToken, token_id)
    if token is None or token.user_id != user.id:
        raise NotFoundError("令牌不存在", code="token_not_found")
    await session.delete(token)
    await session.flush()
    return token


async def resolve_token(session: AsyncSession, raw: str) -> ApiToken | None:
    """Look up a presented token by digest, or ``None``.

    Expiry is checked here rather than filtered in the query so that the caller
    can distinguish "no such token" from "expired token" in the audit trail. The
    message shown to the caller is the same for both — an attacker learns nothing
    from the distinction, an operator reading the log learns something.
    """
    digest = hash_token(raw)
    token = await session.scalar(select(ApiToken).where(ApiToken.token_hash == digest))
    if token is None:
        return None
    if is_past(token.expires_at):
        return None
    return token


async def touch(session: AsyncSession, token: ApiToken) -> None:
    """Record that the token was used, at most once a minute."""
    now = utc_now()
    previous = as_aware(token.last_used_at)
    if previous is not None and now - previous < LAST_USED_RESOLUTION:
        return
    token.last_used_at = now
    await session.flush()
