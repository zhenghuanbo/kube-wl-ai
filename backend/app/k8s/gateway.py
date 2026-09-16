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


# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------
#: ``ClusterIP`` for internal-only access, ``NodePort`` to expose SSH.
ServiceType = str  # "ClusterIP" | "NodePort"

#: Default mount point for the instance's home directory.
HOME_MOUNT_PATH = "/home/dev"


@dataclass(frozen=True, slots=True)
class ContainerPort:
    """A port the container listens on."""

    name: str
    container_port: int


@dataclass(frozen=True, slots=True)
class ServicePort:
    """A port the Service exposes, optionally pinned to a fixed NodePort."""

    name: str
    port: int
    target_port: int
    node_port: int | None = None
    protocol: str = "TCP"


@dataclass(frozen=True, slots=True)
class WorkloadSpec:
    """A single-replica Deployment: one developer environment.

    Resource requests and limits are set to the *same* value on purpose. A
    request below the limit lets the scheduler over-commit a node, and an
    instance that cannot get its memory back is a far worse experience than one
    that waits for a node with room.
    """

    name: str
    namespace: str
    image: str
    labels: dict[str, str] = field(default_factory=dict)
    cpu_milli: int = 1000
    memory_mb: int = 2048
    gpu_type: str = "none"
    gpu_count: int = 0
    #: Explicit argv. Never a shell string: the plan forbids shell interpolation
    #: so a user-supplied value can never become a command.
    command: tuple[str, ...] | None = None
    args: tuple[str, ...] | None = None
    env: dict[str, str] = field(default_factory=dict)
    ports: tuple[ContainerPort, ...] = ()
    #: Home directory claim, and where it is mounted.
    pvc_name: str | None = None
    mount_path: str = HOME_MOUNT_PATH
    #: Secret whose ``authorized_keys`` entry is mounted for SSH.
    ssh_secret_name: str | None = None
    #: Attached storage, beyond the home claim above.
    extra_mounts: tuple[VolumeMountSpec, ...] = ()
    node_selector: dict[str, str] = field(default_factory=dict)
    tolerations: tuple[dict[str, Any], ...] = ()
    replicas: int = 1
    #: Grace period before SIGKILL. Long enough for an editor to flush buffers,
    #: short enough that "stop" feels immediate.
    termination_grace_seconds: int = 30


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """A Service in front of a workload.

    ``node_port`` is set by the cluster when the type is ``NodePort`` and none
    was requested; :meth:`K8sGateway.apply_service` returns the resolved spec so
    the caller can record the port that was actually allocated.
    """

    name: str
    namespace: str
    selector: dict[str, str] = field(default_factory=dict)
    ports: tuple[ServicePort, ...] = ()
    service_type: ServiceType = "ClusterIP"
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IngressSpec:
    """An Ingress exposing a workload under a path prefix."""

    name: str
    namespace: str
    host: str
    path: str
    service_name: str
    service_port: int
    annotations: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SecretSpec:
    """An opaque Secret. Values are stored verbatim (never logged)."""

    name: str
    namespace: str
    data: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PvcSpec:
    """A PersistentVolumeClaim for an instance home directory."""

    name: str
    namespace: str
    size_gb: int
    #: None means "whatever the cluster's default StorageClass is".
    storage_class: str | None = None
    access_modes: tuple[str, ...] = ("ReadWriteOnce",)
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VolumeMountSpec:
    """An additional volume attached to a workload.

    ``read_only`` is applied to both the volume and the mount. Setting only the
    mount leaves the volume writable, and a process that opens the device node
    or remounts can then write to a dataset the platform promised was
    immutable.
    """

    pvc_name: str
    mount_path: str
    read_only: bool = False
    #: Mount a sub-directory of the volume rather than its root, so a shared
    #: volume can hold per-user folders without exposing them all.
    sub_path: str | None = None


@dataclass(frozen=True, slots=True)
class PvcInfo:
    """The observed state of a PersistentVolumeClaim."""

    name: str
    #: ``Pending`` | ``Bound`` | ``Lost``
    phase: str
    #: The StorageClass the cluster actually used, which may differ from the one
    #: requested when the request was empty and a default applies.
    storage_class: str | None = None
    capacity_gb: int | None = None
    #: Why it is not Bound. ``WaitForFirstConsumer`` volumes sit Pending until a
    #: pod schedules, which is normal and must not be reported as a failure.
    message: str | None = None
    access_modes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JobSpec:
    """A run-to-completion workload.

    ``backoff_limit`` defaults to 0 rather than Kubernetes' 6: a research task
    that fails because of a typo should fail immediately and tell the user,
    instead of burning the same GPU six more times while they wait.
    """

    name: str
    namespace: str
    image: str
    labels: dict[str, str] = field(default_factory=dict)
    #: argv. For a *task* the third element may legitimately be a user's shell
    #: script — see ``app/models/task.py`` — but the platform never builds these
    #: strings itself.
    command: tuple[str, ...] | None = None
    args: tuple[str, ...] | None = None
    cpu_milli: int = 1000
    memory_mb: int = 2048
    gpu_type: str = "none"
    gpu_count: int = 0
    env: dict[str, str] = field(default_factory=dict)
    working_dir: str | None = None
    #: Optional read-only data or home volume.
    pvc_name: str | None = None
    mount_path: str = HOME_MOUNT_PATH
    node_selector: dict[str, str] = field(default_factory=dict)
    tolerations: tuple[dict[str, Any], ...] = ()
    backoff_limit: int = 0
    #: Kubernetes enforces this, so a task cannot outlive its cap while the
    #: backend is down.
    active_deadline_seconds: int | None = None
    ttl_seconds_after_finished: int | None = None


@dataclass(frozen=True, slots=True)
class JobInfo:
    """The observed state of one Job, reduced to what the platform acts on."""

    name: str
    #: ``Pending`` | ``Running`` | ``Complete`` | ``Failed``
    state: str
    succeeded: int = 0
    failed: int = 0
    active: int = 0
    message: str | None = None
    pod_name: str | None = None
    node_name: str | None = None
    exit_code: int | None = None
    #: True when Kubernetes killed it for exceeding ``activeDeadlineSeconds``,
    #: which the UI must report differently from a non-zero exit.
    deadline_exceeded: bool = False


@dataclass(frozen=True, slots=True)
class PodInfo:
    """The observed state of one pod."""

    name: str
    #: Kubernetes phase: Pending / Running / Succeeded / Failed / Unknown.
    phase: str
    ready: bool
    node_name: str | None = None
    restart_count: int = 0
    #: The most useful human-readable reason, taken from the container status
    #: (e.g. ``ImagePullBackOff``) or the pod condition.
    message: str | None = None


@dataclass(frozen=True, slots=True)
class WorkloadRefs:
    """The object names that make up one instance, for deletion."""

    deployment: str | None = None
    #: A run-to-completion workload instead of a Deployment; never both.
    job: str | None = None
    #: One instance may own several Services (SSH and the IDE are separate).
    services: tuple[str, ...] = ()
    ingress: str | None = None
    pvc: str | None = None
    secret: str | None = None


class ExecSession(Protocol):
    """A bidirectional stream into a container.

    Modelled as polling (``recv`` with a timeout) rather than blocking reads so
    that the same interface works for the Kubernetes websocket client, which
    polls, and for a scripted fake. The caller drives it from an async context
    with ``asyncio.to_thread``.
    """

    def recv(self, timeout: float) -> bytes | None:
        """Return available bytes, ``b""`` if none arrived in ``timeout``
        seconds, or ``None`` once the stream is finished."""

    def send(self, data: bytes) -> None:
        """Write to the container's stdin."""

    def resize(self, rows: int, cols: int) -> None:
        """Tell the container its terminal changed size."""

    def close(self) -> None:
        """Close the stream. Safe to call more than once."""

    @property
    def closed(self) -> bool: ...


class ExecSessionFactory(Protocol):
    """Opens an :class:`ExecSession` — a separate protocol so the gateway's
    ``open_exec`` can return something whose lifetime the caller owns."""

    def __call__(self, session: ExecSession) -> None: ...


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

    # --- workloads ---------------------------------------------------------
    def apply_deployment(self, spec: WorkloadSpec) -> None:
        """Create the Deployment, or patch it to match ``spec``."""

    def apply_service(self, spec: ServiceSpec) -> ServiceSpec:
        """Create or patch a Service and return it with any allocated NodePort."""

    def apply_ingress(self, spec: IngressSpec) -> None: ...

    def apply_secret(self, spec: SecretSpec) -> None:
        """Create or replace a Secret."""

    def apply_pvc(self, spec: PvcSpec) -> None:
        """Create the claim if absent; never expand it either.

        Growth is a separate call on purpose: Kubernetes permits expansion but
        never shrinkage, so a resize that silently failed to grow would leave
        the row and the cluster disagreeing.
        """

    def read_pvc(self, namespace: str, name: str) -> PvcInfo | None:
        """Read one claim; ``None`` when it does not exist."""

    def resize_pvc(self, namespace: str, name: str, size_gb: int) -> None:
        """Grow a claim. Shrinking is rejected by Kubernetes and by this method."""

    def scale_deployment(self, namespace: str, name: str, replicas: int) -> None:
        """Start (1) or stop (0) a workload by scaling it."""

    def read_pod(self, namespace: str, name: str) -> PodInfo | None:
        """Read one pod; ``None`` when it does not exist yet."""

    def list_pods(self, namespace: str, label_selector: str) -> list[PodInfo]:
        """List pods matching a selector, newest first."""

    def apply_job(self, spec: JobSpec) -> None:
        """Create the Job, or leave an existing one alone.

        Not a replace: a Job's pod template is immutable after creation, and
        re-applying one that is already running would be rejected by the API
        server. Callers that need to change a task delete it and submit again.
        """

    def read_job(self, namespace: str, name: str) -> JobInfo | None:
        """Read one Job; ``None`` when it no longer exists."""

    def delete_job(self, namespace: str, name: str, *, foreground: bool = True) -> None:
        """Delete a Job and, with ``foreground``, wait for its pods to go."""

    def read_pod_logs(
        self,
        namespace: str,
        name: str,
        *,
        container: str = "main",
        tail_lines: int = 500,
        previous: bool = False,
    ) -> str:
        """Read a pod's logs.

        ``previous`` reads the *terminated* container's logs, which is the only
        way to see why a container crashed: the current instance has already
        restarted and its log is empty.
        """

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
        """Attach an interactive TTY to a running container.

        ``command`` is an argv tuple, never a shell string, so nothing a user
        types into the terminal can be reinterpreted as a command by the
        platform. Whatever they type is written to the container's stdin, where
        the container's own shell decides what it means.
        """

    def delete_workload(self, namespace: str, refs: WorkloadRefs) -> None:
        """Delete an instance's objects. Missing objects are not an error."""


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
        #: Keyed by ``"namespace/name"`` because names are only unique inside a
        #: namespace, exactly as they are in a cluster.
        self.deployments: dict[str, WorkloadSpec] = {}
        self.services: dict[str, ServiceSpec] = {}
        self.ingresses: dict[str, IngressSpec] = {}
        self.secrets: dict[str, SecretSpec] = {}
        self.pvcs: dict[str, PvcSpec] = {}
        #: Scripted observations, keyed ``namespace/name``. A claim with no
        #: scripted state reports ``Bound`` at its requested size, because that
        #: is what a working cluster does and what most tests mean.
        self.pvc_states: dict[str, PvcInfo] = {}
        self.pods: dict[str, PodInfo] = {}
        #: Scripted log output per ``"namespace/pod"``.
        self.pod_logs: dict[str, str] = {}
        #: Chunks handed to successive terminal sessions, in order.
        self.exec_script: list[list[bytes]] = []
        self.exec_sessions: list[FakeExecSession] = []
        #: Jobs, and the state a test wants ``read_job`` to report for each.
        self.jobs: dict[str, JobSpec] = {}
        self.job_states: dict[str, JobInfo] = {}
        #: Next port handed out by :meth:`apply_service` for NodePort.
        self._next_node_port = 30000
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

    # --- workloads ---------------------------------------------------------
    def apply_deployment(self, spec: WorkloadSpec) -> None:
        self._record("apply_deployment", spec.namespace, spec.name)
        self.deployments[f"{spec.namespace}/{spec.name}"] = spec

    def apply_service(self, spec: ServiceSpec) -> ServiceSpec:
        self._record("apply_service", spec.namespace, spec.name)
        if spec.service_type == "NodePort":
            ports = tuple(
                replace(port, node_port=port.node_port or self._allocate_node_port())
                for port in spec.ports
            )
            spec = replace(spec, ports=ports)
        self.services[f"{spec.namespace}/{spec.name}"] = spec
        return spec

    def _allocate_node_port(self) -> int:
        self._next_node_port += 1
        return self._next_node_port

    def apply_ingress(self, spec: IngressSpec) -> None:
        self._record("apply_ingress", spec.namespace, spec.name)
        self.ingresses[f"{spec.namespace}/{spec.name}"] = spec

    def apply_secret(self, spec: SecretSpec) -> None:
        self._record("apply_secret", spec.namespace, spec.name)
        self.secrets[f"{spec.namespace}/{spec.name}"] = spec

    def apply_pvc(self, spec: PvcSpec) -> None:
        self._record("apply_pvc", spec.namespace, spec.name)
        key = f"{spec.namespace}/{spec.name}"
        existing = self.pvcs.get(key)
        # Never shrink: a claim that already exists at a larger size is left
        # alone, matching the cluster, which also refuses to shrink.
        if existing is None or spec.size_gb >= existing.size_gb:
            self.pvcs[key] = spec

    def read_pvc(self, namespace: str, name: str) -> PvcInfo | None:
        self._record("read_pvc", namespace, name)
        key = f"{namespace}/{name}"
        scripted = self.pvc_states.get(key)
        if scripted is not None:
            return scripted
        spec = self.pvcs.get(key)
        if spec is None:
            return None
        return PvcInfo(
            name=name,
            phase="Bound",
            storage_class=spec.storage_class,
            capacity_gb=spec.size_gb,
            access_modes=spec.access_modes,
        )

    def resize_pvc(self, namespace: str, name: str, size_gb: int) -> None:
        self._record("resize_pvc", namespace, name, size_gb)
        key = f"{namespace}/{name}"
        existing = self.pvcs.get(key)
        if existing is None:
            raise K8sError(f"cannot resize missing claim {key}")
        if size_gb < existing.size_gb:
            # Mirrors the API server: PVCs may grow, never shrink.
            raise K8sError(f"cannot shrink claim {key} from {existing.size_gb} to {size_gb}")
        self.pvcs[key] = replace(existing, size_gb=size_gb)

    def scale_deployment(self, namespace: str, name: str, replicas: int) -> None:
        self._record("scale_deployment", namespace, name, replicas)
        key = f"{namespace}/{name}"
        existing = self.deployments.get(key)
        if existing is not None:
            self.deployments[key] = replace(existing, replicas=replicas)

    def read_pod(self, namespace: str, name: str) -> PodInfo | None:
        self._record("read_pod", namespace, name)
        return self.pods.get(f"{namespace}/{name}")

    def list_pods(self, namespace: str, label_selector: str) -> list[PodInfo]:
        self._record("list_pods", namespace, label_selector)
        wanted = _parse_selector(label_selector)
        found: list[PodInfo] = []
        for key, pod in self.pods.items():
            if not key.startswith(f"{namespace}/"):
                continue
            deployment_key = f"{namespace}/{pod.name.rsplit('-', 1)[0]}"
            labels = self.deployments.get(deployment_key)
            if wanted and (labels is None or not _labels_match(labels.labels, wanted)):
                continue
            found.append(pod)
        return found

    def apply_job(self, spec: JobSpec) -> None:
        self._record("apply_job", spec.namespace, spec.name)
        self.jobs[f"{spec.namespace}/{spec.name}"] = spec

    def read_job(self, namespace: str, name: str) -> JobInfo | None:
        self._record("read_job", namespace, name)
        key = f"{namespace}/{name}"
        if key not in self.jobs:
            return None
        if self.job_states.get(key) is None:
            # A Job with no scripted state is still starting up, which is what
            # a real cluster reports immediately after creation.
            return JobInfo(name=name, state="Pending")
        return self.job_states[key]

    def delete_job(self, namespace: str, name: str, *, foreground: bool = True) -> None:
        self._record("delete_job", namespace, name)
        key = f"{namespace}/{name}"
        self.jobs.pop(key, None)
        self.job_states.pop(key, None)
        # Deleting a Job takes its pods with it; leaving them would make a
        # cancelled task still visible in the pod listing.
        deployment = name
        for pod_key in [k for k in self.pods if k.startswith(f"{namespace}/{deployment}-")]:
            self.pods.pop(pod_key, None)

    def read_pod_logs(
        self,
        namespace: str,
        name: str,
        *,
        container: str = "main",
        tail_lines: int = 500,
        previous: bool = False,
    ) -> str:
        self._record("read_pod_logs", namespace, name)
        # Scripted per pod so a test can assert on what the UI would display.
        return self.pod_logs.get(f"{namespace}/{name}", "")

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
        self._record("open_exec", namespace, pod)
        if self.fail_on.get("open_exec"):
            raise K8sError(self.fail_on["open_exec"])
        session = FakeExecSession(script=self.exec_script.pop(0) if self.exec_script else [])
        session.resize(rows, cols)
        self.exec_sessions.append(session)
        return session

    def delete_workload(self, namespace: str, refs: WorkloadRefs) -> None:
        self._record("delete_workload", namespace)
        for store, name in (
            (self.deployments, refs.deployment),
            (self.ingresses, refs.ingress),
            (self.pvcs, refs.pvc),
            (self.secrets, refs.secret),
        ):
            if name:
                store.pop(f"{namespace}/{name}", None)
        for service_name in refs.services:
            self.services.pop(f"{namespace}/{service_name}", None)


def _parse_selector(selector: str) -> dict[str, str]:
    """Parse ``k=v,k2=v2`` — the only selector syntax this platform emits."""
    parsed: dict[str, str] = {}
    for clause in selector.split(","):
        clause = clause.strip()
        if not clause or "=" not in clause:
            continue
        key, _, value = clause.partition("=")
        parsed[key.strip()] = value.strip()
    return parsed


def _labels_match(labels: dict[str, str], wanted: dict[str, str]) -> bool:
    return all(labels.get(key) == value for key, value in wanted.items())


class FakeExecSession:
    """Scripted :class:`ExecSession` for tests.

    Records everything written to it, so a test can assert that keystrokes reach
    the container and that resizes are forwarded — the two things that actually
    break a browser terminal.
    """

    def __init__(
        self,
        *,
        script: list[bytes] | None = None,
        eof_after_writes: int | None = None,
        eof_on_exhausted: bool = False,
    ) -> None:
        self._pending: list[bytes] = list(script or [])
        #: Whether running out of scripted output ends the session. Off by
        #: default so a test can script output *and* keep the session open.
        self._eof_on_exhausted = eof_on_exhausted
        self.written: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self._closed = False
        self._scripted = bool(script)
        #: Close the session once this many writes have arrived. Lets a test end
        #: a terminal after a known number of keystrokes without racing.
        self._eof_after_writes = eof_after_writes

    def recv(self, timeout: float) -> bytes | None:
        if self._pending:
            return self._pending.pop(0)
        if self._eof_after_writes is not None and len(self.written) >= self._eof_after_writes:
            return None
        if self._eof_on_exhausted and self._scripted:
            return None
        # `None` means finished, which is what ends the websocket. Returning
        # b"" forever would leave the browser showing a dead terminal.
        return None if self._closed else b""

    def send(self, data: bytes) -> None:
        if self._closed:
            raise K8sError("exec session is closed")
        self.written.append(data)

    def resize(self, rows: int, cols: int) -> None:
        self.resizes.append((rows, cols))

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed
