"""Celery application factory.

Workers and beat share this app. Tasks are registered per phase
(`app.workers.tasks.*`); the periodic schedule is defined in `beat_schedule`.
"""

from __future__ import annotations

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "kube-wl-ai",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
)

# Importing the task modules registers them with this app. Without this the
# worker starts with an empty registry and every scheduled message is rejected
# as unknown — the failure looks like "beat is running but nothing happens".
from app.workers.tasks import images, reconcile, usage  # noqa: E402,F401  (registration imports)

#: Every 30 seconds. Sweeping is a handful of reads per live workload, so the
#: cost is dominated by the number of running things, not by the interval —
#: and a user who stops an instance expects the row to agree within seconds.
celery_app.conf.beat_schedule = {
    "reconcile-workloads": {
        "task": "app.workers.tasks.reconcile.reconcile_workloads",
        "schedule": 30.0,
    },
    # Every 20 minutes. The rollup window is three days and the work is a few
    # seconds, so this is cheap; a daily run would leave today's numbers — the
    # ones somebody is actually watching — hours stale.
    "rollup-usage": {
        "task": "app.workers.tasks.usage.rollup_usage",
        "schedule": 1200.0,
    },
    # Daily by default. Nothing else moves an image from "pushed to Harbor" to
    # "visible in the picker", and the endpoint that does needs `image:share`,
    # which the person asking "why can't I see my image?" does not have. The
    # interval is configurable because a small installation can afford to run it
    # far more often than a large one, where a pass walks every project and tag.
    "sync-images": {
        "task": "app.workers.tasks.images.sync_images",
        "schedule": settings.image_sync_interval_seconds,
    },
}
