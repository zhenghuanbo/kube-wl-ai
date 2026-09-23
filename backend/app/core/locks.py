"""A best-effort mutual-exclusion lock for work that must not overlap twice.

There is exactly one user of this: the catalogue sync. The reason it is worth a
module rather than an inline call is the failure policy.

**It fails open.** If Redis is unreachable the lock is treated as acquired and the
work proceeds, because a lock is an optimisation against duplicated effort and
never a correctness requirement — the operations it guards are idempotent. The
alternative, refusing to sync because a cache is down, trades a real outage for an
imagined one. The warning is logged so the degradation is visible.

It matters for two concrete cases rather than for theory: a manual sync racing the
scheduled one, and two `beat` replicas firing the same schedule simultaneously.
Both would otherwise have two passes read the same tags, both find no catalogue row
and both insert — a unique-constraint violation that rolls one of them back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from redis.asyncio import Redis

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Released only by the holder. Comparing the token before deleting keeps a slow
#: pass whose TTL already expired from deleting the lock a *later* pass is now
#: holding — which would let a third pass start on top of the second.
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


@asynccontextmanager
async def exclusive(key: str, *, ttl_seconds: int, url: str) -> AsyncIterator[bool]:
    """Hold ``key`` for the duration, yielding whether it was acquired.

    ``False`` means somebody else holds it and the caller should step aside. A
    caller that receives ``True`` must still treat its work as interruptible: the
    TTL can lapse mid-run, so this serialises effort and does not enforce much
    else.
    """
    token = uuid4().hex
    client = Redis.from_url(
        url,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
        decode_responses=True,
    )
    acquired = False
    try:
        try:
            acquired = bool(await client.set(key, token, nx=True, ex=ttl_seconds))
        except Exception as exc:
            logger.warning("lock_unavailable", key=key, error=str(exc))
            yield True
            return

        if not acquired:
            yield False
            return

        try:
            yield True
        finally:
            try:
                await client.eval(_RELEASE, 1, key, token)
            except Exception as exc:
                logger.warning("lock_release_failed", key=key, error=str(exc))
    finally:
        await client.aclose()
