"""The scheduled node sync.

Node discovery used to happen only when an administrator pressed a button, which
meant the node inventory — and therefore the monitor's view of the cluster — was
only ever as fresh as the last time somebody remembered. Worse, once pool
membership became a fact the platform owns, a *stale* label stopped being cosmetic:
``wl-pool`` is what ``nodeSelector`` matches, so a node added to the cluster and
never synced is a node no workload can reach, and a node moved between pools is one
whose workloads sit pending until the label catches up.

So this runs often. A pass is one ``list_node`` call plus one patch for each node
that actually moved, and a steady state patches nothing — the sync compares before
writing. That is cheap enough that a two-minute interval costs less than the
support question it prevents.

No actor: this is the installation reconciling its own inventory rather than a
person doing something, so the log attributes it to the scheduler instead of
whichever administrator happens to exist.
"""

from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.k8s import get_gateway
from app.services import resources as resource_service
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


async def _sync() -> dict[str, object]:
    settings = get_settings()
    init_engine(settings)
    gateway = get_gateway(settings)
    factory = get_session_factory()
    try:
        async with factory() as session:
            outcome = await resource_service.sync_nodes(session, None, gateway)
            # Committed here rather than by the request layer, because there is no
            # request. Without this the label patches would be written to the
            # cluster while the rows that justify them are rolled back.
            await session.commit()
    finally:
        await dispose_engine()

    # A cluster that cannot be reached is an expected condition — a development
    # machine with no kubeconfig, or an API server mid-upgrade — and logging it at
    # error level every two minutes would train people to ignore the log.
    logger.info(
        "node_sync_ran",
        seen=outcome.nodes_seen,
        created=outcome.nodes_created,
        updated=outcome.nodes_updated,
        labels_patched=outcome.labels_patched,
        unpooled=outcome.nodes_unpooled,
    )
    return {
        "nodes_seen": outcome.nodes_seen,
        "nodes_created": outcome.nodes_created,
        "nodes_updated": outcome.nodes_updated,
        "labels_patched": outcome.labels_patched,
        "nodes_unpooled": outcome.nodes_unpooled,
    }


# Celery's decorator is untyped and ``--strict`` will not let that silence the
# function's own signature.
@celery_app.task(name="app.workers.tasks.nodes.sync_nodes")  # type: ignore[untyped-decorator]
def sync_nodes() -> dict[str, object]:
    """Re-read the cluster's nodes and re-derive their resource pools."""
    return asyncio.run(_sync())
