"""The instance IDE reverse proxy.

Kept out of ``app/api/v1`` for the same reason the terminal websockets are: it is
not part of the versioned JSON API. It carries whatever an IDE speaks — HTML,
asset bytes, event streams, notebook saves, websockets — and none of that is
expressible as the platform's request/response envelope.
"""

from __future__ import annotations

from app.api.ide.proxy import router as ide_router

__all__ = ["ide_router"]
