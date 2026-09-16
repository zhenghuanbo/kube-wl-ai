"""Drift correction between the database and the cluster.

The API reports intent the moment it is accepted; only a sweep can notice that a
pod has since died, a Job has finished, or an instance has outlived its runtime
cap. Without this the platform would keep saying ``creating`` forever for
something that failed ten minutes ago.

Everything here is **idempotent and safe to run concurrently**. Two workers
racing produce the same final state, because each transition is derived from
what the cluster reports rather than from a counter. That matters more than
efficiency: beat runs this every 30 seconds and a rolling deploy briefly runs
two of them.

The async services are called through :func:`asyncio.run` because Celery's
execution model is synchronous. Each task therefore opens and closes its own
engine, which is also what keeps a long-lived worker from holding a stale
connection pool across a database failover.
"""

from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.k8s import get_gateway
from app.services import instances as instance_service
from app.services import tasks as task_service
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


async def _reconcile() -> dict[str, int]:
    """One sweep. Returns counters, which is what makes the log useful."""
    settings = get_settings()
    init_engine(settings)
    gateway = get_gateway(settings)
    factory = get_session_factory()

    try:
        async with factory() as session:
            instances = await instance_service.reconcile_all(session, gateway)
        async with factory() as session:
            expired = await instance_service.expire_overdue(session, gateway)
        async with factory() as session:
            tasks = await task_service.reconcile_all(session, gateway)
    finally:
        await dispose_engine()

    return {"instances": instances, "expired": expired, "tasks": tasks}


# Celery's decorator is untyped, and ``--strict`` refuses to let that silence
# the function's own signature.
@celery_app.task(name="app.workers.tasks.reconcile.reconcile_workloads")  # type: ignore[untyped-decorator]
def reconcile_workloads() -> dict[str, int]:
    """Advance every instance and task that has not reached a resting state."""
    result = asyncio.run(_reconcile())
    if any(result.values()):
        logger.info("reconcile_swept", **result)
    return result
