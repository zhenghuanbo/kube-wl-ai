"""Interactive terminal over a WebSocket.

Protocol
--------
The browser cannot set an ``Authorization`` header on a WebSocket, so the access
token is passed as the ``token`` query parameter and verified exactly as the
HTTP path verifies it — including the per-request re-read of roles, so revoking
``instance:exec`` closes the door at the next connection.

Once open, frames are split by *type* rather than by content:

* **binary** frames are raw keystrokes, written straight to the container's
  stdin;
* **text** frames are control messages — today only ``resize``.

That split is deliberate. The alternative, sniffing text frames for JSON, breaks
the moment a user pastes something that looks like JSON into their shell, which
is exactly when a terminal must not misbehave.

The container side is a blocking socket, so it is driven from a worker thread
and handed back through a queue; the event loop is never blocked on it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.auth.tokens import decode_access_token
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.k8s import ExecSession, K8sError, get_gateway, run_gateway
from app.k8s.naming import INSTANCE_LABEL
from app.models.enums import InstanceStatus
from app.models.user import User

logger = get_logger(__name__)

router = APIRouter()

#: How long the terminal waits for container output before looping. Short
#: enough that a close is noticed promptly, long enough not to spin.
POLL_SECONDS = 0.1

#: Close codes the browser receives. 1008 is "policy violation".
WS_POLICY_VIOLATION = 1008
WS_INTERNAL_ERROR = 1011


async def _authenticate(session: AsyncSession, token: str | None) -> User:
    """Resolve the WebSocket's user, or raise :class:`AppError`."""
    from app.core.errors import ForbiddenError, UnauthorizedError

    if not token:
        raise UnauthorizedError("请先登录", code="missing_token")

    settings = get_settings()
    decoded = decode_access_token(token, settings)
    user = await session.get(User, decoded.user_id)
    if user is None:
        raise UnauthorizedError("用户不存在", code="invalid_token")
    if not user.is_active or user.is_expired:
        raise ForbiddenError("账户不可用", code="account_disabled")
    return user


async def _reject(websocket: WebSocket, close_code: int, message: str) -> None:
    """Tell the client why, without leaking whether an id exists."""
    with contextlib.suppress(RuntimeError):
        await websocket.send_json({"type": "error", "message": message})
    with contextlib.suppress(RuntimeError):
        await websocket.close(code=close_code)


@router.websocket("/ws/terminal/{instance_id}")
async def terminal(websocket: WebSocket, instance_id: int) -> None:
    await websocket.accept()

    token = websocket.query_params.get("token")
    factory = get_session_factory()

    async with factory() as session:
        try:
            user = await _authenticate(session, token)
        except AppError as exc:
            await _reject(websocket, WS_POLICY_VIOLATION, exc.message)
            return

        # Imported here rather than at module scope: the service module imports
        # the gateway package, and the websocket router is imported from the
        # same package that owns the gateway.
        from app.services import instances as instance_service

        try:
            instance = await instance_service.get_instance(session, user, instance_id)
            policy.require_permission(user, "instance:exec")
        except AppError as exc:
            await _reject(websocket, WS_POLICY_VIOLATION, exc.message)
            return

        if instance.status is not InstanceStatus.RUNNING:
            await _reject(
                websocket,
                WS_POLICY_VIOLATION,
                f"实例当前状态为 {instance.status.value}，无法打开终端",
            )
            return

        namespace = instance_service.instance_workspace_ns(instance)
        gateway = get_gateway()
        try:
            pods = await run_gateway(
                gateway.list_pods, namespace, f"{INSTANCE_LABEL}={instance.name}"
            )
        except K8sError as exc:
            await _reject(websocket, WS_INTERNAL_ERROR, f"无法连接实例：{exc}")
            return

        ready = next((pod for pod in pods if pod.ready), None)
        if ready is None:
            await _reject(websocket, WS_POLICY_VIOLATION, "容器尚未就绪")
            return

        rows = _int_param(websocket, "rows", 24)
        cols = _int_param(websocket, "cols", 80)
        try:
            exec_session = await run_gateway(
                gateway.open_exec, namespace, ready.name, rows=rows, cols=cols
            )
        except K8sError as exc:
            await _reject(websocket, WS_INTERNAL_ERROR, f"打开终端失败：{exc}")
            return

        logger.info(
            "terminal_opened",
            instance=instance.name,
            namespace=namespace,
            user=user.username,
        )
        try:
            await _bridge(websocket, exec_session)
        finally:
            exec_session.close()
            logger.info("terminal_closed", instance=instance.name, user=user.username)


def _int_param(websocket: WebSocket, name: str, default: int) -> int:
    raw = websocket.query_params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    # Clamp: a bogus geometry must not become a bogus ioctl in the container.
    return max(1, min(value, 1000))


async def _bridge(websocket: WebSocket, session: ExecSession) -> None:
    """Pump bytes both ways until either side finishes."""
    to_container: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def read_from_client() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if (data := message.get("bytes")) is not None:
                    await to_container.put(data)
                elif (text := message.get("text")) is not None:
                    _handle_control(session, text)
        finally:
            await to_container.put(None)

    async def write_to_client() -> None:
        while True:
            # Off the event loop: the Kubernetes exec client blocks.
            chunk = await asyncio.to_thread(session.recv, POLL_SECONDS)
            if chunk is None:
                return
            if chunk:
                await websocket.send_bytes(chunk)

    async def drain_to_container() -> None:
        while True:
            data = await to_container.get()
            if data is None:
                return
            await asyncio.to_thread(session.send, data)

    tasks = [
        asyncio.create_task(read_from_client()),
        asyncio.create_task(write_to_client()),
        asyncio.create_task(drain_to_container()),
    ]
    try:
        # First to finish decides the session is over; the rest are cancelled so
        # a half-closed socket cannot keep the handler alive.
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            with contextlib.suppress(WebSocketDisconnect, K8sError, RuntimeError):
                task.result()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await websocket.close()


def _handle_control(session: ExecSession, text: str) -> None:
    """Apply a control frame; unknown types are ignored, not fatal."""
    try:
        message = json.loads(text)
    except json.JSONDecodeError:
        return
    if not isinstance(message, dict) or message.get("type") != "resize":
        return
    rows = message.get("rows")
    cols = message.get("cols")
    if isinstance(rows, int) and isinstance(cols, int) and rows > 0 and cols > 0:
        with contextlib.suppress(K8sError):
            session.resize(rows, cols)
