"""Access tokens (JWT), opaque refresh tokens and the JWKS document.

Design decisions
----------------
* The access token carries **identity only** (``sub``, ``username``, ``jti``).
  Roles and group scope are read from the database on every request by
  ``get_current_user``, so a role change or account deactivation takes effect
  immediately rather than after the token expires. The token is short-lived
  (15 min by default) to bound the window for a stolen token.
* Refresh tokens are opaque random strings; only their SHA-256 digest is
  persisted, so a database leak yields nothing usable.
* RS256 is used whenever a private key is configured (required for the Grafana
  JWKS integration in Phase 7); HS256 with ``SECRET_KEY`` is the development
  fallback so the stack runs without key material.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import Settings
from app.core.errors import UnauthorizedError

ACCESS_TOKEN_TYPE = "access"
GRAFANA_TOKEN_TYPE = "grafana"
#: Short-lived ticket authorising one web-IDE session on one instance.
IDE_TOKEN_TYPE = "ide"

#: Length of the opaque refresh token before base64 expansion.
REFRESH_TOKEN_BYTES = 48


@dataclass(frozen=True, slots=True)
class DecodedToken:
    user_id: int
    username: str
    jti: str
    expires_at: datetime


# --- key material ------------------------------------------------------------
def _signing_key(settings: Settings) -> tuple[str, str]:
    private_key = settings.jwt_private_key.get_secret_value()
    if private_key:
        return "RS256", private_key
    return "HS256", settings.secret_key.get_secret_value()


def _verification_key(settings: Settings) -> tuple[str, str]:
    public_key = settings.jwt_public_key.get_secret_value()
    if public_key:
        return "RS256", public_key
    private_key = settings.jwt_private_key.get_secret_value()
    if private_key:
        # A PEM private key carries the public half; verification still works.
        # Deployments should set JWT_PUBLIC_KEY so the private key never has to
        # be present where only verification happens.
        return "RS256", private_key
    return "HS256", settings.secret_key.get_secret_value()


def key_id(settings: Settings) -> str:
    """Stable key identifier for the JWKS document and token headers."""
    material = (
        settings.jwt_public_key.get_secret_value() or settings.jwt_private_key.get_secret_value()
    )
    if not material:
        return "hs256-dev"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def is_asymmetric(settings: Settings) -> bool:
    return bool(
        settings.jwt_private_key.get_secret_value() or settings.jwt_public_key.get_secret_value()
    )


# --- access tokens -----------------------------------------------------------
def create_access_token(
    *,
    user_id: int,
    username: str,
    settings: Settings,
    extra_claims: dict[str, Any] | None = None,
    token_type: str = ACCESS_TOKEN_TYPE,
    expires_in: timedelta | None = None,
) -> tuple[str, datetime]:
    """Mint a signed access token. Returns ``(token, expires_at)``."""
    now = datetime.now(UTC)
    expires_at = now + (expires_in or timedelta(minutes=settings.access_token_expire_minutes))

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "typ": token_type,
        "iss": settings.jwt_issuer,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if extra_claims:
        payload.update(extra_claims)

    algorithm, key = _signing_key(settings)
    encoded = jwt.encode(
        payload,
        key,
        algorithm=algorithm,
        headers={"kid": key_id(settings)},
    )
    # PyJWT >= 2 returns str; older versions returned bytes.
    token = encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded
    return token, expires_at


def create_ide_ticket(
    *,
    user_id: int,
    username: str,
    instance_id: int,
    namespace: str,
    settings: Settings,
) -> tuple[str, datetime]:
    """Mint the ticket that lets a browser open an instance's web IDE.

    Deliberately a separate token *type* rather than a reuse of the access
    token. The ingress validates these on a path that carries no other
    authentication, so the ticket must be useless as an API credential, and an
    access token must be useless here — ``decode_access_token`` enforces the
    type, which is what keeps the two from being interchangeable.

    It also pins ``namespace`` and ``instance``: a ticket for one workspace
    cannot be replayed against another, which is the plan's requirement that the
    backend lock the Grafana/IDE variables rather than trusting the URL.
    """
    return create_access_token(
        user_id=user_id,
        username=username,
        settings=settings,
        token_type=IDE_TOKEN_TYPE,
        expires_in=timedelta(seconds=settings.ide_ticket_seconds),
        extra_claims={"instance": instance_id, "namespace": namespace},
    )


def decode_ide_ticket(token: str, settings: Settings) -> dict[str, Any]:
    """Verify an IDE ticket and return its claims."""
    return decode_token_claims(token, settings, expected_type=IDE_TOKEN_TYPE)


def decode_token_claims(token: str, settings: Settings, *, expected_type: str) -> dict[str, Any]:
    """Verify a token and return the raw claims.

    Unlike :func:`decode_access_token` this keeps the extra claims, which is
    what the IDE and Grafana integrations need to read back.
    """
    algorithm, key = _verification_key(settings)
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[algorithm],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("凭证已过期", code="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("凭证无效", code="invalid_token") from exc

    if payload.get("typ") != expected_type:
        raise UnauthorizedError("凭证类型不匹配", code="invalid_token_type")
    return payload


def decode_access_token(
    token: str,
    settings: Settings,
    *,
    expected_type: str = ACCESS_TOKEN_TYPE,
) -> DecodedToken:
    """Verify a token and return its identity claims.

    Raises ``UnauthorizedError`` for any invalid, expired or wrong-type token —
    the caller never has to reason about PyJWT's exception hierarchy.
    """
    algorithm, key = _verification_key(settings)
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[algorithm],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Token has expired", code="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Invalid token", code="invalid_token") from exc

    if payload.get("typ") != expected_type:
        raise UnauthorizedError("Invalid token type", code="invalid_token_type")

    try:
        user_id = int(payload["sub"])
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise UnauthorizedError("Malformed token claims", code="invalid_token") from exc

    return DecodedToken(
        user_id=user_id,
        username=str(payload.get("username", "")),
        jti=str(payload["jti"]),
        expires_at=expires_at,
    )


# --- refresh tokens ----------------------------------------------------------
def generate_refresh_token() -> tuple[str, str]:
    """Return ``(raw_token, sha256_hex_digest)``."""
    raw = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    """SHA-256 digest used as the stored representation of a token."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_token(prefix: str = "wl") -> tuple[str, str, str]:
    """Return ``(raw_token, sha256_digest, display_prefix)`` for a personal token."""
    raw = f"{prefix}_{secrets.token_urlsafe(32)}"
    return raw, hash_token(raw), raw[:12]


# --- JWKS --------------------------------------------------------------------
def jwks_document(settings: Settings) -> dict[str, Any]:
    """Build the JWKS document served at ``/.well-known/jwks.json``.

    Grafana validates our JWTs against this document (Phase 7). With the HS256
    development fallback there is nothing to publish, so the key set is empty
    and the caller can surface a configuration warning.
    """
    public_key = settings.jwt_public_key.get_secret_value()
    if not public_key:
        return {"keys": []}

    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        from jwt.algorithms import RSAAlgorithm
    except ImportError:  # pragma: no cover - cryptography is a hard dependency
        return {"keys": []}

    key = load_pem_public_key(public_key.encode("utf-8"))
    jwk: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(key))
    jwk.update({"use": "sig", "alg": "RS256", "kid": key_id(settings)})
    return {"keys": [jwk]}
