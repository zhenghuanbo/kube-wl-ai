"""The real Kubernetes gateway.

This is the only module that imports the ``kubernetes`` client. It is synchronous
— the client library is — so callers in async code must wrap each call in
``asyncio.to_thread``; the service layer does that at exactly one place
(:func:`app.services.workspaces.apply_cluster_state` and friends) rather than
scattering it.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import replace
from typing import Any

from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream

from app.core.logging import get_logger
from app.k8s.gateway import (
    DeploymentStatus,
    ExecSession,
    JobInfo,
    JobSpec,
    K8sError,
    LimitSpec,
    NamespaceSpec,
    NodeInfo,
    PodInfo,
    PvcInfo,
    PvcSpec,
    PvInfo,
    PvSpec,
    QuotaSpec,
    SecretSpec,
    ServiceInfo,
    ServiceSpec,
    WorkloadRefs,
    WorkloadSpec,
)
from app.k8s.naming import INSTANCE_LABEL, MANAGED_LABEL, MANAGED_LABEL_VALUE, is_managed_namespace
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


def parse_storage_gb(value: str) -> int | None:
    """Parse a storage quantity (``"10Gi"``, ``"500Mi"``) into whole GiB.

    Returns ``None`` rather than 0 for anything unparseable: (None) means "the
    cluster did not say", (0) means "an empty volume", and conflating the two
    would let a claim that failed to report its size look like a zero-byte one.
    """
    match = _QUANTITY_RE.match(str(value).strip())
    if not match:
        return None
    number, suffix = match.groups()
    if suffix in _MEMORY_SUFFIXES:
        return int(float(number) * _MEMORY_SUFFIXES[suffix] / (1024**3))
    if suffix in _QUANTITY_SUFFIXES:
        return int(float(number) * _QUANTITY_SUFFIXES[suffix] / (1000**3))
    try:
        return int(float(number) / (1024**3))
    except ValueError:  # pragma: no cover - the regex already guarantees a float
        return None


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


#: Channel the Kubernetes websocket protocol uses for terminal resizes.
_RESIZE_CHANNEL = 4


class KubernetesExecSession:
    """Thin wrapper over the Kubernetes websocket exec client.

    The client's own API raises when the pod exits mid-stream rather than
    signalling EOF, so every operation is translated into the small, total
    :class:`~app.k8s.gateway.ExecSession` contract the terminal handler expects.
    """

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    def recv(self, timeout: float) -> bytes | None:
        try:
            self._ws.update(timeout=timeout)
        except Exception:
            return None

        for peek, read in (
            (self._ws.peek_stdout, self._ws.read_stdout),
            (self._ws.peek_stderr, self._ws.read_stderr),
        ):
            try:
                if peek():
                    chunk: str = read()
                    return chunk.encode("utf-8", errors="replace")
            except Exception:
                return None

        try:
            return b"" if self._ws.is_open() else None
        except Exception:
            return None

    def send(self, data: bytes) -> None:
        try:
            self._ws.write_stdin(data.decode("utf-8", errors="replace"))
        except Exception as exc:
            raise K8sError(f"write to terminal failed: {exc}") from exc

    def resize(self, rows: int, cols: int) -> None:
        try:
            self._ws.write_channel(_RESIZE_CHANNEL, json.dumps({"Height": rows, "Width": cols}))
        except Exception as exc:
            # A failed resize is cosmetic — the shell keeps the old size — so it
            # must never tear down a working session.
            logger.warning("terminal_resize_failed", error=str(exc))

    def close(self) -> None:
        # Closing an already-closed socket is expected, not exceptional.
        with contextlib.suppress(Exception):
            self._ws.close()

    @property
    def closed(self) -> bool:
        try:
            open_: bool = self._ws.is_open()
            return not open_
        except Exception:
            return True


def _pv_info(volume: Any) -> PvInfo:
    """Reduce a volume to the few facts the platform acts on.

    Used by both ``read_pv`` and ``list_pvs`` so a volume read by name and the same
    volume read from a list can never disagree — which matters because the cleanup
    sweep trusts the list to decide what to delete.
    """
    spec = getattr(volume, "spec", None)
    status = getattr(volume, "status", None)
    host_path = getattr(getattr(spec, "host_path", None), "path", None)
    capacity = None
    raw_capacity = (getattr(spec, "capacity", None) or {}).get("storage")
    if raw_capacity:
        capacity = parse_storage_gb(str(raw_capacity))
    claim = getattr(spec, "claim_ref", None)
    return PvInfo(
        name=getattr(getattr(volume, "metadata", None), "name", None) or "",
        phase=getattr(status, "phase", None) or "Available",
        host_path=host_path,
        capacity_gb=capacity,
        claim_namespace=getattr(claim, "namespace", None),
        claim_name=getattr(claim, "name", None),
    )


def _pod_info(pod: Any) -> PodInfo:
    """Reduce a pod to the few facts the platform actually acts on."""
    statuses = pod.status.container_statuses or []
    ready = bool(statuses) and all(container.ready for container in statuses)
    restarts = sum(container.restart_count or 0 for container in statuses)

    reason: str | None = None
    message: str | None = None
    for container in statuses:
        state = container.state
        waiting = state.waiting if state else None
        if waiting and waiting.reason:
            # `ImagePullBackOff` is the single most common reason an instance
            # never starts, and it is invisible without this.
            reason = waiting.reason
            message = f"{waiting.reason}: {waiting.message}" if waiting.message else waiting.reason
            break
        # Between restarts a crashing container is not `waiting` — it is
        # `terminated`, carrying the exit code that killed it. Without this the
        # container looks merely "not ready yet" during that window, because the
        # pod's own phase stays `Running` the whole time it is crash-looping.
        terminated = state.terminated if state else None
        if terminated is not None and (terminated.exit_code or 0) != 0:
            reason = terminated.reason or "Error"
            message = f"{reason}: exit code {terminated.exit_code}"
            if terminated.message:
                message = f"{message} ({terminated.message})"
            break
    if message is None:
        for condition in pod.status.conditions or []:
            if condition.type == "PodScheduled" and condition.status == "False":
                message = condition.message or condition.reason
                break

    return PodInfo(
        name=pod.metadata.name,
        phase=pod.status.phase or "Unknown",
        ready=ready,
        node_name=pod.spec.node_name if pod.spec else None,
        restart_count=restarts,
        message=message,
        reason=reason,
        # The label, not the name: it is the only reliable way back to the owning
        # row when a listing covers several instances at once.
        instance_name=(pod.metadata.labels or {}).get(INSTANCE_LABEL),
    )


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
        self._apps = k8s.AppsV1Api()
        self._batch = k8s.BatchV1Api()

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def _raise(action: str, exc: ApiException) -> K8sError:
        detail = getattr(exc, "body", None) or getattr(exc, "reason", str(exc))
        return K8sError(f"{action} failed: {detail}")

    @staticmethod
    def _is_not_found(exc: ApiException) -> bool:
        not_found: bool = exc.status == 404
        return not_found

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

    # --- workloads --------------------------------------------------------
    @staticmethod
    def _container(spec: WorkloadSpec) -> Any:
        resources = k8s.V1ResourceRequirements(
            # Requests equal limits: a request below the limit lets the
            # scheduler over-commit a node, and an instance that cannot get its
            # memory back is worse than one that waits for room.
            requests={
                "cpu": f"{spec.cpu_milli}m",
                "memory": f"{spec.memory_mb}Mi",
            },
            limits={
                "cpu": f"{spec.cpu_milli}m",
                "memory": f"{spec.memory_mb}Mi",
            },
        )
        if spec.gpu_count > 0 and spec.gpu_type not in ("", "none"):
            # Whole cards only. Fractional GPUs need MIG or time-slicing, which
            # changes the accounting entirely; neither is configured here.
            resources.limits["nvidia.com/gpu"] = str(spec.gpu_count)

        mounts: list[Any] = []
        if spec.pvc_name:
            mounts.append(k8s.V1VolumeMount(name="home", mount_path=spec.mount_path))
        for index, extra in enumerate(spec.extra_mounts):
            mounts.append(
                k8s.V1VolumeMount(
                    name=f"attached-{index}",
                    mount_path=extra.mount_path,
                    # Read-only on the mount *and* on the volume below. Setting only
                    # one leaves a path a process can remount or write through, which
                    # is not what "read-only share" means.
                    read_only=extra.read_only or None,
                )
            )
        if spec.ssh_secret_name:
            mounts.append(
                k8s.V1VolumeMount(
                    name="ssh-keys",
                    mount_path="/etc/ssh/authorized_keys.d",
                    read_only=True,
                )
            )

        # Tuple-typed argv, never a shell string: a user-supplied value can
        # never become a command.
        return k8s.V1Container(
            name="main",
            image=spec.image,
            command=list(spec.command) if spec.command else None,
            args=list(spec.args) if spec.args else None,
            env=[k8s.V1EnvVar(name=key, value=value) for key, value in spec.env.items()] or None,
            ports=[
                k8s.V1ContainerPort(name=port.name, container_port=port.container_port)
                for port in spec.ports
            ]
            or None,
            resources=resources,
            volume_mounts=mounts or None,
        )

    @staticmethod
    def _volumes(spec: WorkloadSpec) -> list[Any] | None:
        volumes: list[Any] = []
        if spec.pvc_name:
            volumes.append(
                k8s.V1Volume(
                    name="home",
                    persistent_volume_claim=k8s.V1PersistentVolumeClaimVolumeSource(
                        claim_name=spec.pvc_name
                    ),
                )
            )
        for index, extra in enumerate(spec.extra_mounts):
            volumes.append(
                k8s.V1Volume(
                    name=f"attached-{index}",
                    persistent_volume_claim=k8s.V1PersistentVolumeClaimVolumeSource(
                        claim_name=extra.pvc_name,
                        read_only=extra.read_only or None,
                    ),
                )
            )
        if spec.ssh_secret_name:
            volumes.append(
                k8s.V1Volume(
                    name="ssh-keys",
                    secret=k8s.V1SecretVolumeSource(
                        secret_name=spec.ssh_secret_name,
                        # 0444: readable, not writable, and owned by root so the
                        # unprivileged user cannot alter their own keys from
                        # inside the container.
                        default_mode=0o444,
                    ),
                )
            )
        return volumes or None

    def apply_deployment(self, spec: WorkloadSpec) -> None:
        labels = dict(spec.labels)
        body = k8s.V1Deployment(
            metadata=k8s.V1ObjectMeta(name=spec.name, namespace=spec.namespace, labels=labels),
            spec=k8s.V1DeploymentSpec(
                replicas=spec.replicas,
                # `Recreate` rather than `RollingUpdate`: two copies of a
                # developer environment must never run at once, or they would
                # race over the same home directory claim.
                strategy=k8s.V1DeploymentStrategy(type="Recreate"),
                selector=k8s.V1LabelSelector(match_labels=labels),
                template=k8s.V1PodTemplateSpec(
                    metadata=k8s.V1ObjectMeta(labels=labels),
                    spec=k8s.V1PodSpec(
                        containers=[self._container(spec)],
                        volumes=self._volumes(spec),
                        node_selector=spec.node_selector or None,
                        tolerations=[
                            k8s.V1Toleration(**toleration) for toleration in spec.tolerations
                        ]
                        or None,
                        termination_grace_period_seconds=spec.termination_grace_seconds,
                    ),
                ),
            ),
        )
        api = self._apps
        try:
            api.read_namespaced_deployment(spec.name, spec.namespace)
            api.replace_namespaced_deployment(spec.name, spec.namespace, body)
        except ApiException as exc:
            if not self._is_not_found(exc):
                raise self._raise(f"update deployment {spec.name}", exc) from exc
            try:
                api.create_namespaced_deployment(spec.namespace, body)
            except ApiException as create_exc:
                raise self._raise(f"create deployment {spec.name}", create_exc) from create_exc

    def apply_service(self, spec: ServiceSpec) -> ServiceSpec:
        body = k8s.V1Service(
            metadata=k8s.V1ObjectMeta(
                name=spec.name, namespace=spec.namespace, labels=spec.labels or None
            ),
            spec=k8s.V1ServiceSpec(
                type=spec.service_type,
                selector=spec.selector or None,
                ports=[
                    k8s.V1ServicePort(
                        name=port.name,
                        port=port.port,
                        target_port=port.target_port,
                        node_port=port.node_port,
                        protocol=port.protocol,
                    )
                    for port in spec.ports
                ],
            ),
        )
        api = self._core
        try:
            existing = api.read_namespaced_service(spec.name, spec.namespace)
            # A NodePort is immutable once allocated, so carry the current one
            # forward instead of asking the cluster to re-pick it.
            allocated = {
                port.name: port.node_port for port in (existing.spec.ports or []) if port.node_port
            }
            for port in body.spec.ports:
                if port.node_port is None and port.name in allocated:
                    port.node_port = allocated[port.name]
            api.replace_namespaced_service(spec.name, spec.namespace, body)
            created = api.read_namespaced_service(spec.name, spec.namespace)
        except ApiException as exc:
            if not self._is_not_found(exc):
                raise self._raise(f"update service {spec.name}", exc) from exc
            try:
                created = api.create_namespaced_service(spec.namespace, body)
            except ApiException as create_exc:
                raise self._raise(f"create service {spec.name}", create_exc) from create_exc

        resolved = {port.name: port.node_port for port in (created.spec.ports or [])}
        return replace(
            spec,
            ports=tuple(
                replace(port, node_port=resolved.get(port.name, port.node_port))
                for port in spec.ports
            ),
        )

    def apply_secret(self, spec: SecretSpec) -> None:
        body = k8s.V1Secret(
            metadata=k8s.V1ObjectMeta(
                name=spec.name, namespace=spec.namespace, labels=spec.labels or None
            ),
            type="Opaque",
            string_data=spec.data or None,
        )
        api = self._core
        try:
            api.read_namespaced_secret(spec.name, spec.namespace)
            api.replace_namespaced_secret(spec.name, spec.namespace, body)
        except ApiException as exc:
            if not self._is_not_found(exc):
                raise self._raise(f"update secret {spec.name}", exc) from exc
            try:
                api.create_namespaced_secret(spec.namespace, body)
            except ApiException as create_exc:
                raise self._raise(f"create secret {spec.name}", create_exc) from create_exc

    def apply_pvc(self, spec: PvcSpec) -> None:
        try:
            existing = self._core.read_namespaced_persistent_volume_claim(spec.name, spec.namespace)
        except ApiException as exc:
            if not self._is_not_found(exc):
                raise self._raise(f"read pvc {spec.name}", exc) from exc
            existing = None

        if existing is not None:
            # Never shrink, and never rewrite the storage class: both are
            # immutable in the cluster and a failed patch here would abort a
            # reconcile that is otherwise fine.
            return

        body = k8s.V1PersistentVolumeClaim(
            metadata=k8s.V1ObjectMeta(
                name=spec.name, namespace=spec.namespace, labels=spec.labels or None
            ),
            spec=k8s.V1PersistentVolumeClaimSpec(
                access_modes=list(spec.access_modes),
                storage_class_name=spec.storage_class,
                # Set for a claim against a statically created volume. Without it
                # the claim waits for a provisioner that will never run, and the
                # only symptom is a claim that stays Pending forever.
                volume_name=spec.volume_name,
                resources=k8s.V1VolumeResourceRequirements(
                    requests={"storage": f"{spec.size_gb}Gi"}
                ),
            ),
        )
        try:
            self._core.create_namespaced_persistent_volume_claim(spec.namespace, body)
        except ApiException as exc:
            raise self._raise(f"create pvc {spec.name}", exc) from exc

    def read_pvc(self, namespace: str, name: str) -> PvcInfo | None:
        try:
            claim = self._core.read_namespaced_persistent_volume_claim(name, namespace)
        except ApiException as exc:
            if self._is_not_found(exc):
                return None
            raise self._raise(f"read pvc {name}", exc) from exc

        phase = getattr(getattr(claim, "status", None), "phase", None) or "Pending"
        capacity = None
        raw_capacity = (getattr(getattr(claim, "status", None), "capacity", None) or {}).get(
            "storage"
        )
        if raw_capacity:
            capacity = parse_storage_gb(str(raw_capacity))

        spec = getattr(claim, "spec", None)
        storage_class = getattr(spec, "storage_class_name", None)
        access_modes = tuple(getattr(spec, "access_modes", None) or ())

        # A ``WaitForFirstConsumer`` claim stays Pending until a pod schedules.
        # That is normal, and reporting it as a problem would make every
        # freshly created dataset look broken.
        message = None
        for condition in getattr(getattr(claim, "status", None), "conditions", None) or []:
            if getattr(condition, "type", "") == "Resizing":
                message = getattr(condition, "message", None)
                break
        if phase == "Pending" and not message:
            message = "等待调度（WaitForFirstConsumer 卷在 Pod 调度前保持 Pending）"

        return PvcInfo(
            name=name,
            phase=phase,
            storage_class=storage_class,
            capacity_gb=capacity,
            message=message,
            access_modes=access_modes,
        )

    def apply_pv(self, spec: PvSpec) -> None:
        """Create a statically provisioned volume, or leave an existing one be.

        The reclaim policy is pinned to ``Retain`` here rather than taken from
        the caller: this volume points at a directory that holds the platform's
        users' data, and a ``Delete`` policy would eventually let Kubernetes
        remove that directory. There is no case where the platform wants that, so
        it is not offered as an option.
        """
        try:
            self._core.read_persistent_volume(spec.name)
            # Already there. Rewriting it could repoint a bound claim at a
            # different directory, so an existing volume is authoritative.
            return
        except ApiException as exc:
            if not self._is_not_found(exc):
                raise self._raise(f"read pv {spec.name}", exc) from exc

        claim_ref = None
        if spec.claim_name and spec.claim_namespace:
            claim_ref = k8s.V1ObjectReference(
                kind="PersistentVolumeClaim",
                namespace=spec.claim_namespace,
                name=spec.claim_name,
            )
        body = k8s.V1PersistentVolume(
            metadata=k8s.V1ObjectMeta(name=spec.name, labels=spec.labels or None),
            spec=k8s.V1PersistentVolumeSpec(
                capacity={"storage": f"{spec.capacity_gb}Gi"},
                access_modes=list(spec.access_modes),
                persistent_volume_reclaim_policy="Retain",
                volume_mode="Filesystem",
                # `DirectoryOrCreate`, deliberately: registering a volume is then
                # enough to make it usable, with no separate provisioning step and
                # no way for a create to fail because a directory was not made yet.
                #
                # The sharp edge is that a mistyped `host_path` also succeeds — the
                # kubelet creates the wrong directory and the container comes up
                # with an empty one, which reads to the user as "my data is gone"
                # rather than as an error. `type="Directory"` would turn that into a
                # loud mount failure instead. This was chosen knowingly; the
                # mitigation is that only an administrator can register a volume
                # (`storage:manage`), so the path is written once, deliberately,
                # rather than derived from anything a tenant controls.
                host_path=k8s.V1HostPathVolumeSource(path=spec.host_path, type="DirectoryOrCreate"),
                # Pre-binding is what makes the directory↔volume correspondence
                # exact: without it the claim could be satisfied by any volume
                # that happens to be large enough.
                claim_ref=claim_ref,
            ),
        )
        try:
            self._core.create_persistent_volume(body)
        except ApiException as exc:
            raise self._raise(f"create pv {spec.name}", exc) from exc

    def read_pv(self, name: str) -> PvInfo | None:
        try:
            volume = self._core.read_persistent_volume(name)
        except ApiException as exc:
            if self._is_not_found(exc):
                return None
            raise self._raise(f"read pv {name}", exc) from exc

        return _pv_info(volume)

    def list_pvs(self, label_selector: str) -> list[PvInfo]:
        """Volumes matching a selector. See the protocol for why this is needed."""
        try:
            response = self._core.list_persistent_volume(label_selector=label_selector)
        except ApiException as exc:
            raise self._raise(f"list pvs ({label_selector})", exc) from exc
        return [_pv_info(volume) for volume in response.items]

    def delete_pv(self, name: str) -> None:
        """Delete the volume object, never the directory behind it.

        Safe because the reclaim policy is ``Retain``: this removes the platform's
        record of the directory, not the directory. A user's data surviving the
        deletion of its volume record is the intended outcome, and is why
        deletion is allowed to be this blunt.
        """
        try:
            self._core.delete_persistent_volume(name)
        except ApiException as exc:
            if self._is_not_found(exc):
                return
            raise self._raise(f"delete pv {name}", exc) from exc

    def scale_deployment(self, namespace: str, name: str, replicas: int) -> None:
        try:
            self._apps.patch_namespaced_deployment_scale(
                name, namespace, {"spec": {"replicas": replicas}}
            )
        except ApiException as exc:
            if self._is_not_found(exc):
                raise K8sError(f"deployment {name} not found in {namespace}") from exc
            raise self._raise(f"scale deployment {name}", exc) from exc

    def read_service(self, namespace: str, name: str) -> ServiceInfo | None:
        """Read one Service, for its ClusterIP.

        The IDE proxy needs the address to forward to. Returning ``None`` for a
        missing Service rather than raising is deliberate: a request for an
        instance whose Service was never created is a user-visible "not ready",
        not a cluster fault to be reported as a 502.
        """
        try:
            service = self._core.read_namespaced_service(name, namespace)
        except ApiException as exc:
            if self._is_not_found(exc):
                return None
            raise self._raise(f"read service {name}", exc) from exc
        return ServiceInfo(
            name=name,
            namespace=namespace,
            cluster_ip=service.spec.cluster_ip if service.spec else None,
        )

    def read_deployment_status(self, namespace: str, name: str) -> DeploymentStatus | None:
        """Read a Deployment's own account of whether it could create pods.

        A pod refused at admission never exists, so no pod listing can show the
        problem — the controller records it as a ``ReplicaFailure`` condition instead.
        That condition is the only place a workload which can *never* start is
        distinguishable from one that has simply not started yet.
        """
        try:
            deployment = self._apps.read_namespaced_deployment(name, namespace)
        except ApiException as exc:
            if self._is_not_found(exc):
                return None
            raise self._raise(f"read deployment {name}", exc) from exc

        failure: str | None = None
        status = deployment.status
        for condition in (status.conditions if status else None) or []:
            if condition.type == "ReplicaFailure" and condition.status == "True":
                failure = condition.message or condition.reason or "副本控制器无法创建容器"
                break
        return DeploymentStatus(name=name, namespace=namespace, failure=failure)

    def read_pod(self, namespace: str, name: str) -> PodInfo | None:
        try:
            pod = self._core.read_namespaced_pod(name, namespace)
        except ApiException as exc:
            if self._is_not_found(exc):
                return None
            raise self._raise(f"read pod {name}", exc) from exc
        return _pod_info(pod)

    def list_pods(self, namespace: str, label_selector: str) -> list[PodInfo]:
        try:
            response = self._core.list_namespaced_pod(namespace, label_selector=label_selector)
        except ApiException as exc:
            raise self._raise(f"list pods in {namespace}", exc) from exc
        # Newest first: when a deployment is being replaced, the observable pod
        # is the most recently created one.
        items = sorted(
            response.items,
            key=lambda pod: pod.metadata.creation_timestamp or 0,
            reverse=True,
        )
        return [_pod_info(pod) for pod in items]

    def apply_job(self, spec: JobSpec) -> None:
        """Create a Job. An existing one is left untouched.

        A Job's pod template is immutable, so a "replace" could only fail; the
        API server rejects it with a 422 that would be reported to the user as
        a cluster fault. Re-submitting is expressed as delete-then-create.
        """
        body = k8s.V1Job(
            metadata=k8s.V1ObjectMeta(
                name=spec.name, namespace=spec.namespace, labels=spec.labels or None
            ),
            spec=k8s.V1JobSpec(
                backoff_limit=spec.backoff_limit,
                active_deadline_seconds=spec.active_deadline_seconds,
                ttl_seconds_after_finished=spec.ttl_seconds_after_finished,
                template=k8s.V1PodTemplateSpec(
                    metadata=k8s.V1ObjectMeta(labels=spec.labels or None),
                    spec=k8s.V1PodSpec(
                        # Never, not OnFailure: a task that fails must be
                        # reported, not silently retried on a second GPU.
                        restart_policy="Never",
                        containers=[self._job_container(spec)],
                        volumes=self._job_volumes(spec),
                        node_selector=spec.node_selector or None,
                        tolerations=[
                            k8s.V1Toleration(**toleration) for toleration in spec.tolerations
                        ]
                        or None,
                    ),
                ),
            ),
        )
        try:
            self._batch.create_namespaced_job(spec.namespace, body)
        except ApiException as exc:
            if exc.status == 409:
                # Already submitted. Losing this race is harmless: the Job that
                # exists is the one the caller asked for.
                return
            raise self._raise(f"create job {spec.name}", exc) from exc

    @staticmethod
    def _job_container(spec: JobSpec) -> Any:
        resources = k8s.V1ResourceRequirements(
            requests={"cpu": f"{spec.cpu_milli}m", "memory": f"{spec.memory_mb}Mi"},
            limits={"cpu": f"{spec.cpu_milli}m", "memory": f"{spec.memory_mb}Mi"},
        )
        if spec.gpu_count > 0 and spec.gpu_type not in ("", "none"):
            resources.limits["nvidia.com/gpu"] = str(spec.gpu_count)

        mounts: list[Any] = []
        if spec.pvc_name:
            mounts.append(k8s.V1VolumeMount(name="home", mount_path=spec.mount_path))
        for index, extra in enumerate(spec.extra_mounts):
            mounts.append(
                k8s.V1VolumeMount(
                    name=f"attached-{index}",
                    mount_path=extra.mount_path,
                    # Read-only on the mount *and* on the volume below. Setting only
                    # one leaves a path a process can remount or write through, which
                    # is not what "read-only share" means.
                    read_only=extra.read_only or None,
                )
            )
        return k8s.V1Container(
            name="main",
            image=spec.image,
            command=list(spec.command) if spec.command else None,
            args=list(spec.args) if spec.args else None,
            env=[k8s.V1EnvVar(name=k, value=v) for k, v in spec.env.items()] or None,
            working_dir=spec.working_dir,
            resources=resources,
            volume_mounts=mounts or None,
        )

    @staticmethod
    def _job_volumes(spec: JobSpec) -> list[Any] | None:
        volumes: list[Any] = []
        if spec.pvc_name:
            volumes.append(
                k8s.V1Volume(
                    name="home",
                    persistent_volume_claim=k8s.V1PersistentVolumeClaimVolumeSource(
                        claim_name=spec.pvc_name
                    ),
                )
            )
        for index, extra in enumerate(spec.extra_mounts):
            volumes.append(
                k8s.V1Volume(
                    name=f"attached-{index}",
                    persistent_volume_claim=k8s.V1PersistentVolumeClaimVolumeSource(
                        claim_name=extra.pvc_name,
                        read_only=extra.read_only or None,
                    ),
                )
            )
        return volumes or None

    def read_job(self, namespace: str, name: str) -> JobInfo | None:
        try:
            job = self._batch.read_namespaced_job(name, namespace)
        except ApiException as exc:
            if self._is_not_found(exc):
                return None
            raise self._raise(f"read job {name}", exc) from exc

        status = job.status
        succeeded = status.succeeded or 0
        failed = status.failed or 0
        active = status.active or 0

        message: str | None = None
        deadline_exceeded = False
        for condition in status.conditions or []:
            if condition.type == "Failed" and condition.status == "True":
                message = condition.message or condition.reason
                deadline_exceeded = condition.reason == "DeadlineExceeded"
        for condition in status.conditions or []:
            if condition.type == "Complete" and condition.status == "True":
                message = message or condition.reason

        if succeeded:
            state = "Complete"
        elif failed:
            state = "Failed"
        elif active:
            state = "Running"
        else:
            state = "Pending"

        pod_name, node_name, exit_code = self._job_pod(namespace, name)
        return JobInfo(
            name=name,
            state=state,
            succeeded=succeeded,
            failed=failed,
            active=active,
            message=message,
            pod_name=pod_name,
            node_name=node_name,
            exit_code=exit_code,
            deadline_exceeded=deadline_exceeded,
        )

    def _job_pod(self, namespace: str, name: str) -> tuple[str | None, str | None, int | None]:
        """Find the Job's pod and read its exit code.

        The exit code lives on the container's *terminated* state, which is the
        only place that distinguishes "your script returned 2" from "the node
        evicted the pod".
        """
        try:
            pods = self._core.list_namespaced_pod(namespace, label_selector=f"job-name={name}")
        except ApiException:
            return None, None, None
        if not pods.items:
            return None, None, None

        pod = pods.items[0]
        exit_code: int | None = None
        for container in pod.status.container_statuses or []:
            terminated = container.state.terminated if container.state else None
            if terminated is not None and terminated.exit_code is not None:
                exit_code = terminated.exit_code
        node_name = pod.spec.node_name if pod.spec else None
        return pod.metadata.name, node_name, exit_code

    def delete_job(self, namespace: str, name: str, *, foreground: bool = True) -> None:
        try:
            self._batch.delete_namespaced_job(
                name,
                namespace,
                # Foreground: the pods are gone before the call returns, so a
                # cancelled task's GPU is actually freed when the user is told
                # it was cancelled.
                propagation_policy="Foreground" if foreground else "Background",
            )
        except ApiException as exc:
            if self._is_not_found(exc):
                return
            raise self._raise(f"delete job {name}", exc) from exc

    def read_pod_logs(
        self,
        namespace: str,
        name: str,
        *,
        container: str = "main",
        tail_lines: int = 500,
        previous: bool = False,
    ) -> str:
        try:
            output = self._core.read_namespaced_pod_log(
                name,
                namespace,
                container=container,
                tail_lines=tail_lines,
                previous=previous,
                # `_preload_content=False` returns the raw stream; the default
                # buffers the entire log into memory before returning.
                _preload_content=False,
            )
        except ApiException as exc:
            if self._is_not_found(exc):
                return ""
            raise self._raise(f"read logs for pod {name}", exc) from exc
        body: str = output.data.decode("utf-8", errors="replace")
        return body

    def open_exec(
        self,
        namespace: str,
        pod: str,
        *,
        container: str = "main",
        command: tuple[str, ...] = ("/bin/bash", "-l"),
        rows: int = 24,
        cols: int = 80,
    ) -> ExecSession:

        try:
            ws = stream(
                self._core.connect_get_namespaced_pod_exec,
                pod,
                namespace,
                container=container,
                # argv, never a shell string: the platform must not be able to
                # turn user input into a command of its own.
                command=list(command),
                stderr=True,
                stdin=True,
                stdout=True,
                tty=True,
                _preload_content=False,
            )
        except ApiException as exc:
            raise self._raise(f"open terminal on pod {pod}", exc) from exc

        session = KubernetesExecSession(ws)
        session.resize(rows, cols)
        return session

    def delete_workload(self, namespace: str, refs: WorkloadRefs) -> None:
        """Delete in reverse dependency order, tolerating already-absent objects."""
        steps: list[tuple[str, Any, str | None]] = [
            # Ingress and Services first: stop routing traffic before the pod
            # disappears, so users see a clean failure rather than a timeout.
            ("ingress", self._networking.delete_namespaced_ingress, refs.ingress),
            (
                "deployment",
                self._apps.delete_namespaced_deployment,
                refs.deployment,
            ),
            ("secret", self._core.delete_namespaced_secret, refs.secret),
            (
                "pvc",
                self._core.delete_namespaced_persistent_volume_claim,
                refs.pvc,
            ),
        ]
        steps.extend(
            ("service", self._core.delete_namespaced_service, name) for name in refs.services
        )
        for kind, deleter, name in steps:
            if not name:
                continue
            try:
                deleter(name, namespace)
            except ApiException as exc:
                if self._is_not_found(exc):
                    continue
                raise self._raise(f"delete {kind} {name}", exc) from exc

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

    def patch_node_labels(self, name: str, labels: dict[str, str]) -> None:
        """Merge ``labels`` into a node, leaving every other label alone.

        A strategic-merge patch rather than a replace, deliberately: nodes carry
        labels the platform has no business overwriting (``kubernetes.io/*``, the
        device plugin's ``nvidia.com/*``, whatever the operator uses for their own
        bookkeeping). Sending only the keys we own means a concurrent edit to an
        unrelated label cannot be clobbered by a sync that read the node a moment
        earlier.

        This is the platform's only write to a cluster-scoped object other than a
        namespace. It exists because pool membership has to be visible to the
        scheduler, and the scheduler only reads node labels.
        """
        if not labels:
            return
        try:
            self._core.patch_node(name, {"metadata": {"labels": labels}})
        except ApiException as exc:
            raise self._raise(f"patch labels on node {name}", exc) from exc
