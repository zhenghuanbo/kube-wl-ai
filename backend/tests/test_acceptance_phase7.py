"""Phase 7 acceptance: the monitoring centre and the Grafana handoff.

The centre of gravity here is the *handoff*. Everything else on the page is a
read of data the platform already owns and already scopes; the Grafana URL is the
one artefact that leaves our process carrying an authorisation decision, and it
does so as a bearer credential inside a query string.

So the tests are mostly about what that URL must not contain or must not be
mintable for:

* a normal user's URL is locked to their own namespaces, and a colleague's
  namespace never appears in it;
* an administrator's URL is *unfiltered*, which is not the same as filtered to
  the empty string — the difference between "sees everything" and "sees nothing"
  is one missing parameter, and getting it backwards is silent;
* a dashboard the caller may not open is a 404 and not a 403, because a 403
  confirms the dashboard exists;
* with Grafana unconfigured, nothing is minted at all and the page still works.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.enums import GroupRole, IdeType, InstanceStatus, WorkspaceStatus
from app.models.group import Group
from app.models.instance import Instance
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from tests import factories

PASSWORD = "phase7-password"
OVERVIEW = "/api/v1/monitor/overview"
EMBED = "/api/v1/monitor/dashboards/{key}/embed"
GRAFANA_BASE = "https://grafana.example.edu/grafana"


async def act_as(client: AsyncClient, username: str) -> AsyncClient:
    client.headers.pop("Authorization", None)
    response = await client.post(
        "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    client.headers["Authorization"] = f"Bearer {response.json()['accessToken']}"
    return client


def claims_of(url: str) -> dict[str, Any]:
    """Decode (without verifying) the token Grafana would be handed."""
    token = parse_qs(urlparse(url).query)["auth_token"][0]
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def query_of(url: str) -> dict[str, str]:
    return {key: value[0] for key, value in parse_qs(urlparse(url).query).items()}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
class World:
    lab_a: Group
    lab_b: Group
    user_a: User
    user_b: User
    admin_a: User
    observer: User
    workspace_a: int
    workspace_b: int
    namespace_a: str
    namespace_b: str
    pod_a: str
    pod_b: str


async def build_world(session: AsyncSession) -> World:
    """Two labs with one live container each, so isolation is observable."""
    lab_a = await factories.create_group(session, name="lab-a", display_name="自然语言处理")
    lab_b = await factories.create_group(session, name="lab-b", display_name="计算机视觉")

    user_a = await factories.create_user(
        session, username="dana", password=PASSWORD, role="user", group=lab_a, full_name="戴娜"
    )
    user_b = await factories.create_user(
        session, username="erin", password=PASSWORD, role="user", group=lab_b, full_name="艾琳"
    )
    admin_a = await factories.create_user(
        session,
        username="boss-a",
        password=PASSWORD,
        role="group_admin",
        group=lab_a,
        group_role=GroupRole.GROUP_ADMIN,
        full_name="A 组管理员",
    )
    observer = await factories.create_user(
        session, username="gazer", password=PASSWORD, role="observer", group=lab_a
    )
    await factories.create_user(session, username="root2", password=PASSWORD, role="global_admin")

    workspace_a = Workspace(
        name="alpha",
        display_name="A 组工作空间",
        group_id=lab_a.id,
        k8s_namespace="wl-lab-a-alpha",
        status=WorkspaceStatus.ACTIVE,
    )
    workspace_b = Workspace(
        name="beta",
        display_name="B 组工作空间",
        group_id=lab_b.id,
        k8s_namespace="wl-lab-b-beta",
        status=WorkspaceStatus.ACTIVE,
    )
    session.add_all([workspace_a, workspace_b])
    await session.flush()

    for user, workspace in ((user_a, workspace_a), (admin_a, workspace_a), (user_b, workspace_b)):
        session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id))
    await session.flush()

    await add_instance(
        session,
        workspace_id=workspace_a.id,
        owner_user_id=user_a.id,
        status=InstanceStatus.RUNNING,
        pod_name="wl-dev-a-1-abc",
        name="dev-a",
        gpu_count=1,
        gpu_type="A100",
    )
    await add_instance(
        session,
        workspace_id=workspace_b.id,
        owner_user_id=user_b.id,
        status=InstanceStatus.RUNNING,
        pod_name="wl-dev-b-1-xyz",
        name="dev-b",
    )
    await session.commit()

    world = World()
    world.lab_a, world.lab_b = lab_a, lab_b
    world.user_a, world.user_b = user_a, user_b
    world.admin_a, world.observer = admin_a, observer
    world.workspace_a, world.workspace_b = workspace_a.id, workspace_b.id
    world.namespace_a, world.namespace_b = "wl-lab-a-alpha", "wl-lab-b-beta"
    world.pod_a, world.pod_b = "wl-dev-a-1-abc", "wl-dev-b-1-xyz"
    return world


async def add_instance(
    session: AsyncSession,
    *,
    workspace_id: int,
    owner_user_id: int,
    status: InstanceStatus = InstanceStatus.RUNNING,
    pod_name: str | None = None,
    name: str = "inst",
    gpu_count: int = 0,
    gpu_type: str = "none",
) -> Instance:
    instance = Instance(
        name=name,
        display_name=name,
        workspace_id=workspace_id,
        owner_user_id=owner_user_id,
        cpu_milli=1000,
        memory_mb=1024,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        storage_gb=10,
        image="registry.local/pytorch:2.4",
        ide_type=IdeType.NONE,
        status=status,
        pod_name=pod_name,
    )
    session.add(instance)
    await session.flush()
    return instance


@pytest.fixture
async def world(seeded: AsyncSession) -> World:
    return await build_world(seeded)


@pytest.fixture
def grafana_on(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """The reference deployment: Grafana reachable behind the app's own origin."""
    monkeypatch.setattr(settings, "grafana_public_url", GRAFANA_BASE)
    return settings


# ---------------------------------------------------------------------------
# The page, with Grafana switched off (the default)
# ---------------------------------------------------------------------------
async def test_monitoring_requires_a_permission(client: AsyncClient, world: World) -> None:
    response = await client.get(OVERVIEW)

    assert response.status_code == 401


async def test_overview_reports_grafana_as_unconfigured(client: AsyncClient, world: World) -> None:
    """No Grafana is a supported state, not a broken one."""
    await act_as(client, "dana")

    response = await client.get(OVERVIEW)
    body = response.json()

    assert response.status_code == 200
    assert body["enabled"] is False
    assert body["disabledReason"]
    assert body["dashboards"] == []
    # The inventory is still worth having: it comes from the platform's own rows.
    assert "inventory" in body


async def test_an_observer_may_open_the_monitoring_page(client: AsyncClient, world: World) -> None:
    """`monitor:read_self` is deliberate: read-only does not mean blind."""
    await act_as(client, "gazer")

    response = await client.get(OVERVIEW)

    assert response.status_code == 200
    assert [c["name"] for c in response.json()["containers"]] == []


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------
async def test_a_user_sees_only_their_own_container(client: AsyncClient, world: World) -> None:
    await act_as(client, "dana")

    body = (await client.get(OVERVIEW)).json()

    assert [c["name"] for c in body["containers"]] == ["dev-a"]
    assert body["scope"]["namespaces"] == [world.namespace_a]
    assert body["scope"]["pods"] == [world.pod_a]
    assert body["scope"]["unrestricted"] is False
    assert body["scope"]["empty"] is False


async def test_a_group_admin_sees_their_group_and_not_the_other(
    client: AsyncClient, world: World
) -> None:
    """The admin of lab A administers A's workspace, and only that one."""
    await act_as(client, "boss-a")

    body = (await client.get(OVERVIEW)).json()

    assert [c["name"] for c in body["containers"]] == ["dev-a"]
    assert body["scope"]["namespaces"] == [world.namespace_a]


async def test_a_global_admin_is_unrestricted(client: AsyncClient, world: World) -> None:
    await act_as(client, "root2")

    body = (await client.get(OVERVIEW)).json()

    assert body["scope"]["unrestricted"] is True
    assert body["scope"]["namespaces"] == []
    assert {c["name"] for c in body["containers"]} == {"dev-a", "dev-b"}


async def test_an_empty_scope_is_not_the_same_as_no_scope(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    """
    A user with nothing running is filtered-to-nothing, not unrestricted.

    Both produce an empty `namespaces` list, and collapsing them would hand a
    brand-new user a fleet-wide dashboard.
    """
    await act_as(client, "gazer")

    scope = (await client.get(OVERVIEW)).json()["scope"]

    assert scope["namespaces"] == []
    assert scope["unrestricted"] is False
    assert scope["empty"] is True


async def test_stopped_containers_are_not_offered(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    """A stopped instance has no pod, so a panel filtered on it is permanently blank."""
    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.user_a.id,
        status=InstanceStatus.STOPPED,
        name="retired",
        pod_name=None,
    )
    await seeded.commit()

    await act_as(client, "dana")
    body = (await client.get(OVERVIEW)).json()

    assert "retired" not in {c["name"] for c in body["containers"]}


# ---------------------------------------------------------------------------
# The catalogue is permission-filtered
# ---------------------------------------------------------------------------
async def test_a_user_only_sees_their_own_containers_dashboard(
    client: AsyncClient, world: World, grafana_on: Settings
) -> None:
    await act_as(client, "dana")

    body = (await client.get(OVERVIEW)).json()

    assert [d["key"] for d in body["dashboards"]] == ["my-containers"]


async def test_a_group_admin_sees_the_lab_dashboard_but_not_the_fleet(
    client: AsyncClient, world: World, grafana_on: Settings
) -> None:
    await act_as(client, "boss-a")

    keys = [d["key"] for d in (await client.get(OVERVIEW)).json()["dashboards"]]

    assert keys == ["lab-overview", "my-containers"]
    assert "gpu-fleet" not in keys


async def test_a_global_admin_sees_every_dashboard(
    client: AsyncClient, world: World, grafana_on: Settings
) -> None:
    await act_as(client, "root2")

    keys = [d["key"] for d in (await client.get(OVERVIEW)).json()["dashboards"]]

    assert set(keys) == {"lab-overview", "my-containers", "gpu-fleet", "node-health"}


# ---------------------------------------------------------------------------
# The handoff
# ---------------------------------------------------------------------------
async def test_the_embed_url_locks_a_user_to_their_own_namespace(
    client: AsyncClient, world: World, grafana_on: Settings
) -> None:
    await act_as(client, "dana")

    embed = (await client.get(OVERVIEW)).json()["embeds"]["my-containers"]
    query = query_of(embed["url"])

    assert embed["url"].startswith(f"{GRAFANA_BASE}/d/wl-my-containers")
    assert query["var-namespace"] == world.namespace_a
    assert query["var-pod"] == world.pod_a
    assert query["kiosk"] == "tv"
    # The credential travels as a query parameter, so the URL itself must never
    # name the other lab.
    assert world.namespace_b not in embed["url"]
    assert world.pod_b not in embed["url"]


async def test_the_embed_token_carries_the_scope(
    client: AsyncClient, world: World, grafana_on: Settings
) -> None:
    await act_as(client, "dana")

    embed = (await client.get(OVERVIEW)).json()["embeds"]["my-containers"]
    claims = claims_of(embed["url"])

    assert claims["typ"] == "grafana"
    assert claims["wl_namespaces"] == [world.namespace_a]
    assert claims["wl_pods"] == [world.pod_a]
    assert claims["wl_unrestricted"] is False
    assert claims["wl_dashboard"] == "my-containers"
    assert claims["aud"] == "grafana"


async def test_the_embed_token_is_short_lived(
    client: AsyncClient, world: World, grafana_on: Settings
) -> None:
    await act_as(client, "dana")

    embed = (await client.get(OVERVIEW)).json()["embeds"]["my-containers"]
    claims = claims_of(embed["url"])

    lifetime = claims["exp"] - claims["iat"]
    assert lifetime == grafana_on.grafana_jwt_ttl_seconds
    assert lifetime <= 600


async def test_an_admin_gets_no_namespace_filter_rather_than_a_wildcard(
    client: AsyncClient, world: World, grafana_on: Settings
) -> None:
    """
    The distinction that matters most in this file.

    `$__all` is only meaningful to a multi-value Grafana variable; passing it for
    a single-value one selects *nothing*. Omitting the parameter lets the
    dashboard fall back to its own "All" default, so the safe-looking wildcard is
    the one that produces an empty panel.
    """
    await act_as(client, "root2")

    embed = (await client.get(OVERVIEW)).json()["embeds"]["gpu-fleet"]
    query = query_of(embed["url"])

    assert "var-namespace" not in query
    assert "$__all" not in embed["url"]
    assert claims_of(embed["url"])["wl_unrestricted"] is True


async def test_the_lab_dashboard_has_no_pod_variable(
    client: AsyncClient, world: World, grafana_on: Settings
) -> None:
    """A lab-wide panel is not filtered per container, and must not pretend to be."""
    await act_as(client, "boss-a")

    query = query_of((await client.get(OVERVIEW)).json()["embeds"]["lab-overview"]["url"])

    assert "var-namespace" in query
    assert "var-pod" not in query


async def test_a_fresh_url_can_be_minted_for_one_dashboard(
    client: AsyncClient, world: World, grafana_on: Settings
) -> None:
    await act_as(client, "dana")

    response = await client.post(EMBED.format(key="my-containers"))
    body = response.json()

    assert response.status_code == 200
    assert query_of(body["url"])["var-namespace"] == world.namespace_a
    assert body["expiresIn"] == grafana_on.grafana_jwt_ttl_seconds


async def test_cannot_mint_a_url_for_a_dashboard_you_may_not_open(
    client: AsyncClient, world: World, grafana_on: Settings
) -> None:
    """404, not 403: a 403 would confirm the fleet dashboard exists."""
    await act_as(client, "dana")

    response = await client.post(EMBED.format(key="gpu-fleet"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "dashboard_not_found"


async def test_a_forbidden_dashboard_does_not_reveal_that_it_exists(
    client: AsyncClient, world: World, grafana_on: Settings
) -> None:
    """
    The verdict is identical; only the echoed key differs, and that is the
    caller's own input. What must not leak is the catalogue — no title, no
    description, nothing that confirms "GPU 集群" is a real dashboard.
    """
    from app.monitoring.grafana import DASHBOARDS

    await act_as(client, "dana")

    forbidden = await client.post(EMBED.format(key="gpu-fleet"))
    unknown = await client.post(EMBED.format(key="no-such-dashboard"))

    assert forbidden.status_code == unknown.status_code == 404
    assert forbidden.json()["error"]["code"] == unknown.json()["error"]["code"]

    body = json.dumps(forbidden.json(), ensure_ascii=False)
    for spec in DASHBOARDS:
        assert spec.title not in body
        assert spec.uid not in body


async def test_an_admin_may_mint_the_fleet_dashboard(
    client: AsyncClient, world: World, grafana_on: Settings
) -> None:
    await act_as(client, "root2")

    response = await client.post(EMBED.format(key="gpu-fleet"))

    assert response.status_code == 200


async def test_nothing_is_minted_when_grafana_is_unconfigured(
    client: AsyncClient, world: World
) -> None:
    await act_as(client, "dana")

    overview = (await client.get(OVERVIEW)).json()
    minted = await client.post(EMBED.format(key="my-containers"))

    assert overview["embeds"] == {}
    assert minted.status_code == 404
    assert minted.json()["error"]["code"] == "monitoring_disabled"


async def test_a_minted_url_is_refused_without_a_session(client: AsyncClient, world: World) -> None:
    client.headers.pop("Authorization", None)

    response = await client.post(EMBED.format(key="my-containers"))

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Signing, and the JWKS Grafana fetches
# ---------------------------------------------------------------------------
async def test_the_embed_token_is_signed_with_the_published_key(
    client: AsyncClient, world: World, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The deployment path: RS256, verified against `/.well-known/jwks.json`.

    With the HS256 development fallback the JWKS document is empty and Grafana
    cannot be configured at all, so the asymmetric path is the one that has to
    work — and the only way to know it does is to verify a real token against the
    real document.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )

    monkeypatch.setattr(settings, "jwt_private_key", SecretStr(private_pem))
    monkeypatch.setattr(settings, "jwt_public_key", SecretStr(public_pem))
    monkeypatch.setattr(settings, "grafana_public_url", GRAFANA_BASE)

    await act_as(client, "dana")
    url = (await client.get(OVERVIEW)).json()["embeds"]["my-containers"]["url"]

    jwks = (await client.get("/.well-known/jwks.json")).json()
    assert len(jwks["keys"]) == 1
    jwk = jwks["keys"][0]
    assert jwk["alg"] == "RS256"
    assert jwk["use"] == "sig"

    token = parse_qs(urlparse(url).query)["auth_token"][0]
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256"
    assert header["kid"] == jwk["kid"]

    public_key = jwt.PyJWK(jwk).key  # type: ignore[attr-defined]
    decoded = jwt.decode(
        token,
        public_key,  # type: ignore[arg-type]
        algorithms=["RS256"],
        audience="grafana",
        issuer=settings.jwt_issuer,
    )
    assert decoded["wl_namespaces"] == [world.namespace_a]


async def test_a_monitoring_token_is_not_accepted_as_an_access_token(
    client: AsyncClient, world: World, grafana_on: Settings
) -> None:
    """
    Token confusion is the classic failure of a multi-audience issuer.

    A Grafana token is a bearer credential for one dashboard; pasting it into the
    API must not authenticate anybody.
    """
    await act_as(client, "dana")
    url = (await client.get(OVERVIEW)).json()["embeds"]["my-containers"]["url"]
    stolen = parse_qs(urlparse(url).query)["auth_token"][0]

    client.headers["Authorization"] = f"Bearer {stolen}"
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
async def test_the_inventory_counts_instances_by_status(client: AsyncClient, world: World) -> None:
    await act_as(client, "root2")

    inventory = (await client.get(OVERVIEW)).json()["inventory"]
    counted = {row["status"]: row["count"] for row in inventory["instances"]}

    assert counted.get("running") == 2


async def test_the_inventory_counts_accelerators_by_model(
    client: AsyncClient, seeded: AsyncSession, world: World
) -> None:
    from app.models.resource import ComputeNode, GpuDevice

    node = ComputeNode(
        name="node-1", display_name="节点一", cpu_total_milli=64000, memory_total_mb=262144
    )
    seeded.add(node)
    await seeded.flush()
    seeded.add_all(
        [
            GpuDevice(node_id=node.id, index=0, model="A100", memory_mb=81920),
            GpuDevice(node_id=node.id, index=1, model="A100", memory_mb=81920),
            GpuDevice(node_id=node.id, index=2, model="A100", memory_mb=81920),
        ]
    )
    await seeded.commit()

    await act_as(client, "root2")
    inventory = (await client.get(OVERVIEW)).json()["inventory"]

    assert inventory["nodes"][0]["gpuCount"] == 3
    assert inventory["gpuByModel"] == [{"model": "A100", "total": 3, "allocated": 0}]


async def test_overview_is_one_response(
    client: AsyncClient, world: World, grafana_on: Settings
) -> None:
    """
    One request, not four.

    Four parallel requests can be answered at four different moments; a page
    assembled from them describes a state that never existed.
    """
    await act_as(client, "dana")

    body = (await client.get(OVERVIEW)).json()

    assert set(body) >= {"enabled", "scope", "inventory", "containers", "dashboards", "embeds"}


async def test_the_container_list_shows_where_each_one_runs(
    client: AsyncClient, world: World
) -> None:
    """The pod and namespace are the join keys to Grafana, so the page shows them."""
    await act_as(client, "dana")

    container = (await client.get(OVERVIEW)).json()["containers"][0]

    assert container["namespace"] == world.namespace_a
    assert container["podName"] == world.pod_a
    assert container["ownerUsername"] == "dana"
    assert container["gpuType"] == "A100"
    assert container["gpuCount"] == 1


# ---------------------------------------------------------------------------
# The catalogue and the provisioned files must not drift
# ---------------------------------------------------------------------------
def test_every_catalogued_dashboard_is_actually_provisioned() -> None:
    """
    A uid in the catalogue that no provisioned file defines is a 404 inside the
    iframe — which reads as "monitoring is broken", not as "the deployment is
    inconsistent". The two lists live in different languages and different
    directories, so nothing but this test connects them.
    """
    from pathlib import Path

    from app.monitoring.grafana import DASHBOARDS

    root = Path(__file__).resolve().parent.parent.parent / "deploy" / "grafana" / "dashboards"
    provisioned = {
        json.loads(path.read_text(encoding="utf-8"))["uid"] for path in root.rglob("*.json")
    }

    assert {spec.uid for spec in DASHBOARDS} == provisioned


def test_provisioned_dashboards_declare_the_variables_the_backend_fills() -> None:
    """
    The backend sends `var-namespace` / `var-pod` and nothing else.

    A dashboard that named its variable `ns` would accept the parameter silently
    and ignore it, and every user would see the whole institution. The generator
    enforces the filters; this checks the *names* agree with what the URL writes.
    """
    from pathlib import Path

    from app.monitoring.grafana import DASHBOARDS

    root = Path(__file__).resolve().parent.parent.parent / "deploy" / "grafana" / "dashboards"
    for path in root.rglob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        declared = {item["name"] for item in document["templating"]["list"]}
        spec = next(s for s in DASHBOARDS if s.uid == document["uid"])
        assert declared == set(spec.scoped_to), document["uid"]


# ---------------------------------------------------------------------------
# Failing closed
# ---------------------------------------------------------------------------
async def test_a_user_with_no_scheduled_pod_is_filtered_to_nothing(
    client: AsyncClient, seeded: AsyncSession, world: World, grafana_on: Settings
) -> None:
    """
    The podless case, which is the normal state for the first few seconds of an
    instance's life and the permanent state whenever the reconciler is down.

    The temptation is to omit `var-pod` when there is nothing to put in it, on
    the grounds that an empty filter is no filter. It is not: the pod variable is
    the *only* thing separating a user's containers from their colleagues', and a
    workspace is one namespace shared by the whole group. Omitting it does not
    show nothing, it shows the lab.

    So an empty selection is sent as the literal `none`, which matches no pod.
    This test exists to keep it that way.
    """
    from app.monitoring.grafana import DASHBOARDS_BY_KEY, dashboard_variables

    await add_instance(
        seeded,
        workspace_id=world.workspace_a,
        owner_user_id=world.observer.id,
        status=InstanceStatus.CREATING,
        pod_name=None,
        name="still-starting",
    )
    await seeded.commit()

    await act_as(client, "gazer")
    body = (await client.get(OVERVIEW)).json()

    # The namespace is known — the instance declares it — but no pod exists yet.
    assert body["scope"]["namespaces"] == [world.namespace_a]
    assert body["scope"]["pods"] == []
    assert body["scope"]["empty"] is False

    scoped = body["embeds"]["my-containers"]["url"]
    query = query_of(scoped)
    assert query["var-pod"] == "none"
    assert query["var-namespace"] == world.namespace_a

    # And the same rule at the unit level, where the sentinel is decided.
    from app.services.monitor import MonitorScope

    variables = dashboard_variables(
        MonitorScope(namespaces=(world.namespace_a,), pods=(), unrestricted=False),
        DASHBOARDS_BY_KEY["my-containers"],
    )
    assert variables["var-pod"] == "none"


async def test_several_namespaces_are_joined_into_one_multi_value_selection(
    client: AsyncClient, seeded: AsyncSession, world: World, grafana_on: Settings
) -> None:
    """
    Grafana reads `var-namespace=a,b` as a two-value selection only when the
    variable is multi-value, which is why the dashboard generator refuses to emit
    one that is not. This pins the wire format that assumption rests on.

    The multi-namespace case belongs to a **group administrator**, not to a
    normal user: `workspace_members.user_id` is unique, because the requirement is
    that a person belongs to exactly one workspace. An administrator of a group
    with several workspaces is the one who genuinely watches more than one
    namespace at a time — and is therefore the one for whom a broken multi-value
    filter would silently show a single lab instead of all of them.
    """
    second = Workspace(
        name="alpha-2",
        display_name="A 组第二工作空间",
        group_id=world.lab_a.id,
        k8s_namespace="wl-lab-a-alpha-2",
        status=WorkspaceStatus.ACTIVE,
    )
    seeded.add(second)
    await seeded.flush()
    await add_instance(
        seeded,
        workspace_id=second.id,
        owner_user_id=world.user_a.id,
        pod_name="wl-dev-a-2-def",
        name="dev-a-2",
    )
    await seeded.commit()

    await act_as(client, "boss-a")
    body = (await client.get(OVERVIEW)).json()
    query = query_of(body["embeds"]["lab-overview"]["url"])

    assert set(body["scope"]["namespaces"]) == {world.namespace_a, "wl-lab-a-alpha-2"}
    assert set(query["var-namespace"].split(",")) == {world.namespace_a, "wl-lab-a-alpha-2"}

    # The lab dashboard declares no pod variable, so it is not sent one.
    assert "var-pod" not in query
