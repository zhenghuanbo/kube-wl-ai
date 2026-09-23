"""LDAP / FreeIPA authentication provider.

FreeIPA is the primary identity source (plan D2). Two separate operations are
performed:

1. **Bind as the user** to verify the password. This is the only step that
   proves the credential.
2. **Bind as the service account** to read the user's attributes and group
   memberships. The service account is used because directory read policy
   routinely restricts what a regular user may see.

``ldap3`` is synchronous, so both steps run in a worker thread via
``asyncio.to_thread`` — a blocking LDAP round-trip must never stall the event
loop.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import ldap3
from ldap3.core.exceptions import (
    LDAPBindError,
    LDAPExceptionError,
    LDAPInvalidCredentialsResult,
    LDAPSocketOpenError,
    LDAPStartTLSError,
)
from ldap3.utils.conv import escape_filter_chars
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.providers.base import AuthProvider, AuthProviderError, ExternalIdentity
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Usernames are interpolated into a DN, so they must not contain DN syntax.
_SAFE_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,149}$")

#: First RDN of a DN, e.g. ``uid=alice`` in ``uid=alice,cn=users,...``.
_DN_UID = re.compile(r"^(?:uid|cn)=([^,]+)", re.IGNORECASE)

#: Attributes read from the user entry after a successful bind.
#:
#: ``memberOf`` is deliberately absent. It looks like the obvious way to read
#: groups, but it is an overlay-maintained attribute that a bare directory need
#: not provide, and it is not scoped to the group subtree we configured. Groups
#: come from :meth:`LDAPProvider._read_groups` instead, which searches for
#: ``(member=<user dn>)`` under ``ldap_group_base_dn``. Requesting an attribute
#: nothing reads is an extra round-trip's worth of data for no benefit.
_USER_ATTRIBUTES = ["uid", "cn", "displayName", "givenName", "sn", "mail"]


class LDAPProvider(AuthProvider):
    name = "ldap"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._timeout = 10

    # --- configuration -------------------------------------------------------
    def is_configured(self) -> bool:
        return bool(self._settings.ldap_uri and self._settings.ldap_base_dn)

    def describe(self) -> str:
        uri = self._settings.ldap_uri or "(unset)"
        return f"ldap ({uri})"

    def _user_dn(self, username: str) -> str:
        return self._settings.ldap_user_dn_template.format(
            username=username,
            base_dn=self._settings.ldap_base_dn,
        )

    def _group_base_dn(self) -> str:
        return self._settings.ldap_group_base_dn.format(base_dn=self._settings.ldap_base_dn)

    def _server(self) -> ldap3.Server:
        tls: ldap3.Tls | None = None
        if self._settings.ldap_ca_cert:
            tls = ldap3.Tls(ca_certs_file=self._settings.ldap_ca_cert)
        return ldap3.Server(
            self._settings.ldap_uri,
            get_info=None,
            connect_timeout=self._timeout,
            tls=tls,
        )

    # --- service-account connection ------------------------------------------
    @contextmanager
    def _service_connection(self) -> Iterator[ldap3.Connection]:
        """Bound connection for reading directory data.

        Every read goes through here so that the failure mode is uniform. Callers
        previously built their own connections, and the one that read user
        attributes forgot to guard its search: an ACL rejection surfaced as an
        unhandled ``ldap3`` error and a 500, when every other directory failure
        on this path correctly becomes a 503. Consolidating means the conversion
        cannot be forgotten at a new call site.
        """
        bind_dn = self._settings.ldap_bind_dn or None
        bind_password = self._settings.ldap_bind_password.get_secret_value() or None

        if not bind_dn:
            # ldap3 binds anonymously when both are None, and that usually
            # *succeeds* — so the symptom is not a failed login but a user who
            # arrives with no email, no full name and no groups, holding only the
            # default role. Naming the cause here is the difference between a
            # five-minute fix and an afternoon.
            logger.warning(
                "ldap_service_account_not_configured",
                detail=(
                    "LDAP_BIND_DN is empty; reading attributes and groups "
                    "anonymously. Set LDAP_BIND_DN and LDAP_BIND_PASSWORD so group "
                    "membership sync can work."
                ),
            )

        server = self._server()
        try:
            connection = ldap3.Connection(
                server,
                user=bind_dn,
                password=bind_password,
                auto_bind=True,
                receive_timeout=self._timeout,
                raise_exceptions=True,
            )
        except LDAPExceptionError as exc:
            raise AuthProviderError(f"LDAP service bind failed: {exc}") from exc

        try:
            yield connection
        finally:
            connection.unbind()

    @staticmethod
    def _search_on(
        connection: ldap3.Connection,
        search_base: str,
        search_filter: str,
        attributes: list[str],
        *,
        size_limit: int = 1000,
    ) -> list[Any]:
        """Run one search on an already-bound connection."""
        try:
            connection.search(
                search_base=search_base,
                search_filter=search_filter,
                attributes=attributes,
                size_limit=size_limit,
            )
        except LDAPExceptionError as exc:
            raise AuthProviderError(f"LDAP search failed: {exc}") from exc
        return list(connection.entries or [])

    # --- blocking helpers (run in a thread) ----------------------------------
    def _verify_password(self, username: str, password: str) -> bool:
        """Bind as the user. Returns False only for rejected credentials."""
        server = self._server()
        try:
            connection = ldap3.Connection(
                server,
                user=self._user_dn(username),
                password=password,
                auto_bind=True,
                receive_timeout=self._timeout,
                raise_exceptions=True,
            )
        except LDAPInvalidCredentialsResult:
            return False
        except LDAPBindError as exc:
            message = str(exc).lower()
            if "invalidcredential" in message or "invalid credential" in message:
                return False
            # e.g. account locked, password expired, unsupported auth
            raise AuthProviderError(f"LDAP bind failed: {exc}") from exc
        except (LDAPSocketOpenError, LDAPStartTLSError) as exc:
            raise AuthProviderError(f"LDAP server unreachable: {exc}") from exc
        except LDAPExceptionError as exc:
            raise AuthProviderError(f"LDAP error during bind: {exc}") from exc
        else:
            connection.unbind()
            return True

    def _read_identity(self, username: str, user_dn: str) -> ExternalIdentity:
        """Read attributes and groups using the service account."""
        with self._service_connection() as connection:
            entries = self._search_on(
                connection,
                self._settings.ldap_base_dn,
                f"(uid={escape_filter_chars(username)})",
                _USER_ATTRIBUTES,
                size_limit=2,
            )
            entry = entries[0] if entries else None

            email = self._attr(entry, "mail")
            full_name = (
                self._attr(entry, "displayName")
                or " ".join(
                    part
                    for part in (self._attr(entry, "givenName"), self._attr(entry, "sn"))
                    if part
                )
                or self._attr(entry, "cn")
                or username
            )

            groups = self._read_groups(connection, user_dn, username)

        return ExternalIdentity(
            username=username,
            email=email,
            full_name=full_name,
            external_id=user_dn,
            groups=groups,
        )

    def _read_groups(
        self, connection: ldap3.Connection, user_dn: str, username: str
    ) -> tuple[str, ...]:
        """Group ``cn`` values the user is a direct member of.

        Both membership spellings are searched, and they must be, because a
        directory uses one or the other and the two are not interchangeable:

        * ``member`` holds the member's **full DN** — ``groupOfNames``, Active
          Directory, FreeIPA.
        * ``memberUid`` holds the bare **login name** — ``posixGroup``, which is
          what a plain OpenLDAP deployment almost always uses.

        The bulk reconcile has always read both (``_members_from_entry``). This
        login-time path read only ``member``, so on a ``posixGroup`` directory the
        two disagreed: a sync would map a user's groups while the same user's own
        login reported none, leaving them on the default role until somebody
        happened to run the sync by hand.
        """
        try:
            connection.search(
                search_base=self._group_base_dn(),
                search_filter=(
                    f"(|(member={escape_filter_chars(user_dn)})"
                    f"(memberUid={escape_filter_chars(username)}))"
                ),
                attributes=["cn"],
                size_limit=500,
            )
        except LDAPExceptionError as exc:
            # Group lookup failing must not block login: the user simply gets
            # the default role and an operator can fix the bind DN later.
            logger.warning("ldap_group_lookup_failed", error=str(exc), user_dn=user_dn)
            return ()

        names: list[str] = []
        for entry in connection.entries or []:
            value = self._attr(entry, "cn")
            if value:
                names.append(value)
        return tuple(sorted(set(names)))

    @staticmethod
    def _attr_list(entry: Any | None, name: str) -> list[str]:
        if entry is None or name not in entry:
            return []
        value = entry[name].value
        if value is None:
            return []
        if isinstance(value, list | tuple):
            return [str(item) for item in value]
        return [str(value)]

    @staticmethod
    def uid_from_dn(dn: str) -> str | None:
        """Extract the login name from a FreeIPA/LDAP DN."""
        match = _DN_UID.match(dn.strip())
        return match.group(1) if match else None

    def _search(
        self,
        search_base: str,
        search_filter: str,
        attributes: list[str],
        *,
        size_limit: int = 1000,
    ) -> list[Any]:
        """Run a search with the service account, on its own connection."""
        with self._service_connection() as connection:
            return self._search_on(
                connection, search_base, search_filter, attributes, size_limit=size_limit
            )

    def _list_groups(self) -> list[str]:
        entries = self._search(self._group_base_dn(), "(objectClass=posixgroup)", ["cn"])
        return sorted({name for entry in entries if (name := self._attr(entry, "cn"))})

    @staticmethod
    def _members_from_entry(entry: Any) -> list[str]:
        """Login names listed by one group entry.

        Both spellings are read because directories differ: ``member`` holds DNs
        (OpenLDAP, AD) while ``memberUid`` holds bare login names (posixGroup).
        """
        members = set(LDAPProvider._attr_list(entry, "memberUid"))
        for dn in LDAPProvider._attr_list(entry, "member"):
            uid = LDAPProvider.uid_from_dn(dn)
            if uid:
                members.add(uid)
        return sorted(members)

    def _group_members(self, group_name: str) -> list[str]:
        entries = self._search(
            self._group_base_dn(),
            f"(cn={escape_filter_chars(group_name)})",
            ["member", "memberUid"],
            size_limit=2,
        )
        if not entries:
            return []
        return self._members_from_entry(entries[0])

    def _group_members_many(self, group_names: Sequence[str]) -> dict[str, list[str]]:
        """Members of many groups over **one** connection.

        The reconcile walks every matched group, and opening a connection per
        group meant a full sync cost one TCP+TLS handshake per group — 50 groups,
        51 handshakes, almost all of it connection setup rather than directory
        work. Searches still run one per group, which keeps the filters simple
        and lets a single unreadable group fail on its own.
        """
        if not group_names:
            return {}

        result: dict[str, list[str]] = {}
        with self._service_connection() as connection:
            for name in group_names:
                entries = self._search_on(
                    connection,
                    self._group_base_dn(),
                    f"(cn={escape_filter_chars(name)})",
                    ["member", "memberUid"],
                    size_limit=2,
                )
                result[name] = self._members_from_entry(entries[0]) if entries else []
        return result

    @staticmethod
    def _attr(entry: Any | None, name: str) -> str | None:
        if entry is None:
            return None
        value = entry[name].value if name in entry else None
        if value is None:
            return None
        if isinstance(value, list | tuple):
            return str(value[0]) if value else None
        return str(value)

    # --- public API ----------------------------------------------------------
    async def authenticate(
        self,
        username: str,
        password: str,
        *,
        session: AsyncSession,
    ) -> ExternalIdentity | None:
        del session  # LDAP does not use our database

        if not username or not password:
            return None
        if not _SAFE_USERNAME.match(username):
            # Reject DN-injection attempts before they reach the directory.
            logger.warning("ldap_unsafe_username_rejected", username=username[:64])
            return None

        verified = await asyncio.to_thread(self._verify_password, username, password)
        if not verified:
            return None

        return await asyncio.to_thread(self._read_identity, username, self._user_dn(username))

    # --- directory browsing (used by group sync) -----------------------------
    async def directory_groups(self) -> list[str] | None:
        """Every group name in the directory, or ``None`` if unsupported."""
        if not self.is_configured():
            return None
        return await asyncio.to_thread(self._list_groups)

    async def directory_group_members(self, group_name: str) -> list[str] | None:
        """Login names of a group's direct members, or ``None`` if unsupported."""
        if not self.is_configured():
            return None
        return await asyncio.to_thread(self._group_members, group_name)

    async def directory_group_members_many(
        self, group_names: Sequence[str]
    ) -> dict[str, list[str]] | None:
        """Members of many groups in one pass, or ``None`` if unsupported.

        Used by the reconcile, which otherwise pays a connection handshake per
        group. ``None`` means "call the single-group method instead".
        """
        if not self.is_configured():
            return None
        return await asyncio.to_thread(self._group_members_many, list(group_names))
