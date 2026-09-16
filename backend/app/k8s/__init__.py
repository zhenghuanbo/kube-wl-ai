"""Kubernetes integration.

Callers should obtain a gateway via :func:`get_gateway` rather than constructing
one, so that tests and cluster-less deployments can substitute the fake.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.k8s.gateway import (
    FakeK8sGateway,
    K8sError,
    K8sGateway,
    LimitSpec,
    NamespaceSpec,
    NodeInfo,
    QuotaSpec,
)

logger = get_logger(__name__)

__all__ = [
    "FakeK8sGateway",
    "K8sError",
    "K8sGateway",
    "LimitSpec",
    "NamespaceSpec",
    "NodeInfo",
    "QuotaSpec",
    "get_gateway",
    "run_gateway",
    "set_gateway",
]


async def run_gateway[T](fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run a blocking gateway call without stalling the event loop.

    The Kubernetes client is synchronous. Rather than sprinkle ``to_thread``
    through the service layer, every cluster call goes through here, so there is
    exactly one place to change if that ever stops being true.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


_gateway: K8sGateway | None = None


def get_gateway(settings: Settings | None = None) -> K8sGateway:
    """Return the process-wide gateway, building it on first use.

    When Kubernetes is not configured the fake is returned, so the API can run
    and be exercised locally without a cluster. That is a deliberate trade: the
    alternative is every developer needing cluster credentials to start the app.
    """
    global _gateway
    if _gateway is not None:
        return _gateway

    resolved = settings or get_settings()

    try:
        from app.k8s.client import RealK8sGateway

        _gateway = RealK8sGateway(kubeconfig=resolved.kubeconfig or None)
    except Exception as exc:
        # Any failure here means "there is no cluster to talk to", which is a
        # supported way to run the API locally. Log it loudly enough that a
        # genuine misconfiguration in production is not mistaken for this.
        logger.warning("k8s_gateway_unavailable", error=str(exc))
        _gateway = FakeK8sGateway()
    return _gateway


def set_gateway(gateway: K8sGateway | None) -> None:
    """Override the gateway — used by tests and by explicit configuration."""
    global _gateway
    _gateway = gateway
