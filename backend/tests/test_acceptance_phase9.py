"""System information endpoint.

The endpoint is small; most of what is worth pinning down is *what it must not
say*. An administration page that summarises a deployment's configuration is the
single most likely place for a credential to escape, and it fails silently: a
leaked secret renders as a perfectly ordinary row and nobody notices.

So the load-bearing tests here are the negative ones:

* nothing anywhere in the payload matches a configured secret, a password, a
  connection string or the JWT key;
* a normal user, and a group administrator, cannot read it at all — even though
  a group administrator *can* read the audit trail;
* the warnings fire for the misconfigurations they claim to detect, and every one
  of them names the setting that fixes it.

The last point is what makes the page worth having. A warning list that silently
stops reporting is worse than no page at all, because it certifies a broken
deployment as healthy.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from tests import factories

PASSWORD = "phase9-password"
SYSTEM = "/api/v1/system/info"
TOKENS = "/api/v1/api-tokens"

#: Values that must never appear in the response, however the payload is shaped.
#: These are the settings a naive implementation would leak by dumping
#: `Settings.model_dump()`.
CANARIES = {
    "ldap_password": "canary-ldap-bind-password",
    "local_admin_password": "canary-local-admin-password",
    "harbor_secret": "canary-harbor-robot-secret",
}


async def act_as(client: AsyncClient, username: str) -> AsyncClient:
    """Re-point an existing client at another account."""
    client.headers.pop("Authorization", None)
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['accessToken']}"
    return client


@pytest.fixture
async def canary_settings(settings: Settings) -> Settings:
    """Point every secret setting at a value that is easy to search for.

    The point is not that these values are realistic — it is that a leak becomes
    a substring match instead of a judgement call.
    """
    settings.ldap_bind_password = SecretStr(CANARIES["ldap_password"])
    settings.local_admin_password = SecretStr(CANARIES["local_admin_password"])
    settings.harbor_robot_secret = SecretStr(CANARIES["harbor_secret"])
    settings.ldap_uri = "ldaps://ipa.example.edu"
    settings.harbor_url = "https://harbor.example.edu"
    settings.grafana_public_url = "/grafana"
    settings.prometheus_url = "http://prometheus:9090"
    return settings


async def _make_user(session: AsyncSession, username: str, role: str) -> Any:
    user = await factories.create_user(session, username=username, password=PASSWORD, role=role)
    await session.commit()
    return user


# ---------------------------------------------------------------------------
# Who may read it
# ---------------------------------------------------------------------------


async def test_a_global_admin_can_read_it(
    admin_client: AsyncClient, canary_settings: Settings
) -> None:
    response = await admin_client.get(SYSTEM)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["appName"] == "kube-wl-ai"
    assert payload["environment"] == canary_settings.app_env


async def test_a_plain_user_cannot_read_it(
    client: AsyncClient, admin: Any, seeded: AsyncSession, canary_settings: Settings
) -> None:
    await _make_user(seeded, "dana", "user")

    await act_as(client, "dana")
    response = await client.get(SYSTEM)
    assert response.status_code == 403, response.text


async def test_a_group_admin_cannot_read_it(
    client: AsyncClient, admin: Any, seeded: AsyncSession, canary_settings: Settings
) -> None:
    """
    Deliberately narrower than the audit trail.

    A group administrator may read their own group's audit rows, because those
    describe people they administer. This page describes the *deployment* — which
    integrations are unconfigured, whether rate limiting is off — and that is not
    a group-scoped fact. Holding `audit:read` must not carry `system:config`.
    """
    group = await factories.create_group(seeded, name="nlp")
    user = await factories.create_user(
        seeded, username="carol", password=PASSWORD, role="group_admin"
    )
    await factories.add_to_group(seeded, user, group)
    await seeded.commit()

    await act_as(client, "carol")
    response = await client.get(SYSTEM)
    assert response.status_code == 403, response.text


async def test_it_requires_authentication(client: AsyncClient, seeded: AsyncSession) -> None:
    response = await client.get(SYSTEM)
    assert response.status_code == 401, response.text


async def test_a_token_scoped_away_from_system_config_cannot_read_it(
    client: AsyncClient, admin: Any, seeded: AsyncSession, canary_settings: Settings
) -> None:
    """
    An API token inherits its owner's roles, so this is really a statement about
    scopes: a token narrowed away from `system:config` cannot reach the page even
    though its owner can.

    Minted through the API rather than the factory, because the factory returns
    the row and never the plaintext — by design, since the secret exists in
    exactly one response and nowhere else.
    """
    await _make_user(seeded, "dana", "global_admin")

    await act_as(client, "dana")
    issued = await client.post(
        TOKENS, json={"name": "narrow", "scopes": ["user:read"], "expiresInDays": 1}
    )
    assert issued.status_code == 201, issued.text

    client.headers["Authorization"] = f"Bearer {issued.json()['token']}"
    response = await client.get(SYSTEM)
    assert response.status_code == 403, response.text


async def test_a_token_that_keeps_system_config_can_read_it(
    client: AsyncClient, admin: Any, seeded: AsyncSession, canary_settings: Settings
) -> None:
    """The converse, so the test above cannot pass for the wrong reason."""
    await _make_user(seeded, "dana", "global_admin")

    await act_as(client, "dana")
    issued = await client.post(
        TOKENS, json={"name": "wide", "scopes": ["system:config"], "expiresInDays": 1}
    )
    assert issued.status_code == 201, issued.text

    client.headers["Authorization"] = f"Bearer {issued.json()['token']}"
    response = await client.get(SYSTEM)
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# What it must not say
# ---------------------------------------------------------------------------


async def test_no_secret_value_appears_anywhere(
    admin_client: AsyncClient, canary_settings: Settings, settings: Settings
) -> None:
    """
    The whole payload, searched for every canary.

    Asserted against the serialised body rather than against named fields: a
    field-by-field check only covers the leak somebody thought of, and the
    interesting failure is a value that arrives somewhere new — nested inside a
    feature's `detail`, or in a warning's `remedy`, where a reviewer skimming a
    table of integration status would never look.
    """
    response = await admin_client.get(SYSTEM)
    assert response.status_code == 200, response.text
    body = response.text

    # The live signing key, not a substituted one — so this covers the value the
    # running process would actually have to disclose to be exploitable.
    canaries = {**CANARIES, "secret_key": settings.secret_key.get_secret_value()}
    for name, value in canaries.items():
        assert value not in body, f"{name} leaked into the system payload"


async def test_the_connection_string_is_not_reported(
    admin_client: AsyncClient, canary_settings: Settings
) -> None:
    """
    The database URL is the most tempting thing to print and the worst.

    It carries the password, and a page titled "system settings" is exactly where
    someone would expect to find it. The page reports counts instead; this
    asserts that no host, port or scheme from the URL survives either.
    """
    body = (await admin_client.get(SYSTEM)).text
    # The integration *service* URLs are reported on purpose — a Prometheus or
    # Harbor address is not a credential, and an administrator needs to see it.
    # The database DSN is the one that must never appear, because it carries a
    # password, so the check is for its parts rather than for "://" at large.
    for fragment in ("aiosqlite", "sqlite", "asyncpg", "postgresql", "test.db"):
        assert fragment not in body, fragment


async def test_key_material_is_not_reported(
    admin_client: AsyncClient, canary_settings: Settings
) -> None:
    body = (await admin_client.get(SYSTEM)).text
    assert "BEGIN" not in body  # any PEM block


async def test_the_settings_object_is_not_serialised_wholesale(
    admin_client: AsyncClient, canary_settings: Settings
) -> None:
    """
    The structural guarantee, stated as a test.

    The rule is that `Settings` is never dumped and every reported value is
    copied across by hand, so a newly-added secret setting cannot reach this page
    merely by existing. That is enforced by construction rather than by review,
    and this asserts the construction held: no top-level key matches a settings
    field name that is not one of the ones deliberately reported.
    """
    payload = (await admin_client.get(SYSTEM)).json()
    reported = {
        "appName",
        "appVersion",
        "environment",
        "debug",
        "authProviders",
        "features",
        "warnings",
        "accessTokenMinutes",
        "refreshTokenDays",
        "loginRateLimitEnabled",
        "loginRateLimitWindowSeconds",
        "loginRateLimitAttemptsPerIp",
        "loginRateLimitAttemptsPerUsername",
        "counts",
        "sshMode",
        "sshHost",
        "storageClasses",
    }
    assert set(payload) == reported


# ---------------------------------------------------------------------------
# The warning list
# ---------------------------------------------------------------------------


async def test_a_healthy_deployment_reports_nothing_critical(
    admin_client: AsyncClient, settings: Settings
) -> None:
    """With everything configured, nothing may be raised as critical."""
    settings.app_env = "development"
    settings.debug = False
    settings.k8s_enabled = False  # warned about, but only as a warning
    settings.ldap_uri = "ldaps://ipa.example.edu"
    settings.local_admin_password = SecretStr("a-strong-password")
    settings.instance_ssh_host = "ssh.example.edu"
    settings.storage_class_home = "ceph-rbd"
    settings.storage_class_data = "ceph-rbd"
    settings.storage_class_share = "cephfs"

    warnings = (await admin_client.get(SYSTEM)).json()["warnings"]
    critical = [w for w in warnings if w["severity"] == "critical"]
    assert critical == [], critical


async def test_debug_in_production_is_critical(
    admin_client: AsyncClient, settings: Settings
) -> None:
    settings.app_env = "production"
    settings.debug = True

    codes = {w["code"]: w for w in (await admin_client.get(SYSTEM)).json()["warnings"]}
    assert "debug_in_production" in codes
    assert codes["debug_in_production"]["severity"] == "critical"


async def test_an_unused_storage_class_setting_is_information_not_a_warning(
    admin_client: AsyncClient, settings: Settings
) -> None:
    """`STORAGE_CLASS_*` no longer decides anything, and the page must say so.

    It used to be a *warning* naming whichever of the three was unset — accurate
    while instances and tasks each created their own claim. Workloads have no disk of
    their own any more, and managed volumes are static ``hostPath`` PVs that never
    consult a StorageClass, so nothing can fail because these are empty. A warning
    here would be a lie, and an operator chasing it would find nothing.

    They are still reported — as information — because an operator who sets them
    deserves to know they currently have no effect.
    """
    settings.storage_class_home = "ceph-rbd"
    settings.storage_class_data = ""
    settings.storage_class_share = "cephfs"

    warnings = (await admin_client.get(SYSTEM)).json()["warnings"]
    entry = next(w for w in warnings if w["code"] == "storage_classes_unused")

    assert entry["severity"] == "info"
    # The unset one is still named: "these do nothing" is more useful with the
    # specific setting than without it.
    assert "STORAGE_CLASS_DATA" in entry["detail"]
    assert "STORAGE_CLASS_HOME" not in entry["detail"]
    assert "STORAGE_CLASS_SHARE" not in entry["detail"]
    # And the old warning must be gone, not merely joined by a new entry.
    assert not [w for w in warnings if w["code"] == "storage_class_missing"]


async def test_ldap_enabled_without_a_uri_is_critical(
    admin_client: AsyncClient, settings: Settings
) -> None:
    """
    The failure this catches is silent.

    `AUTH_PROVIDERS=ldap` with no `LDAP_URI` makes every directory login fail
    with an authentication error, which reads as "wrong password" rather than
    "the deployment is misconfigured".
    """
    settings.auth_providers = ["ldap", "local"]
    settings.ldap_uri = ""

    codes = {w["code"]: w for w in (await admin_client.get(SYSTEM)).json()["warnings"]}
    assert codes["ldap_without_uri"]["severity"] == "critical"


async def test_ldap_without_a_service_account_is_reported(
    admin_client: AsyncClient, settings: Settings
) -> None:
    """
    The more insidious half of the same misconfiguration.

    A reachable directory with no ``LDAP_BIND_DN`` still lets people log in,
    because ldap3 quietly binds anonymously. What they lose is everything else —
    email, real name and group membership — so they land on the default role with
    no explanation, while the deployment looks healthy from the outside.
    """
    settings.auth_providers = ["ldap", "local"]
    settings.ldap_uri = "ldaps://ipa.example.edu"
    settings.ldap_base_dn = "dc=example,dc=edu"
    settings.ldap_bind_dn = ""

    codes = {w["code"]: w for w in (await admin_client.get(SYSTEM)).json()["warnings"]}
    assert codes["ldap_without_service_account"]["severity"] == "warning"
    assert "LDAP_BIND_DN" in codes["ldap_without_service_account"]["remedy"]


async def test_ldap_with_a_service_account_is_not_reported(
    admin_client: AsyncClient, settings: Settings
) -> None:
    settings.auth_providers = ["ldap", "local"]
    settings.ldap_uri = "ldaps://ipa.example.edu"
    settings.ldap_base_dn = "dc=example,dc=edu"
    settings.ldap_bind_dn = "uid=svc,cn=users,cn=accounts,dc=example,dc=edu"
    settings.ldap_bind_password = SecretStr("service-secret")

    codes = {w["code"] for w in (await admin_client.get(SYSTEM)).json()["warnings"]}
    assert "ldap_without_service_account" not in codes


async def test_a_disabled_rate_limiter_is_reported(
    admin_client: AsyncClient, settings: Settings
) -> None:
    settings.login_rate_limit_enabled = False

    codes = {w["code"] for w in (await admin_client.get(SYSTEM)).json()["warnings"]}
    assert "rate_limit_disabled" in codes


async def test_a_missing_ssh_host_is_reported(
    admin_client: AsyncClient, settings: Settings
) -> None:
    """
    Worth its own check because the default works on a single-node cluster.

    `INSTANCE_SSH_HOST` empty falls back to the node's own hostname, so this
    misconfiguration is invisible until the cluster grows — which is the worst
    possible time to discover it.
    """
    settings.instance_ssh_host = ""

    codes = {w["code"] for w in (await admin_client.get(SYSTEM)).json()["warnings"]}
    assert "ssh_host_missing" in codes


async def test_every_warning_carries_a_remedy(
    admin_client: AsyncClient, settings: Settings
) -> None:
    """
    A warning with an empty remedy is a dead end.

    Checked across the whole list from a deliberately broken configuration rather
    than for the checks this file happens to exercise: it holds for any warning
    added later, which is the point.
    """
    settings.app_env = "production"
    settings.debug = True
    settings.k8s_enabled = False
    settings.login_rate_limit_enabled = False
    settings.ldap_uri = ""
    settings.storage_class_home = ""
    settings.storage_class_data = ""
    settings.storage_class_share = ""
    settings.instance_ssh_host = ""
    settings.grafana_public_url = ""
    settings.prometheus_url = "http://prometheus:9090"

    warnings = (await admin_client.get(SYSTEM)).json()["warnings"]
    assert warnings, "expected the deliberately broken settings to raise something"
    for warning in warnings:
        assert warning["remedy"].strip(), warning
        assert warning["detail"].strip(), warning
        assert warning["title"].strip(), warning


# ---------------------------------------------------------------------------
# Counts, and the version
# ---------------------------------------------------------------------------


async def test_counts_reflect_the_database(
    admin_client: AsyncClient, seeded: AsyncSession, settings: Settings
) -> None:
    group = await factories.create_group(seeded, name="nlp")
    user = await factories.create_user(seeded, username="dana", password=PASSWORD, role="user")
    await factories.add_to_group(seeded, user, group)
    await seeded.commit()

    counts = (await admin_client.get(SYSTEM)).json()["counts"]
    # `admin` plus `dana`.
    assert counts["users"] == 2
    assert counts["activeUsers"] == 2
    assert counts["groups"] == 1


async def test_the_version_matches_the_health_probe(
    admin_client: AsyncClient, settings: Settings
) -> None:
    """
    One version, reported in two places.

    These were separate literals until this endpoint was added, which is how a
    build ends up claiming two versions. The assertion is trivial; what it
    protects is the single definition behind it.
    """
    health = await admin_client.get("/healthz")
    system = await admin_client.get(SYSTEM)
    assert health.status_code == 200, health.text
    assert health.json()["version"] == system.json()["appVersion"]


def test_the_version_looks_like_a_version() -> None:
    from app.core.version import VERSION

    assert VERSION.count(".") == 2
