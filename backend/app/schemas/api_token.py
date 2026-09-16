"""Personal access token read models."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.models.token import ApiToken
from app.schemas.base import ApiModel


class ApiTokenRead(ApiModel):
    """A token as it may be shown after creation.

    There is no ``token`` field here and there must never be one: the secret is
    returned exactly once, by `ApiTokenIssued`, and is not recoverable. Anything
    that could render a stored token would mean we had stored it.
    """

    id: int
    name: str
    #: The leading characters, shown so a person can tell two tokens apart.
    prefix: str
    scopes: list[str]
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime

    @classmethod
    def from_token(cls, token: ApiToken) -> ApiTokenRead:
        return cls(
            id=token.id,
            name=token.name,
            prefix=token.prefix,
            scopes=[part for part in token.scopes.split() if part],
            expires_at=token.expires_at,
            last_used_at=token.last_used_at,
            created_at=token.created_at,
        )


class ApiTokenIssued(ApiTokenRead):
    """A freshly minted token, with the one and only sight of the secret."""

    #: The raw credential. Shown once; the server keeps only its digest.
    token: str

    @classmethod
    def from_issued(cls, token: ApiToken, secret: str) -> ApiTokenIssued:
        base = ApiTokenRead.from_token(token)
        return cls(**base.model_dump(), token=secret)


class ApiTokenCreate(ApiModel):
    """Request body for minting a token."""

    name: str
    #: Empty or omitted means "inherit my roles" — the token then gains and loses
    #: permissions with the account rather than being pinned to a snapshot.
    scopes: list[str] = Field(default_factory=list)
    #: Absent means no expiry. Bounded above by ``MAX_TOKEN_DAYS``.
    expires_in_days: int | None = None
