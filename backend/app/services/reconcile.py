"""One drift-correction sweep, shared by everything that runs one.

Two things sweep: the Celery beat task (:mod:`app.workers.tasks.reconcile`) and
the in-process reconciler (:mod:`app.workers.sweeper`). They must not each
assemble their own list of what to reconcile — that is how two sweeps drift
apart, and the drift stays invisible until the one nobody is watching stops
covering something.

What this deliberately does **not** own is the engine. Celery has to build and
tear down its own, because a synchronous worker process has none. The in-process
reconciler runs *inside* the web process, where disposing the engine on every tick
would take the API's database away from under it mid-request. So the caller that
needs engine management does it, and this function assumes a live engine.

Each batch commits its own work (see :func:`app.services.instances.reconcile_all`,
and the note there about why that is not optional), so no transaction is held open
across the three.

Everything in a sweep is **idempotent and safe to run concurrently**: two sweeps
racing reach the same final state, because every transition is derived from what the
cluster reports rather than from a counter. That property is what makes it
acceptable for the Celery beat task and the in-process reconciler to overlap — as
they do on a multi-worker deployment, or briefly during a rolling deploy. Wasteful,
not wrong.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.k8s import K8sGateway
from app.services import instances as instance_service
from app.services import tasks as task_service

logger = get_logger(__name__)


async def sweep_all(gateway: K8sGateway) -> dict[str, int]:
    """Reconcile instances, expiries and tasks once.

    ``instances`` and ``tasks`` count statuses that *changed*; ``expired`` counts
    instances stopped for having run past their cap. An all-zero result therefore
    means "a sweep happened and nothing moved" — the only shape a caller can use to
    stay quiet, and the only one under which a printed summary is true.
    """
    factory = get_session_factory()

    async with factory() as session:
        instances = await instance_service.reconcile_all(session, gateway)
    async with factory() as session:
        expired = await instance_service.expire_overdue(session, gateway)
    async with factory() as session:
        tasks = await task_service.reconcile_all(session, gateway)

    return {"instances": instances, "expired": expired, "tasks": tasks}


__all__ = ["sweep_all"]
