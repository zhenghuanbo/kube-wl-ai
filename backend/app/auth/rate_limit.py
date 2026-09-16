"""Login rate limiting.

The counter is the **audit trail itself**. That is the whole design, and it is
worth defending, because the obvious implementation is a Redis key with a TTL.

Counting failed ``auth:login`` rows instead buys three things:

* There is no second source of truth. A Redis counter and an audit row can
  disagree, and when they do, the person asking "why am I locked out" is reading
  the one that is not the answer.
* The state survives a restart. An in-memory limiter resets on deploy, which is
  precisely when an attacker who has been watching for a restart would act.
* The decision is auditable by construction. Every denial that fed the count is a
  row with an address, a time and a request id.

The cost is two queries per login attempt. Login is rare; that is a trade worth
making.

**The window resets on success.** Failures are counted only since the last
successful login for that account. Without this, a person who mistypes their
password on Monday, Tuesday and Wednesday would eventually be locked out for no
reason — a limiter that punishes the innocent is worse than none.

**The known limitation, stated plainly:** a per-account limit lets an attacker
lock a known username out by failing deliberately. The mitigation is the shape of
the limits rather than a claim that the problem does not exist — the per-account
window is short (15 minutes by default) and the per-address limit is loose enough
for a shared NAT, so a lockout costs the victim a quarter of an hour and the
attacker a sustained campaign from one address that is itself being counted.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import TooManyRequestsError
from app.core.logging import get_logger
from app.core.time import as_aware, utc_now
from app.models.audit import AuditLog

logger = get_logger(__name__)

#: The action code the login endpoint and the audit middleware both write.
LOGIN_ACTION = "auth:login"


async def _failures_since(
    session: AsyncSession,
    *,
    window_start: datetime,
    ip_address: str | None,
    username: str | None,
) -> tuple[int, int]:
    """Count recent failures by address and by account.

    Two counts from one round trip, because the decision needs both and they
    differ only in their predicate.
    """
    base = (
        select(func.count())
        .select_from(AuditLog)
        .where(
            AuditLog.action == LOGIN_ACTION,
            AuditLog.status == "denied",
            AuditLog.created_at >= window_start,
        )
    )

    by_ip = 0
    if ip_address:
        by_ip = int(await session.scalar(base.where(AuditLog.ip_address == ip_address)) or 0)

    by_username = 0
    if username:
        by_username = int(
            await session.scalar(
                base.where(
                    or_(
                        AuditLog.actor_username == username,
                        # Older rows recorded before the login handler started
                        # denormalising the attempted name.
                        AuditLog.detail["username"].as_string() == username,
                    )
                )
            )
            or 0
        )
    return by_ip, by_username


async def _last_success(
    session: AsyncSession, *, ip_address: str | None, username: str | None
) -> datetime | None:
    """When this address or account last logged in successfully.

    The later of the two, so either a successful login from the address or into
    the account clears the slate. A person who finally remembers their password
    should not stay half-locked-out on either axis.
    """
    predicates = []
    if ip_address:
        predicates.append(AuditLog.ip_address == ip_address)
    if username:
        predicates.append(
            or_(
                AuditLog.actor_username == username,
                AuditLog.detail["username"].as_string() == username,
            )
        )
    if not predicates:
        return None

    latest: datetime | None = await session.scalar(
        select(func.max(AuditLog.created_at)).where(
            AuditLog.action == LOGIN_ACTION,
            AuditLog.status == "success",
            or_(*predicates),
        )
    )
    return as_aware(latest)


async def enforce_login_rate_limit(
    session: AsyncSession,
    *,
    username: str,
    ip_address: str | None,
    settings: Settings,
) -> None:
    """Raise ``TooManyRequestsError`` if this attempt should be refused.

    Called **before** the credentials are verified, so a refused attempt costs no
    directory round trip. That ordering matters beyond performance: it is what
    stops a lockout from being a way to make the LDAP server do work.

    The response deliberately does not say which limit was hit, nor how many
    attempts remain. Telling an attacker how close they are to the threshold is
    telling them how to pace themselves under it.
    """
    if not settings.login_rate_limit_enabled:
        return

    window = timedelta(seconds=settings.login_rate_limit_window_seconds)
    since = await _last_success(session, ip_address=ip_address, username=username)
    floor = max(since, utc_now() - window) if since is not None else utc_now() - window

    by_ip, by_username = await _failures_since(
        session, window_start=floor, ip_address=ip_address, username=username
    )

    if by_ip >= settings.login_rate_limit_attempts_per_ip:
        raise TooManyRequestsError(
            "登录尝试过于频繁，请稍后再试",
            code="login_rate_limited",
        )
    if by_username >= settings.login_rate_limit_attempts_per_username:
        raise TooManyRequestsError(
            "登录尝试过于频繁，请稍后再试",
            code="login_rate_limited",
        )


async def record_login_failure(
    *,
    request: Request | None,
    username: str,
    error_message: str | None = None,
) -> None:
    """Write the denial row that feeds the counter above.

    **On its own session, committed immediately**, and that is the whole reason
    this function exists rather than a call to `record_audit` in the handler. A
    failed login ends by raising, and the request's session rolls back when it
    does — so a row staged there is discarded along with the failure it was meant
    to record. The audit middleware has the same problem and solves it the same
    way; the difference is that the middleware sees only the response, and
    therefore cannot know *which account* was attempted. That name is what the
    per-account limit counts.

    `actor_user_id` stays NULL. Nobody authenticated, and the username is the
    claimant's, not a proven identity — which is also why these rows are visible
    only to a global administrator (see `policy.SCOPE_RULES[AuditLog]`).

    Never raises: a failure to record must not replace a 401 with a 500, and must
    not become a way to *avoid* being counted. It is logged loudly instead.
    """
    from app.db.session import get_session_factory

    try:
        factory = get_session_factory()
        async with factory() as session:
            session.add(
                AuditLog(
                    actor_user_id=None,
                    actor_username=username[:150],
                    action=LOGIN_ACTION,
                    object_type="user",
                    status="denied",
                    detail={"username": username, "provider": "failed"},
                    ip_address=(_client_ip(request) or "")[:64] or None,
                    request_id=_request_id()[:64] or None,
                    error_message=error_message,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("login_failure_audit_failed", username=username)

    # Suppress the middleware's own row for this request: one attempt is one row,
    # and the one written here is the more informative of the two.
    if request is not None:
        request.state.audit_recorded = True


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    from app.core.context import get_client_ip

    return get_client_ip()


def _request_id() -> str:
    from app.core.context import request_id_var

    return request_id_var.get() or ""
