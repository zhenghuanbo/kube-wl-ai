"""Personal access token endpoints (plan §12, Phase 8).

Self-service, and scoped to the caller by construction rather than by a check:
there is no ``/{token_id}`` route that takes somebody else's id and no
``?userId=`` parameter. A token is a personal credential, and the way to stop a
person using the API is to remove their role, which narrows every token they
hold on the next request.

Note what is absent: nothing here lets an administrator list or revoke another
person's tokens. That is deliberate. An administrator who needs to cut off
automation deactivates the account — one action, immediate effect, and an audit
row that says what happened. A per-token administrative delete would be a second
way to do the same thing, with a worse trail.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.audit import record_audit
from app.core.deps import CurrentUser, DbSession, require
from app.schemas.api_token import ApiTokenCreate, ApiTokenIssued, ApiTokenRead
from app.schemas.base import ApiMessage
from app.services import api_tokens as token_service

router = APIRouter(prefix="/api-tokens", tags=["api-tokens"])

CanRead = Annotated[object, Depends(require("api_token:read"))]
CanCreate = Annotated[object, Depends(require("api_token:create"))]
CanDelete = Annotated[object, Depends(require("api_token:delete"))]


@router.get(
    "",
    response_model=list[ApiTokenRead],
    summary="List the caller's API tokens",
    dependencies=[Depends(require("api_token:read"))],
)
async def list_api_tokens(session: DbSession, actor: CurrentUser) -> list[ApiTokenRead]:
    """The caller's own tokens. Returns a bare list, like the other small
    catalogues, because a person has a handful of tokens and never pages them."""
    rows = await token_service.list_tokens(session, actor)
    return [ApiTokenRead.from_token(row) for row in rows]


@router.post(
    "",
    response_model=ApiTokenIssued,
    status_code=201,
    summary="Create a personal access token",
    dependencies=[Depends(require("api_token:create"))],
)
async def create_api_token(
    session: DbSession,
    actor: CurrentUser,
    request: Request,
    payload: ApiTokenCreate,
) -> ApiTokenIssued:
    """Mint a token and return it **once**.

    The response is the only time the secret exists in a readable form. A client
    that loses it revokes the token and makes another — which is a better
    outcome than a system that can reproduce secrets on demand.

    Scopes are validated against what the caller holds right now, so a token can
    never be created naming a permission its owner lacks. A token created *by*
    another token is further limited to that token's own scopes, so a credential
    cannot mint a successor with more authority than itself.
    """
    issued = await token_service.create_token(
        session,
        actor,
        name=payload.name,
        scopes=payload.scopes,
        expires_in_days=payload.expires_in_days,
    )

    result = ApiTokenIssued.from_issued(issued.token, issued.secret)
    record_audit(
        session,
        action="api_token:create",
        status="success",
        actor=actor,
        object_type="api_token",
        object_id=str(issued.token.id),
        # The secret is never written to the trail, and neither is its digest.
        # `prefix` is enough to identify the token in a later investigation.
        detail={
            "name": issued.token.name,
            "prefix": issued.token.prefix,
            "scopes": result.scopes,
            "expiresAt": result.expires_at.isoformat() if result.expires_at else None,
        },
        request=request,
    )
    return result


@router.delete(
    "/{token_id}",
    response_model=ApiMessage,
    summary="Revoke one of the caller's API tokens",
    dependencies=[Depends(require("api_token:delete"))],
)
async def revoke_api_token(
    session: DbSession,
    actor: CurrentUser,
    request: Request,
    token_id: int,
) -> ApiMessage:
    """Revoke a token by id.

    Revocation is immediate and total: the row is deleted, so the next request
    presenting the credential fails to resolve it. Out-of-scope ids are a 404
    rather than a 403 — token ids are sequential, and a 403 would confirm that
    somebody else's token exists.
    """
    token = await token_service.revoke_token(session, actor, token_id)
    record_audit(
        session,
        action="api_token:revoke",
        status="success",
        actor=actor,
        object_type="api_token",
        object_id=str(token_id),
        detail={"name": token.name, "prefix": token.prefix},
        request=request,
    )
    return ApiMessage(message="令牌已吊销")
