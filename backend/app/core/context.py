"""Request-scoped context variables.

Kept separate from the middleware so services can read the current request id
without importing web-layer modules.
"""

from __future__ import annotations

from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
client_ip_var: ContextVar[str | None] = ContextVar("client_ip", default=None)


def get_request_id() -> str | None:
    """The current request id, or ``None`` outside a request (e.g. in a worker)."""
    return request_id_var.get()


def get_client_ip() -> str | None:
    return client_ip_var.get()
