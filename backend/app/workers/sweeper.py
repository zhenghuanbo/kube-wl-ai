"""In-process drift correction, for deployments that do not run Celery.

The sweep is not new; what is new is that something runs it. Until this existed,
the only automatic caller was the Celery beat task, so on an installation without
a reachable broker there was **no** periodic reconcile at all — and the symptom was
quiet rather than loud. An instance whose pod had been ready for a quarter of an
hour still read ``creating``, its ``pod_name`` was still NULL (which is how you can
tell nobody ever looked at it), and the runtime cap was never enforced even once.
Every HTTP request touching that row kept working perfectly, so the platform
looked healthy from every angle except the one that mattered.

It complements the Celery task rather than replacing it. Running both is wasteful
and harmless: every transition is derived from what the cluster reports, never
from a counter, so two sweeps racing reach the same final state. On a deployment
with several workers, prefer the beat task and set
``RECONCILE_INTERVAL_SECONDS=0`` so there is one sweeper rather than one per
process.

Two properties this loop has to have:

* **The sweep stays off the event loop.** Every cluster call goes through
  ``run_gateway``, i.e. ``asyncio.to_thread``, so a slow API server delays the
  sweep and not the HTTP request running beside it.
* **A failed tick is not a dead reconciler.** A cluster hiccup, a database
  reconnect, a translation bug on one instance — none of them may end the loop,
  because the loop is the only thing that would have noticed the next one.
"""

from __future__ import annotations

import asyncio

from app.core.config import Settings
from app.core.logging import get_logger
from app.k8s import K8sGateway, get_gateway
from app.services.reconcile import sweep_all

logger = get_logger(__name__)


async def sweep_once(gateway: K8sGateway) -> dict[str, int]:
    """One tick, with the failure contained. Returns ``{}`` if the sweep failed.

    Swallowing the exception here — rather than in the loop — is what lets a test
    assert the containment without also asserting the scheduling.
    """
    try:
        result = await sweep_all(gateway)
    except asyncio.CancelledError:
        # Shutdown, not a failure. Must not be logged as one or swallowed.
        raise
    except Exception as exc:
        logger.warning("reconcile_sweep_failed", source="in_process", error=str(exc))
        return {}

    # The counters count instances and tasks whose status *changed*, so this only
    # fires on real movement. Logging on "examined" instead would print the same
    # line every five seconds for as long as anything is running, which is how a
    # useful log line becomes noise nobody reads.
    if any(result.values()):
        logger.info("reconcile_swept", source="in_process", **result)
    return result


async def run_reconciler(interval_seconds: float, gateway: K8sGateway) -> None:
    """Sweep, wait, repeat. Cancelled by the lifespan at shutdown.

    Sequential by construction, so a sweep that runs longer than the interval
    delays the next one instead of overlapping with it.
    """
    while True:
        await sweep_once(gateway)
        await asyncio.sleep(interval_seconds)


def start_reconciler(settings: Settings) -> asyncio.Task[None] | None:
    """Start the loop if this deployment wants one; otherwise return ``None``.

    Returns ``None`` rather than raising for both opt-outs, because "this
    installation does not run a reconciler" is a legitimate configuration and the
    lifespan should not have to branch on why.
    """
    if not (settings.k8s_in_cluster or settings.k8s_enabled):
        # No cluster to observe. Sweeping against the fake would not merely be
        # pointless: it would write invented statuses into real rows.
        return None
    if settings.reconcile_interval_seconds <= 0:
        return None

    gateway = get_gateway(settings)
    task = asyncio.create_task(run_reconciler(settings.reconcile_interval_seconds, gateway))
    logger.info(
        "reconciler_started",
        interval_seconds=settings.reconcile_interval_seconds,
        source="in_process",
    )
    return task


__all__ = ["run_reconciler", "start_reconciler", "sweep_once"]
