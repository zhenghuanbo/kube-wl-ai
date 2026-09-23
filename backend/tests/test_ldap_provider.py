"""Directory provider behaviour that must not degrade silently.

Two failure modes are covered here, and both were previously invisible:

* A search rejected by the directory's ACL raised a raw ``ldap3`` exception and
  became a 500. Every other directory failure on this path is deliberately a 503,
  because "the directory is unreachable" and "the platform is broken" need
  different responses from whoever is on call.
* An empty ``LDAP_BIND_DN`` makes ldap3 bind anonymously — which usually
  *succeeds*, so the symptom was not a failed login but directory users arriving
  with no email, no name and no groups, holding only the default role.

The ``ldap3`` client is replaced wholesale: these tests are about how this code
interprets the library, not about the library.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

import pytest
from ldap3.core.exceptions import LDAPExceptionError
from pydantic import SecretStr
from structlog.testing import capture_logs

from app.auth.providers.base import AuthProviderError
from app.auth.providers.ldap import _USER_ATTRIBUTES, LDAPProvider
from app.core.config import Settings

USER_DN = "uid=zhangsan,cn=users,cn=accounts,dc=example,dc=edu"


class _Attribute:
    def __init__(self, value: Any) -> None:
        self.value = value


class _Entry:
    def __init__(self, attributes: dict[str, Any]) -> None:
        self._attributes = attributes

    def __contains__(self, name: str) -> bool:
        return name in self._attributes

    def __getitem__(self, name: str) -> _Attribute:
        return _Attribute(self._attributes[name])


class _FakeConnection:
    """Records how it was used, and lets a test script each search.

    ``handler`` receives the search that is about to run and returns the entries
    to report — or raises, which is how the failure paths are exercised.
    """

    instances: ClassVar[list[_FakeConnection]] = []
    handler: ClassVar[Callable[[dict[str, Any]], list[_Entry]] | None] = None

    def __init__(
        self,
        server: Any,
        user: str | None = None,
        password: str | None = None,
        auto_bind: bool = False,
        receive_timeout: int | None = None,
        raise_exceptions: bool = False,
    ) -> None:
        self.server = server
        self.user = user
        self.password = password
        self.unbound = False
        self.searches: list[dict[str, Any]] = []
        self.entries: list[_Entry] = []
        self.raise_on_init: Exception | None = None
        _FakeConnection.instances.append(self)

    def search(
        self,
        search_base: str,
        search_filter: str,
        attributes: list[str],
        size_limit: int = 0,
    ) -> None:
        call = {
            "base": search_base,
            "filter": search_filter,
            "attributes": attributes,
        }
        self.searches.append(call)
        handler = type(self).handler
        self.entries = handler(call) if handler is not None else []

    def unbind(self) -> None:
        self.unbound = True


def _entries_for(attributes: dict[str, Any]) -> Callable[[dict[str, Any]], list[_Entry]]:
    entry = _Entry(attributes)

    def handler(_: dict[str, Any]) -> list[_Entry]:
        return [entry]

    return handler


@pytest.fixture
def ldap_settings(settings: Settings) -> Settings:
    settings.ldap_uri = "ldaps://ipa.example.edu"
    settings.ldap_base_dn = "dc=example,dc=edu"
    settings.ldap_bind_dn = "uid=svc,cn=users,cn=accounts,dc=example,dc=edu"
    settings.ldap_bind_password = SecretStr("service-secret")
    return settings


@pytest.fixture
def fake_ldap(monkeypatch: pytest.MonkeyPatch) -> type[_FakeConnection]:
    import app.auth.providers.ldap as module

    _FakeConnection.instances = []
    _FakeConnection.handler = None
    monkeypatch.setattr(module.ldap3, "Connection", _FakeConnection)
    monkeypatch.setattr(module.ldap3, "Server", lambda *a, **k: object())
    return _FakeConnection


def _connection() -> _FakeConnection:
    return _FakeConnection.instances[-1]


# --- the failure that used to be a 500 ----------------------------------------
def test_attribute_search_failure_becomes_a_provider_error(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    """An ACL rejection must read as "directory unavailable", not "we crashed"."""

    def handler(call: dict[str, Any]) -> list[_Entry]:
        raise LDAPExceptionError("insufficientAccessRights")

    fake_ldap.handler = handler
    provider = LDAPProvider(ldap_settings)

    with pytest.raises(AuthProviderError):
        provider._read_identity("zhangsan", USER_DN)


def test_service_bind_failure_is_a_provider_error(
    ldap_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.auth.providers.ldap as module

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise LDAPExceptionError("cannot contact LDAP server")

    monkeypatch.setattr(module.ldap3, "Connection", boom)
    monkeypatch.setattr(module.ldap3, "Server", lambda *a, **k: object())
    provider = LDAPProvider(ldap_settings)

    with pytest.raises(AuthProviderError), provider._service_connection():
        pass


def test_group_search_failure_does_not_block_login(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    """Groups failing must not stop the login — the user takes the default role."""
    identity = _Entry({"uid": "zhangsan", "mail": "z@example.edu"})

    def handler(call: dict[str, Any]) -> list[_Entry]:
        if call["base"].startswith("cn=groups"):
            raise LDAPExceptionError("group subtree unreadable")
        return [identity]

    fake_ldap.handler = handler

    with capture_logs() as logs:
        result = LDAPProvider(ldap_settings)._read_identity("zhangsan", USER_DN)

    assert result.groups == ()
    assert result.email == "z@example.edu"
    assert "ldap_group_lookup_failed" in {entry["event"] for entry in logs}


# --- posixGroup: both membership spellings ------------------------------------
GROUP_BASE = "cn=groups,cn=accounts,dc=example,dc=edu"


def _is_group_search(call: dict[str, Any]) -> bool:
    """The group subtree search, as opposed to the user attribute search."""
    return call["base"] == GROUP_BASE


def _groups_search() -> dict[str, Any]:
    return next(call for call in _connection().searches if _is_group_search(call))


def test_group_search_asks_for_member_uid_as_well_as_member(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    """A ``posixGroup`` directory lists members by login name, not by DN.

    Reading only ``member`` made login disagree with the bulk reconcile on the
    same directory: the sync mapped the membership and the user's own login
    reported no groups, so they sat on the default role until somebody ran the
    sync by hand.
    """
    fake_ldap.handler = _entries_for({"uid": "zhangsan", "mail": "z@example.edu"})

    LDAPProvider(ldap_settings)._read_identity("zhangsan", USER_DN)

    search = _groups_search()
    assert "memberUid=zhangsan" in search["filter"]
    # The DN form must stay: FreeIPA/AD groups carry `member` instead.
    assert f"member={USER_DN}" in search["filter"]


def test_group_search_escapes_the_username(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    """The username reaches a filter, so a metacharacter must not break out of it."""
    provider = LDAPProvider(ldap_settings)

    # `_read_groups` is called directly, i.e. past `authenticate`'s own guard,
    # because this asserts on what the filter builder does rather than on whether
    # such a login would be allowed through.
    provider._read_groups(_FakeConnection(server=object()), USER_DN, "a*)(uid=*")

    search = _groups_search()
    assert "a*)(uid=*" not in search["filter"]
    assert "\\2a\\29\\28uid=\\2a" in search["filter"]


def test_posix_group_membership_is_reported_at_login(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    """End to end on the login path: a memberUid match becomes a real group."""
    person = _Entry({"uid": "zhangsan", "mail": "z@example.edu", "cn": "Zhang San"})
    group = _Entry({"cn": "hpc"})

    def handler(call: dict[str, Any]) -> list[_Entry]:
        if _is_group_search(call):
            # The directory only answers this because the filter mentions
            # memberUid; a member-only filter would return nothing here.
            return [group] if "memberUid=zhangsan" in call["filter"] else []
        return [person]

    fake_ldap.handler = handler

    result = LDAPProvider(ldap_settings)._read_identity("zhangsan", USER_DN)

    assert result.groups == ("hpc",)


# --- what is asked of the directory -------------------------------------------
def test_member_of_is_not_requested() -> None:
    """``memberOf`` was fetched and never read; groups come from the reverse search."""
    assert "memberOf" not in _USER_ATTRIBUTES


def test_only_attributes_we_consume_are_requested() -> None:
    assert set(_USER_ATTRIBUTES) == {"uid", "cn", "displayName", "givenName", "sn", "mail"}


def test_full_name_falls_back_through_display_name_to_uid(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    fake_ldap.handler = _entries_for({"givenName": "San", "sn": "Zhang"})
    identity = LDAPProvider(ldap_settings)._read_identity("zhangsan", USER_DN)

    assert identity.full_name == "San Zhang"


def test_full_name_falls_back_to_username_when_nothing_is_set(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    fake_ldap.handler = _entries_for({"uid": "zhangsan"})
    identity = LDAPProvider(ldap_settings)._read_identity("zhangsan", USER_DN)

    assert identity.full_name == "zhangsan"


# --- the service account ------------------------------------------------------
def test_empty_bind_dn_is_warned_about(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    """The anonymous-bind fallback must be announced, not silent."""
    ldap_settings.ldap_bind_dn = ""
    provider = LDAPProvider(ldap_settings)

    with capture_logs() as logs, provider._service_connection():
        pass

    assert "ldap_service_account_not_configured" in {entry["event"] for entry in logs}


def test_configured_bind_dn_is_not_warned_about(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    provider = LDAPProvider(ldap_settings)

    with capture_logs() as logs, provider._service_connection():
        pass

    assert "ldap_service_account_not_configured" not in {entry["event"] for entry in logs}


def test_service_account_credentials_are_used(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    provider = LDAPProvider(ldap_settings)

    with provider._service_connection():
        pass

    assert _connection().user == ldap_settings.ldap_bind_dn
    assert _connection().password == "service-secret"


def test_connection_is_always_unbound(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    provider = LDAPProvider(ldap_settings)

    with provider._service_connection():
        pass

    assert _connection().unbound is True


# --- one connection for a whole reconcile -------------------------------------
def test_bulk_member_lookup_uses_one_connection(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    """The reconcile used to pay a TCP+TLS handshake per group."""
    provider = LDAPProvider(ldap_settings)

    members = provider._group_members_many(["lab-a", "lab-b", "lab-c"])

    assert len(_FakeConnection.instances) == 1
    assert set(members) == {"lab-a", "lab-b", "lab-c"}
    assert _connection().unbound is True


def test_bulk_member_lookup_searches_each_group(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    provider = LDAPProvider(ldap_settings)

    provider._group_members_many(["lab-a", "lab-b"])

    assert [search["filter"] for search in _connection().searches] == [
        "(cn=lab-a)",
        "(cn=lab-b)",
    ]


def test_bulk_member_lookup_of_nothing_opens_nothing(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    provider = LDAPProvider(ldap_settings)

    assert provider._group_members_many([]) == {}
    assert _FakeConnection.instances == []


def test_bulk_member_lookup_reads_both_member_spellings(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    """``member`` holds DNs, ``memberUid`` holds bare names; directories differ."""
    fake_ldap.handler = _entries_for(
        {
            "cn": "lab-a",
            "member": [USER_DN],
            "memberUid": ["lisi"],
        }
    )
    provider = LDAPProvider(ldap_settings)

    members = provider._group_members_many(["lab-a"])

    assert members["lab-a"] == ["lisi", "zhangsan"]


def test_bulk_member_lookup_reports_missing_group_as_empty(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    fake_ldap.handler = lambda call: []
    provider = LDAPProvider(ldap_settings)

    assert provider._group_members_many(["ghost"]) == {"ghost": []}


def test_bulk_member_lookup_accepts_cn_prefixed_dns(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    """``cn=`` DNs are accepted alongside ``uid=``.

    Nested groups appear in ``member`` as ``cn=<group>,...``. Such a value yields
    a name that matches no user, and the reconcile reports it as an unknown
    member rather than failing — so tolerating it costs nothing.
    """
    fake_ldap.handler = _entries_for(
        {"member": ["cn=group-of-groups,dc=example,dc=edu"], "memberUid": []}
    )
    provider = LDAPProvider(ldap_settings)

    assert provider._group_members_many(["lab-a"]) == {"lab-a": ["group-of-groups"]}


def test_bulk_member_lookup_ignores_a_dn_with_no_leading_attribute(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    fake_ldap.handler = _entries_for({"member": ["dc=example,dc=edu"], "memberUid": []})
    provider = LDAPProvider(ldap_settings)

    assert provider._group_members_many(["lab-a"]) == {"lab-a": []}


# --- unconfigured -------------------------------------------------------------
async def test_bulk_lookup_returns_none_when_not_configured(
    settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    settings.ldap_uri = ""
    provider = LDAPProvider(settings)

    assert await provider.directory_group_members_many(["lab-a"]) is None


async def test_single_lookup_returns_none_when_not_configured(
    settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    settings.ldap_uri = ""
    provider = LDAPProvider(settings)

    assert await provider.directory_group_members("lab-a") is None


async def test_bulk_lookup_returns_members_when_configured(
    ldap_settings: Settings, fake_ldap: type[_FakeConnection]
) -> None:
    fake_ldap.handler = _entries_for({"memberUid": ["lisi"]})
    provider = LDAPProvider(ldap_settings)

    assert await provider.directory_group_members_many(["lab-a"]) == {"lab-a": ["lisi"]}
