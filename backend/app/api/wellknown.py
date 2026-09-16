"""Root-level well-known endpoints.

These are served outside ``/api/v1`` because external systems insist on the
exact path — Grafana fetches ``/.well-known/jwks.json`` to verify the short-lived
monitoring tokens the backend mints for it (plan D6).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.auth.tokens import jwks_document
from app.core.config import Settings, get_settings

router = APIRouter(tags=["well-known"])


@router.get(
    "/.well-known/jwks.json",
    summary="JSON Web Key Set",
    response_model=None,
)
async def jwks(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Public keys Grafana uses to validate monitoring tokens.

    Contains nothing secret; with HMAC signing the set is empty and Grafana is
    configured with the shared secret instead.
    """
    return jwks_document(settings)
