"""Authentication providers (LDAP/FreeIPA, OIDC, local break-glass)."""

from app.auth.providers.base import AuthProvider, AuthProviderError, ExternalIdentity
from app.auth.providers.registry import SUPPORTED_PROVIDERS, build_providers

__all__ = [
    "SUPPORTED_PROVIDERS",
    "AuthProvider",
    "AuthProviderError",
    "ExternalIdentity",
    "build_providers",
]
