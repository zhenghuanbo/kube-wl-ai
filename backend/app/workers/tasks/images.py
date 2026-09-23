"""The scheduled catalogue refresh.

The catalogue is a cache of Harbor and nothing watches Harbor, so without this the
only thing that ever moves an image from "pushed" to "visible" is an administrator
remembering to press the sync button. That is a poor answer to "I pushed my image,
why can't I see it?" — and the person who asks is never the person who can run the
sync, because the endpoint needs ``image:share``.

Running daily is a deliberate trade. A sync is a full walk of every project and
every tag, so running it hourly would spend most of the day re-reading a listing
that has not changed. The cost of the delay is that a freshly pushed image is
invisible for up to a day; the alternative, users running the sync themselves,
would be a permission a normal account should not have.

The pass is safe to run alongside itself and to run twice: it derives rows from
what the registry reports rather than from a running total, so a missed run costs
nothing and a duplicated one costs nothing. The lock exists only to stop two passes
from inserting the same row at the same moment.
"""

from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.core.locks import exclusive
from app.core.logging import get_logger
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.registries import get_registry
from app.services import images as image_service
from app.workers.celery_app import celery_app

logger = get_logger(__name__)


async def _sync(projects: list[str] | None) -> dict[str, object]:
    settings = get_settings()

    # No actor: this is the installation refreshing its own cache, not somebody
    # registering an image. `created_by` is left null rather than attributed to
    # whichever administrator happened to be configured.
    async with exclusive(
        image_service.SYNC_LOCK_KEY,
        ttl_seconds=image_service.SYNC_LOCK_TTL_SECONDS,
        url=settings.redis_url,
    ) as acquired:
        if not acquired:
            logger.info("image_sync_skipped", reason="another pass holds the lock")
            return {"skipped": True, "reason": "another sync is running"}

        init_engine(settings)
        factory = get_session_factory()
        try:
            async with factory() as session:
                result = await image_service.sync_images(
                    session,
                    None,
                    get_registry(settings),
                    settings,
                    projects=projects,
                )
                await session.commit()
        finally:
            await dispose_engine()

    payload: dict[str, object] = {
        "skipped": False,
        "created": result.created,
        "updated": result.updated,
        "unchanged": result.unchanged,
        "removed": result.removed,
        "failed": result.failed,
        "errors": result.errors,
    }
    # An unreachable registry is an expected condition — Harbor being down, or not
    # configured at all in a development environment — and logging it at error
    # level every day would train people to ignore the log.
    logger.info(
        "image_sync_ran",
        created=result.created,
        updated=result.updated,
        removed=result.removed,
        failed=result.failed,
        error_count=len(result.errors),
    )
    return payload


# Celery's decorator is untyped and ``--strict`` will not let that silence the
# function's own signature.
@celery_app.task(name="app.workers.tasks.images.sync_images")  # type: ignore[untyped-decorator]
def sync_images(projects: list[str] | None = None) -> dict[str, object]:
    """Refresh the image catalogue from the registry."""
    return asyncio.run(_sync(projects))
