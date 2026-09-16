"""The real Kubernetes gateway.

This is the only module that imports the ``kubernetes`` client. It is synchronous
— the client library is — so callers in async code must wrap each call in
``asyncio.to_thread``; the service layer does that at exactly one place
(:func:`app.services.workspaces.apply_cluster_state` and friends) rather than
scattering it.
"""

from __future__ import annotations

import re
from typing import Any

from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

from app.core.logging import get_logger
from app.k8s.gateway import K8sError, LimitSpec, NamespaceSpec, NodeInfo, QuotaSpec
from app.k8s.naming import MANAGED_LABEL, MANAGED_LABEL_VALUE, is_managed_namespace
from app.models.enums import NetworkType

logger = get_logger(__name__)

_QUANTITY_SUFFIXES: dict[str, int] = {
    "n": 1,
    "u": 1_000,
    "m": 1_000_000,
    "": 1_000_000_000,
    "k": 1_000_000_000_000,
    "M": 1_000_000_000_000_000,
    "G": 1_000_000_000_000_000_000,
}

_MEMORY_SUFFIXES: dict[str, int] = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}

_QUANTITY_RE = re.compile(r"^([0-9.]+)\s*([A-Za-z]*)$")

#: NVIDIA's GPU feature-discovery label, e.g. ``NVIDIA-A100-SXM4-40GB``.
_GPU_PRODUCT_LABEL = "nvidia.com/gpu.product"


def parse_quantity(value: str) -> float:
    """Parse a Kubernetes CPU quantity into a float of base units."""
    match = _QUANTITY_RE.match(str(value).strip())
    if not match:
        return 0.0
    number, suffix = match.groups()
    return float(number) * _QUANTITY_SUFFIXES.get(suffix, 1) / 1_000_000_000


def parse_cpu_milli(value: str) -> int:
    """Parse a CPU quantity (``"8"``, ``"3850m"``) into millicores."""
    return round(parse_quantity(value) * 1000)


def parse_memory_mb(value: str) -> int:
    """Parse a memory quantity (``"32Gi"``, ``"1024Mi"``) into mebibytes."""
    match = _QUANTITY_RE.match(str(value).strip())
    if not match:
        return 0
    number, suffix = match.groups()
    if suffix in _MEMORY_SUFFIXES:
        return int(float(number) * _MEMORY_SUFFIXES[suffix] / (1024**2))
    # A bare number is bytes.
    return int(float(number) / (1024**2))


def gpu_model_from_labels(labels: dict[str, str]) -> str | None:
    """Best-effort accelerator model from a node's labels.

    Prefers the platform's own ``wl-gpu-model`` override, then NVIDIA's
    feature-discovery product string. Returns ``None`` when the node carries no
    recognisable accelerator, which is how a CPU-only node is identified.
    """
    explicit = labels.get("wl-gpu-model")
    if explicit:
        return explicit.lower()

    product = labels.get(_GPU_PRODUCT_LABEL)
    if not product:
        return None

    lowered = product.lower()
    for model in ("h100", "h800", "a100", "a800", "v100", "l40s", "a10"):
        if model in lowered.replace("-", "").replace("_", ""):
            return model
    # Unknown model: keep the raw string so it is visible rather than dropped.
    return lowered


def build_load_config(*, kubeconfig: str | None = None, context: str | None = None) -> None:
    """Load cluster credentials, in-cluster first.

    In-cluster is tried first because that is how the application actually runs
    in production; falling back to a kubeconfig keeps local development working
    without changing code.
    """
    try:
        k8s_config.load_incluster_config()
        return
    except k8s_config.ConfigException:
        pass

    if kubeconfig:
        k8s_config.load_kube_config(config_file=kubeconfig, context=context)
        return

    k8s_config.load_kube_config(context=context)


class RealK8sGateway:
    """Kubernetes-backed implementation of :class:`~app.k8s.gateway.K8sGateway`."""

    def __init__(self, *, kubeconfig: str | None = None, context: str | None = None) -> None:
        build_load_config(kubeconfig=kubeconfig, context=context)
        self._core = k8s.CoreV1Api()
        self._networking = k8s.NetworkingV1Api()

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _raise(action: str, exc: ApiException) -> K8sError:
        detail = getattr(exc, "body", None) or getattr(exc, "reason", str(exc))
        return K8sError(f"{action} failed: {detail}")

    def _read_namespace(self, name: str) -> Any | None:
        try:
            return self._core.read_namespace(name)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise self._raise(f"read namespace {name}", exc) from exc

    # --- namespaces -------------------------------------------------------
    def ensure_namespace(self, spec: NamespaceSpec) -> None:
        labels = {**spec.labels, MANAGED_LABEL: MANAGED_LABEL_VALUE}
        body = k8s.V1Namespace(
            metadata=k8s.V1ObjectMeta(
                name=spec.name, labels=labels, annotations=spec.annotations or None
            )
        )
        existing = self._read_namespace(spec.name)
        try:
            if existing is None:
                self._core.create_namespace(body)
                logger.info("k8s_namespace_created", namespace=spec.name)
            else:
                # Label drift is worth correcting: the managed label is what
                # keeps the platform's RBAC boundary intact.
                self._core.patch_namespace(spec.name, body)
                logger.info("k8s_namespace_updated", namespace=spec.name)
        except ApiException as exc:
            raise self._raise(f"ensure namespace {spec.name}", exc) from exc

    def delete_namespace(self, name: str) -> None:
        if not is_managed_namespace(name):
            # A guard, not a formality: this call is irreversible.
            raise K8sError(f"refusing to delete non-managed namespace {name!r}")
        try:
            self._core.delete_namespace(name)
            logger.info("k8s_namespace_deleted", namespace=name)
        except ApiException as exc:
            if exc.status == 404:
                return
            raise self._raise(f"delete namespace {name}", exc) from exc

    def namespace_exists(self, name: str) -> bool:
        return self._read_namespace(name) is not None

    # --- quota & limits ---------------------------------------------------
    def apply_resource_quota(self, namespace: str, spec: QuotaSpec) -> None:
        body = k8s.V1ResourceQuota(
            metadata=k8s.V1ObjectMeta(name=spec.name, namespace=namespace),
            spec=k8s.V1ResourceQuotaSpec(hard=spec.hard),
        )
        api = self._core
        try:
            api.read_namespaced_resource_quota(spec.name, namespace)
            api.replace_namespaced_resource_quota(spec.name, namespace, body)
        except ApiException as exc:
            if exc.status != 404:
                raise self._raise(f"update quota in {namespace}", exc) from exc
            try:
                api.create_namespaced_resource_quota(namespace, body)
            except ApiException as create_exc:
                raise self._raise(f"create quota in {namespace}", create_exc) from create_exc

    def apply_limit_range(self, namespace: str, spec: LimitSpec) -> None:
        limits = k8s.V1LimitRangeSpec(
            limits=[
                k8s.V1LimitRangeItem(
                    type="Container",
                    default={"cpu": spec.default_cpu, "memory": spec.default_memory},
                    default_request={
                        "cpu": spec.default_request_cpu,
                        "memory": spec.default_request_memory,
                    },
                    max=(
                        {"cpu": spec.max_cpu, "memory": spec.max_memory}
                        if spec.max_cpu and spec.max_memory
                        else None
                    ),
                )
            ]
        )
        body = k8s.V1LimitRange(
            metadata=k8s.V1ObjectMeta(name=spec.name, namespace=namespace), spec=limits
        )
        api = self._core
        try:
            api.read_namespaced_limit_range(spec.name, namespace)
            api.replace_namespaced_limit_range(spec.name, namespace, body)
        except ApiException as exc:
            if exc.status != 404:
                raise self._raise(f"update limit range in {namespace}", exc) from exc
            try:
                api.create_namespaced_limit_range(namespace, body)
            except ApiException as create_exc:
                raise self._raise(f"create limit range in {namespace}", create_exc) from create_exc

    # --- network ----------------------------------------------------------
    def apply_network_policy(self, namespace: str, network_type: NetworkType) -> None:
        name = f"{namespace}-default"
        selector = k8s.V1LabelSelector()  # matches every pod in the namespace

        egress: list[Any] | None
        if network_type is NetworkType.ISOLATED:
            # Empty egress list = deny all egress.
            egress = []
        elif network_type is NetworkType.INTERNAL:
            egress = [
                k8s.V1NetworkPolicyEgressRule(
                    to=[k8s.V1NetworkPolicyPeer(namespace_selector=k8s.V1LabelSelector())]
                ),
                # DNS must stay reachable or nothing resolves, including the
                # in-cluster registry.
                k8s.V1NetworkPolicyEgressRule(
                    ports=[
                        k8s.V1NetworkPolicyPort(protocol="UDP", port=53),
                        k8s.V1NetworkPolicyPort(protocol="TCP", port=53),
                    ]
                ),
            ]
        else:
            egress = None  # None = allow all egress

        policy = k8s.V1NetworkPolicy(
            metadata=k8s.V1ObjectMeta(name=name, namespace=namespace),
            spec=k8s.V1NetworkPolicySpec(
                pod_selector=selector,
                policy_types=["Ingress", "Egress"],
                ingress=[] if network_type is NetworkType.ISOLATED else None,
                egress=egress,
            ),
        )

        self._delete_network_policy(name, namespace)
        try:
            self._networking.create_namespaced_network_policy(namespace, policy)
        except ApiException as exc:
            raise self._raise(f"apply network policy in {namespace}", exc) from exc

    def _delete_network_policy(self, name: str, namespace: str) -> None:
        try:
            self._networking.delete_namespaced_network_policy(name, namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise self._raise(f"delete network policy in {namespace}", exc) from exc

    # --- nodes ------------------------------------------------------------
    def list_nodes(self) -> list[NodeInfo]:
        try:
            response = self._core.list_node()
        except ApiException as exc:
            raise self._raise("list nodes", exc) from exc

        nodes: list[NodeInfo] = []
        for node in response.items:
            status = node.status
            capacity = status.capacity or {}
            allocatable = status.allocatable or {}
            labels = node.metadata.labels or {}

            ready = any(
                condition.type == "Ready" and condition.status == "True"
                for condition in (status.conditions or [])
            )
            unschedulable = bool(node.spec and node.spec.unschedulable)

            gpu_count = int(allocatable.get("nvidia.com/gpu", 0) or 0)
            model = gpu_model_from_labels(labels)
            devices: list[tuple[int, str, int, str | None]] = []
            if gpu_count and model:
                # Per-card UUID and memory are not exposed by the scheduler's
                # view; the device plugin reports aggregates only. The node-sync
                # job refines these from the accelerator exporter when available.
                for index in range(gpu_count):
                    devices.append((index, model, 0, None))

            nodes.append(
                NodeInfo(
                    name=node.metadata.name,
                    status="ready" if ready else "not_ready",
                    schedulable=not unschedulable,
                    cpu_total_milli=parse_cpu_milli(capacity.get("cpu", "0")),
                    memory_total_mb=parse_memory_mb(capacity.get("memory", "0")),
                    cpu_allocatable_milli=parse_cpu_milli(allocatable.get("cpu", "0")),
                    memory_allocatable_mb=parse_memory_mb(allocatable.get("memory", "0")),
                    labels=dict(labels),
                    gpu_devices=tuple(devices),
                )
            )
        return nodes
