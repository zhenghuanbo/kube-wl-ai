"""End-to-end tests for the /auth API surface (Phase 1)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.cookies import REFRESH_COOKIE_NAME
from app.auth.permissions import ALL_PERMISSION_CODES
from app.auth.tokens import hash_token
from app.models.enums import GroupRole
from app.models.token import RefreshToken
from app.models.user import User
from tests import factories

LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"
PERMISSIONS = "/api/v1/auth/permissions"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
LOGIN_OPTIONS = "/api/v1/auth/login-options"


async def login(client: AsyncClient, username: str, password: str) -> str:
    """Log in and attach the access token, as the browser client does.

    The access token is only ever returned in the body — it is deliberately not
    a cookie — so every authenticated call has to carry it explicitly.
    """
    response = await client.post(LOGIN, json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    token: str = response.json()["accessToken"]
    client.headers["Authorization"] = f"Bearer {token}"
    return token


async def test_login_returns_token_and_sets_refresh_cookie(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    await factories.create_user(
        seeded, username="alice", password="correct-horse-battery", role="user"
    )
    await seeded.commit()

    response = await client.post(
        LOGIN, json={"username": "alice", "password": "correct-horse-battery"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tokenType"] == "bearer"
    assert body["expiresIn"] > 0
    assert body["user"]["username"] == "alice"
    assert body["user"]["displayName"] == "Alice"
    assert body["user"]["roles"] == ["user"]

    # The refresh token must only ever travel as an httpOnly cookie.
    assert REFRESH_COOKIE_NAME in response.cookies
    assert "refreshToken" not in body
    assert "refresh_token" not in body


async def test_login_response_uses_camel_case(client: AsyncClient, seeded: AsyncSession) -> None:
    await factories.create_user(seeded, username="alice", password="correct-horse-battery")
    await seeded.commit()

    body = (
        await client.post(LOGIN, json={"username": "alice", "password": "correct-horse-battery"})
    ).json()

    assert "accessToken" in body
    assert "access_token" not in body
    assert "fullName" in body["user"]
    assert "roleAssignments" in body["user"]


async def test_login_accepts_snake_case_body(client: AsyncClient, seeded: AsyncSession) -> None:
    """Scripts and curl should not have to care about the alias spelling."""
    await factories.create_user(seeded, username="alice", password="correct-horse-battery")
    await seeded.commit()

    response = await client.post(
        LOGIN, json={"username": "alice", "password": "correct-horse-battery"}
    )
    assert response.status_code == 200


async def test_login_with_wrong_password_returns_envelope(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    await factories.create_user(seeded, username="alice", password="correct-horse-battery")
    await seeded.commit()

    response = await client.post(LOGIN, json={"username": "alice", "password": "nope"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_login_with_disabled_account_is_403(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    await factories.create_user(
        seeded, username="alice", password="correct-horse-battery", is_active=False
    )
    await seeded.commit()

    response = await client.post(
        LOGIN, json={"username": "alice", "password": "correct-horse-battery"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_disabled"


async def test_failed_login_is_audited(
    client: AsyncClient, seeded: AsyncSession, audit_rows
) -> None:
    await factories.create_user(seeded, username="alice", password="correct-horse-battery")
    await seeded.commit()

    await client.post(LOGIN, json={"username": "alice", "password": "wrong"})

    entries = await audit_rows()
    denied = [entry for entry in entries if entry.status == "denied"]
    assert denied, f"expected a denied audit row, got {[e.action for e in entries]}"
    assert denied[0].actor_username == "anonymous"
    assert denied[0].ip_address is not None


async def test_successful_login_is_audited_once(
    client: AsyncClient, seeded: AsyncSession, audit_rows
) -> None:
    await factories.create_user(seeded, username="alice", password="correct-horse-battery")
    await seeded.commit()

    await client.post(LOGIN, json={"username": "alice", "password": "correct-horse-battery"})

    entries = await audit_rows(action="auth:login")
    assert len(entries) == 1, "explicit audit must suppress the middleware's generic row"
    assert entries[0].actor_username == "alice"
    assert entries[0].status == "success"

    # The caller must have been issued a refresh token.
    tokens = await audit_rows()
    assert all(entry.action != "auth:create" for entry in tokens)


# --- /me and /permissions ----------------------------------------------------
async def test_me_requires_authentication(client: AsyncClient) -> None:
    response = await client.get(ME)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_token"


async def test_me_rejects_garbage_token(client: AsyncClient) -> None:
    response = await client.get(ME, headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_me_returns_current_user(admin_client: AsyncClient) -> None:
    response = await admin_client.get(ME)

    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "admin"
    assert body["isSuperuser"] is True
    assert body["roles"] == ["global_admin"]
    assert body["workspace"] is None, "workspaces arrive in Phase 2"


async def test_permissions_for_global_admin(admin_client: AsyncClient) -> None:
    body = (await admin_client.get(PERMISSIONS)).json()

    assert set(body["permissions"]) == set(ALL_PERMISSION_CODES)
    assert body["isGlobalAdmin"] is True
    assert body["scope"]["kind"] == "all"
    assert body["scope"]["allAccess"] is True


async def test_permissions_for_plain_user(client: AsyncClient, seeded: AsyncSession) -> None:
    await factories.create_user(
        seeded, username="bob", password="correct-horse-battery", role="user"
    )
    await seeded.commit()
    await login(client, "bob", "correct-horse-battery")

    body = (await client.get(PERMISSIONS)).json()

    assert "instance:create" in body["permissions"]
    assert "user:create" not in body["permissions"]
    assert body["scope"]["kind"] == "self"
    assert body["scope"]["groupIds"] == []


async def test_permissions_for_group_admin_carries_group_scope(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    group = await factories.create_group(seeded, name="lab-a")
    await factories.create_user(
        seeded,
        username="carol",
        password="correct-horse-battery",
        role="group_admin",
        group=group,
        group_role=GroupRole.GROUP_ADMIN,
    )
    await seeded.commit()
    await login(client, "carol", "correct-horse-battery")

    body = (await client.get(PERMISSIONS)).json()

    assert body["scope"]["kind"] == "group"
    assert body["scope"]["groupIds"] == [group.id]
    assert body["roles"] == ["group_admin"]


async def test_deactivating_a_user_invalidates_their_token_immediately(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    user = await factories.create_user(
        seeded, username="bob", password="correct-horse-battery", role="user"
    )
    await seeded.commit()
    token = (
        await client.post(LOGIN, json={"username": "bob", "password": "correct-horse-battery"})
    ).json()["accessToken"]

    user.is_active = False
    await seeded.commit()

    response = await client.get(ME, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_disabled"


# --- refresh -----------------------------------------------------------------
async def test_refresh_rotates_the_cookie(client: AsyncClient, seeded: AsyncSession) -> None:
    await factories.create_user(seeded, username="alice", password="correct-horse-battery")
    await seeded.commit()
    await client.post(LOGIN, json={"username": "alice", "password": "correct-horse-battery"})
    first = client.cookies[REFRESH_COOKIE_NAME]

    response = await client.post(REFRESH)

    assert response.status_code == 200, response.text
    assert client.cookies[REFRESH_COOKIE_NAME] != first
    assert response.json()["accessToken"]


async def test_refresh_without_cookie_is_rejected(client: AsyncClient) -> None:
    response = await client.post(REFRESH)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_refresh_token"


async def test_replaying_a_rotated_refresh_token_revokes_the_family(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    user = await factories.create_user(seeded, username="alice", password="correct-horse-battery")
    await seeded.commit()
    await client.post(LOGIN, json={"username": "alice", "password": "correct-horse-battery"})
    stolen = client.cookies[REFRESH_COOKIE_NAME]
    await client.post(REFRESH)

    # An attacker replays the token the browser already rotated away from.
    response = await client.post(REFRESH, json={"refreshToken": stolen})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "refresh_token_reused"

    # ... which must also kill the legitimate session.
    assert (await client.post(REFRESH)).status_code == 401

    stored_tokens = (
        await seeded.scalars(select(RefreshToken).where(RefreshToken.user_id == user.id))
    ).all()
    assert all(token.revoked_at is not None for token in stored_tokens), (
        "replaying a rotated token must revoke every token in the family"
    )


async def test_refresh_accepts_token_in_body_for_non_browser_clients(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    """A CLI cannot use cookies, so the body is an accepted alternative."""
    await factories.create_user(seeded, username="alice", password="correct-horse-battery")
    await seeded.commit()
    login = await client.post(
        LOGIN, json={"username": "alice", "password": "correct-horse-battery"}
    )
    raw = login.cookies[REFRESH_COOKIE_NAME]
    client.cookies.clear()

    response = await client.post(REFRESH, json={"refresh_token": raw})

    assert response.status_code == 200
    assert response.json()["accessToken"]


# --- logout ------------------------------------------------------------------
async def test_logout_clears_cookie_and_revokes_token(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    await factories.create_user(seeded, username="alice", password="correct-horse-battery")
    await seeded.commit()
    login = await client.post(
        LOGIN, json={"username": "alice", "password": "correct-horse-battery"}
    )
    raw = login.cookies[REFRESH_COOKIE_NAME]

    response = await client.post(LOGOUT)

    assert response.status_code == 200
    assert response.json()["message"] == "已退出登录"

    # The token must be unusable even if the cookie is replayed by hand.
    replay = await client.post(REFRESH, json={"refreshToken": raw})
    assert replay.status_code == 401


async def test_logout_without_a_session_is_harmless(client: AsyncClient) -> None:
    response = await client.post(LOGOUT)

    assert response.status_code == 200


async def test_logout_works_with_an_expired_access_token(
    client: AsyncClient, seeded: AsyncSession
) -> None:
    """Logging out must not require a still-valid access token."""
    await factories.create_user(seeded, username="alice", password="correct-horse-battery")
    await seeded.commit()
    await client.post(LOGIN, json={"username": "alice", "password": "correct-horse-battery"})

    response = await client.post(LOGOUT, headers={"Authorization": "Bearer expired.garbage.token"})

    assert response.status_code == 200


# --- login options and JWKS --------------------------------------------------
async def test_login_options_lists_local_provider(client: AsyncClient) -> None:
    body = (await client.get(LOGIN_OPTIONS)).json()

    assert [provider["name"] for provider in body["providers"]] == ["local"]
    assert body["ldapEnabled"] is False
    assert body["oidcEnabled"] is False


async def test_login_options_is_public(client: AsyncClient) -> None:
    assert (await client.get(LOGIN_OPTIONS)).status_code == 200


async def test_jwks_endpoint_is_served_at_the_root(client: AsyncClient) -> None:
    response = await client.get("/.well-known/jwks.json")

    assert response.status_code == 200
    assert "keys" in response.json()


async def test_unknown_route_returns_the_error_envelope(client: AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_request_id_header_is_returned(client: AsyncClient) -> None:
    response = await client.get(LOGIN_OPTIONS)

    assert response.headers.get("X-Request-ID")


@pytest.mark.parametrize("path", ["/api/v1/auth/me", "/api/v1/auth/permissions"])
async def test_protected_endpoints_reject_missing_token(client: AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 401


async def test_refresh_token_is_stored_hashed_only(
    client: AsyncClient, seeded: AsyncSession, reload_from_db
) -> None:
    user = await factories.create_user(
        seeded, username="alice", password="correct-horse-battery", role="user"
    )
    await seeded.commit()
    login = await client.post(
        LOGIN, json={"username": "alice", "password": "correct-horse-battery"}
    )
    raw = login.cookies[REFRESH_COOKIE_NAME]

    assert hash_token(raw) != raw
    stored = await seeded.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw))
    )
    assert stored is not None
    assert stored.token_hash != raw

    reloaded = await reload_from_db(User, user.id)
    assert reloaded.password_hash is not None
    assert raw not in reloaded.password_hash
