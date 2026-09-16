"""Tests for the operational CLI.

`bootstrap-admin` is the only way to create the first account, and the only way
back in after an administrator locks themselves out, so it is worth covering.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import cli
from app.auth.passwords import verify_password
from app.models.enums import UserSource
from app.models.rbac import Role, UserRole
from app.models.user import User
from tests import factories


@pytest.fixture
def cli_session(
    monkeypatch: pytest.MonkeyPatch, session_factory: async_sessionmaker[AsyncSession]
) -> Iterator[None]:
    """Point the CLI at the test database instead of a real engine."""
    monkeypatch.setattr(cli, "get_session_factory", lambda: session_factory)
    yield


async def test_bootstrap_creates_a_usable_global_admin(
    cli_session: None, seeded: AsyncSession
) -> None:
    exit_code = await cli._bootstrap_admin("root", "bootstrap-password-1", update_password=False)

    assert exit_code == 0
    user = await seeded.scalar(select(User).where(User.username == "root"))
    assert user is not None
    assert user.source is UserSource.LOCAL
    assert user.is_active is True
    assert user.is_superuser is True
    assert verify_password("bootstrap-password-1", user.password_hash)

    role = await seeded.scalar(select(Role).where(Role.code == "global_admin"))
    assert role is not None
    grant = await seeded.scalar(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
    )
    assert grant is not None


async def test_bootstrap_is_idempotent(cli_session: None, seeded: AsyncSession) -> None:
    """Re-running must not duplicate the role grant or change the password."""
    await cli._bootstrap_admin("root", "bootstrap-password-1", update_password=False)
    await cli._bootstrap_admin("root", "a-different-password-1", update_password=False)

    user = await seeded.scalar(select(User).where(User.username == "root"))
    assert user is not None
    # The second call left the existing password alone.
    assert verify_password("bootstrap-password-1", user.password_hash)

    role = await seeded.scalar(select(Role).where(Role.code == "global_admin"))
    assert role is not None
    grants = list(
        (
            await seeded.scalars(
                select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
            )
        ).all()
    )
    assert len(grants) == 1


async def test_bootstrap_resets_the_password_on_request(
    cli_session: None, seeded: AsyncSession
) -> None:
    await cli._bootstrap_admin("root", "bootstrap-password-1", update_password=False)
    await cli._bootstrap_admin("root", "bootstrap-password-2", update_password=True)

    user = await seeded.scalar(select(User).where(User.username == "root"))
    assert user is not None
    assert verify_password("bootstrap-password-2", user.password_hash)


async def test_bootstrap_refuses_to_touch_a_directory_account(
    cli_session: None, seeded: AsyncSession
) -> None:
    """Turning an LDAP identity into a local password account would be a bypass."""
    external = await factories.create_user(seeded, username="ipa-user", role="user")
    external.source = UserSource.IPA
    await seeded.commit()

    exit_code = await cli._bootstrap_admin("ipa-user", "bootstrap-password-1", update_password=True)

    assert exit_code == 1
    user = await seeded.scalar(select(User).where(User.username == "ipa-user"))
    assert user is not None
    assert user.password_hash is None


async def test_sync_rbac_is_idempotent(cli_session: None, seeded: AsyncSession) -> None:
    assert await cli._sync_rbac() == 0
    assert await cli._sync_rbac() == 0

    roles = list((await seeded.scalars(select(Role))).all())
    assert {role.code for role in roles} == {
        "observer",
        "user",
        "group_admin",
        "global_admin",
    }
