"""Publishing an instance's web IDE on the platform's own origin.

Why a proxy and not an ``Ingress``
----------------------------------
The IDE used to be published by an ``Ingress`` object at ``/x/{id}/``. That only
works if two things are true at once: the cluster has an ingress controller, and
the platform's origin *is* that controller's host. The second is the problem — it
means the address a user is given has a different hostname from the platform, so
"open my notebook" leaves the origin, and every deployment has to invent and
resolve a new name.

This route keeps one origin. A browser asks the platform for
``/x/{username}/{instance_id}/…``, and the platform forwards to the instance's
ClusterIP Service. The frontend already proxies ``/x`` to this backend (see
``frontend/vite.config.ts`` and ``frontend/nginx.conf``), so the URL has the same
shape in development and in production, and ``ide_path()`` can stay relative.

WHAT GUARDS THIS: nothing, yet
------------------------------
Be clear about the state of this, because it is the one thing here that is not
finished. There is **no authentication on this route**. Anyone who can reach the
platform's origin can open anyone's IDE by knowing a username and an instance id
— and the id is a sequential integer, so the username is the only secret, and
usernames are visible to colleagues.

That would be survivable if the IDEs were locked, but they are not: the platform
starts them with their own authentication disabled (``--ServerApp.token=``,
``code-server --auth none``, ``ttyd --writable``), on the reasoning that the layer
in front would authenticate. The ticket that was supposed to do it was minted and
then never verified — ``decode_ide_ticket`` is called only from tests. So both
layers are open, and this route is the outer one.

Deliberately not papered over here. A credential nothing checks is worse than no
credential: it looks like protection, it leaks into browser history and proxy
logs, and it discourages anyone from asking the question. The seam is kept
instead — :func:`_resolve_target` is the single place a caller is identified, and
the ticket functions are still in ``app/auth/tokens.py``, tested, waiting for a
verification step that would be a few lines in one function.

What the proxy *does* guarantee, and it is not nothing: the instance is reached
through a ClusterIP Service, so no IDE listens on a node address. Exposure is one
port on the platform's own host rather than every node's IP — and the platform can
decide per request, which is the property an ingress could never have given it.

Notes on the forwarding itself
------------------------------
* **``Origin`` is dropped.** The upstreams enforce same-origin checks on their
  websockets — Jupyter defers to tornado, which compares the ``Origin`` host
  against the ``Host`` header. The browser's origin is the platform while the
  upstream sees its own address, so forwarding it produces a 403 on exactly the
  request a notebook cannot work without: the kernel channel. The header is
  meaningless on this leg anyway, since this proxy is not a browser and the
  browser-to-proxy leg is same-origin by construction. Dropping it makes the
  proxied request the same shape as the direct ``curl`` that was verified to work.
* **``Authorization`` is dropped.** The platform's own access token must never
  reach a user's container, and the IDEs do not use it.
* **``Host`` is rewritten** to the upstream address, for the same reason: what the
  upstream compares against has to be what it is.
* **Responses are streamed**, and ``content-encoding``/``content-length`` are
  dropped because httpx has already decoded the body. Forwarding the original
  length would truncate or hang the browser on a compressed response.
* **Redirects are passed through, never followed.** A ``Location`` from Jupyter
  (``/x/user/7/lab``) is already a valid path on the platform's origin. For
  code-server the prefix was stripped on the way in, so a root-relative
  ``Location`` is re-prefixed on the way out — otherwise the browser would be sent
  to the platform root.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx
import websockets
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.datastructures import MutableHeaders
from websockets.typing import Subprotocol

from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.k8s import IDE_STRIPS_PREFIX, K8sGateway, get_gateway, run_gateway
from app.k8s.workloads import IDE_PORTS, ide_base_path
from app.models.enums import IdeType, InstanceStatus
from app.models.instance import Instance

logger = get_logger(__name__)

router = APIRouter()

#: How long a resolved ClusterIP is reused. A Service's address is stable for the
#: instance's life, but a repair deletes and recreates it, so this cannot be
#: forever. Short enough that a repair heals itself, long enough that loading a
#: notebook is not an API call per asset — which it otherwise would be, since one
#: JupyterLab page pulls in dozens of files.
SERVICE_IP_TTL_SECONDS = 30.0

#: Forwarded request timeouts. The read timeout is deliberately huge and matches
#: what the ingress this replaced was configured with: a kernel channel or a
#: terminal socket can sit idle for an hour and must not be reaped. The connect
#: timeout stays short, because a Service that does not answer promptly is down
#: rather than slow.
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 3600.0

#: Headers that describe the hop rather than the message, plus the ones the proxy
#: deliberately owns (see the module docstring).
_DROP_REQUEST_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        # Owned by the proxy, not by the caller.
        "host",
        "origin",
        "authorization",
        # Recomputed by httpx from the body it is given.
        "content-length",
    }
)

_DROP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        # httpx has already decoded the body; keeping either would lie to the
        # browser about what it is receiving.
        "content-encoding",
        "content-length",
    }
)


def _forward_headers(headers: Any) -> list[tuple[str, str]]:
    """The caller's headers, minus the ones this hop owns.

    ``sec-websocket-*`` is the part that is easy to get wrong. The ``websockets``
    client generates ``Sec-WebSocket-Key`` and ``Sec-WebSocket-Version`` itself and
    negotiates extensions itself, so forwarding the caller's copies sends the
    upstream two of each — and tornado refuses that handshake outright with a 400,
    before any of the IDE's own code runs. The symptom is a websocket that cannot
    connect at all, which for Jupyter means a notebook that opens and then cannot
    run a cell.

    The subprotocol is deliberately *not* forwarded here either: it has to be
    forwarded as a handshake argument instead, so that the client library can put
    the upstream's answer back in the reply to the browser.
    """
    forwarded: list[tuple[str, str]] = []
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in _DROP_REQUEST_HEADERS or lowered.startswith("sec-websocket-"):
            continue
        forwarded.append((key, value))
    return forwarded


METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

#: ``(namespace, service) -> (monotonic timestamp, cluster IP)``.
_SERVICE_IPS: dict[tuple[str, str], tuple[float, str]] = {}

#: One client for the whole process, so that the dozens of asset requests behind a
#: single page reuse connections instead of opening one each. Created lazily; the
#: lifespan closes it.
_client: httpx.AsyncClient | None = None


def _http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT_SECONDS,
                read=READ_TIMEOUT_SECONDS,
                write=READ_TIMEOUT_SECONDS,
                pool=CONNECT_TIMEOUT_SECONDS,
            ),
            # The browser's redirect has to be the browser's decision: Jupyter's
            # `/x/u/7/` -> `/x/u/7/lab` has to reach the address bar.
            follow_redirects=False,
            # No proxy settings from the environment. This leg goes to a ClusterIP
            # address; an inherited HTTP_PROXY would send it somewhere else.
            trust_env=False,
        )
    return _client


async def close_ide_http_client() -> None:
    """Release the shared client. Called from the application lifespan."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


@dataclass(frozen=True, slots=True)
class Target:
    """Where one request has to go, once the instance is resolved."""

    namespace: str
    service_name: str
    port: int
    #: The external prefix, always ending in ``/``.
    base_path: str
    #: Whether the prefix must be removed before the request reaches the upstream.
    strips_prefix: bool

    def upstream_path(self, rest: str) -> str:
        """The path to request from the upstream, given the caller's tail."""
        return f"/{rest}" if self.strips_prefix else f"{self.base_path}{rest}"

    def external_path(self, upstream_location: str) -> str:
        """Rewrite a ``Location`` from the upstream into a platform-origin path."""
        if not self.strips_prefix or not upstream_location.startswith("/"):
            return upstream_location
        # ``//host`` is protocol-relative, not a path; rewriting it would produce
        # nonsense.
        if upstream_location.startswith("//"):
            return upstream_location
        return f"{self.base_path.rstrip('/')}{upstream_location}"


#: Statuses under which the deployment is deliberately scaled to zero, so there is
#: provably nothing to connect to. Kept as an explicit list rather than "anything
#: that is not RUNNING", because ``CREATING`` and ``PENDING`` mean "not yet" — the
#: Service exists and the honest answer is "try again shortly", which is what an
#: attempted connection will produce anyway.
NOT_RUNNING_STATUSES = frozenset(
    {InstanceStatus.STOPPED, InstanceStatus.STOPPING, InstanceStatus.DELETING}
)


class Refusal(StrEnum):
    """Why a path does not lead anywhere, at the level of detail a browser gets."""

    #: No such instance, not this user's instance, or no IDE at all. One answer
    #: for all three, on purpose.
    NOT_FOUND = "not_found"
    #: It exists and it is this user's, but it is not running.
    NOT_RUNNING = "not_running"


async def _resolve_target(username: str, instance_id: int) -> Target | Refusal:
    """Find the instance this path names. **The auth seam.**

    This is the one place that decides whether a caller may reach an instance, so
    it is the one place a check has to be added. Today it establishes only facts
    about the *instance* — that it exists, that the named user owns it, that it is
    running, and that it has an IDE — never anything about the caller.

    ``NOT_FOUND`` covers "no such instance", "not yours" and "no IDE"
    indistinguishably, so that a stranger cannot use the difference to enumerate
    ids: the id is a sequential integer, so an error that told them apart would be
    a directory listing. ``NOT_RUNNING`` is deliberately *separate*, because the
    caller who reaches it already knows the instance exists — they were looking at
    it in the platform a moment ago — and answering "没有这个实例" to someone who
    just clicked 访问 on their own stopped instance is simply a lie.
    """
    factory = get_session_factory()
    async with factory() as session:
        instance = await session.get(Instance, instance_id)
        if instance is None:
            return Refusal.NOT_FOUND
        # The username is part of the address, so it has to agree with the owner.
        # Not a permission check — just what makes the URL honest.
        if instance.owner is None or instance.owner.username != username:
            return Refusal.NOT_FOUND
        if not instance.is_active:
            return Refusal.NOT_FOUND
        if instance.ide_type is IdeType.NONE:
            return Refusal.NOT_FOUND
        if not instance.k8s_ide_service:
            # Built before the IDE Service existed, or the apply failed. Either
            # way there is nothing to forward to yet.
            return Refusal.NOT_FOUND
        if instance.status in NOT_RUNNING_STATUSES:
            return Refusal.NOT_RUNNING
        port = IDE_PORTS.get(instance.ide_type)
        if port is None:  # pragma: no cover - every non-NONE type has a port
            return Refusal.NOT_FOUND
        return Target(
            namespace=instance.workspace.k8s_namespace,
            service_name=instance.k8s_ide_service,
            port=port,
            base_path=ide_base_path(instance.owner.username, instance.id),
            strips_prefix=IDE_STRIPS_PREFIX.get(instance.ide_type, False),
        )


async def _service_address(gateway: K8sGateway, target: Target) -> str | None:
    """The ``host:port`` to forward to, cached briefly."""
    key = (target.namespace, target.service_name)
    now = time.monotonic()
    cached = _SERVICE_IPS.get(key)
    if cached is not None and now - cached[0] < SERVICE_IP_TTL_SECONDS:
        return f"{cached[1]}:{target.port}"

    info = await run_gateway(gateway.read_service, target.namespace, target.service_name)
    if info is None or not info.cluster_ip:
        # A Service without an address yet is normal for a moment after creation,
        # and permanent for a headless one. Either way: not ready.
        return None
    _SERVICE_IPS[key] = (now, info.cluster_ip)
    return f"{info.cluster_ip}:{target.port}"


def _forget_address(target: Target) -> None:
    """Drop a cached address after a failed connection, so the next try re-reads.

    Without this, a repaired instance whose Service was recreated behind a new
    address would keep failing until the TTL expired, and the user would see a
    broken IDE with no reason to believe retrying could help.
    """
    _SERVICE_IPS.pop((target.namespace, target.service_name), None)


def _not_ready(reason: str) -> HTMLResponse:
    """A page a person can read, because this is opened in a browser tab.

    The platform's JSON error envelope is the wrong shape here: whoever sees this
    did not make an API call, they clicked a button.
    """
    return HTMLResponse(
        status_code=503,
        content=(
            "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>IDE 暂不可用</title></head>"
            "<body style='font-family:sans-serif;padding:2rem;line-height:1.6'>"
            "<h1 style='font-size:1.25rem'>IDE 暂不可用</h1>"
            f"<p>{reason}</p>"
            "<p>实例可能仍在创建中，稍后刷新本页即可。</p>"
            "</body></html>"
        ),
    )


@router.get("/x/{username}/{instance_id}", include_in_schema=False)
async def ide_redirect(username: str, instance_id: int, request: Request) -> Response:
    """Send the bare path to its trailing-slash form.

    Not cosmetic. Every IDE resolves its asset URLs *relative* to the page, so at
    ``/x/user/7`` a reference to ``./static/x.js`` resolves to ``/x/user/static/``
    — one level up, and wrong. At ``/x/user/7/`` it resolves correctly.
    """
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/x/{username}/{instance_id}/{query}", status_code=307)


# `include_in_schema=False` is not tidiness. FastAPI derives a route's operation id
# from `list(route.methods)[0]` alone, so a single route declaring seven methods
# collides with itself six times and OpenAPI generation raises — which surfaces as
# a 500 on `/api/v1/openapi.json` and therefore on `/docs`. And even without that,
# this route has no business in the platform's API document: it does not speak the
# envelope, its responses are whatever the IDE sends, and a client generated from
# the schema would be misled about both.
@router.api_route(
    "/x/{username}/{instance_id}/{rest:path}", methods=METHODS, include_in_schema=False
)
async def ide_proxy(username: str, instance_id: int, rest: str, request: Request) -> Response:
    """Forward one HTTP request to an instance's IDE."""
    target = await _resolve_target(username, instance_id)
    if isinstance(target, Refusal):
        # 404, not 403: see `_resolve_target`.
        if target is Refusal.NOT_RUNNING:
            return _not_ready("该实例当前未在运行，请先启动它。")
        return PlainTextResponse("没有这个实例，或它没有 Web IDE。", status_code=404)

    gateway = get_gateway()
    address = await _service_address(gateway, target)
    if address is None:
        return _not_ready("实例的 IDE 服务还没有地址。")

    url = f"http://{address}{target.upstream_path(rest)}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = _forward_headers(request.headers)

    client = _http_client()
    try:
        upstream_request = client.build_request(
            request.method,
            url,
            headers=headers,
            # Streamed, not buffered: a notebook upload should not be held in the
            # API process's memory in full first.
            content=request.stream(),
        )
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        _forget_address(target)
        logger.warning(
            "ide_proxy_unreachable",
            instance=instance_id,
            service=f"{target.namespace}/{target.service_name}",
            error=type(exc).__name__,
        )
        return _not_ready("无法连接实例的 IDE，它可能正在重启。")

    out_headers = MutableHeaders()
    for key, value in upstream.headers.multi_items():
        if key.lower() in _DROP_RESPONSE_HEADERS:
            continue
        out_headers.append(key, target.external_path(value) if key.lower() == "location" else value)

    if request.method == "HEAD":
        # No body to stream, and the generator below would never be iterated —
        # which would leave the upstream response open until the pool reaped it.
        await upstream.aclose()
        return Response(status_code=upstream.status_code, headers=out_headers)

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(content=body(), status_code=upstream.status_code, headers=out_headers)


@router.websocket("/x/{username}/{instance_id}/{rest:path}")
async def ide_proxy_socket(
    websocket: WebSocket, username: str, instance_id: int, rest: str
) -> None:
    """Forward a WebSocket to an instance's IDE.

    Not optional for any of the three IDEs: a Jupyter kernel channel, a
    code-server extension host, and the whole of ttyd's terminal are websockets.
    Proxying only HTTP would produce a notebook that loads but cannot run a cell.
    """
    target = await _resolve_target(username, instance_id)
    if isinstance(target, Refusal):
        # 1008 is "policy violation"; the reason is deliberately vague, as above.
        await websocket.close(code=1008 if target is Refusal.NOT_FOUND else 1011)
        return

    gateway = get_gateway()
    address = await _service_address(gateway, target)
    if address is None:
        await websocket.close(code=1011)
        return

    url = f"ws://{address}{target.upstream_path(rest)}"
    if websocket.url.query:
        url = f"{url}?{websocket.url.query}"

    headers = _forward_headers(websocket.headers)
    # The browser offers subprotocols and the upstream may pick one; the handshake
    # back to the browser has to name the same one or it will refuse the socket.
    offered = websocket.headers.get("sec-websocket-protocol")
    subprotocols: list[Subprotocol] | None = (
        [Subprotocol(item.strip()) for item in offered.split(",")] if offered else None
    )

    try:
        upstream = await websockets.connect(
            url,
            additional_headers=headers,
            subprotocols=subprotocols,
            open_timeout=CONNECT_TIMEOUT_SECONDS,
            # Notebook output and terminal scrollback are routinely larger than
            # the 1 MiB default, and a silently dropped frame is far worse than
            # the memory it costs.
            max_size=None,
        )
    except (OSError, websockets.exceptions.WebSocketException) as exc:
        _forget_address(target)
        logger.warning(
            "ide_proxy_socket_unreachable",
            instance=instance_id,
            service=f"{target.namespace}/{target.service_name}",
            error=type(exc).__name__,
        )
        await websocket.close(code=1011)
        return

    await websocket.accept(subprotocol=upstream.subprotocol)

    async def client_to_upstream() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if text is not None:
                await upstream.send(text)
                continue
            data = message.get("bytes")
            if data is not None:
                await upstream.send(data)

    async def upstream_to_client() -> None:
        async for message in upstream:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)

    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]
    try:
        # Either direction ending means the session is over; waiting for the other
        # would leave a task reading a socket nobody is on.
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await upstream.close()
