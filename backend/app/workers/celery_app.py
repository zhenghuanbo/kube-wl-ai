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

# Jobs are added here as their phases land, e.g.:
#   celery_app.conf.beat_schedule = {
#       "reconcile-workloads": {
#           "task": "app.workers.tasks.reconcile.reconcile_workloads",
#           "schedule": 30.0,
#       },
#   }
celery_app.conf.beat_schedule = {}
