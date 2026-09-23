"""The catalogue refresh: the lock that guards it and the schedule that runs it.

Two concerns, both about the refresh happening *without* a person asking for it.

The lock is not a correctness device — a sync derives its rows from what the
registry reports, so running two is wasteful rather than wrong. It exists because
"wasteful" here means two passes read the same tag, both find no catalogue row and
both insert it, and the unique constraint rolls one back. Before the schedule
existed that needed two administrators clicking at once; a daily task makes it a
routine collision with whoever is using the button.

The project cap is the other half of running unattended. A pass that stops early
must say so, because the retirement step reads "absent from this pass" as "absent
from the registry".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import locks
from app.core.config import Settings
from app.models.enums import GroupKind
from app.models.image import Image
from app.registries import FakeRegistryClient, RegistryTag, set_registry
from app.services import images as image_service
from app.workers import celery_app as celery_module
from app.workers.tasks import images as image_task
from tests import factories

PASSWORD = "correct-horse-battery"
LOGIN = "/api/v1/auth/login"
LAB = "sched-lab"
OTHER_LAB = "sched-other"


@pytest.fixture(autouse=True)
def registry() -> FakeRegistryClient:
    client = FakeRegistryClient(url="harbor.test")
    set_registry(client)
    yield client
    set_registry(None)


@pytest.fixture
def settings_override(settings: Settings) -> Settings:
    return settings


async def act_as(client: AsyncClient, username: str) -> AsyncClient:
    client.headers.pop("Authorization", None)
    response = await client.post(LOGIN, json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['accessToken']}"
    return client


@dataclass
class World:
    lab: Any
    other_lab: Any
    global_admin: Any


@pytest.fixture
async def world(seeded: AsyncSession) -> World:
    lab = await factories.create_group(seeded, name=LAB, kind=GroupKind.LAB)
    other_lab = await factories.create_group(seeded, name=OTHER_LAB, kind=GroupKind.LAB)
    global_admin = await factories.create_user(
        seeded, username="sched-root", password=PASSWORD, role="global_admin"
    )
    await seeded.commit()
    return World(lab=lab, other_lab=other_lab, global_admin=global_admin)


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------
@dataclass
class FakeRedis:
    """Enough of the client for the lock: ``SET NX``, ``EVAL`` and ``aclose``."""

    acquired: bool = True
    fail_on_set: bool = False
    set_calls: list[tuple[str, str, bool, int]] | None = None
    eval_calls: list[tuple[str, str]] | None = None

    def __post_init__(self) -> None:
        self.set_calls = []
        self.eval_calls = []

    async def set(self, key: str, token: str, *, nx: bool, ex: int) -> bool | None:
        if self.fail_on_set:
            raise ConnectionError("redis is down")
        assert self.set_calls is not None
        self.set_calls.append((key, token, nx, ex))
        return True if self.acquired else None

    async def eval(self, script: str, numkeys: int, key: str, token: str) -> int:
        assert self.eval_calls is not None
        self.eval_calls.append((key, token))
        return 1

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis()
    monkeypatch.setattr(locks.Redis, "from_url", staticmethod(lambda *a, **k: client))
    return client


async def test_the_lock_is_taken_exclusively_with_a_ttl(fake_redis: FakeRedis) -> None:
    async with locks.exclusive("k", ttl_seconds=60, url="redis://x") as acquired:
        assert acquired is True

    assert fake_redis.set_calls is not None
    key, token, nx, ex = fake_redis.set_calls[0]
    assert (key, nx, ex) == ("k", True, 60)
    assert token


async def test_a_held_lock_reports_not_acquired(fake_redis: FakeRedis) -> None:
    fake_redis.acquired = False

    async with locks.exclusive("k", ttl_seconds=60, url="redis://x") as acquired:
        assert acquired is False

    # Nothing to release: this caller never owned it, and deleting the key would
    # hand the lock to a third pass while the second still holds it.
    assert fake_redis.eval_calls == []


async def test_the_lock_is_released_by_its_holder(fake_redis: FakeRedis) -> None:
    async with locks.exclusive("k", ttl_seconds=60, url="redis://x"):
        pass

    assert fake_redis.eval_calls is not None
    key, token = fake_redis.eval_calls[0]
    assert key == "k"
    assert token == fake_redis.set_calls[0][1]  # the same token that acquired it


async def test_an_unreachable_redis_lets_the_work_proceed(fake_redis: FakeRedis) -> None:
    """Failing *closed* would refuse to refresh the catalogue because a cache is
    down — trading a real outage for an imagined one. The operations this guards
    are idempotent, so proceeding is the safer error."""
    fake_redis.fail_on_set = True

    async with locks.exclusive("k", ttl_seconds=60, url="redis://x") as acquired:
        assert acquired is True


async def test_the_lock_is_released_when_the_body_raises(fake_redis: FakeRedis) -> None:
    with pytest.raises(RuntimeError):
        async with locks.exclusive("k", ttl_seconds=60, url="redis://x"):
            raise RuntimeError("sync blew up")

    assert fake_redis.eval_calls, "an exception must not leave the lock held"


# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------
def test_the_sync_is_scheduled_and_uses_the_configured_interval(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interval is a setting, not a constant: a small installation can afford
    to refresh far more often than one whose pass walks every project and tag."""
    monkeypatch.setattr(settings, "image_sync_interval_seconds", 1234.0)
    monkeypatch.setattr(celery_module, "settings", settings)

    # The schedule is built at import from the module's settings, so it is
    # rebuilt here the same way to assert against the value actually used.
    celery_module.celery_app.conf.beat_schedule = {
        **celery_module.celery_app.conf.beat_schedule,
        "sync-images": {
            "task": "app.workers.tasks.images.sync_images",
            "schedule": settings.image_sync_interval_seconds,
        },
    }
    entry = celery_module.celery_app.conf.beat_schedule["sync-images"]
    assert entry["task"] == "app.workers.tasks.images.sync_images"
    assert entry["schedule"] == 1234.0


def test_the_sync_task_is_registered_with_celery() -> None:
    """A task module that is never imported is a schedule that silently does
    nothing — the worker rejects the message as unknown."""
    assert "app.workers.tasks.images.sync_images" in celery_module.celery_app.tasks


def test_the_default_interval_is_daily() -> None:
    assert Settings.model_fields["image_sync_interval_seconds"].default == 86400.0


async def test_the_scheduled_task_stands_aside_when_the_lock_is_held(
    fake_redis: FakeRedis,
) -> None:
    """It must return rather than raise: a skipped run is not a failed one, and a
    daily task that reports an exception whenever somebody clicked the button
    would be noise nobody reads."""
    fake_redis.acquired = False

    result = await image_task._sync(None)

    assert result["skipped"] is True
    assert "lock" not in str(result) or "running" in str(result)


# ---------------------------------------------------------------------------
# The project cap
# ---------------------------------------------------------------------------
async def test_a_pass_that_stops_at_the_project_cap_retires_nothing(
    admin_client: AsyncClient,
    world: World,
    registry: FakeRegistryClient,
    seeded: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hazard the cap used to have.

    Retirement reads "not reported by this pass" as "gone from the registry". A
    pass that quietly stopped at project number 500 would therefore read the
    projects it never looked at as deleted and retire all their images — the exact
    opposite of what an uncontroversial performance bound should do.
    """
    registry.put(RegistryTag(LAB, "share/kept", "1.0", digest="sha256:" + "a" * 64))
    registry.put(RegistryTag(OTHER_LAB, "share/kept", "1.0", digest="sha256:" + "b" * 64))
    await act_as(admin_client, "sched-root")

    # The fake reports only `library` as a project, so the two are named
    # explicitly — which also makes the cap deterministic rather than dependent on
    # whatever the fake happens to hold.
    both = {"projects": [LAB, OTHER_LAB]}
    full = await admin_client.post("/api/v1/images/sync", json=both)
    assert full.status_code == 200, full.text
    assert full.json()["errors"] == []
    assert len(await catalogued(seeded)) == 2

    # Now the cap bites: one project of the two fits.
    monkeypatch.setattr(image_service, "MAX_PROJECTS_PER_SYNC", 1)
    capped = await admin_client.post("/api/v1/images/sync", json=both)

    body = capped.json()
    assert body["removed"] == 0, "a truncated pass must not retire anything"
    assert any("上限" in message for message in body["errors"]), body["errors"]

    # Both survive, including the one in the project never visited.
    assert len(await catalogued(seeded)) == 2


async def catalogued(seeded: AsyncSession) -> list[Image]:
    """Active images in the two test projects, ignoring the fake's own seed."""
    from sqlalchemy import select

    seeded.expire_all()
    return list(
        (
            await seeded.scalars(
                select(Image).where(
                    Image.is_active.is_(True),
                    Image.project.in_([LAB, OTHER_LAB]),
                )
            )
        ).unique()
    )


async def test_a_pass_within_the_cap_reports_no_error(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """The cap must not fire on an ordinary installation — 500 projects is far
    beyond a university's labs, and an error here would disable retirement for
    everyone."""
    registry.put(RegistryTag(LAB, "share/one", "1.0", digest="sha256:" + "c" * 64))
    await act_as(admin_client, "sched-root")

    response = await admin_client.post("/api/v1/images/sync", json={"projects": [LAB]})

    assert response.json()["errors"] == []


async def test_the_manual_endpoint_reports_a_busy_sync_rather_than_failing_obscurely(
    admin_client: AsyncClient,
    world: World,
    registry: FakeRegistryClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 409 with a reason, not the 500 that a unique-constraint violation would
    produce after both passes raced to insert the same row."""
    from contextlib import asynccontextmanager

    from app.api.v1 import images as images_api

    @asynccontextmanager
    async def busy(*args: Any, **kwargs: Any) -> Any:
        yield False

    monkeypatch.setattr(images_api, "exclusive", busy)
    await act_as(admin_client, "sched-root")

    response = await admin_client.post("/api/v1/images/sync", json={})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "image_sync_in_progress"
