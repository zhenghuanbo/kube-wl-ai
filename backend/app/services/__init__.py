"""Business logic.

Routers stay thin: parse, authorise, delegate, serialise. Anything that touches
more than one table, enforces an invariant, or needs to run inside the caller's
transaction lives here.
"""
