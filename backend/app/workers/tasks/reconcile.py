"""Drift correction between the database and the cluster.

The API reports intent the moment it is accepted; only a sweep can notice that a
pod has since died, a Job has finished, or an instance has outlived its runtime
cap. Without this the platform would keep saying ``creating`` forever for
something that failed ten minutes ago.

The sweep itself now lives in :mod:`app.services.reconcile`, shared with the
in-process reconciler (:mod:`app.workers.sweeper`) so the two cannot drift into
covering different things.

This file is also where the sweep used to be thrown away. It assembled three
``async with factory() as session:`` blocks with no ``commit()`` anywhere, and a
session *rolls back* an uncommitted transaction when the block exits — so every
sweep read the cluster correctly, wrote the answer, and discarded it. Instance
statuses therefore never left ``creating`` and the runtime cap was never enforced,
while task reconciliation kept working only because its service happened to commit
for itself. The commit now lives with the batch it belongs to; see
:func:`app.services.instances.reconcile_all`.

The async services are called through :func:`asyncio.run` because Celery's
execution model is synchronous. Each task therefore opens and closes its own
engine, which is also what keeps a long-lived worker from holding a stale
connection pool across a database failover. That is why the engine lives here and
not in :func:`~app.services.reconcile.sweep_all`: the in-process reconciler runs
inside the web process, where disposing the engine every few seconds would take
the API's database away from under it.
"""

from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import dispose_engine, init_engine
from app.k8s import get_gateway
from app.services.reconcile import sweep_all
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


async def _reconcile() -> dict[str, int]:
    """One sweep, wrapped in this process's own engine."""
    settings = get_settings()
    init_engine(settings)
    try:
        return await sweep_all(get_gateway(settings))
    finally:
        await dispose_engine()


# Celery's decorator is untyped, and ``--strict`` refuses to let that silence
# the function's own signature.
@celery_app.task(name="app.workers.tasks.reconcile.reconcile_workloads")  # type: ignore[untyped-decorator]
def reconcile_workloads() -> dict[str, int]:
    """Advance every instance and task that has not reached a resting state."""
    result = asyncio.run(_reconcile())
    if any(result.values()):
        logger.info("reconcile_swept", source="celery", **result)
    return result
