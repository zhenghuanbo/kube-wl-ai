"""Authentication provider interface.

A provider answers one question: *is this username/password pair valid, and who
does it belong to?* Everything else — JIT provisioning, group mapping, role
assignment, session issuance — is the auth service's job (`app.auth.service`).

Adding a provider means implementing this class and registering it in
`app.auth.providers.registry`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    """A successfully authenticated external identity."""

    username: str
    email: str | None = None
    full_name: str = ""
    #: Stable identifier upstream (LDAP DN, OIDC ``sub``).
    external_id: str | None = None
    #: Group names the identity belongs to, as reported by the directory.
    groups: tuple[str, ...] = field(default_factory=tuple)
    #: Free-form extra attributes worth persisting or auditing.
    attributes: dict[str, str] = field(default_factory=dict)


class AuthProvider(ABC):
    """Base class for authentication providers."""

    #: Short identifier used in ``AUTH_PROVIDERS`` and in the UI.
    name: ClassVar[str]

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether the provider has enough configuration to be usable."""

    # --- Optional directory browsing ----------------------------------------
    #: Providers that can enumerate their directory let the platform reconcile
    #: group memberships in bulk. Returning ``None`` means "not supported".
    async def directory_groups(self) -> list[str] | None:
        return None

    async def directory_group_members(self, group_name: str) -> list[str] | None:
        return None

    async def directory_group_members_many(
        self, group_names: Sequence[str]
    ) -> dict[str, list[str]] | None:
        """Members of many groups, when the provider can do it in one pass.

        ``None`` means "not supported"; callers fall back to
        :meth:`directory_group_members` per group. Providers whose directory
        charges a connection per query should override this.
        """
        return None

    @abstractmethod
    async def authenticate(
        self,
        username: str,
        password: str,
        *,
        session: AsyncSession,
    ) -> ExternalIdentity | None:
        """Verify credentials.

        Returns the identity on success and ``None`` when the credentials are
        simply wrong. Infrastructure failures (directory unreachable, TLS
        error) must raise :class:`AuthProviderError` instead, so that an outage
        is never mistaken for a wrong password and silently denied.
        """

    def describe(self) -> str:
        """Human readable description used by the system status endpoint."""
        return self.name


class AuthProviderError(Exception):
    """The provider could not reach its authority, or the exchange failed.

    Deliberately distinct from "credentials rejected": the API maps this to
    ``503`` rather than ``401`` so operators can tell an outage from a typo.
    """
