"""Phase 8 acceptance: the audit trail, personal access tokens, and login limits.

Three things that share one theme — *a credential, and what it is allowed to
prove afterwards*.

The audit trail is the record. Personal access tokens are a second way to
authenticate, and the interesting question about them is not whether they work
but whether they can be made to work as somebody more privileged than their
owner. Login rate limiting is the control that keeps the record from being
nothing but an attacker's shopping list.

The tests that carry the most weight:

* a token whose scopes were narrowed cannot mint a successor with wider scopes;
* a token's scopes are resolved against the owner's *current* roles, so removing
  a role narrows the token on the next request rather than whenever it expires;
* the secret appears in exactly one response and nowhere in the audit trail;
* anonymous audit rows — failed logins, probes — are visible to a global
  administrator and to nobody else.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.auth.tokens import hash_token
from app.core.config import Settings
from app.models.enums import GroupRole
from app.models.token import ApiToken
from app.models.user import User
from tests import factories

PASSWORD = "phase8-password"
AUDIT = "/api/v1/audit-logs"
TOKENS = "/api/v1/api-tokens"


async def act_as(client: AsyncClient, username: str) -> AsyncClient:
    client.headers.pop("Authorization", None)
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['accessToken']}"
    return client


def rows_of(page: Any) -> list[dict[str, Any]]:
    return list(page["items"]) if isinstance(page, dict) else []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class World:
    lab_a: Any
    lab_b: Any
    user_a: User
    user_b: User
    boss_a: User
    observer: User
    admin: User


async def build_world(session: AsyncSession) -> World:
    lab_a = await factories.create_group(session, name="lab-a", display_name="甲实验室")
    lab_b = await factories.create_group(session, name="lab-b", display_name="乙实验室")

    user_a = await factories.create_user(
        session, username="dana", password=PASSWORD, role="user", group=lab_a
    )
    user_b = await factories.create_user(
        session, username="erin", password=PASSWORD, role="user", group=lab_b
    )
    boss_a = await factories.create_user(
        session,
        username="boss-a",
        password=PASSWORD,
        role="group_admin",
        group=lab_a,
        group_role=GroupRole.GROUP_ADMIN,
    )
    observer = await factories.create_user(
        session, username="gazer", password=PASSWORD, role="observer", group=lab_a
    )
    admin = await factories.create_user(
        session, username="root", password=PASSWORD, role="global_admin", is_superuser=True
    )
    await session.commit()

    world = World()
    world.lab_a, world.lab_b = lab_a, lab_b
    world.user_a, world.user_b = user_a, user_b
    world.boss_a, world.observer, world.admin = boss_a, observer, admin
    return world


@pytest.fixture
async def world(seeded: AsyncSession) -> World:
    return await build_world(seeded)


async def seed_audit(
    session: AsyncSession, actor: User, action: str, status: str = "success"
) -> None:
    from app.core.audit import record_audit

    record_audit(session, action=action, actor=actor, status=status, object_type="instance")
    await session.commit()


# ---------------------------------------------------------------------------
# The audit trail — who may read it
# ---------------------------------------------------------------------------
async def test_the_trail_requires_a_permission(client: AsyncClient, world: World) -> None:
    await act_as(client, "dana")

    assert (await client.get(AUDIT)).status_code == 403


async def test_the_trail_requires_authentication(client: AsyncClient, world: World) -> None:
    assert (await client.get(AUDIT)).status_code == 401


async def test_an_observer_cannot_read_the_trail(client: AsyncClient, world: World) -> None:
    """Read-only access to *workloads* is not access to the security record."""
    await act_as(client, "gazer")

    assert (await client.get(AUDIT)).status_code == 403


async def test_a_group_admin_sees_their_own_group(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    await seed_audit(seeded, world.user_a, "instance:create")
    await seed_audit(seeded, world.user_b, "instance:create")

    await act_as(client, "boss-a")
    actors = {row["actorUsername"] for row in rows_of((await client.get(AUDIT)).json())}

    assert "dana" in actors
    assert "erin" not in actors


async def test_a_global_admin_sees_everyone(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    await seed_audit(seeded, world.user_a, "instance:create")
    await seed_audit(seeded, world.user_b, "instance:create")

    await act_as(client, "root")
    actors = {row["actorUsername"] for row in rows_of((await client.get(AUDIT)).json())}

    assert {"dana", "erin"} <= actors


async def test_failed_logins_are_visible_only_to_a_global_admin(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    """
    A failed login has no *verified* actor, so it belongs to no group.

    The username on such a row is the claimant's, not a proven identity — it is
    whatever string was typed, and it is therefore attacker-controlled. Keying a
    group's scope on it would let anyone place a row in any group's history by
    guessing a name, so the row matches nobody and only a global administrator
    sees it.

    The cost of that choice is real and worth naming: a group administrator
    cannot see attempts against their own members, and must ask the global
    administrator for them. The alternative is worse — a scope rule that an
    unauthenticated stranger can write into.
    """
    from app.core.audit import record_audit

    # A genuine action by dana: the group admin *does* see this.
    record_audit(seeded, action="instance:create", actor=world.user_a)
    await seeded.commit()

    # A failed login naming dana. Nobody authenticated, so no actor id.
    record_audit(
        seeded,
        action="auth:login",
        status="denied",
        actor_username="dana",
        detail={"username": "dana", "provider": "failed"},
    )
    await seeded.commit()

    await act_as(client, "boss-a")
    boss = rows_of((await client.get(f"{AUDIT}?action=instance")).json())
    # `status=denied` narrows to the failed attempts; the boss's own successful
    # login is legitimately visible to them and is not what this is about.
    boss_denials = rows_of((await client.get(f"{AUDIT}?action=auth:login&status=denied")).json())

    assert "dana" in {row["actorUsername"] for row in boss}
    assert boss_denials == []

    await act_as(client, "root")
    admin_denials = rows_of((await client.get(f"{AUDIT}?action=auth:login&status=denied")).json())
    assert "dana" in {row["actorUsername"] for row in admin_denials}


async def test_the_trail_cannot_be_written_through_the_api(
    client: AsyncClient, world: World
) -> None:
    """There is no create, no update, and no delete. There is not meant to be."""
    await act_as(client, "root")

    # Either "no such method on this path" or "no such path"; both mean the
    # trail is not writable through the API, which is the property under test.
    for response in (
        await client.post(AUDIT, json={"action": "forged"}),
        await client.delete(f"{AUDIT}/1"),
        await client.patch(f"{AUDIT}/1", json={}),
        await client.put(f"{AUDIT}/1", json={}),
    ):
        assert response.status_code in (404, 405), response.status_code
        assert response.status_code >= 400


# ---------------------------------------------------------------------------
# The audit trail — reading it
# ---------------------------------------------------------------------------
async def test_filters_narrow_the_result(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    await seed_audit(seeded, world.user_a, "instance:create")
    await seed_audit(seeded, world.user_a, "instance:delete")
    await seed_audit(seeded, world.user_a, "storage:create")

    await act_as(client, "root")

    by_prefix = rows_of((await client.get(f"{AUDIT}?action=instance")).json())
    assert {row["action"] for row in by_prefix} == {"instance:create", "instance:delete"}

    by_status = rows_of((await client.get(f"{AUDIT}?status=denied")).json())
    assert by_status == []


async def test_search_spans_the_useful_columns(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    await seed_audit(seeded, world.user_a, "instance:create")

    await act_as(client, "root")

    assert rows_of((await client.get(f"{AUDIT}?search=dana")).json())
    assert rows_of((await client.get(f"{AUDIT}?search=instance:create")).json())
    assert rows_of((await client.get(f"{AUDIT}?search=nothing-matches-this")).json()) == []


async def test_the_trail_is_newest_first(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    """A record whose most interesting row is always the one just written."""
    for action in ("instance:create", "instance:start", "instance:stop"):
        await seed_audit(seeded, world.user_a, action)

    # Filtered to the seeded actor: signing in is itself audited, and those rows
    # are legitimately newer than anything the fixture wrote.
    await act_as(client, "root")
    actions = [
        row["action"] for row in rows_of((await client.get(f"{AUDIT}?actorUsername=dana")).json())
    ]

    assert actions[0] == "instance:stop"


async def test_the_request_id_filter_finds_one_exchange(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    """
    The filter that makes this a diagnostic screen rather than a browsing one:
    the same id appears on every log line the application emitted for a request,
    so one value ties the audit row to the trace.
    """
    from app.core.audit import record_audit
    from app.models.audit import AuditLog

    record_audit(seeded, action="instance:create", actor=world.user_a)
    record_audit(seeded, action="instance:start", actor=world.user_a)
    await seeded.commit()

    target = await seeded.scalar(select(AuditLog).where(AuditLog.action == "instance:start"))
    assert target is not None
    target.request_id = "trace-me-1234"
    await seeded.commit()

    await act_as(client, "root")
    found = rows_of((await client.get(f"{AUDIT}?requestId=trace-me-1234")).json())

    assert [entry["action"] for entry in found] == ["instance:start"]


async def test_meta_only_offers_values_within_scope(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    """
    Offering an action nobody in the caller's scope has performed would disclose
    that the action exists somewhere else.
    """
    await seed_audit(seeded, world.user_a, "instance:create")
    await seed_audit(seeded, world.user_b, "storage:resize")

    await act_as(client, "boss-a")
    meta = (await client.get(f"{AUDIT}/meta")).json()

    assert "instance:create" in meta["actions"]
    assert "storage:resize" not in meta["actions"]
    assert set(meta["statuses"]) == {"success", "denied", "error"}


async def test_the_export_is_the_same_rows_as_the_listing(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    await seed_audit(seeded, world.user_a, "instance:create")
    await seed_audit(seeded, world.user_b, "instance:create")

    await act_as(client, "boss-a")
    listing = {row["id"] for row in rows_of((await client.get(AUDIT)).json())}

    response = await client.get(f"{AUDIT}/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")

    text = response.text
    assert text.startswith("\ufeff"), "Excel reads Chinese columns as mojibake without a BOM"
    parsed = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    assert {int(row["id"]) for row in parsed} == listing


async def test_the_export_carries_the_same_scope_as_the_listing(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    """
    An export assembled separately from the listing eventually gets assembled
    differently — and on this table the difference is a file of rows somebody
    cannot see on screen.
    """
    await seed_audit(seeded, world.user_a, "instance:create")
    await seed_audit(seeded, world.user_b, "instance:create")

    await act_as(client, "boss-a")
    text = (await client.get(f"{AUDIT}/export")).text

    assert "dana" in text
    assert "erin" not in text


async def test_a_truncated_export_says_so(
    client: AsyncClient, seeded: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A CSV that stops early while claiming a date range is the artefact most
    likely to end up in an incident report unread.
    """
    from app.services import audit as audit_service

    await seed_audit(seeded, world.user_a, "instance:create")
    await seed_audit(seeded, world.user_a, "instance:start")

    monkeypatch.setattr(audit_service, "MAX_EXPORT_ROWS", 1)
    await act_as(client, "root")
    text = (await client.get(f"{AUDIT}/export")).text

    assert "结果被截断" in text


# ---------------------------------------------------------------------------
# Personal access tokens
# ---------------------------------------------------------------------------
async def test_a_token_is_shown_once_and_stored_as_a_digest(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    await act_as(client, "dana")

    created = await client.post(TOKENS, json={"name": "ci"})
    assert created.status_code == 201, created.text
    secret = created.json()["token"]

    assert secret.startswith("wl_")

    row = await seeded.scalar(select(ApiToken).where(ApiToken.user_id == world.user_a.id))
    assert row is not None
    assert row.token_hash == hash_token(secret)
    assert secret not in row.token_hash

    # And it is not recoverable from the listing.
    listed = (await client.get(TOKENS)).json()
    assert all("token" not in item for item in listed)
    assert [item["prefix"] for item in listed] == [secret[:12]]


async def test_a_token_authenticates_as_its_owner(client: AsyncClient, world: World) -> None:
    await act_as(client, "dana")
    secret = (await client.post(TOKENS, json={"name": "ci"})).json()["token"]

    client.headers["Authorization"] = f"Bearer {secret}"
    me = await client.get("/api/v1/auth/me")

    assert me.status_code == 200
    assert me.json()["username"] == "dana"


async def test_a_revoked_token_stops_working_immediately(client: AsyncClient, world: World) -> None:
    await act_as(client, "dana")
    created = (await client.post(TOKENS, json={"name": "ci"})).json()
    secret = created["token"]

    client.headers["Authorization"] = f"Bearer {secret}"
    assert (await client.get("/api/v1/auth/me")).status_code == 200

    # Revoke using the session, then present the token again.
    client.headers.pop("Authorization", None)
    await act_as(client, "dana")
    assert (await client.delete(f"{TOKENS}/{created['id']}")).status_code == 200

    client.headers["Authorization"] = f"Bearer {secret}"
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_a_garbage_token_is_rejected(client: AsyncClient, world: World) -> None:
    client.headers["Authorization"] = "Bearer wl_not-a-real-token"

    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_an_expired_token_is_rejected(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    from datetime import UTC, datetime, timedelta

    await seeded.scalar(select(User).where(User.id == world.user_a.id))
    token = ApiToken(
        user_id=world.user_a.id,
        name="old",
        token_hash=hash_token("wl_expired-token-value"),
        prefix="wl_expired-t",
        scopes="",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    seeded.add(token)
    await seeded.commit()

    client.headers["Authorization"] = "Bearer wl_expired-token-value"
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_the_secret_never_reaches_the_audit_trail(
    client: AsyncClient, world: World, audit_rows: Any
) -> None:
    """
    The trail records that a token was made, and enough to identify it later —
    never the credential itself. An audit table is read by more people than a
    token table is.
    """
    await act_as(client, "dana")
    secret = (await client.post(TOKENS, json={"name": "ci"})).json()["token"]

    rows = await audit_rows(action="api_token:create")
    assert len(rows) == 1
    rendered = json.dumps(rows[0].detail, ensure_ascii=False, default=str)

    assert secret not in rendered
    assert hash_token(secret) not in rendered
    assert rows[0].detail["prefix"] == secret[:12]


async def test_a_token_records_when_it_was_last_used(
    client: AsyncClient, world: World, reload_from_db: Any
) -> None:
    await act_as(client, "dana")
    created = (await client.post(TOKENS, json={"name": "ci"})).json()

    client.headers["Authorization"] = f"Bearer {created['token']}"
    await client.get("/api/v1/auth/me")

    row = await reload_from_db(ApiToken, created["id"])
    assert row.last_used_at is not None


async def test_token_names_are_unique_per_owner(client: AsyncClient, world: World) -> None:
    await act_as(client, "dana")
    assert (await client.post(TOKENS, json={"name": "ci"})).status_code == 201

    duplicate = await client.post(TOKENS, json={"name": "ci"})

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "token_name_taken"


async def test_another_persons_token_is_not_revocable(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    """404, not 403: token ids are sequential and a 403 confirms one exists."""
    await act_as(client, "dana")
    created = (await client.post(TOKENS, json={"name": "ci"})).json()

    await act_as(client, "erin")
    response = await client.delete(f"{TOKENS}/{created['id']}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "token_not_found"
    # And it still works for its owner.
    client.headers["Authorization"] = f"Bearer {created['token']}"
    assert (await client.get("/api/v1/auth/me")).status_code == 200


async def test_one_persons_token_list_is_not_anothers(client: AsyncClient, world: World) -> None:
    await act_as(client, "dana")
    await client.post(TOKENS, json={"name": "dana-ci"})

    await act_as(client, "erin")

    assert (await client.get(TOKENS)).json() == []


# ---------------------------------------------------------------------------
# Tokens cannot exceed their owner
# ---------------------------------------------------------------------------
async def test_a_token_may_not_declare_a_permission_its_owner_lacks(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")

    response = await client.post(TOKENS, json={"name": "sneaky", "scopes": ["user:delete"]})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "token_scope_not_granted"


async def test_a_scoped_token_is_narrower_than_its_owner(client: AsyncClient, world: World) -> None:
    await act_as(client, "dana")
    secret = (
        await client.post(TOKENS, json={"name": "readonly", "scopes": ["instance:read"]})
    ).json()["token"]

    client.headers["Authorization"] = f"Bearer {secret}"

    assert (await client.get("/api/v1/instances")).status_code == 200
    # `storage:read` is something dana holds, but this token does not.
    assert (await client.get("/api/v1/storages")).status_code == 403


async def test_a_scoped_token_narrows_a_superuser_too(client: AsyncClient, world: World) -> None:
    """
    "My token may only read instances" is a statement an administrator makes
    about a credential. Honouring it for everyone except administrators would
    make the scopes field a lie exactly where the blast radius is largest.
    """
    await act_as(client, "root")
    secret = (await client.post(TOKENS, json={"name": "ro", "scopes": ["instance:read"]})).json()[
        "token"
    ]

    client.headers["Authorization"] = f"Bearer {secret}"

    assert (await client.get("/api/v1/instances")).status_code == 200
    assert (await client.get("/api/v1/users")).status_code == 403


async def test_a_token_cannot_delegate_more_than_it_holds(
    client: AsyncClient, world: World
) -> None:
    """
    The escalation this closes: a narrow token with `api_token:create` minting a
    successor with **no** scopes, which would inherit the owner's full roles.

    So a token-created token has its delegable set written down explicitly,
    rather than being allowed to say "inherit".
    """
    await act_as(client, "dana")
    narrow = (
        await client.post(
            TOKENS, json={"name": "narrow", "scopes": ["api_token:create", "instance:read"]}
        )
    ).json()["token"]

    client.headers["Authorization"] = f"Bearer {narrow}"
    child = await client.post(TOKENS, json={"name": "child"})

    assert child.status_code == 201, child.text
    assert set(child.json()["scopes"]) == {"api_token:create", "instance:read"}

    # And the child really is narrow.
    client.headers["Authorization"] = f"Bearer {child.json()['token']}"
    assert (await client.get("/api/v1/instances")).status_code == 200
    assert (await client.get("/api/v1/storages")).status_code == 403


async def test_a_token_tracks_its_owners_current_roles(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    """
    The property that makes revocation work.

    A token with no scopes is not a snapshot of the permissions held at creation
    — it is resolved against the account on every request. Remove a role and the
    token loses what that role carried, on the next call, without the token being
    touched.
    """
    await act_as(client, "dana")
    secret = (await client.post(TOKENS, json={"name": "inherit"})).json()["token"]

    client.headers["Authorization"] = f"Bearer {secret}"
    assert (await client.get("/api/v1/storages")).status_code == 200

    # Demote the owner in the database, leaving the token exactly as it was.
    user = await seeded.get(User, world.user_a.id)
    assert user is not None
    for link in list(user.role_links):
        await seeded.delete(link)
    await seeded.commit()

    assert (await client.get("/api/v1/storages")).status_code == 403


async def test_a_deactivated_owner_takes_their_tokens_with_them(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    await act_as(client, "dana")
    secret = (await client.post(TOKENS, json={"name": "ci"})).json()["token"]

    user = await seeded.get(User, world.user_a.id)
    assert user is not None
    user.is_active = False
    await seeded.commit()

    client.headers["Authorization"] = f"Bearer {secret}"
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "account_disabled"


async def test_token_scopes_do_not_widen_the_data_scope(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    """
    The two layers stay orthogonal: scopes decide which *verbs* are available,
    roles decide *whose rows* those verbs reach. A token is not a way to become a
    different, better-placed user.
    """
    await seed_audit(seeded, world.user_a, "instance:create")
    await seed_audit(seeded, world.user_b, "instance:create")

    await act_as(client, "boss-a")
    secret = (
        await client.post(TOKENS, json={"name": "audit-ro", "scopes": ["audit:read"]})
    ).json()["token"]

    client.headers["Authorization"] = f"Bearer {secret}"
    actors = {row["actorUsername"] for row in rows_of((await client.get(AUDIT)).json())}

    # Still scoped to lab A, even though the token names a permission that
    # unrestricted administrators hold. The action scope and the row scope are
    # separate layers, and a token moves only the first.
    assert "dana" in actors
    assert "erin" not in actors


async def test_an_expiry_is_bounded(client: AsyncClient, world: World) -> None:
    await act_as(client, "dana")

    too_long = await client.post(TOKENS, json={"name": "a", "expiresInDays": 4000})
    too_short = await client.post(TOKENS, json={"name": "b", "expiresInDays": 0})

    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "token_expiry_too_long"
    assert too_short.status_code == 422
    assert too_short.json()["error"]["code"] == "token_expiry_too_short"


async def test_an_empty_name_is_refused(client: AsyncClient, world: World) -> None:
    await act_as(client, "dana")

    response = await client.post(TOKENS, json={"name": "   "})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "token_name_required"


async def test_an_observer_may_hold_a_token_that_only_reads(
    client: AsyncClient, world: World
) -> None:
    """
    Self-service, including for the least privileged role. A token cannot exceed
    its owner, so an observer's token is read-only by construction — and gating
    the self-service endpoints behind a privilege would mean only administrators
    could automate anything.
    """
    await act_as(client, "gazer")
    secret = (await client.post(TOKENS, json={"name": "observe"})).json()["token"]

    client.headers["Authorization"] = f"Bearer {secret}"

    assert (await client.get("/api/v1/instances")).status_code == 200
    assert (await client.post("/api/v1/instances", json={})).status_code == 403


# ---------------------------------------------------------------------------
# The ceiling mechanism itself
# ---------------------------------------------------------------------------
def test_the_ceiling_is_absent_outside_a_request() -> None:
    """Nothing may leave a token's restriction in force for the next caller."""
    assert policy.current_permission_ceiling() is None


async def test_the_ceiling_intersects_rather_than_unions(world: World) -> None:
    """A ceiling can only ever remove, never add.

    Asserted on a user with no roles at all, so the *only* thing that could make
    a permission appear is the ceiling itself. If this ever returns something
    non-empty, the ceiling has become a grant.
    """
    bare = User(username="x", email="x@example.edu", role_links=[])
    assert policy.effective_permissions(bare) == frozenset()

    with policy.permission_ceiling(frozenset({"instance:read", "user:delete"})):
        assert policy.effective_permissions(bare) == frozenset()
        assert not policy.has_permission(bare, "instance:read")

    assert policy.current_permission_ceiling() is None


# ---------------------------------------------------------------------------
# Login rate limiting
# ---------------------------------------------------------------------------
async def test_repeated_failures_are_eventually_refused(
    client: AsyncClient, world: World, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_rate_limit_attempts_per_username", 3)
    monkeypatch.setattr(settings, "login_rate_limit_attempts_per_ip", 100)

    codes = []
    for _ in range(5):
        response = await client.post(
            "/api/v1/auth/login", json={"username": "dana", "password": "wrong"}
        )
        codes.append(response.status_code)

    assert codes[:3] == [401, 401, 401]
    assert codes[3] == 429
    assert codes[4] == 429


async def test_a_successful_login_clears_the_count(
    client: AsyncClient, world: World, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A limiter that punishes the innocent is worse than none: somebody who
    mistypes on Monday, Tuesday and Wednesday must not be locked out on Thursday.
    """
    monkeypatch.setattr(settings, "login_rate_limit_attempts_per_username", 3)
    monkeypatch.setattr(settings, "login_rate_limit_attempts_per_ip", 100)

    for _ in range(2):
        await client.post("/api/v1/auth/login", json={"username": "dana", "password": "wrong"})
    assert (
        await client.post("/api/v1/auth/login", json={"username": "dana", "password": PASSWORD})
    ).status_code == 200

    # The slate is clean, so two more failures are still just failures.
    for _ in range(2):
        response = await client.post(
            "/api/v1/auth/login", json={"username": "dana", "password": "wrong"}
        )
        assert response.status_code == 401


async def test_one_accounts_failures_do_not_lock_another(
    client: AsyncClient, world: World, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_rate_limit_attempts_per_username", 2)
    monkeypatch.setattr(settings, "login_rate_limit_attempts_per_ip", 100)

    for _ in range(3):
        await client.post("/api/v1/auth/login", json={"username": "dana", "password": "wrong"})

    response = await client.post(
        "/api/v1/auth/login", json={"username": "erin", "password": PASSWORD}
    )
    assert response.status_code == 200


async def test_the_limit_can_be_switched_off(
    client: AsyncClient, world: World, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_rate_limit_enabled", False)
    monkeypatch.setattr(settings, "login_rate_limit_attempts_per_username", 1)

    for _ in range(4):
        response = await client.post(
            "/api/v1/auth/login", json={"username": "dana", "password": "wrong"}
        )
        assert response.status_code == 401


async def test_a_refused_attempt_is_recorded(
    client: AsyncClient,
    world: World,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    audit_rows: Any,
) -> None:
    """The counter is the trail, so a lockout is explainable after the fact."""
    monkeypatch.setattr(settings, "login_rate_limit_attempts_per_username", 1)
    monkeypatch.setattr(settings, "login_rate_limit_attempts_per_ip", 100)

    await client.post("/api/v1/auth/login", json={"username": "dana", "password": "wrong"})
    await client.post("/api/v1/auth/login", json={"username": "dana", "password": "wrong"})

    rows = await audit_rows(action="auth:login")
    denied = [row for row in rows if row.status == "denied"]

    assert len(denied) >= 2
    assert denied[0].actor_username == "dana"
    assert denied[0].detail["username"] == "dana"


async def test_the_refusal_does_not_say_how_many_attempts_remain(
    client: AsyncClient, world: World, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telling an attacker how close they are is telling them how to pace."""
    monkeypatch.setattr(settings, "login_rate_limit_attempts_per_username", 1)
    monkeypatch.setattr(settings, "login_rate_limit_attempts_per_ip", 100)

    await client.post("/api/v1/auth/login", json={"username": "dana", "password": "wrong"})
    response = await client.post(
        "/api/v1/auth/login", json={"username": "dana", "password": "wrong"}
    )

    body = response.json()
    assert response.status_code == 429
    assert body["error"]["code"] == "login_rate_limited"
    assert "1" not in json.dumps(body["error"].get("detail") or {})
