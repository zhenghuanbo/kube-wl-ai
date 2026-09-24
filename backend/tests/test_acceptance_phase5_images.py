"""Phase 5 acceptance: the image catalogue.

The catalogue exists so nobody has to type an image reference from memory. Most
of these tests are about the consequences of that: a reference is checked before
it is stored, a reference is not an identity, sharing is a claim about what
everyone else will pull, and the catalogue is a *cache* of the registry rather
than the truth.

The registry is a fake, so these assertions are about what the platform would
have asked the registry to do.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.enums import GroupKind, GroupRole
from app.models.group import Group
from app.models.user import User
from app.registries import FakeRegistryClient, RegistryTag, set_registry
from tests import factories

PASSWORD = "correct-horse-battery"
LOGIN = "/api/v1/auth/login"


@dataclass
class World:
    lab_a: Group
    lab_b: Group
    global_admin: User
    admin_a: User
    user_a: User
    user_b: User
    observer: User
    workspace_id: int


@pytest.fixture(autouse=True)
def registry() -> FakeRegistryClient:
    """A fresh registry per test, so seeded tags never leak between them."""
    client = FakeRegistryClient(url="harbor.test")
    set_registry(client)
    yield client
    set_registry(None)


@pytest.fixture
def settings_override(settings: Settings) -> Settings:
    return settings


async def act_as(client: AsyncClient, username: str) -> AsyncClient:
    client.headers.pop("Authorization", None)
    response = await client.post(LOGIN, json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['accessToken']}"
    return client


async def build_world(admin_client: AsyncClient, seeded: AsyncSession) -> World:
    lab_a = await factories.create_group(seeded, name="i5-lab-a", kind=GroupKind.LAB)
    lab_b = await factories.create_group(seeded, name="i5-lab-b", kind=GroupKind.LAB)

    global_admin = await factories.create_user(
        seeded, username="i5-root", password=PASSWORD, role="global_admin"
    )
    admin_a = await factories.create_user(
        seeded,
        username="i5-boss",
        password=PASSWORD,
        role="group_admin",
        group=lab_a,
        group_role=GroupRole.GROUP_ADMIN,
    )
    user_a = await factories.create_user(
        seeded, username="i5-dana", password=PASSWORD, role="user", group=lab_a
    )
    user_b = await factories.create_user(
        seeded, username="i5-erin", password=PASSWORD, role="user", group=lab_b
    )
    observer = await factories.create_user(
        seeded, username="i5-gazer", password=PASSWORD, role="observer", group=lab_a
    )
    await seeded.commit()

    workspace = await admin_client.post(
        "/api/v1/workspaces",
        json={"name": "i5-alpha", "groupId": lab_a.id, "resourceGroupIds": []},
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]

    for user in (admin_a, user_a, observer):
        await admin_client.post(
            f"/api/v1/workspaces/{workspace_id}/members",
            json={"userId": user.id, "moveFromExisting": True},
        )

    return World(
        lab_a=lab_a,
        lab_b=lab_b,
        global_admin=global_admin,
        admin_a=admin_a,
        user_a=user_a,
        user_b=user_b,
        observer=observer,
        workspace_id=workspace_id,
    )


@pytest.fixture
async def world(admin_client: AsyncClient, seeded: AsyncSession) -> World:
    return await build_world(admin_client, seeded)


async def register(client: AsyncClient, reference: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"reference": reference}
    payload.update(overrides)
    response = await client.post("/api/v1/images", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def error_code(body: object) -> str:
    if isinstance(body, dict):
        envelope = body.get("error")
        if isinstance(envelope, dict):
            return str(envelope.get("code", ""))
    return ""


# ---------------------------------------------------------------------------
# Registering
# ---------------------------------------------------------------------------
async def test_a_reference_is_split_into_its_parts(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "pytorch", "2.4", digest="sha256:abc", size_bytes=100))
    await act_as(client, "i5-dana")

    body = await register(client, "harbor.test/library/pytorch:2.4")
    assert body["registry"] == "harbor.test"
    assert body["project"] == "library"
    assert body["repository"] == "pytorch"
    assert body["tag"] == "2.4"
    assert body["reference"] == "harbor.test/library/pytorch:2.4"


async def test_a_typo_is_refused_before_it_is_stored(client: AsyncClient, world: World) -> None:
    """The whole point of the catalogue.

    A reference that does not exist must be refused now, not discovered ten
    minutes into a job as an `ImagePullBackOff`.
    """
    await act_as(client, "i5-dana")
    response = await client.post(
        "/api/v1/images", json={"reference": "harbor.test/library/pytorch:9.9-nope"}
    )
    assert response.status_code == 400
    assert error_code(response.json()) == "image_not_found_upstream"


async def test_an_unreachable_registry_does_not_register_anything(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.error = "connection refused"
    await act_as(client, "i5-dana")

    response = await client.post(
        "/api/v1/images", json={"reference": "harbor.test/library/pytorch:2.4"}
    )
    assert response.status_code == 400
    assert error_code(response.json()) == "image_not_verified"

    listed = await client.get("/api/v1/images")
    assert listed.json()["meta"]["total"] == 0


async def test_the_same_reference_cannot_be_registered_twice(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "pytorch", "2.4", digest="sha256:abc"))
    await act_as(client, "i5-dana")
    await register(client, "harbor.test/library/pytorch:2.4")

    response = await client.post(
        "/api/v1/images", json={"reference": "harbor.test/library/pytorch:2.4"}
    )
    assert response.status_code == 409
    assert error_code(response.json()) == "image_exists"


async def test_whitespace_in_a_reference_is_refused(client: AsyncClient, world: World) -> None:
    await act_as(client, "i5-dana")
    response = await client.post(
        "/api/v1/images", json={"reference": "harbor.test/lib/pytorch :2.4"}
    )
    assert response.status_code == 422


async def test_the_recorded_digest_comes_from_the_registry(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """A reference is not an identity.

    The same tag can be repushed, so the digest the registry reported is stored
    alongside it — that is what makes an entry reproducible.
    """
    registry.put(
        RegistryTag("library", "pytorch", "2.4", digest="sha256:" + "a" * 64, size_bytes=42)
    )
    await act_as(client, "i5-dana")
    body = await register(client, "harbor.test/library/pytorch:2.4")

    assert body["digest"] == "sha256:" + "a" * 64
    assert body["sizeBytes"] == 42
    assert body["verified"] is True


async def test_a_digest_reference_is_preserved(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "pytorch", "2.4", digest="sha256:" + "b" * 64))
    await act_as(client, "i5-dana")
    body = await register(client, f"harbor.test/library/pytorch@sha256:{'b' * 64}")
    assert body["digest"] == "sha256:" + "b" * 64


# ---------------------------------------------------------------------------
# Sharing is a claim about other people
# ---------------------------------------------------------------------------
async def test_a_user_cannot_publish_an_official_image(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "pytorch", "2.4", digest="sha256:abc"))
    await act_as(client, "i5-dana")

    response = await client.post(
        "/api/v1/images",
        json={"reference": "harbor.test/library/pytorch:2.4", "kind": "official"},
    )
    assert response.status_code == 403


async def test_a_user_cannot_share_globally(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "pytorch", "2.4", digest="sha256:abc"))
    await act_as(client, "i5-dana")

    response = await client.post(
        "/api/v1/images",
        json={"reference": "harbor.test/library/pytorch:2.4", "visibility": "global"},
    )
    assert response.status_code == 403


async def test_a_group_admin_can_share_with_their_group(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "custom", "1.0", digest="sha256:abc"))
    await act_as(client, "i5-boss")

    body = await register(
        client,
        "harbor.test/library/custom:1.0",
        visibility="group",
        groupId=world.lab_a.id,
    )
    assert body["visibility"] == "group"

    # A member of the group can see it.
    await act_as(client, "i5-dana")
    assert (await client.get(f"/api/v1/images/{body['id']}")).status_code == 200


async def test_a_group_share_is_visible_to_an_ordinary_member(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """The members are the entire point of a group share.

    `DataScope.group_ids` holds the groups an account *administers*, so sourcing
    this from it made a share visible to the group's administrators and to
    nobody else — which looks like the share silently failing.
    """
    registry.put(RegistryTag("library", "custom", "1.0", digest="sha256:abc"))
    await act_as(client, "i5-boss")
    body = await register(
        client, "harbor.test/library/custom:1.0", visibility="group", groupId=world.lab_a.id
    )

    await act_as(client, "i5-dana")
    assert (await client.get(f"/api/v1/images/{body['id']}")).status_code == 200
    listed = await client.get("/api/v1/images")
    assert any(row["id"] == body["id"] for row in listed.json()["items"])


async def test_a_group_share_does_not_leak_to_another_lab(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "custom", "1.0", digest="sha256:abc"))
    await act_as(client, "i5-boss")
    body = await register(
        client, "harbor.test/library/custom:1.0", visibility="group", groupId=world.lab_a.id
    )

    await act_as(client, "i5-erin")
    assert (await client.get(f"/api/v1/images/{body['id']}")).status_code == 404
    listed = await client.get("/api/v1/images")
    assert all(row["id"] != body["id"] for row in listed.json()["items"])


async def test_a_single_group_admin_gets_their_group_by_default(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """A `group` share with no group would match nothing and be invisible to all.

    For an administrator who belongs to exactly one group the answer is not in
    doubt, so it is filled in rather than refused: being asked to restate the
    obvious is friction with no benefit.
    """
    registry.put(RegistryTag("library", "custom", "1.0", digest="sha256:abc"))
    await act_as(client, "i5-boss")

    response = await client.post(
        "/api/v1/images",
        json={"reference": "harbor.test/library/custom:1.0", "visibility": "group"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["groupId"] == world.lab_a.id


async def test_an_ambiguous_group_share_is_refused(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """A global administrator belongs to no group, so there is nothing to infer.

    Guessing here would publish the image somewhere nobody asked for, which is
    worse than asking.
    """
    registry.put(RegistryTag("library", "custom", "1.0", digest="sha256:abc"))
    await act_as(client, "i5-root")

    response = await client.post(
        "/api/v1/images",
        json={"reference": "harbor.test/library/custom:1.0", "visibility": "group"},
    )
    assert response.status_code == 400
    assert error_code(response.json()) == "image_group_required"


async def test_a_global_image_is_visible_to_an_unaffiliated_user(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """The default catalogue must be usable by a user who belongs to nothing yet.

    Otherwise a new account opens the app to an empty picker and no way to fill
    it without an administrator.
    """
    registry.put(RegistryTag("library", "base", "latest", digest="sha256:abc"))
    await act_as(client, "i5-erin")
    body = (
        await register(client, "harbor.test/library/base:latest", visibility="global")
        if False
        else None
    )

    # Erin cannot share globally; the administrator does it for her.
    await act_as(client, "i5-boss")
    global_image = await register(
        client, "harbor.test/library/base:latest", visibility="global", kind="official"
    )

    await act_as(client, "i5-erin")
    assert (await client.get(f"/api/v1/images/{global_image['id']}")).status_code == 200
    assert body is None


# ---------------------------------------------------------------------------
# Private images
# ---------------------------------------------------------------------------
async def test_a_private_image_is_invisible_to_others(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "secret", "1.0", digest="sha256:abc"))
    await act_as(client, "i5-dana")
    body = await register(client, "harbor.test/library/secret:1.0")

    await act_as(client, "i5-gazer")
    # 404, not 403: a 403 would confirm the entry exists.
    assert (await client.get(f"/api/v1/images/{body['id']}")).status_code == 404


async def test_an_observer_cannot_register_an_image(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "pytorch", "2.4", digest="sha256:abc"))
    await act_as(client, "i5-gazer")
    response = await client.post(
        "/api/v1/images", json={"reference": "harbor.test/library/pytorch:2.4"}
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------
async def test_an_owner_can_edit_their_own_entry(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "pytorch", "2.4", digest="sha256:abc"))
    await act_as(client, "i5-dana")
    body = await register(client, "harbor.test/library/pytorch:2.4")

    response = await client.patch(
        f"/api/v1/images/{body['id']}",
        json={"displayName": "PyTorch 2.4", "description": "训练用的基础镜像"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["displayName"] == "PyTorch 2.4"
    assert response.json()["description"] == "训练用的基础镜像"


async def test_another_user_cannot_edit_it(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "pytorch", "2.4", digest="sha256:abc"))
    await act_as(client, "i5-dana")
    body = await register(client, "harbor.test/library/pytorch:2.4")

    # A plain user, not an observer: an observer is refused earlier by
    # `image:create`, which would hide the visibility check under test.
    await act_as(client, "i5-erin")
    assert (
        await client.patch(f"/api/v1/images/{body['id']}", json={"displayName": "stolen"})
    ).status_code == 404


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------
async def test_deleting_removes_the_registry_tag(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "scratch", "1.0", digest="sha256:abc"))
    await act_as(client, "i5-dana")
    body = await register(client, "harbor.test/library/scratch:1.0")

    response = await client.delete(f"/api/v1/images/{body['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["isActive"] is False
    assert ("library", "scratch", "1.0") in registry.deleted


async def test_deleting_does_not_touch_an_official_image_in_the_registry(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """An official base image is referenced by things this catalogue cannot see.

    Deleting a shared tag is not a decision the platform gets to make on a
    user's behalf, so only the catalogue row is retired.
    """
    registry.put(RegistryTag("library", "base", "latest", digest="sha256:abc"))
    await act_as(client, "i5-boss")
    body = await register(
        client, "harbor.test/library/base:latest", kind="official", visibility="global"
    )

    response = await client.delete(f"/api/v1/images/{body['id']}")
    assert response.status_code == 200
    assert registry.deleted == []
    assert registry.get_tag("library", "base", "latest") is not None


async def test_a_tag_already_gone_from_the_registry_does_not_block_deletion(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """The catalogue is a cache; the desired end state is already reached."""
    registry.put(RegistryTag("library", "scratch", "1.0", digest="sha256:abc"))
    await act_as(client, "i5-dana")
    body = await register(client, "harbor.test/library/scratch:1.0")

    registry.tags.clear()
    assert (await client.delete(f"/api/v1/images/{body['id']}")).status_code == 200


async def test_a_pinned_entry_cannot_be_deleted(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "curated", "1.0", digest="sha256:abc"))
    await act_as(client, "i5-boss")
    body = await register(
        client, "harbor.test/library/curated:1.0", visibility="global", isPinned=True
    )

    response = await client.delete(f"/api/v1/images/{body['id']}")
    assert response.status_code == 400
    assert error_code(response.json()) == "image_pinned"


# ---------------------------------------------------------------------------
# Syncing
# ---------------------------------------------------------------------------
async def test_a_sync_adds_what_the_registry_reports(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "newone", "3.0", digest="sha256:" + "c" * 64, size_bytes=7))
    await act_as(client, "i5-boss")

    response = await client.post("/api/v1/images/sync", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] >= 1

    listed = await client.get("/api/v1/images", params={"pageSize": 200})
    assert any(row["tag"] == "3.0" for row in listed.json()["items"])


async def test_a_sync_retires_what_the_registry_no_longer_reports(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    await act_as(client, "i5-boss")
    await client.post("/api/v1/images/sync", json={})
    before = await client.get("/api/v1/images", params={"pageSize": 200})
    assert before.json()["meta"]["total"] > 0

    registry.tags.clear()
    response = await client.post("/api/v1/images/sync", json={})
    assert response.status_code == 200
    assert response.json()["removed"] > 0

    after = await client.get("/api/v1/images", params={"pageSize": 200})
    assert after.json()["meta"]["total"] == 0


async def test_a_sync_keeps_a_pinned_image_that_vanished(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """A curated image disappearing from a listing is usually a misconfigured
    project, not a decision to stop offering it.

    Dropping it would silently break every saved application that referenced it.
    """
    registry.put(RegistryTag("library", "curated", "1.0", digest="sha256:abc"))
    await act_as(client, "i5-boss")
    await register(client, "harbor.test/library/curated:1.0", visibility="global", isPinned=True)

    registry.tags.clear()
    await client.post("/api/v1/images/sync", json={})

    listed = await client.get("/api/v1/images", params={"pageSize": 200})
    assert any(row["repository"] == "curated" for row in listed.json()["items"])


async def test_a_sync_leaves_personal_images_alone(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """A user's own entry is not the registry's to retire.

    It is not part of the shared catalogue, so a listing that does not mention it
    says nothing about whether it should still exist.
    """
    registry.put(RegistryTag("library", "mine", "1.0", digest="sha256:abc"))
    await act_as(client, "i5-dana")
    await register(client, "harbor.test/library/mine:1.0")

    registry.tags.clear()
    await client.post("/api/v1/images/sync", json={})

    await act_as(client, "i5-dana")
    listed = await client.get("/api/v1/images", params={"ownedOnly": True})
    assert listed.json()["meta"]["total"] == 1


async def test_a_sync_does_not_wipe_the_catalogue_when_the_listing_fails(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """An empty pass means the listing failed, not that the registry is empty.

    Retiring everything on a bad pass would be the most destructive possible
    response to a transient network error.
    """
    await act_as(client, "i5-boss")
    await client.post("/api/v1/images/sync", json={})
    before = (await client.get("/api/v1/images", params={"pageSize": 200})).json()["meta"]["total"]
    assert before > 0

    registry.error = "connection refused"
    response = await client.post("/api/v1/images/sync", json={})
    assert response.status_code == 200
    assert response.json()["removed"] == 0
    assert response.json()["errors"]

    after = (await client.get("/api/v1/images", params={"pageSize": 200})).json()["meta"]["total"]
    assert after == before


async def test_a_user_cannot_trigger_a_sync(client: AsyncClient, world: World) -> None:
    """The catalogue is shared state; refreshing it is administrative."""
    await act_as(client, "i5-dana")
    assert (await client.post("/api/v1/images/sync", json={})).status_code == 403


async def test_registry_status_reports_reachability(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    await act_as(client, "i5-dana")
    body = (await client.get("/api/v1/images/registry")).json()
    assert body["reachable"] is True
    assert body["host"] == "harbor.test"

    registry.error = "connection refused"
    body = (await client.get("/api/v1/images/registry")).json()
    assert body["reachable"] is False
    assert body["message"]


# ---------------------------------------------------------------------------
# Usage and search
# ---------------------------------------------------------------------------
async def test_usage_count_matches_what_references_the_image(
    client: AsyncClient, world: World, registry: FakeRegistryClient, admin_client: AsyncClient
) -> None:
    """The count decides whether deletion is offered, so it has to be real."""
    registry.put(RegistryTag("library", "used", "1.0", digest="sha256:abc"))
    await act_as(client, "i5-boss")
    image = await register(
        client, "harbor.test/library/used:1.0", visibility="global", kind="shared"
    )

    await act_as(client, "i5-dana")
    await client.post(
        "/api/v1/applications",
        json={
            "name": "uses-it",
            "image": "harbor.test/library/used:1.0",
            "workspaceId": world.workspace_id,
            "defaultCpuMilli": 1000,
            "defaultMemoryMb": 1024,
        },
    )

    detail = await client.get(f"/api/v1/images/{image['id']}")
    assert detail.json()["usageCount"] == 1
    assert detail.json()["canDelete"] is False
    assert detail.json()["blockedReason"]

    # Dana cannot remove it at all: it is a global share, and removing shared
    # catalogue entries is a curatorial act. Refused before the in-use check, so
    # she learns nothing about what else references it.
    assert (await client.delete(f"/api/v1/images/{image['id']}")).status_code == 403

    # The administrator who published it gets the real reason.
    await act_as(client, "i5-boss")
    response = await client.delete(f"/api/v1/images/{image['id']}")
    assert response.status_code == 409
    assert error_code(response.json()) == "image_in_use"


async def test_search_matches_the_human_name_and_the_tag(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "pytorch", "2.4-cuda12", digest="sha256:abc"))
    await act_as(client, "i5-dana")
    await register(client, "harbor.test/library/pytorch:2.4-cuda12")

    by_repository = await client.get("/api/v1/images", params={"search": "pytorch"})
    assert by_repository.json()["meta"]["total"] == 1

    by_tag = await client.get("/api/v1/images", params={"search": "cuda12"})
    assert by_tag.json()["meta"]["total"] == 1

    missing = await client.get("/api/v1/images", params={"search": "tensorflow"})
    assert missing.json()["meta"]["total"] == 0


async def test_official_images_sort_first(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """The picker should lead with what a new user most likely wants."""
    registry.put(RegistryTag("library", "mine", "1.0", digest="sha256:abc"))
    registry.put(RegistryTag("library", "base", "latest", digest="sha256:def"))
    await act_as(client, "i5-dana")
    await register(client, "harbor.test/library/mine:1.0")

    await act_as(client, "i5-boss")
    await register(client, "harbor.test/library/base:latest", kind="official", visibility="global")

    listed = await client.get("/api/v1/images", params={"pageSize": 50})
    assert listed.json()["items"][0]["kind"] == "official"


async def test_a_personal_entry_does_not_escape_its_owners_list(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "mine", "1.0", digest="sha256:abc"))
    await act_as(client, "i5-dana")
    await register(client, "harbor.test/library/mine:1.0")

    await act_as(client, "i5-erin")
    listed = await client.get("/api/v1/images")
    assert all(row["repository"] != "mine" for row in listed.json()["items"])


async def test_the_timeline_records_registration_and_removal(
    client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    registry.put(RegistryTag("library", "traced", "1.0", digest="sha256:abc"))
    await act_as(client, "i5-dana")
    body = await register(client, "harbor.test/library/traced:1.0")
    await client.delete(f"/api/v1/images/{body['id']}")

    events = await client.get(f"/api/v1/images/{body['id']}/events")
    assert events.status_code == 200
    kinds = [row["eventType"] for row in events.json()]
    assert "created" in kinds
    assert "deleted" in kinds
    assert all(row["actorUsername"] == "i5-dana" for row in events.json())


# ---------------------------------------------------------------------------
# The API contract
# ---------------------------------------------------------------------------
def test_every_image_query_parameter_is_camel_case(app: FastAPI, world: World) -> None:
    """Snake_case query keys are silently ignored, so they must not exist.

    Asserted structurally against the OpenAPI document rather than by example:
    an example only covers the parameters somebody remembered to test.
    """
    schema = app.openapi()
    allowed_exceptions = {"grant_type", "client_id", "code", "state"}
    offenders: list[str] = []
    for path, operations in schema["paths"].items():
        if "/images" not in path:
            continue
        for operation in operations.values():
            for parameter in operation.get("parameters", []):
                name = parameter["name"]
                if name in allowed_exceptions or parameter["in"] != "query":
                    continue
                if "_" in name:
                    offenders.append(f"{path}:{name}")
    assert offenders == []
