"""OIDC provider — reserved extension point.

Per decision D2, FreeIPA/LDAP is the primary identity source and OIDC is
*reserved*: the interface, discovery handling and authorization-URL generation
are implemented so a future provider (Keycloak, Azure AD, ...) can be dropped in
without touching the auth service, but the authorization-code exchange is
deliberately not wired yet.

Completing it requires an actual IdP to test against; the remaining work is
`exchange_code` plus ID-token signature validation against the JWKS.
"""

from __future__ import annotations

import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.providers.base import AuthProvider, AuthProviderError, ExternalIdentity
from app.core.config import Settings, get_settings


class OIDCProvider(AuthProvider):
    name = "oidc"

    #: Discovery documents change rarely; cache for five minutes.
    _DISCOVERY_TTL_SECONDS = 300

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._discovery: dict[str, Any] | None = None
        self._discovery_fetched_at: float = 0.0

    # --- configuration -------------------------------------------------------
    def is_configured(self) -> bool:
        settings = self._settings
        return bool(
            settings.oidc_issuer
            and settings.oidc_client_id
            and settings.oidc_client_secret.get_secret_value()
            and settings.oidc_redirect_uri
        )

    def describe(self) -> str:
        return f"oidc ({self._settings.oidc_issuer or '(unset)'})"

    # --- discovery -----------------------------------------------------------
    async def discovery(self) -> dict[str, Any]:
        """Fetch and cache the provider's discovery document."""
        now = time.monotonic()
        if (
            self._discovery is not None
            and now - self._discovery_fetched_at < self._DISCOVERY_TTL_SECONDS
        ):
            return self._discovery

        url = self._settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                document: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            raise AuthProviderError(f"OIDC discovery failed for {url}: {exc}") from exc

        for required in ("authorization_endpoint", "token_endpoint", "issuer"):
            if required not in document:
                raise AuthProviderError(f"OIDC discovery document missing {required!r}")

        self._discovery = document
        self._discovery_fetched_at = now
        return document

    # --- authorization code flow --------------------------------------------
    @staticmethod
    def generate_state() -> str:
        return secrets.token_urlsafe(24)

    @staticmethod
    def generate_code_verifier() -> str:
        return secrets.token_urlsafe(64)

    async def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        scopes: tuple[str, ...] = ("openid", "profile", "email"),
    ) -> str:
        """Build the redirect URL for the authorization-code + PKCE flow."""
        document = await self.discovery()
        params = {
            "response_type": "code",
            "client_id": self._settings.oidc_client_id,
            "redirect_uri": self._settings.oidc_redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{document['authorization_endpoint']}?{urlencode(params)}"

    async def exchange_code(
        self,
        code: str,
        code_verifier: str,
        *,
        expected_nonce: str,
    ) -> ExternalIdentity:
        """Exchange an authorization code for an identity.

        Not implemented yet — see the module docstring. Kept as an explicit
        failure rather than a half-working implementation so a misconfiguration
        can never be mistaken for a successful federated login.
        """
        del code, code_verifier, expected_nonce
        raise AuthProviderError(
            "OIDC authorization-code exchange is not implemented in this release "
            "(decision D2: OIDC is a reserved extension point). "
            "Use the LDAP/FreeIPA provider or the local break-glass account."
        )

    async def authenticate(
        self,
        username: str,
        password: str,
        *,
        session: AsyncSession,
    ) -> ExternalIdentity | None:
        del username, password, session
        raise AuthProviderError(
            "OIDC does not authenticate with a password; use the redirect flow "
            "(/auth/oidc/authorize)."
        )
