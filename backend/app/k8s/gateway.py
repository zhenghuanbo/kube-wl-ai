"""The Kubernetes seam.

Everything the platform does to the cluster goes through :class:`K8sGateway`.
There is deliberately no other place that imports the ``kubernetes`` client, for
two reasons:

1. **Testability.** The unit suite runs against :class:`FakeK8sGateway`, so tests
   never need a cluster and never fail because one is unavailable.
2. **Auditability.** "What can this application do to the cluster?" is answerable
   by reading one Protocol.

The gateway speaks in plain dataclasses rather than Kubernetes API objects, so
callers do not need to know the client library's shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from app.core.errors import KubernetesError
from app.k8s.naming import (
    GROUP_LABEL,
    MANAGED_LABEL,
    MANAGED_LABEL_VALUE,
    WORKSPACE_LABEL,
)
from app.models.enums import NetworkType


@dataclass(frozen=True, slots=True)
class NamespaceSpec:
    """A namespace to create, with the labels that make it discoverable."""

    name: str
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)

    @classmethod
    def for_workspace(
        cls,
        name: str,
        *,
        group: str,
        workspace: str,
        workspace_id: int,
        group_id: int,
    ) -> NamespaceSpec:
        """Build the spec for a workspace namespace.

        A factory rather than free-form construction so the ``wl-managed`` label
        cannot be forgotten: every later sweep, cleanup and reconciliation keys
        off it, and a namespace without it looks like somebody else's.
        """
        return cls(
            name=name,
            labels={
                MANAGED_LABEL: MANAGED_LABEL_VALUE,
                GROUP_LABEL: group,
                WORKSPACE_LABEL: workspace,
                "wl-workspace-id": str(workspace_id),
            },
            annotations={"wl-group-id": str(group_id)},
        )


@dataclass(frozen=True, slots=True)
class QuotaSpec:
    """The ``ResourceQuota`` for a workspace.

    Values are Kubernetes quantity strings (``"8"``, ``"32Gi"``) because that is
    what the API expects; keeping them as strings avoids a unit-conversion layer
    whose rounding would eventually disagree with the scheduler.
    """

    name: str
    hard: dict[str, str]


@dataclass(frozen=True, slots=True)
class LimitSpec:
    """The ``LimitRange`` defaults applied to pods without explicit requests."""

    name: str
    default_cpu: str
    default_memory: str
    default_request_cpu: str
    default_request_memory: str
    max_cpu: str | None = None
    max_memory: str | None = None


@dataclass(frozen=True, slots=True)
class NodeInfo:
    """One node as reported by the cluster."""

    name: str
    status: str
    schedulable: bool
    cpu_total_milli: int
    memory_total_mb: int
    cpu_allocatable_milli: int
    memory_allocatable_mb: int
    labels: dict[str, Any]
    #: ``(index, model, memory_mb, uuid)`` per accelerator.
    gpu_devices: tuple[tuple[int, str, int, str | None], ...] = ()


class K8sError(KubernetesError):
    """Raised when the cluster rejects an operation.

    An :class:`AppError`, not a bare ``RuntimeError``, so it reaches the caller
    as a 502 with ``kubernetes_error`` instead of an opaque 500 — a cluster
    outage is an upstream failure, not a bug in this service. The underlying
    message is carried through for the operator.
    """

    def __init__(self, message: str | None = None, *, op: str | None = None) -> None:
        super().__init__(message, detail={"operation": op} if op else None)


@runtime_checkable
class K8sGateway(Protocol):
    """The complete set of cluster operations this platform performs."""

    def ensure_namespace(self, spec: NamespaceSpec) -> None:
        """Create the namespace, or update its labels if it already exists."""

    def delete_namespace(self, name: str) -> None:
        """Delete a namespace and everything in it.

        Must refuse names that do not look platform-managed.
        """

    def namespace_exists(self, name: str) -> bool: ...

    def apply_resource_quota(self, namespace: str, spec: QuotaSpec) -> None: ...

    def apply_limit_range(self, namespace: str, spec: LimitSpec) -> None: ...

    def apply_network_policy(self, namespace: str, network_type: NetworkType) -> None:
        """Replace the namespace's egress policy."""

    def list_nodes(self) -> list[NodeInfo]: ...


class FakeK8sGateway:
    """In-memory gateway for tests and for running the API without a cluster.

    It records every call so a test can assert *what would have been applied* —
    which is the actual contract, and is far more useful than asserting that a
    mocked method was called.
    """

    def __init__(self) -> None:
        self.namespaces: dict[str, NamespaceSpec] = {}
        self.quotas: dict[str, QuotaSpec] = {}
        self.limit_ranges: dict[str, LimitSpec] = {}
        self.network_policies: dict[str, NetworkType] = {}
        self.nodes: list[NodeInfo] = []
        #: Every method call in order, for assertions about behaviour.
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail_on: dict[str, str] = {}

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if name in self.fail_on:
            raise K8sError(self.fail_on[name])

    def ensure_namespace(self, spec: NamespaceSpec) -> None:
        self._record("ensure_namespace", spec.name)
        # Mirrors the real gateway, which stamps the managed label on create and
        # corrects it on patch. Tests must observe what a cluster would hold.
        if spec.labels.get(MANAGED_LABEL) != MANAGED_LABEL_VALUE:
            spec = replace(spec, labels={**spec.labels, MANAGED_LABEL: MANAGED_LABEL_VALUE})
        self.namespaces[spec.name] = spec

    def delete_namespace(self, name: str) -> None:
        self._record("delete_namespace", name)
        self.namespaces.pop(name, None)
        self.quotas.pop(name, None)
        self.limit_ranges.pop(name, None)
        self.network_policies.pop(name, None)

    def namespace_exists(self, name: str) -> bool:
        self._record("namespace_exists", name)
        return name in self.namespaces

    def apply_resource_quota(self, namespace: str, spec: QuotaSpec) -> None:
        self._record("apply_resource_quota", namespace)
        self.quotas[namespace] = spec

    def apply_limit_range(self, namespace: str, spec: LimitSpec) -> None:
        self._record("apply_limit_range", namespace)
        self.limit_ranges[namespace] = spec

    def apply_network_policy(self, namespace: str, network_type: NetworkType) -> None:
        self._record("apply_network_policy", namespace)
        self.network_policies[namespace] = network_type

    def list_nodes(self) -> list[NodeInfo]:
        self._record("list_nodes")
        return list(self.nodes)
