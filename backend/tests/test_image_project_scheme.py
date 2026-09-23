"""The registry layout: one Harbor project per group, username as the prefix.

Harbor's RBAC stops at the project. A ``Developer`` may create *any* repository
inside one — including a repository named after a colleague — so there is no
registry-side setting that says "this member may push here but not there". The
platform therefore has to carry the convention itself::

    library/pytorch:2.4-cuda12            the installation's official images
    lab-vision/share/cuda-toolkit:12.4   the group's agreed images
    lab-vision/zhangsan/my-env:v3         one member's own image

Two things have to hold for that to be more than a comment. A reference must be
split the way a registry splits it — project first, repository (which may nest)
after — and the split must be the *same* one the sync path arrives at by reading
what Harbor reports, or the same image ends up as two rows. Both are pinned
below, along with the rules that reject a reference the layout cannot describe.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.enums import GroupKind, GroupRole
from app.models.group import Group
from app.models.image import Image
from app.models.user import User
from app.registries import FakeRegistryClient, RegistryTag, parse_reference, set_registry
from app.registries.registry import RegistryError
from tests import factories

PASSWORD = "correct-horse-battery"
LOGIN = "/api/v1/auth/login"

LAB = "scheme-lab"
OTHER_LAB = "scheme-other"


@dataclass
class World:
    lab: Group
    other_lab: Group
    global_admin: User
    group_admin: User
    member: User
    outsider: User


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


@pytest.fixture
async def world(seeded: AsyncSession) -> World:
    lab = await factories.create_group(seeded, name=LAB, kind=GroupKind.LAB)
    other_lab = await factories.create_group(seeded, name=OTHER_LAB, kind=GroupKind.LAB)

    global_admin = await factories.create_user(
        seeded, username="scheme-root", password=PASSWORD, role="global_admin"
    )
    group_admin = await factories.create_user(
        seeded,
        username="scheme-boss",
        password=PASSWORD,
        role="group_admin",
        group=lab,
        group_role=GroupRole.GROUP_ADMIN,
    )
    member = await factories.create_user(
        seeded, username="scheme-dana", password=PASSWORD, role="user", group=lab
    )
    outsider = await factories.create_user(
        seeded, username="scheme-erin", password=PASSWORD, role="user", group=other_lab
    )
    await seeded.commit()
    return World(
        lab=lab,
        other_lab=other_lab,
        global_admin=global_admin,
        group_admin=group_admin,
        member=member,
        outsider=outsider,
    )


def error_code(body: object) -> str:
    if isinstance(body, dict):
        envelope = body.get("error")
        if isinstance(envelope, dict):
            return str(envelope.get("code", ""))
    return ""


async def register(client: AsyncClient, reference: str, **overrides: object) -> object:
    payload: dict[str, object] = {"reference": reference}
    payload.update(overrides)
    return await client.post("/api/v1/images", json=payload)


async def seed_tag(registry: FakeRegistryClient, project: str, repository: str, tag: str) -> None:
    registry.put(RegistryTag(project, repository, tag, digest="sha256:abc"))


# ---------------------------------------------------------------------------
# Splitting a reference the way a registry does
# ---------------------------------------------------------------------------
def test_the_project_is_the_first_segment_and_the_repository_may_nest() -> None:
    """The bug this pins: splitting at the last slash read the project as
    ``scheme-lab/scheme-dana``, which no registry has ever had."""
    parts = parse_reference("harbor.test/scheme-lab/scheme-dana/my-env:v3")

    assert parts["project"] == "scheme-lab"
    assert parts["repository"] == "scheme-dana/my-env"
    assert parts["tag"] == "v3"


def test_a_flat_reference_is_unaffected_by_the_nesting_rule() -> None:
    """The common case must not move: one project, one repository, no nesting."""
    for reference, project, repository in [
        ("harbor.test/library/pytorch:2.4", "library", "pytorch"),
        ("harbor.test/scheme-lab/pytorch:2.4", "scheme-lab", "pytorch"),
        ("harbor.test/pytorch:2.4", "library", "pytorch"),
        ("ubuntu:22.04", "library", "ubuntu"),
    ]:
        parts = parse_reference(reference, default_host="harbor.test")
        assert (parts["project"], parts["repository"]) == (project, repository)


def test_deep_nesting_survives_the_round_trip() -> None:
    parts = parse_reference("harbor.test/a/b/c/d:1")
    assert parts["project"] == "a"
    assert parts["repository"] == "b/c/d"


@pytest.mark.parametrize("reference", ["harbor.test/a//b:1", "harbor.test/a/b/:1", "harbor.test/"])
def test_an_empty_segment_is_refused(reference: str) -> None:
    with pytest.raises(RegistryError):
        parse_reference(reference, default_host="harbor.test")


# ---------------------------------------------------------------------------
# The project has to be a group's
# ---------------------------------------------------------------------------
async def test_a_member_may_catalogue_into_their_groups_project(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    await seed_tag(registry, LAB, "scheme-dana/my-env", "v1")
    await act_as(admin_client, "scheme-dana")

    response = await register(admin_client, f"harbor.test/{LAB}/scheme-dana/my-env:v1")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["project"] == LAB
    assert body["repository"] == "scheme-dana/my-env"
    # The project says which group this belongs to, so the row records it even
    # though the visibility is private: `visibility` decides who may read the
    # row, `group_id` is what makes "whose image is this" answerable.
    assert body["groupId"] == world.lab.id


async def test_a_project_that_is_not_a_group_is_refused(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    await seed_tag(registry, "typo-lab", "scheme-dana/my-env", "v1")
    await act_as(admin_client, "scheme-dana")

    response = await register(admin_client, "harbor.test/typo-lab/scheme-dana/my-env:v1")

    assert response.status_code == 400
    assert error_code(response.json()) == "image_project_unknown"


async def test_a_member_may_not_catalogue_into_another_groups_project(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """404 rather than 403: group names are guessable and must not be probed."""
    await seed_tag(registry, OTHER_LAB, "scheme-erin/my-env", "v1")
    await act_as(admin_client, "scheme-erin")
    # A member of the other lab reaching into this one.
    response = await register(admin_client, f"harbor.test/{LAB}/scheme-erin/my-env:v1")

    assert response.status_code == 404


async def test_the_official_project_is_exempt_from_the_group_rule(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    await seed_tag(registry, "library", "pytorch", "2.4")
    await act_as(admin_client, "scheme-root")

    response = await register(admin_client, "harbor.test/library/pytorch:2.4")

    assert response.status_code == 201, response.text
    assert response.json()["groupId"] is None


# ---------------------------------------------------------------------------
# The username prefix
# ---------------------------------------------------------------------------
async def test_a_personal_image_without_any_prefix_is_refused(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """A missing prefix and a wrong prefix are different mistakes, and the
    error has to suggest a reference the caller can actually use."""
    await seed_tag(registry, LAB, "my-env", "v1")
    await act_as(admin_client, "scheme-dana")

    response = await register(admin_client, f"harbor.test/{LAB}/my-env:v1")

    assert response.status_code == 400
    assert error_code(response.json()) == "image_prefix_mismatch"
    detail = response.json()["error"]["detail"]
    assert detail["expectedPrefix"] == "scheme-dana"
    assert detail["hint"] == f"{LAB}/scheme-dana/my-env"


async def test_a_member_cannot_catalogue_an_image_under_a_colleagues_name(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """The prefix is the whole point: Harbor would let a Developer push here,
    and the catalogue is the only thing that can refuse to record it."""
    await seed_tag(registry, LAB, "scheme-erin/borrowed", "v1")
    await act_as(admin_client, "scheme-dana")

    response = await register(admin_client, f"harbor.test/{LAB}/scheme-erin/borrowed:v1")

    assert response.status_code == 400
    assert error_code(response.json()) == "image_prefix_mismatch"


async def test_the_library_project_has_no_username_prefix(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """`library/pytorch` is nobody's personal image, so the rule does not apply."""
    await seed_tag(registry, "library", "pytorch", "2.4")
    await act_as(admin_client, "scheme-dana")

    response = await register(admin_client, "harbor.test/library/pytorch:2.4")

    assert response.status_code == 201, response.text


async def test_a_shared_image_is_not_given_a_username_prefix(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """`share/` is the group's, so only personal images carry a name. Marking
    one shared is an administrative act, which is why the admin does it."""
    await seed_tag(registry, LAB, "share/cuda-toolkit", "12.4")
    await act_as(admin_client, "scheme-root")

    response = await register(
        admin_client,
        f"harbor.test/{LAB}/share/cuda-toolkit:12.4",
        kind="shared",
        visibility="group",
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "shared"
    assert body["groupId"] == world.lab.id


async def test_a_caller_cannot_pick_a_group_the_project_does_not_belong_to(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """The project is authoritative. An explicit groupId that disagrees would
    write a row whose project and group contradict each other."""
    await seed_tag(registry, LAB, "share/toolkit", "1.0")
    await act_as(admin_client, "scheme-root")

    response = await register(
        admin_client,
        f"harbor.test/{LAB}/share/toolkit:1.0",
        kind="shared",
        visibility="group",
        groupId=world.other_lab.id,
    )

    assert response.status_code == 400
    assert error_code(response.json()) == "image_project_group_mismatch"


# ---------------------------------------------------------------------------
# Registration and sync must agree on what a reference is
# ---------------------------------------------------------------------------
async def test_registration_and_sync_produce_the_same_row(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient, seeded: AsyncSession
) -> None:
    """The defect behind the whole change.

    The sync path builds its rows from what Harbor reports — project
    ``scheme-lab``, repository ``scheme-dana/my-env``. If registration split the
    reference differently it wrote a *second* row for the same image, so the
    catalogue showed the image twice and usage counts split between them.
    """
    await seed_tag(registry, LAB, "scheme-dana/my-env", "v1")
    await act_as(admin_client, "scheme-dana")
    registered = await register(admin_client, f"harbor.test/{LAB}/scheme-dana/my-env:v1")
    assert registered.status_code == 201, registered.text

    await act_as(admin_client, "scheme-root")
    sync = await admin_client.post("/api/v1/images/sync", json={"projects": [LAB]})
    assert sync.status_code == 200, sync.text
    # Nothing new: the registered row already describes this tag.
    assert sync.json()["created"] == 0

    rows = (
        await seeded.scalars(
            select(Image).where(Image.project == LAB, Image.repository == "scheme-dana/my-env")
        )
    ).all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# What a sync does with a project it has never seen
# ---------------------------------------------------------------------------
async def test_a_sync_attributes_a_group_project_to_its_group(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient, seeded: AsyncSession
) -> None:
    """Without this a tag someone pushed to their group's project landed in the
    catalogue as a *global* share and appeared in every lab's picker."""
    await seed_tag(registry, LAB, "scheme-dana/experiment", "v1")
    await act_as(admin_client, "scheme-root")

    sync = await admin_client.post("/api/v1/images/sync", json={"projects": [LAB]})
    assert sync.status_code == 200, sync.text

    row = await seeded.scalar(select(Image).where(Image.repository == "scheme-dana/experiment"))
    assert row is not None
    assert row.group_id == world.lab.id
    # The first segment names a real account, so this is that account's image.
    assert row.kind == "personal"
    assert row.owner_user_id == world.member.id
    # Private, not group-visible: a sync may not grant more visibility than
    # registering the same reference would, and a personal image can only ever
    # be registered as private or workspace.
    assert row.visibility == "private"


async def test_an_owner_can_see_their_own_synced_image(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """The counterpart to the rule above: private must not mean invisible to the
    person who pushed it."""
    await seed_tag(registry, LAB, "scheme-dana/experiment", "v1")
    await act_as(admin_client, "scheme-root")
    await admin_client.post("/api/v1/images/sync", json={"projects": [LAB]})

    await act_as(admin_client, "scheme-dana")
    listed = await admin_client.get("/api/v1/images")

    assert any(row["repository"] == "scheme-dana/experiment" for row in listed.json()["items"])


async def test_a_sync_files_a_nested_shared_repository_as_the_groups(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient, seeded: AsyncSession
) -> None:
    await seed_tag(registry, LAB, "share/cuda-toolkit", "12.4")
    await act_as(admin_client, "scheme-root")

    await admin_client.post("/api/v1/images/sync", json={"projects": [LAB]})

    row = await seeded.scalar(select(Image).where(Image.repository == "share/cuda-toolkit"))
    assert row is not None
    assert row.kind == "shared"
    assert row.visibility == "group"
    assert row.group_id == world.lab.id
    assert row.owner_user_id is None


async def test_a_sync_keeps_the_official_project_global(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient, seeded: AsyncSession
) -> None:
    await seed_tag(registry, "library", "pytorch", "2.4")
    await act_as(admin_client, "scheme-root")

    await admin_client.post("/api/v1/images/sync", json={"projects": ["library"]})

    row = await seeded.scalar(select(Image).where(Image.project == "library"))
    assert row is not None
    assert row.kind == "official"
    assert row.visibility == "global"
    assert row.group_id is None


async def test_a_synced_group_image_is_invisible_to_another_lab(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """The consequence of the attribution above, stated as the thing a member
    of another lab would actually notice."""
    await seed_tag(registry, LAB, "scheme-dana/experiment", "v1")
    await act_as(admin_client, "scheme-root")
    await admin_client.post("/api/v1/images/sync", json={"projects": [LAB]})

    await act_as(admin_client, "scheme-erin")
    listed = await admin_client.get("/api/v1/images")

    assert all(row["repository"] != "scheme-dana/experiment" for row in listed.json()["items"])


async def test_a_sync_does_not_overwrite_an_administrators_curation(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient, seeded: AsyncSession
) -> None:
    """Classification applies to new rows only. A sync mirrors the registry; it
    does not get to undo a decision someone made in the catalogue."""
    await seed_tag(registry, LAB, "share/toolkit", "1.0")
    await act_as(admin_client, "scheme-root")
    created = await register(
        admin_client,
        f"harbor.test/{LAB}/share/toolkit:1.0",
        kind="shared",
        visibility="global",
    )
    assert created.status_code == 201, created.text

    await admin_client.post("/api/v1/images/sync", json={"projects": [LAB]})

    seeded.expire_all()
    row = await seeded.get(Image, created.json()["id"])
    assert row is not None
    assert row.visibility == "global"


# ---------------------------------------------------------------------------
# The reference a workload carries
# ---------------------------------------------------------------------------
async def test_a_nested_reference_round_trips_through_the_catalogue(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """`Image.reference` is what gets handed to Kubernetes, so it has to be the
    same string the user typed."""
    await seed_tag(registry, LAB, "scheme-dana/my-env", "v3")
    await act_as(admin_client, "scheme-dana")

    body = (await register(admin_client, f"harbor.test/{LAB}/scheme-dana/my-env:v3")).json()

    assert body["reference"] == f"harbor.test/{LAB}/scheme-dana/my-env:v3"


async def test_a_workload_can_find_a_nested_image_by_its_reference(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient, seeded: AsyncSession
) -> None:
    """`find_by_reference` re-parses the string a workload carries, so a nested
    reference must resolve back to the row rather than to nothing."""
    from app.services import images as image_service

    await seed_tag(registry, LAB, "scheme-dana/my-env", "v3")
    await act_as(admin_client, "scheme-dana")
    created = (await register(admin_client, f"harbor.test/{LAB}/scheme-dana/my-env:v3")).json()

    found = await image_service.find_by_reference(
        seeded, f"harbor.test/{LAB}/scheme-dana/my-env:v3"
    )

    assert found is not None
    assert found.id == created["id"]


# ---------------------------------------------------------------------------
# Harbor's URL encoding
# ---------------------------------------------------------------------------
def test_a_nested_repository_is_encoded_twice_in_a_harbor_path() -> None:
    """Harbor documents the parameter as *"If it contains slash, encode it twice
    over with URL encoding. e.g. a/b -> a%2Fb -> a%252Fb"*. A raw slash reads as
    extra path segments and answers 404 — which is what every tag listing,
    verification and delete did for a nested repository."""
    from app.registries.harbor import _repository_path

    assert _repository_path("pytorch") == "pytorch"
    assert _repository_path("scheme-dana/my-env") == "scheme-dana%252Fmy-env"
    assert _repository_path("a/b/c") == "a%252Fb%252Fc"


def test_the_encoding_reaches_the_wire_unchanged() -> None:
    """httpx preserves percent-encoding, so the double encoding has to survive
    URL building — a client that normalised it would silently undo the fix."""
    import httpx

    from app.registries.harbor import _repository_path

    client = httpx.Client(base_url="http://harbor.test")
    request = client.build_request(
        "GET",
        f"/api/v2.0/projects/{LAB}/repositories/{_repository_path('scheme-dana/my-env')}/artifacts",
    )

    assert "scheme-dana%252Fmy-env" in request.url.raw_path.decode()


# ---------------------------------------------------------------------------
# What the picker offers
# ---------------------------------------------------------------------------
# The user picks an image when creating a task, and the platform decides what
# that list holds. Three kinds belong in it and nothing else:
#
#   · 集群内置  the installation's own images, usable by every account
#   · 组共享    the group's agreed images, usable by its members
#   · 个人      the caller's own
#
# The tests below state that as a contract, because "what the picker shows" is
# the only place the registry layout becomes visible to the person it protects.
@pytest.fixture
async def picked(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient, seeded: AsyncSession
) -> dict[str, int]:
    """Curate one image of each category and return their ids by label."""
    ids: dict[str, int] = {}

    # 集群内置: an official image in the installation's own space.
    await seed_tag(registry, "library", "pytorch", "2.4")
    await act_as(admin_client, "scheme-root")
    ids["official"] = (
        await register(admin_client, "harbor.test/library/pytorch:2.4", kind="official")
    ).json()["id"]

    # 组共享: the group's agreed image, shared with the group.
    await seed_tag(registry, LAB, "share/cuda-toolkit", "12.4")
    await act_as(admin_client, "scheme-boss")
    ids["shared"] = (
        await register(
            admin_client,
            f"harbor.test/{LAB}/share/cuda-toolkit:12.4",
            kind="shared",
            visibility="group",
        )
    ).json()["id"]

    # 个人: the member's own image.
    await seed_tag(registry, LAB, "scheme-dana/mine", "v1")
    await act_as(admin_client, "scheme-dana")
    ids["personal"] = (
        await register(admin_client, f"harbor.test/{LAB}/scheme-dana/mine:v1")
    ).json()["id"]

    return ids


async def visible_ids(client: AsyncClient) -> set[int]:
    listed = await client.get("/api/v1/images", params={"pageSize": 100})
    assert listed.status_code == 200, listed.text
    return {row["id"] for row in listed.json()["items"]}


async def test_a_member_is_offered_all_three_categories(
    admin_client: AsyncClient, world: World, picked: dict[str, int]
) -> None:
    await act_as(admin_client, "scheme-dana")

    offered = await visible_ids(admin_client)

    assert offered >= set(picked.values())


async def test_a_member_is_not_offered_a_colleagues_personal_image(
    admin_client: AsyncClient, world: World, picked: dict[str, int], registry: FakeRegistryClient
) -> None:
    """The category the spec is really about. A colleague's personal image is
    not group-shared however it was filed, so it must not appear."""
    await seed_tag(registry, LAB, "scheme-boss/theirs", "v1")
    await act_as(admin_client, "scheme-boss")
    theirs = (await register(admin_client, f"harbor.test/{LAB}/scheme-boss/theirs:v1")).json()

    await act_as(admin_client, "scheme-dana")
    offered = await visible_ids(admin_client)

    assert theirs["id"] not in offered


async def test_another_lab_is_offered_only_the_built_in_images(
    admin_client: AsyncClient, world: World, picked: dict[str, int]
) -> None:
    """A member of another group gets the installation's images and nothing of
    this group's — neither its shared images nor anyone's personal ones."""
    await act_as(admin_client, "scheme-erin")

    offered = await visible_ids(admin_client)

    assert picked["official"] in offered
    assert picked["shared"] not in offered
    assert picked["personal"] not in offered


async def test_the_owner_sees_their_own_image_but_a_colleague_does_not(
    admin_client: AsyncClient, world: World, picked: dict[str, int]
) -> None:
    await act_as(admin_client, "scheme-dana")
    assert picked["personal"] in await visible_ids(admin_client)

    await act_as(admin_client, "scheme-boss")
    assert picked["personal"] not in await visible_ids(admin_client)


async def test_images_from_a_project_that_is_neither_a_group_nor_library_are_not_advertised(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient, seeded: AsyncSession
) -> None:
    """A stray Harbor project — someone clicked "New Project" by hand. Nothing
    says who should see it, so it is catalogued for administrators and offered
    to nobody else. The alternative, which this pins against, was for a sync to
    file it as a global share and put it in every account's picker."""
    await seed_tag(registry, "stray-project", "whoever/thing", "v1")
    await act_as(admin_client, "scheme-root")
    await admin_client.post("/api/v1/images/sync", json={"projects": ["stray-project"]})

    row = await seeded.scalar(select(Image).where(Image.project == "stray-project"))
    assert row is not None, "the sync must still catalogue it for administrators"

    await act_as(admin_client, "scheme-dana")
    assert row.id not in await visible_ids(admin_client)


# ---------------------------------------------------------------------------
# A push that went straight to Harbor
# ---------------------------------------------------------------------------
# A user can always bypass the platform: `docker push` talks to Harbor and the
# catalogue never hears about it. What follows is the answer to "so can anyone
# see it?", written down where it can be re-checked.
async def test_a_tag_pushed_straight_to_harbor_is_invisible_until_a_sync(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """The catalogue is a cache of the registry, not a mirror of it.

    Nothing watches Harbor: there is no periodic sync, so a tag that appears in
    the registry stays absent from the catalogue — and therefore from every
    picker — until an administrator runs a sync. A user who pushes an image and
    then cannot find it has not hit a bug.
    """
    await seed_tag(registry, LAB, "scheme-dana/fresh", "v1")

    await act_as(admin_client, "scheme-dana")
    assert not any(
        row["repository"] == "scheme-dana/fresh"
        for row in (await admin_client.get("/api/v1/images")).json()["items"]
    )

    # Only an administrator can pull the registry's view in.
    await act_as(admin_client, "scheme-root")
    sync = await admin_client.post("/api/v1/images/sync", json={"projects": [LAB]})
    assert sync.status_code == 200, sync.text
    assert sync.json()["created"] >= 1

    # Now it is catalogued, and its owner can see it.
    await act_as(admin_client, "scheme-dana")
    assert any(
        row["repository"] == "scheme-dana/fresh"
        for row in (await admin_client.get("/api/v1/images")).json()["items"]
    )


async def test_a_tag_pushed_straight_to_harbor_is_the_owners_by_its_prefix(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient, seeded: AsyncSession
) -> None:
    """The prefix is what tells a sync who an undiscovered tag belongs to, which
    is the only thing standing between a direct push and the whole group's
    picker."""
    await seed_tag(registry, LAB, "scheme-dana/fresh", "v1")
    await act_as(admin_client, "scheme-root")
    await admin_client.post("/api/v1/images/sync", json={"projects": [LAB]})

    row = await seeded.scalar(select(Image).where(Image.repository == "scheme-dana/fresh"))
    assert row is not None
    assert row.owner_user_id == world.member.id
    assert row.visibility == "private"

    # A colleague in the same group does not get it.
    await act_as(admin_client, "scheme-boss")
    assert not any(
        row["repository"] == "scheme-dana/fresh"
        for row in (await admin_client.get("/api/v1/images")).json()["items"]
    )


async def test_a_tag_deleted_in_harbor_leaves_the_catalogue_on_the_next_sync(
    admin_client: AsyncClient, world: World, registry: FakeRegistryClient
) -> None:
    """The other direction of divergence: someone removes a tag in Harbor (or a
    group administrator deletes an off-convention push). The next clean sync
    takes it out of the catalogue."""
    await seed_tag(registry, LAB, "share/toolkit", "1.0")
    await act_as(admin_client, "scheme-root")
    await admin_client.post("/api/v1/images/sync", json={"projects": [LAB]})
    assert any(
        row["repository"] == "share/toolkit"
        for row in (await admin_client.get("/api/v1/images")).json()["items"]
    )

    registry.tags.clear()
    sync = await admin_client.post("/api/v1/images/sync", json={"projects": [LAB]})

    assert sync.json()["removed"] >= 1
    assert not any(
        row["repository"] == "share/toolkit"
        for row in (await admin_client.get("/api/v1/images")).json()["items"]
    )
