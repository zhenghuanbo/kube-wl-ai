"""Pure-ASGI middleware.

``BaseHTTPMiddleware`` runs the downstream application in its own task, which
breaks context variables and interferes with streaming responses — both of which
this platform relies on (SSE log tails, WebSocket terminals). Everything here is
therefore implemented directly against the ASGI interface.
"""
