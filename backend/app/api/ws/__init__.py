"""WebSocket endpoints.

Kept out of ``app/api/v1`` because they are not part of the versioned JSON API:
they carry a browser-terminal protocol rather than the REST envelope, and mixing
the two would confuse the OpenAPI document, which cannot express a websocket.
"""

from __future__ import annotations

from app.api.ws.terminal import router as terminal_router

__all__ = ["terminal_router"]
