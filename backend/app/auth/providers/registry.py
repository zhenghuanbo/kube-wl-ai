"""Provider registry and construction.

`AUTH_PROVIDERS` lists which providers are enabled; the registry turns that into
ordered provider instances. Ordering is by trust, not by the order in the
environment variable: a directory-verified identity always wins over the local
break-glass account, so a local password can never shadow an LDAP user.
"""

from __future__ import annotations

from collections.abc import Callable

from app.auth.providers.base import AuthProvider, AuthProviderError, ExternalIdentity
from app.auth.providers.ldap import LDAPProvider
from app.auth.providers.local import LocalProvider
from app.auth.providers.oidc import OIDCProvider
from app.core.config import Settings, get_settings

_REGISTRY: dict[str, Callable[[Settings | None], AuthProvider]] = {
    LDAPProvider.name: LDAPProvider,
    OIDCProvider.name: OIDCProvider,
    LocalProvider.name: LocalProvider,
}

#: Lower number = tried first.
_PRIORITY: dict[str, int] = {
    "ldap": 10,
    "oidc": 20,
    "local": 90,
}

SUPPORTED_PROVIDERS: tuple[str, ...] = tuple(_REGISTRY)


def build_providers(settings: Settings | None = None) -> list[AuthProvider]:
    """Instantiate the enabled, configured providers in trust order.

    Unknown names in ``AUTH_PROVIDERS`` are ignored with a warning rather than
    breaking startup — a typo in an environment variable must not take the
    platform down.
    """
    settings = settings or get_settings()
    providers: list[AuthProvider] = []

    for name in settings.auth_providers:
        key = name.strip().lower()
        if not key:
            continue
        provider_cls = _REGISTRY.get(key)
        if provider_cls is None:
            continue
        provider = provider_cls(settings)
        if provider.is_configured():
            providers.append(provider)

    providers.sort(key=lambda provider: _PRIORITY.get(provider.name, 50))
    return providers


__all__ = [
    "SUPPORTED_PROVIDERS",
    "AuthProvider",
    "AuthProviderError",
    "ExternalIdentity",
    "build_providers",
]
