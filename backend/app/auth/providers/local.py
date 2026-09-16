"""Local (break-glass) authentication provider.

Verifies against ``users.password_hash`` in our own database. This is the
emergency path when the directory is unavailable, and the mechanism used by the
test suite. It can be disabled in production by removing ``local`` from
``AUTH_PROVIDERS``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import verify_password
from app.auth.providers.base import AuthProvider, ExternalIdentity
from app.core.config import Settings, get_settings
from app.models.enums import UserSource
from app.models.user import User


class LocalProvider(AuthProvider):
    name = "local"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def is_configured(self) -> bool:
        # Always usable: it only needs the database we are already running on.
        return True

    async def authenticate(
        self,
        username: str,
        password: str,
        *,
        session: AsyncSession,
    ) -> ExternalIdentity | None:
        user = await session.scalar(select(User).where(User.username == username))

        # Only genuine local accounts may use this provider. A directory-backed
        # user must never be able to authenticate here even if a password hash
        # somehow ends up on the row.
        if user is not None and user.source != UserSource.LOCAL:
            return None

        # Verify even when the user is unknown, so response timing does not leak
        # which usernames exist.
        stored_hash = user.password_hash if user else None
        if not verify_password(password, stored_hash):
            return None

        if user is None or not user.password_hash:
            # Either unknown, or a local row without a password set.
            return None

        return ExternalIdentity(
            username=user.username,
            email=user.email,
            full_name=user.full_name,
            external_id=user.external_id,
            groups=(),
        )


def bootstrap_credentials_are_default() -> bool:
    """Whether the configured break-glass password is still the shipped default.

    Surfaced by the system status endpoint so an operator cannot accidentally
    leave ``change-me`` in place in production.
    """
    settings = get_settings()
    return settings.local_admin_password.get_secret_value() in {"", "change-me", "replace-me"}
