"""Tests for the authentication service (Phase 1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.providers import ExternalIdentity
from app.auth.tokens import decode_access_token, hash_token
from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    ForbiddenError,
    UnauthorizedError,
)
from app.models.enums import GroupRole, UserSource
from app.models.rbac import UserRole
from app.models.token import RefreshToken
from tests import factories


# --- credential verification -------------------------------------------------
async def test_login_with_valid_credentials(seeded: AsyncSession, settings: Settings) -> None:
    await factories.create_user(seeded, username="alice", password="correct-horse-battery")

    user, provider = await service.authenticate(
        seeded, "alice", "correct-horse-battery", settings=settings
    )

    assert user.username == "alice"
    assert provider == "local"
    assert user.last_login_at is not None


async def test_login_with_wrong_password_is_rejected(
    seeded: AsyncSession, settings: Settings
) -> None:
    await factories.create_user(seeded, username="alice", password="correct-horse-battery")

    with pytest.raises(UnauthorizedError) as exc:
        await service.authenticate(seeded, "alice", "wrong", settings=settings)

    assert exc.value.code == "invalid_credentials"


async def test_login_with_unknown_user_is_rejected(
    seeded: AsyncSession, settings: Settings
) -> None:
    with pytest.raises(UnauthorizedError):
        await service.authenticate(seeded, "nobody", "whatever", settings=settings)


async def test_login_is_rejected_for_disabled_account(
    seeded: AsyncSession, settings: Settings
) -> None:
    await factories.create_user(
        seeded, username="alice", password="correct-horse-battery", is_active=False
    )

    with pytest.raises(ForbiddenError) as exc:
        await service.authenticate(seeded, "alice", "correct-horse-battery", settings=settings)

    assert exc.value.code == "account_disabled"


async def test_login_is_rejected_for_expired_account(
    seeded: AsyncSession, settings: Settings
) -> None:
    user = await factories.create_user(seeded, username="alice", password="correct-horse-battery")
    user.expires_at = datetime.now(UTC) - timedelta(days=1)
    await seeded.flush()

    with pytest.raises(ForbiddenError) as exc:
        await service.authenticate(seeded, "alice", "correct-horse-battery", settings=settings)

    assert exc.value.code == "account_expired"


async def test_directory_user_cannot_use_local_provider(
    seeded: AsyncSession, settings: Settings
) -> None:
    """An IPA-sourced row must never authenticate through the local provider."""
    await factories.create_user(
        seeded,
        username="bob",
        password="correct-horse-battery",
        source=UserSource.IPA,
    )

    with pytest.raises(UnauthorizedError):
        await service.authenticate(seeded, "bob", "correct-horse-battery", settings=settings)


# --- JIT provisioning --------------------------------------------------------
async def test_jit_provisioning_creates_user_with_default_role(
    seeded: AsyncSession, settings: Settings
) -> None:
    identity = ExternalIdentity(
        username="carol",
        email="carol@example.edu",
        full_name="Carol Danvers",
        external_id="uid=carol,cn=users,cn=accounts,dc=example,dc=edu",
        groups=(),
    )

    user = await service.provision_user(seeded, identity, source=UserSource.IPA, settings=settings)
    await seeded.refresh(user, ["role_links"])

    assert user.username == "carol"
    assert user.email == "carol@example.edu"
    assert user.full_name == "Carol Danvers"
    assert user.source == UserSource.IPA
    assert {link.role.code for link in user.role_links} == {"user"}


async def test_observer_group_maps_to_observer_role(
    seeded: AsyncSession, settings: Settings
) -> None:
    settings.ldap_observer_group = "ipa-readonly"
    identity = ExternalIdentity(username="dave", groups=("ipa-readonly", "other"))

    user = await service.provision_user(seeded, identity, source=UserSource.IPA, settings=settings)
    await seeded.refresh(user, ["role_links"])

    assert {link.role.code for link in user.role_links} == {"observer"}


async def test_provisioning_refuses_to_take_over_local_account(
    seeded: AsyncSession, settings: Settings
) -> None:
    """A directory identity must not inherit a same-named local admin account."""
    await factories.create_user(
        seeded, username="admin", password="local-password", role="global_admin"
    )

    identity = ExternalIdentity(username="admin", external_id="uid=admin,dc=example,dc=edu")

    with pytest.raises(ConflictError) as exc:
        await service.provision_user(seeded, identity, source=UserSource.IPA, settings=settings)

    assert exc.value.code == "username_conflict"


async def test_provisioning_is_idempotent(seeded: AsyncSession, settings: Settings) -> None:
    identity = ExternalIdentity(
        username="erin", email="erin@example.edu", external_id="uid=erin,dc=example,dc=edu"
    )

    first = await service.provision_user(seeded, identity, source=UserSource.IPA, settings=settings)
    second = await service.provision_user(
        seeded, identity, source=UserSource.IPA, settings=settings
    )

    assert first.id == second.id

    roles = await seeded.scalars(select(UserRole).where(UserRole.user_id == first.id))
    assert len(list(roles)) == 1, "default role must not be granted twice"


# --- directory group sync ----------------------------------------------------
async def test_directory_group_sync_maps_local_groups(
    seeded: AsyncSession, settings: Settings
) -> None:
    await factories.create_group(seeded, name="vision-lab")
    await factories.create_group(seeded, name="nlp-lab")
    user = await factories.create_user(seeded, username="frank")

    identity = ExternalIdentity(username="frank", groups=("vision-lab", "unknown-group"))
    await service.sync_directory_groups(seeded, user, identity, source=UserSource.IPA)

    await seeded.refresh(user, ["group_links"])
    assert [link.group.name for link in user.group_links] == ["vision-lab"]
    assert user.group_links[0].is_directory_managed is True
    assert user.group_links[0].is_primary is True

    # The group exists but was not reported this time -> directory-owned link goes.
    identity_without = ExternalIdentity(username="frank", groups=())
    await service.sync_directory_groups(seeded, user, identity_without, source=UserSource.IPA)
    await seeded.refresh(user, ["group_links"])
    assert [link.group.name for link in user.group_links] == ["vision-lab"], (
        "an empty group list must not wipe memberships"
    )

    identity_other = ExternalIdentity(username="frank", groups=("nlp-lab",))
    await service.sync_directory_groups(seeded, user, identity_other, source=UserSource.IPA)
    await seeded.refresh(user, ["group_links"])
    assert [link.group.name for link in user.group_links] == ["nlp-lab"]


async def test_manual_membership_survives_directory_sync(
    seeded: AsyncSession, settings: Settings
) -> None:
    await factories.create_group(seeded, name="vision-lab")
    manual = await factories.create_group(seeded, name="manual-group")
    user = await factories.create_user(seeded, username="grace")
    await factories.add_to_group(seeded, user, manual, is_primary=True)

    identity = ExternalIdentity(username="grace", groups=("vision-lab",))
    await service.sync_directory_groups(seeded, user, identity, source=UserSource.IPA)

    await seeded.refresh(user, ["group_links"])
    names = sorted(link.group.name for link in user.group_links)
    assert names == ["manual-group", "vision-lab"]

    primary = [link for link in user.group_links if link.is_primary]
    assert len(primary) == 1, "exactly one primary group must remain"
    assert primary[0].group.name == "manual-group", "hand-granted primary is preserved"


async def test_group_admin_role_is_not_derived_from_directory(
    seeded: AsyncSession, settings: Settings
) -> None:
    """group_admin must be an explicit grant, never inferred from a group name."""
    await factories.create_group(seeded, name="vision-lab")
    identity = ExternalIdentity(
        username="heidi",
        external_id="uid=heidi,dc=example,dc=edu",
        groups=("vision-lab",),
    )

    user = await service.provision_user(seeded, identity, source=UserSource.IPA, settings=settings)
    await service.sync_directory_groups(seeded, user, identity, source=UserSource.IPA)
    await seeded.refresh(user, ["role_links"])

    assert {link.role.code for link in user.role_links} == {"user"}


# --- sessions ----------------------------------------------------------------
async def test_issued_access_token_is_decodable(seeded: AsyncSession, settings: Settings) -> None:
    user = await factories.create_user(seeded, username="ivan")
    issued = await service.issue_session(seeded, user, settings=settings)

    decoded = decode_access_token(issued.access_token, settings)
    assert decoded.user_id == user.id
    assert decoded.username == "ivan"


async def test_refresh_token_rotation(seeded: AsyncSession, settings: Settings) -> None:
    user = await factories.create_user(seeded, username="judy")
    first = await service.issue_session(seeded, user, settings=settings)

    second = await service.rotate_refresh_token(seeded, first.refresh_token, settings=settings)
    assert second.refresh_token != first.refresh_token
    assert second.user.id == user.id

    old = await seeded.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(first.refresh_token))
    )
    assert old is not None
    assert old.revoked_at is not None
    assert old.replaced_by_id is not None


async def test_replayed_refresh_token_revokes_whole_family(
    seeded: AsyncSession, settings: Settings
) -> None:
    user = await factories.create_user(seeded, username="karl")
    first = await service.issue_session(seeded, user, settings=settings)
    second = await service.rotate_refresh_token(seeded, first.refresh_token, settings=settings)

    # Replaying the already-rotated token is treated as theft.
    with pytest.raises(UnauthorizedError) as exc:
        await service.rotate_refresh_token(seeded, first.refresh_token, settings=settings)
    assert exc.value.code == "refresh_token_reused"

    # ... and the legitimately rotated token is revoked too.
    with pytest.raises(UnauthorizedError):
        await service.rotate_refresh_token(seeded, second.refresh_token, settings=settings)


async def test_expired_refresh_token_is_rejected(seeded: AsyncSession, settings: Settings) -> None:
    user = await factories.create_user(seeded, username="laura")
    issued = await service.issue_session(seeded, user, settings=settings)

    token = await seeded.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(issued.refresh_token))
    )
    assert token is not None
    token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await seeded.flush()

    with pytest.raises(UnauthorizedError) as exc:
        await service.rotate_refresh_token(seeded, issued.refresh_token, settings=settings)
    assert exc.value.code == "refresh_token_expired"


async def test_revoked_refresh_token_cannot_be_used(
    seeded: AsyncSession, settings: Settings
) -> None:
    user = await factories.create_user(seeded, username="mallory")
    issued = await service.issue_session(seeded, user, settings=settings)

    await service.revoke_refresh_token(seeded, issued.refresh_token)

    with pytest.raises(UnauthorizedError) as exc:
        await service.rotate_refresh_token(seeded, issued.refresh_token, settings=settings)
    assert exc.value.code == "refresh_token_reused"


async def test_revoke_user_tokens_revokes_all(seeded: AsyncSession, settings: Settings) -> None:
    user = await factories.create_user(seeded, username="nina")
    await service.issue_session(seeded, user, settings=settings)
    await service.issue_session(seeded, user, settings=settings)

    count = await service.revoke_user_tokens(seeded, user.id)
    assert count == 2


async def test_refresh_rejects_disabled_user(seeded: AsyncSession, settings: Settings) -> None:
    user = await factories.create_user(seeded, username="oscar")
    issued = await service.issue_session(seeded, user, settings=settings)

    user.is_active = False
    await seeded.flush()

    with pytest.raises(ForbiddenError):
        await service.rotate_refresh_token(seeded, issued.refresh_token, settings=settings)


async def test_primary_group_helper_keeps_exactly_one(
    seeded: AsyncSession, settings: Settings
) -> None:
    first = await factories.create_group(seeded, name="lab-a")
    second = await factories.create_group(seeded, name="lab-b")
    user = await factories.create_user(seeded, username="peggy")
    await factories.add_to_group(seeded, user, first, is_primary=True)
    await factories.add_to_group(seeded, user, second, is_primary=True)

    await service.ensure_primary_group(seeded, user)
    await seeded.refresh(user, ["group_links"])

    assert sum(1 for link in user.group_links if link.is_primary) == 1


async def test_group_role_enum_default(seeded: AsyncSession, settings: Settings) -> None:
    group = await factories.create_group(seeded, name="lab-c")
    user = await factories.create_user(seeded, username="quinn")
    link = await factories.add_to_group(seeded, user, group)

    assert link.role_in_group == GroupRole.MEMBER
    assert link.is_directory_managed is False
