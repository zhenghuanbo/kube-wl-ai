"""The scheduled usage rollup.

Consumption is recorded as it happens — a live interval is known the moment a
workload starts and stops — so the daily table is a *summary* that can always be
rebuilt, never a ledger that has to be kept in step. That is why this task is
allowed to simply recompute rather than to increment: a missed run costs
nothing, a double run costs nothing, and a bug fixed today can be applied to
last quarter by recomputing it.

The schedule is deliberately frequent and the window deliberately short. A
running instance's contribution to *today* grows every minute, so a daily job
would leave the current day hours out of date and the usage page would look
broken to the one person who is watching their own instance. Recomputing three
days every twenty minutes keeps the visible numbers live at a cost of a few
seconds of work.

Everything is idempotent and safe to run concurrently: two workers racing write
the same rows, because both derive them from the same source records rather than
from a running total.
"""

from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.services import usage as usage_service
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


async def _rollup(lookback_days: int) -> dict[str, int]:
    settings = get_settings()
    init_engine(settings)
    factory = get_session_factory()
    try:
        async with factory() as session:
            result = await usage_service.rollup_recent(session, lookback_days=lookback_days)
            await session.commit()
    finally:
        await dispose_engine()

    return {
        "rows_written": result.rows_written,
        "instances": result.instances_scanned,
        "tasks": result.tasks_scanned,
        "gpu_seconds": result.gpu_seconds,
        "outside_window": result.outside_window,
    }


# Celery's decorator is untyped and ``--strict`` will not let that silence the
# function's own signature.
@celery_app.task(name="app.workers.tasks.usage.rollup_usage")  # type: ignore[untyped-decorator]
def rollup_usage(lookback_days: int = usage_service.DEFAULT_ROLLUP_LOOKBACK_DAYS) -> dict[str, int]:
    """Recompute the last few days of the usage table."""
    result = asyncio.run(_rollup(lookback_days))
    logger.info("usage_rolled_up", lookback_days=lookback_days, **result)
    return result
