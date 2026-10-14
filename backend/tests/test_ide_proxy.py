"""The instance IDE proxy: `/x/{username}/{instance_id}/…`.

What this file is really testing is the shape of the *address* the platform hands
out. Publishing an IDE through an ``Ingress`` gave every instance a URL on a
different hostname, so "open my notebook" left the platform's origin; the proxy
keeps one origin, and most of these tests are about the consequences of that: that
the forwarded path is the path the browser asked for (or deliberately not, for
code-server), that redirects stay inside the prefix, and that nothing the browser
sends leaks into a user's container.

The upstream is a ``MockTransport``, so the bytes are asserted rather than
performed. The websocket half of this route is *not* covered here — it is covered
end to end against a real cluster by running a cell in a real JupyterLab, because
a mocked kernel channel would only prove that two mocks agree with each other.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ide import proxy
from app.core.config import Settings
from app.k8s import FakeK8sGateway, IngressSpec
from app.models.instance import Instance
from tests.test_acceptance_phase3 import World, act_as, build_world, make_instance


@pytest.fixture
async def world(admin_client: httpx.AsyncClient, seeded: AsyncSession, settings: Settings) -> World:
    """Phase 3's world: a lab with a granted pool, and one without.

    Redeclared rather than shared, because ``world`` is a fixture *inside*
    ``test_acceptance_phase3.py`` and pytest does not export it. The alternative —
    moving it into ``conftest.py`` — would put a fixture with nine model objects in
    scope for every test file in the suite.
    """
    return await build_world(admin_client, seeded, settings)


class Upstream:
    """A stand-in for the instance's IDE, recording what it was asked."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []
        #: Tests that care about the reply replace this.
        self.responder: Callable[[httpx.Request], httpx.Response] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.responder is not None:
            return self.responder(request)
        return httpx.Response(
            200,
            text="<title>JupyterLab</title>",
            headers={"content-type": "text/html; charset=utf-8"},
        )

    @property
    def last(self) -> httpx.Request:
        assert self.seen, "nothing was forwarded"
        return self.seen[-1]


@pytest.fixture(autouse=True)
def _clear_address_cache() -> None:
    """The resolved-ClusterIP cache is process-wide, so a stale hit would hide a bug.

    Each test gets a fresh ``FakeK8sGateway`` whose addresses come from the same
    deterministic sequence, so an address cached by the previous test would look
    valid and the proxy would never consult the gateway at all.
    """
    proxy._SERVICE_IPS.clear()


@pytest.fixture
async def upstream(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Upstream]:
    """Point the proxy's shared client at a recording transport."""
    state = Upstream()
    client = httpx.AsyncClient(transport=httpx.MockTransport(state.handle), follow_redirects=False)
    monkeypatch.setattr(proxy, "_client", client)
    yield state
    await client.aclose()


# ---------------------------------------------------------------------------
# The address the platform hands out
# ---------------------------------------------------------------------------
async def test_the_ide_url_is_a_path_on_the_platform_origin(
    client: httpx.AsyncClient, world: World
) -> None:
    """The whole point of the change: no new hostname, so no new origin.

    The returned URL is relative, which is what makes it work unchanged in
    development (Vite proxies ``/x``), in production (nginx proxies ``/x``), and
    behind whatever host the platform happens to be served from.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world)

    response = await client.get(f"/api/v1/instances/{body['id']}/ide-access")

    assert response.status_code == 200, response.text
    # Exact equality, not `startswith`: it also pins down that nothing else came
    # back. The response used to carry `expiresIn` beside a `?ticket=`, and a
    # credential that nothing verifies is the specific thing being removed.
    assert response.json() == {"url": f"/x/dana/{body['id']}/lab"}


@pytest.mark.parametrize(
    ("ide_type", "expected_suffix"),
    [
        # Jupyter has to be sent to the page itself, because ``/`` is a redirect
        # and the redirect is exactly what a shared link should skip.
        ("jupyter", "lab"),
        ("vscode", ""),
        ("webssh", ""),
    ],
)
async def test_the_ide_paths_match_what_each_ide_expects(
    client: httpx.AsyncClient, world: World, ide_type: str, expected_suffix: str
) -> None:
    """One address shape for all three, because the prefix is the platform's.

    Parametrised rather than looped: the pool grants two instances and 4000m of
    CPU, so a single test creating three would fail on quota and prove nothing
    about paths.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world, ideType=ide_type)

    suffix = f"/{expected_suffix}" if expected_suffix else "/"
    assert body["idePath"] == f"/x/dana/{body['id']}{suffix}"


async def test_no_ingress_is_created_any_more(
    client: httpx.AsyncClient, world: World, gateway: FakeK8sGateway
) -> None:
    """The old route is gone, not merely unused.

    An ``Ingress`` left behind would keep a second, unauthenticated door to the
    same IDE — reachable through any ingress controller that appears later, on a
    hostname nobody is watching.
    """
    await act_as(client, "dana")
    await make_instance(client, world)

    assert gateway.ingresses == {}


async def test_the_route_is_at_the_root_and_not_under_the_versioned_prefix(
    client: httpx.AsyncClient, world: World, upstream: Upstream
) -> None:
    """The proxy serves whole applications, so it is not part of the JSON API."""
    await act_as(client, "dana")
    body = await make_instance(client, world)

    assert (await client.get(f"/x/dana/{body['id']}/lab")).status_code == 200

    # Nothing is mounted at the versioned prefix: that surface only speaks the
    # platform's envelope, which is exactly what this route cannot speak.
    response = await client.get(f"/api/v1/x/dana/{body['id']}/lab")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# Forwarding
# ---------------------------------------------------------------------------
async def test_the_proxy_forwards_along_the_same_path(
    client: httpx.AsyncClient, world: World, upstream: Upstream
) -> None:
    """Jupyter is told its prefix, so it must receive the full path."""
    await act_as(client, "dana")
    body = await make_instance(client, world)

    response = await client.get(f"/x/dana/{body['id']}/lab")

    assert response.status_code == 200, response.text
    assert "<title>JupyterLab</title>" in response.text
    assert upstream.last.url.path == f"/x/dana/{body['id']}/lab"
    # A ClusterIP: the IDE is on no node address, so the only way in is here.
    assert upstream.last.url.host.startswith("10.43.")
    assert upstream.last.url.port == 8888


async def test_the_query_string_and_assets_survive(
    client: httpx.AsyncClient, world: World, upstream: Upstream
) -> None:
    """Every asset and every API call on the page is another request through here."""
    await act_as(client, "dana")
    body = await make_instance(client, world)

    await client.get(f"/x/dana/{body['id']}/static/lab/index.js?v=1&x=2")

    assert upstream.last.url.path == f"/x/dana/{body['id']}/static/lab/index.js"
    assert upstream.last.url.query == b"v=1&x=2"


async def test_code_server_is_reached_with_the_prefix_removed(
    client: httpx.AsyncClient, world: World, upstream: Upstream
) -> None:
    """code-server cannot be told to serve a subpath, so the proxy hides it.

    This is the one place the three IDEs differ, and it is not a preference:
    ``--ServerApp.base_url`` exists for Jupyter and ``--base-path`` for ttyd, and
    code-server has no equivalent. It serves from the root or not at all.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world, ideType="vscode")

    response = await client.get(f"/x/dana/{body['id']}/static/main.js")

    assert response.status_code == 200, response.text
    assert upstream.last.url.path == "/static/main.js"
    assert upstream.last.url.port == 8080


async def test_a_stripped_prefix_is_put_back_on_a_redirect(
    client: httpx.AsyncClient, world: World, upstream: Upstream
) -> None:
    """Otherwise the browser is sent to the platform root and shown the SPA.

    The upstream answers from its own root, because it does not know it is behind
    a prefix; every ``Location`` it produces would leave ``/x/`` without this.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world, ideType="vscode")
    upstream.responder = lambda _: httpx.Response(302, headers={"location": "/login"})

    response = await client.get(f"/x/dana/{body['id']}/")

    assert response.status_code == 302
    assert response.headers["location"] == f"/x/dana/{body['id']}/login"


async def test_webssh_is_left_alone_because_it_was_told_the_prefix(
    client: httpx.AsyncClient, world: World, upstream: Upstream
) -> None:
    """ttyd gets ``--base-path``, so nothing is stripped and no rewrite is needed."""
    await act_as(client, "dana")
    body = await make_instance(client, world, ideType="webssh")

    response = await client.get(f"/x/dana/{body['id']}/token")

    assert response.status_code == 200, response.text
    assert upstream.last.url.path == f"/x/dana/{body['id']}/token"
    assert upstream.last.url.port == 7681


# ---------------------------------------------------------------------------
# What must not reach the container
# ---------------------------------------------------------------------------
async def test_the_platforms_own_credentials_never_reach_the_instance(
    client: httpx.AsyncClient, world: World, upstream: Upstream
) -> None:
    """A user's access token is for the platform, not for their container.

    Forwarding it would put a platform credential inside a workload the user
    controls, where it could be read out of a header dump, a request log or an
    error page — and the IDEs do not use it.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world)

    await client.get(
        f"/x/dana/{body['id']}/lab",
        headers={
            "Origin": "http://evil.example",
            "Authorization": "Bearer platform-token-must-not-travel",
            "Referer": "http://evil.example/",
        },
    )

    sent = upstream.last.headers
    assert "authorization" not in sent
    assert "origin" not in sent
    # Rewritten, not passed through: the upstream's own origin check compares the
    # `Host` it sees against the `Origin` it is handed, and those have to agree.
    assert sent["host"].startswith("10.43.")
    # Ordinary headers still travel, or nothing would work.
    assert sent["referer"] == "http://evil.example/"


# ---------------------------------------------------------------------------
# Failure, and what a browser is told about it
# ---------------------------------------------------------------------------
async def test_every_kind_of_no_is_the_same_no(
    client: httpx.AsyncClient, world: World, upstream: Upstream
) -> None:
    """Unknown id, wrong owner and no-IDE must be indistinguishable.

    The id is a sequential integer, so an error that told them apart would let
    anyone enumerate which instances exist. It is also why the check that will
    eventually go here belongs in ``_resolve_target`` alone.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world)
    without_ide = await make_instance(client, world, name="plain", ideType="none")

    for path in (
        f"/x/dana/{body['id'] + 5000}/lab",
        # Right instance, wrong owner.
        f"/x/erin/{body['id']}/lab",
        f"/x/dana/{without_ide['id']}/lab",
    ):
        response = await client.get(path)
        assert response.status_code == 404, path
        assert upstream.seen == [], path

    assert response.text == "没有这个实例，或它没有 Web IDE。"


async def test_a_stopped_instance_says_so_instead_of_denying_it_exists(
    client: httpx.AsyncClient, world: World, upstream: Upstream
) -> None:
    """The caller can see this instance in the platform, so "no such instance" is a lie.

    ``stop`` scales the deployment to zero but leaves ``is_active`` true, so the
    row still resolves — and forwarding anyway would mean connecting to a Service
    with no endpoints and reporting it as "IDE unreachable", blaming the container
    for a decision the user made.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world)
    stopped = await client.post(f"/api/v1/instances/{body['id']}/stop")
    assert stopped.status_code == 200, stopped.text

    response = await client.get(f"/x/dana/{body['id']}/lab")

    assert response.status_code == 503
    assert "未在运行" in response.text
    assert upstream.seen == []


async def test_a_service_without_an_address_gets_a_page_not_a_json_error(
    client: httpx.AsyncClient,
    world: World,
    gateway: FakeK8sGateway,
    upstream: Upstream,
) -> None:
    """This is opened in a browser tab, so the envelope would be unreadable.

    A Service has no ClusterIP for a moment after creation, which is exactly when
    a user who just clicked 启动 will hit it.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world)
    gateway.service_ips.clear()

    response = await client.get(f"/x/dana/{body['id']}/lab")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("text/html")
    assert "IDE 暂不可用" in response.text
    assert upstream.seen == []


async def test_an_unreachable_ide_is_reported_and_the_address_is_forgotten(
    client: httpx.AsyncClient, world: World, upstream: Upstream
) -> None:
    """A repaired instance gets a new ClusterIP; a cached one would wedge the tab."""

    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    upstream.responder = refuse
    await act_as(client, "dana")
    body = await make_instance(client, world)

    response = await client.get(f"/x/dana/{body['id']}/lab")

    assert response.status_code == 503
    assert "无法连接" in response.text
    # The cache must not still hold the address that just failed.
    assert proxy._SERVICE_IPS == {}


async def test_the_bare_path_is_sent_to_its_trailing_slash_form(
    client: httpx.AsyncClient, world: World
) -> None:
    """Relative asset URLs resolve one level too high without the slash.

    At ``/x/dana/7``, ``./static/x.js`` means ``/x/dana/static/x.js``; at
    ``/x/dana/7/`` it means ``/x/dana/7/static/x.js``. Every IDE builds its asset
    URLs that way, so this redirect is load-bearing rather than cosmetic.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world)

    response = await client.get(f"/x/dana/{body['id']}", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == f"/x/dana/{body['id']}/"


# ---------------------------------------------------------------------------
# The gap, written down
# ---------------------------------------------------------------------------
async def test_the_proxy_route_carries_no_authentication_yet(
    client: httpx.AsyncClient, world: World, upstream: Upstream
) -> None:
    """This test asserts a *gap*, deliberately, so that it cannot be forgotten.

    It is not an endorsement. Today ``/x/`` is an open door for anyone who can
    reach the platform: no ticket, no session, no header. When authentication is
    added, this is the test that has to be inverted — and a failing test is a
    better reminder than a comment, because it cannot be scrolled past.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world)

    client.headers.pop("Authorization", None)
    response = await client.get(f"/x/dana/{body['id']}/lab")

    assert response.status_code == 200, response.text
    assert upstream.seen, "the request was forwarded with no credential at all"


async def test_the_shared_client_is_closed_with_the_application() -> None:
    """Otherwise every shutdown leaves sockets to a ClusterIP range behind."""
    from app.api.ide.proxy import close_ide_http_client

    assert proxy._http_client() is not None
    await close_ide_http_client()
    assert proxy._client is None
    # Idempotent: the lifespan also runs for tests that never opened one.
    await close_ide_http_client()


async def test_a_legacy_ingress_is_still_deleted(
    client: httpx.AsyncClient,
    world: World,
    seeded: AsyncSession,
    gateway: FakeK8sGateway,
) -> None:
    """Rows created before the proxy existed still name an Ingress, and must lose it.

    ``instance.k8s_ingress`` outlived the code that wrote it, on purpose:
    ``delete_instance`` builds its list of objects to remove from those columns, so
    dropping the field along with the rest of the ingress code would have left real
    Ingresses running in real namespaces with nothing left that knew their names.
    This is the only reader of that column now, which is exactly why it is worth a
    test of its own.
    """
    await act_as(client, "dana")
    body = await make_instance(client, world)

    # Pretend this row predates the proxy.
    instance = await seeded.get(Instance, body["id"])
    assert instance is not None
    legacy_name = f"wl-dev-{body['id']}-ide"
    instance.k8s_ingress = legacy_name
    await seeded.commit()
    gateway.ingresses[f"wl-lab-a-alpha/{legacy_name}"] = IngressSpec(
        name=legacy_name,
        namespace="wl-lab-a-alpha",
        host="",
        path=f"/x/{body['id']}/",
        service_name=f"wl-dev-{body['id']}-ide",
        service_port=8888,
    )

    removed = await client.delete(f"/api/v1/instances/{body['id']}")

    assert removed.status_code == 200, removed.text
    assert gateway.ingresses == {}


def test_the_websocket_handshake_headers_are_not_forwarded() -> None:
    """A bug that only a real cluster exposed, pinned down here.

    ``websockets.connect`` generates ``Sec-WebSocket-Key`` and
    ``Sec-WebSocket-Version`` itself. Forwarding the caller's copies too sends the
    upstream two of each, and tornado refuses that handshake with a 400 before any
    of the IDE's code runs. The visible symptom was a notebook that loaded fine and
    then could not run a cell — the kernel channel is a websocket, and it never
    opened. Nothing in a mocked-HTTP test could see it, so the assertion is on the
    header list directly.
    """
    from starlette.datastructures import Headers

    handshake = Headers(
        raw=[
            (b"host", b"127.0.0.1:8000"),
            (b"upgrade", b"websocket"),
            (b"connection", b"Upgrade"),
            (b"sec-websocket-key", b"dGhlIHNhbXBsZSBub25jZQ=="),
            (b"sec-websocket-version", b"13"),
            (b"sec-websocket-extensions", b"permessage-deflate"),
            (b"sec-websocket-protocol", b"tty"),
            (b"origin", b"http://127.0.0.1:5181"),
            (b"authorization", b"Bearer platform-token"),
            (b"cookie", b"session=abc"),
            (b"user-agent", b"probe/1.0"),
        ]
    )

    forwarded = proxy._forward_headers(handshake)
    names = [name.lower() for name, _ in forwarded]

    assert [n for n in names if n.startswith("sec-websocket-")] == []
    # The proxy's own decisions: what the upstream compares against must be what it
    # is, and the platform's credential is not for the container.
    assert "host" not in names
    assert "origin" not in names
    assert "authorization" not in names
    assert "upgrade" not in names
    assert "connection" not in names
    # What has to survive, or the IDE would not see the user at all.
    assert dict(forwarded)["cookie"] == "session=abc"
    assert dict(forwarded)["user-agent"] == "probe/1.0"
