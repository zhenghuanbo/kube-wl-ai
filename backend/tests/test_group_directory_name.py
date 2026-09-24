"""Matching a platform group to its directory counterpart.

A platform group's ``name`` is a lowercase ASCII slug because it is composed into
a Kubernetes namespace; a directory ``cn`` is arbitrary UTF-8. Before
``directory_name`` existed, the two were forced to be equal, so a lab called
``Lab_Vision`` in FreeIPA could not be associated with anything: the API refused
to create a group by that name, and matching on ``name`` would never have found
``lab-vision``. The group's members simply never arrived.

These tests pin the matching rule (``directory_name or name``), the uniqueness
guarantee that keeps the rule unambiguous, and what happens when a reconcile
meets a directory group nothing answers to.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.providers import ExternalIdentity
from app.core.config import Settings
from app.core.errors import ConflictError
from app.models.enums import UserSource
from app.models.group import Group, UserGroupLink
from app.models.user import User
from app.schemas.group import GroupCreate, GroupUpdate
from app.services import groups
from tests import factories


class _FakeDirectory:
    """Stands in for the LDAP provider; only the read surface is used."""

    name = "ldap"

    def __init__(self, groups: list[str], members: dict[str, list[str]] | None = None) -> None:
        self._groups = groups
        self._members = members or {}
        self.bulk_calls = 0

    async def directory_groups(self) -> list[str]:
        return self._groups

    async def directory_group_members(self, group_name: str) -> list[str]:
        return self._members.get(group_name, [])

    async def directory_group_members_many(self, group_names: list[str]) -> dict[str, list[str]]:
        self.bulk_calls += 1
        return {name: self._members.get(name, []) for name in group_names}


@pytest.fixture
def directory(monkeypatch: pytest.MonkeyPatch) -> _FakeDirectory:
    fake = _FakeDirectory([])
    # Takes the settings the service threads through and ignores them: this stub
    # stands in for "a directory is configured", which is the precondition the
    # fixture exists to establish.
    monkeypatch.setattr(groups, "_directory_provider", lambda settings=None: fake)
    return fake


def _use(directory: _FakeDirectory, names: list[str], members: dict[str, list[str]]) -> None:
    directory._groups = names
    directory._members = members


# --- the schema treats a blank string as "no directory name" -------------------
def test_blank_directory_name_normalises_to_none() -> None:
    assert GroupCreate(name="lab-a", directory_name="   ").directory_name is None
    assert GroupCreate(name="lab-a", directory_name="").directory_name is None


def test_directory_name_is_trimmed() -> None:
    assert GroupCreate(name="lab-a", directory_name="  Lab_Vision ").directory_name == "Lab_Vision"


# --- the matching key ---------------------------------------------------------
def test_directory_key_falls_back_to_name() -> None:
    """Every pre-existing row means "match on name" — hence no backfill."""
    assert Group(name="lab-a", display_name="lab-a").directory_key == "lab-a"
    assert (
        Group(name="lab-a", directory_name="Lab_Vision", display_name="lab-a").directory_key
        == "Lab_Vision"
    )


# --- uniqueness ---------------------------------------------------------------
async def test_create_accepts_a_directory_name_that_differs_from_the_slug(
    seeded: AsyncSession, admin: User
) -> None:
    """The whole point: `Lab_Vision` in the directory, `lab-vision` here."""
    group = await groups.create_group(
        seeded, admin, GroupCreate(name="lab-vision", directory_name="Lab_Vision")
    )

    assert group.name == "lab-vision"
    assert group.directory_name == "Lab_Vision"
    assert group.directory_key == "Lab_Vision"


async def test_create_rejects_a_directory_name_taken_by_another_groups_name(
    seeded: AsyncSession, admin: User
) -> None:
    """The cross-column case the database alone cannot catch."""
    await factories.create_group(seeded, name="lab-a")

    with pytest.raises(ConflictError) as exc:
        await groups.create_group(seeded, admin, GroupCreate(name="lab-b", directory_name="lab-a"))

    assert exc.value.code == "directory_name_taken"


async def test_create_rejects_a_directory_name_taken_by_another_directory_name(
    seeded: AsyncSession, admin: User
) -> None:
    await groups.create_group(seeded, admin, GroupCreate(name="lab-a", directory_name="Lab_Vision"))

    with pytest.raises(ConflictError) as exc:
        await groups.create_group(
            seeded, admin, GroupCreate(name="lab-b", directory_name="Lab_Vision")
        )

    assert exc.value.code == "directory_name_taken"


async def test_create_still_rejects_a_duplicate_name(seeded: AsyncSession, admin: User) -> None:
    await factories.create_group(seeded, name="lab-a")

    with pytest.raises(ConflictError) as exc:
        await groups.create_group(seeded, admin, GroupCreate(name="lab-a"))

    assert exc.value.code == "group_name_taken"


async def test_create_without_a_directory_name_claims_the_name(
    seeded: AsyncSession, admin: User
) -> None:
    """A group with no explicit directory name answers to its `name`."""
    await groups.create_group(seeded, admin, GroupCreate(name="lab-a"))

    with pytest.raises(ConflictError):
        await groups.create_group(seeded, admin, GroupCreate(name="lab-b", directory_name="lab-a"))


async def test_update_sets_and_clears_the_directory_name(seeded: AsyncSession, admin: User) -> None:
    group = await factories.create_group(seeded, name="lab-vision")
    assert group.directory_key == "lab-vision"

    await groups.update_group(seeded, admin, group, GroupUpdate(directory_name="Lab_Vision"))
    assert group.directory_key == "Lab_Vision"

    # Explicit null clears it and reverts to matching on `name`.
    await groups.update_group(seeded, admin, group, GroupUpdate(directory_name=None))
    assert group.directory_name is None
    assert group.directory_key == "lab-vision"


async def test_update_leaves_the_directory_name_alone_when_omitted(
    seeded: AsyncSession, admin: User
) -> None:
    group = await groups.create_group(
        seeded, admin, GroupCreate(name="lab-vision", directory_name="Lab_Vision")
    )

    await groups.update_group(seeded, admin, group, GroupUpdate(display_name="视觉实验室"))

    assert group.directory_name == "Lab_Vision"


async def test_update_rejects_a_colliding_directory_name(seeded: AsyncSession, admin: User) -> None:
    await factories.create_group(seeded, name="lab-a")
    other = await factories.create_group(seeded, name="lab-b")

    with pytest.raises(ConflictError) as exc:
        await groups.update_group(seeded, admin, other, GroupUpdate(directory_name="lab-a"))

    assert exc.value.code == "directory_name_taken"


async def test_update_allows_keeping_its_own_directory_name(
    seeded: AsyncSession, admin: User
) -> None:
    """Re-saving a form must not collide with the row being edited."""
    group = await groups.create_group(
        seeded, admin, GroupCreate(name="lab-vision", directory_name="Lab_Vision")
    )

    await groups.update_group(seeded, admin, group, GroupUpdate(directory_name="Lab_Vision"))

    assert group.directory_name == "Lab_Vision"


# --- the reconcile ------------------------------------------------------------
async def test_sync_matches_on_the_directory_name(
    seeded: AsyncSession, admin: User, directory: _FakeDirectory
) -> None:
    """The user-visible outcome: `Lab_Vision` members land in `lab-vision`."""
    member = await factories.create_user(seeded, username="zhangsan")
    group = await groups.create_group(
        seeded, admin, GroupCreate(name="lab-vision", directory_name="Lab_Vision")
    )
    _use(directory, ["Lab_Vision"], {"Lab_Vision": ["zhangsan"]})

    result = await groups.sync_directory(seeded, admin)
    await seeded.flush()

    assert result.matched_groups == ["Lab_Vision"]
    assert result.unmatched_groups == []
    assert result.memberships_added == 1
    link = await seeded.scalar(
        select(UserGroupLink).where(
            UserGroupLink.user_id == member.id, UserGroupLink.group_id == group.id
        )
    )
    assert link is not None
    assert link.is_directory_managed is True


async def test_sync_still_matches_on_the_name_when_no_directory_name_is_set(
    seeded: AsyncSession, admin: User, directory: _FakeDirectory
) -> None:
    member = await factories.create_user(seeded, username="lisi")
    group = await factories.create_group(seeded, name="lab-a")
    _use(directory, ["lab-a"], {"lab-a": ["lisi"]})

    result = await groups.sync_directory(seeded, admin)
    await seeded.flush()

    assert result.matched_groups == ["lab-a"]
    assert result.memberships_added == 1
    assert (
        await seeded.scalar(
            select(UserGroupLink).where(
                UserGroupLink.user_id == member.id, UserGroupLink.group_id == group.id
            )
        )
        is not None
    )


async def test_sync_reports_unmatched_directory_groups(
    seeded: AsyncSession, admin: User, directory: _FakeDirectory
) -> None:
    """This list is the answer to "I created the group, why did nothing happen?"."""
    await factories.create_group(seeded, name="lab-a")
    _use(directory, ["lab-a", "Lab_Vision", "人工智能实验室"], {})

    result = await groups.sync_directory(seeded, admin)

    assert result.matched_groups == ["lab-a"]
    assert result.unmatched_groups == ["Lab_Vision", "人工智能实验室"]
    assert result.directory_groups == ["Lab_Vision", "lab-a", "人工智能实验室"]


async def test_unmatched_groups_are_reported_on_a_dry_run_too(
    seeded: AsyncSession, admin: User, directory: _FakeDirectory
) -> None:
    _use(directory, ["Lab_Vision"], {})

    result = await groups.sync_directory(seeded, admin, dry_run=True)

    assert result.dry_run is True
    assert result.unmatched_groups == ["Lab_Vision"]


async def test_sync_touches_nothing_when_nothing_matches(
    seeded: AsyncSession, admin: User, directory: _FakeDirectory
) -> None:
    _use(directory, ["Lab_Vision"], {"Lab_Vision": ["zhangsan"]})

    result = await groups.sync_directory(seeded, admin)

    assert result.memberships_added == 0
    assert result.unmatched_groups == ["Lab_Vision"]


async def test_a_contested_directory_name_is_skipped_not_guessed(
    seeded: AsyncSession, admin: User, directory: _FakeDirectory
) -> None:
    """Two groups answering to one directory name must not be reconciled.

    The API prevents this, so the row is written directly to prove the reconcile
    is defensive: picking either group would write memberships into a group the
    directory never mentioned.
    """
    first = await factories.create_group(seeded, name="lab-a")
    second = await factories.create_group(seeded, name="lab-b")
    second.directory_name = "lab-a"
    await seeded.flush()
    _use(directory, ["lab-a"], {"lab-a": ["zhangsan"]})

    result = await groups.sync_directory(seeded, admin)

    assert result.matched_groups == []
    assert result.memberships_added == 0
    assert any("lab-a" in message and "同时对应" in message for message in result.errors)
    for group in (first, second):
        assert (
            await seeded.scalar(select(UserGroupLink).where(UserGroupLink.group_id == group.id))
        ) is None


async def test_setting_a_directory_name_supersedes_the_name_for_matching(
    seeded: AsyncSession, admin: User, directory: _FakeDirectory
) -> None:
    """The group stops answering to its `name` once a directory name is set.

    "The directory name for this group is X" is a replacement, not an addition:
    ``directory_key`` is a single value, which is what stops one group claiming
    two directory names and keeps the uniqueness rule simple. The consequence is
    real — filling the field in changes what the group matches — so it is pinned
    here rather than left to be discovered in production.
    """
    await factories.create_user(seeded, username="zhangsan")
    group = await groups.create_group(seeded, admin, GroupCreate(name="lab-vision"))
    _use(directory, ["lab-vision"], {"lab-vision": ["zhangsan"]})

    first = await groups.sync_directory(seeded, admin)
    await seeded.flush()
    assert first.matched_groups == ["lab-vision"]

    await groups.update_group(seeded, admin, group, GroupUpdate(directory_name="Lab_Vision"))
    # The directory still reports the slug, and the group no longer answers to it.
    second = await groups.sync_directory(seeded, admin)

    assert second.matched_groups == []
    assert second.unmatched_groups == ["lab-vision"]


# --- the per-login path agrees with the bulk reconcile -------------------------
async def test_per_login_sync_matches_on_the_directory_name(
    seeded: AsyncSession, admin: User, settings: Settings
) -> None:
    member = await factories.create_user(seeded, username="wangwu")
    group = await groups.create_group(
        seeded, admin, GroupCreate(name="lab-vision", directory_name="Lab_Vision")
    )

    identity = ExternalIdentity(username="wangwu", groups=("Lab_Vision",))
    await service.sync_directory_groups(seeded, member, identity, source=UserSource.IPA)
    await seeded.flush()

    link = await seeded.scalar(
        select(UserGroupLink).where(
            UserGroupLink.user_id == member.id, UserGroupLink.group_id == group.id
        )
    )
    assert link is not None
    assert link.is_directory_managed is True


async def test_per_login_sync_ignores_an_unmatched_directory_group(
    seeded: AsyncSession, admin: User, settings: Settings
) -> None:
    member = await factories.create_user(seeded, username="wangwu")
    await factories.create_group(seeded, name="lab-a")

    identity = ExternalIdentity(username="wangwu", groups=("Lab_Vision",))
    await service.sync_directory_groups(seeded, member, identity, source=UserSource.IPA)
    await seeded.flush()

    assert (await seeded.scalars(select(UserGroupLink))).all() == []


async def test_per_login_sync_skips_a_contested_directory_name(
    seeded: AsyncSession, admin: User, settings: Settings
) -> None:
    """The per-login path must not resolve a collision by row order either.

    Both groups below answer to "lab-a": one explicitly, one via ``name``. An
    unguarded dict build keeps whichever the database returned first, so the
    membership would land in a different group depending on query planning. The
    correct answer is the same as the bulk reconcile's — join neither.
    """
    member = await factories.create_user(seeded, username="zhaoliu")
    explicit = await factories.create_group(seeded, name="lab-b")
    await groups.update_group(seeded, admin, explicit, GroupUpdate(directory_name="lab-a"))
    # Written directly, because the API refuses this collision — which is why the
    # reconcile has to be defensive about it in the first place.
    by_name = await factories.create_group(seeded, name="lab-a")

    identity = ExternalIdentity(username="zhaoliu", groups=("lab-a",))
    await service.sync_directory_groups(seeded, member, identity, source=UserSource.IPA)
    await seeded.flush()

    joined = {
        link.group_id
        for link in (
            await seeded.scalars(select(UserGroupLink).where(UserGroupLink.user_id == member.id))
        ).all()
    }
    assert joined == set()
    for group in (explicit, by_name):
        assert group.id not in joined
