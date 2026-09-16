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
    ContainerPort,
    ExecSession,
    FakeExecSession,
    FakeK8sGateway,
    IngressSpec,
    K8sError,
    K8sGateway,
    LimitSpec,
    NamespaceSpec,
    NodeInfo,
    PodInfo,
    PvcSpec,
    QuotaSpec,
    SecretSpec,
    ServicePort,
    ServiceSpec,
    WorkloadRefs,
    WorkloadSpec,
)
from app.k8s.workloads import InstanceObjects, build_instance_objects, ide_command, ingress_path

logger = get_logger(__name__)

__all__ = [
    "ContainerPort",
    "ExecSession",
    "FakeExecSession",
    "FakeK8sGateway",
    "IngressSpec",
    "InstanceObjects",
    "K8sError",
    "K8sGateway",
    "LimitSpec",
    "NamespaceSpec",
    "NodeInfo",
    "PodInfo",
    "PvcSpec",
    "QuotaSpec",
    "SecretSpec",
    "ServicePort",
    "ServiceSpec",
    "WorkloadRefs",
    "WorkloadSpec",
    "build_instance_objects",
    "get_gateway",
    "ide_command",
    "ingress_path",
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

    A real cluster is contacted only when the deployment asks for one, via
    ``K8S_IN_CLUSTER`` or ``K8S_ENABLED``. Otherwise the fake is returned, so the
    API runs and can be exercised without a cluster.

    Requiring that explicit opt-in is deliberate. The tempting rule — "use the
    real client when a kubeconfig is present" — means any machine with an
    ambient ``~/.kube/config`` provisions into whatever cluster that file names,
    including production. That is not hypothetical: it is how a test run came to
    create namespaces on a live cluster. A missing cluster should be a startup
    inconvenience, never a destructive surprise.
    """
    global _gateway
    if _gateway is not None:
        return _gateway

    resolved = settings or get_settings()

    if not (resolved.k8s_in_cluster or resolved.k8s_enabled):
        logger.warning(
            "k8s_gateway_disabled",
            hint="set K8S_ENABLED=true (or K8S_IN_CLUSTER=true) to use a real cluster",
        )
        _gateway = FakeK8sGateway()
        return _gateway

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
