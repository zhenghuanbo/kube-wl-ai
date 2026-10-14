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
    #: Where the home volume is mounted inside the container. Required, with no
    #: default: the home is a specific user's managed storage volume, at
    #: ``/home/{username}``, so any fixed default is a path nobody owns — and a
    #: silent one would mount a volume at a location the user cannot find.
    mount_path: str
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
    #: The claim backing ``mount_path``.
    pvc_name: str | None = None
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
    """A PersistentVolumeClaim.

    Used for two unrelated things: a managed volume's claim, which is bound to a
    statically created PersistentVolume, and an instance's scratch claim, which
    relies on dynamic provisioning. ``volume_name`` is what tells them apart.
    """

    name: str
    namespace: str
    size_gb: int
    #: ``None`` means "whatever the cluster's default StorageClass is"; the empty
    #: string means "no StorageClass at all", which is how a claim against a
    #: statically created volume is asked for. The two are not interchangeable:
    #: an empty string is a decision, and leaving the field out is not.
    storage_class: str | None = None
    access_modes: tuple[str, ...] = ("ReadWriteOnce",)
    labels: dict[str, str] = field(default_factory=dict)
    #: Pre-bind this claim to one specific volume. Required for a static volume:
    #: without it the claim waits for a provisioner that will never run, which is
    #: the "Pending forever" failure this whole layer exists to avoid.
    volume_name: str | None = None


@dataclass(frozen=True, slots=True)
class VolumeMountSpec:
    """An additional volume attached to a workload.

    ``read_only`` is applied to both the volume and the mount. Setting only the
    mount leaves the volume writable, and a process that opens the device node or
    remounts can then write to a share the platform promised was read-only.

    There is deliberately no ``sub_path``: one directory is one volume, so selecting
    part of a volume is not something the platform needs to express. Where several
    shares of one kind are attached at once, each gets its own mount point instead —
    see ``storages.resolve_mount_paths``.
    """

    pvc_name: str
    mount_path: str
    read_only: bool = False


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
class PvSpec:
    """A statically provisioned PersistentVolume backed by a host directory.

    Static because the directory already exists and already holds the user's
    data. There is no provisioner to run and nothing to create: the volume's only
    job is to make an existing path mountable under a name the platform controls.

    Because that directory is also where the data actually lives, the reclaim
    policy is fixed at ``Retain`` — see the client, which does not take it from
    the caller. A volume whose deletion would delete the directory is not a
    lifecycle choice worth offering.
    """

    name: str
    host_path: str
    #: Bookkeeping, not enforcement. A hostPath volume cannot limit anything, so
    #: this exists so the claim's request can be satisfied at bind time and so the
    #: platform can account for what it has handed out. It is not a quota.
    capacity_gb: int
    access_modes: tuple[str, ...] = ("ReadWriteMany",)
    labels: dict[str, str] = field(default_factory=dict)
    #: Pre-bind to one claim, so this volume cannot be taken by another. Set
    #: together with ``claim_name`` or not at all.
    claim_namespace: str | None = None
    claim_name: str | None = None


@dataclass(frozen=True, slots=True)
class PvInfo:
    """The observed state of a PersistentVolume."""

    name: str
    #: ``Available`` | ``Bound`` | ``Released`` | ``Failed``
    phase: str
    host_path: str | None = None
    capacity_gb: int | None = None
    claim_namespace: str | None = None
    claim_name: str | None = None


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
    #: Where the home volume is mounted. Required for the same reason as on
    #: ``WorkloadSpec``: see there.
    mount_path: str
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
    #: The owner's managed storage volume, mounted as the task's home.
    pvc_name: str | None = None
    #: Opted-in shares, beyond the home above. Not mounted by default: a task that
    #: wants a group or global share says so when it is submitted.
    extra_mounts: tuple[VolumeMountSpec, ...] = ()
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
class ServiceInfo:
    """Just enough of a Service to send traffic to it.

    ``cluster_ip`` is the point of this type: the IDE proxy forwards to a Service
    rather than to a pod, because a ClusterIP is stable for the instance's whole
    life while a pod's IP changes every time the container restarts. Reading it
    back is cheaper than predicting it, and the alternative — looking up the pod by
    label on every asset request — would mean one API call per file the browser
    loads.
    """

    name: str
    namespace: str
    #: Empty for a headless Service, and ``None`` while the cluster has not
    #: assigned one yet.
    cluster_ip: str | None = None


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
    #: The container's own reason, unformatted — e.g. ``CrashLoopBackOff``.
    #:
    #: Separate from :attr:`message` because callers need to *compare* it, and
    #: `message` is a prose string built for a human ("`CrashLoopBackOff: back-off
    #: 5m0s restarting failed container=main pod=…`"). Matching on that would break
    #: the first time its wording changed.
    reason: str | None = None


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

    def patch_node_labels(self, name: str, labels: dict[str, str]) -> None:
        """Merge labels into a node, leaving its other labels untouched.

        The platform's only cluster-scoped write. Used by the node sync to keep
        the pool label in step with the database.
        """

    # --- workloads ---------------------------------------------------------
    def apply_deployment(self, spec: WorkloadSpec) -> None:
        """Create the Deployment, or patch it to match ``spec``."""

    def apply_service(self, spec: ServiceSpec) -> ServiceSpec:
        """Create or patch a Service and return it with any allocated NodePort."""

    def read_service(self, namespace: str, name: str) -> ServiceInfo | None:
        """Read one Service; ``None`` when it does not exist.

        Exists for the IDE proxy, which needs the ClusterIP it must forward to and
        would otherwise have to predict an address only the cluster can assign.
        """

    def apply_secret(self, spec: SecretSpec) -> None:
        """Create or replace a Secret."""

    def apply_pvc(self, spec: PvcSpec) -> None:
        """Create the claim if absent; never rewrite one that exists.

        A bound claim is not re-specified. Its size, its access modes and — above
        all — the volume it is pinned to are what make it mean one specific
        directory, and rewriting any of them underneath a running pod repoints that
        pod's data. Expansion in particular is not merely unwise here but
        impossible: the API server refuses it for a statically provisioned claim.
        """

    def read_pvc(self, namespace: str, name: str) -> PvcInfo | None:
        """Read one claim; ``None`` when it does not exist."""

    # --- statically provisioned volumes ------------------------------------
    #: Cluster-scoped, and created by the platform rather than by a provisioner.
    #: They exist because the directory they point at already exists: a managed
    #: volume is a way of naming a path, not a way of allocating one.
    def apply_pv(self, spec: PvSpec) -> None:
        """Create the volume if absent; never rewrite one that exists.

        Not a replace: the claim reference and the host path are what make this
        volume mean one specific directory, and rewriting either underneath a
        bound claim would repoint somebody's mounted data at a different place.
        """

    def read_pv(self, name: str) -> PvInfo | None:
        """Read one volume; ``None`` when it does not exist."""

    def delete_pv(self, name: str) -> None:
        """Delete a volume object.

        Deletes the *object*. With the reclaim policy this layer pins, the
        directory behind it is never touched — which is the point.
        """

    def list_pvs(self, label_selector: str) -> list[PvInfo]:
        """Volumes matching a selector, in a stable order.

        Exists because a global share's claim is created on demand in whatever
        namespace mounts it, and no row records those namespaces. The label every
        platform volume carries is the only thing that can find them again, so
        retiring a share has to sweep by label rather than delete known names.
        """

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
        #: ClusterIPs "assigned" by :meth:`apply_service`, keyed like ``services``.
        self.service_ips: dict[str, str] = {}
        self.ingresses: dict[str, IngressSpec] = {}
        self.secrets: dict[str, SecretSpec] = {}
        self.pvcs: dict[str, PvcSpec] = {}
        #: Scripted observations, keyed ``namespace/name``. A claim with no
        #: scripted state reports ``Bound`` at its requested size, because that
        #: is what a working cluster does and what most tests mean.
        self.pvc_states: dict[str, PvcInfo] = {}
        #: Statically provisioned volumes. Cluster-scoped, so keyed by name alone
        #: — unlike claims, which are only unique inside a namespace.
        self.pvs: dict[str, PvSpec] = {}
        self.pv_states: dict[str, PvInfo] = {}
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

    def patch_node_labels(self, name: str, labels: dict[str, str]) -> None:
        self._record("patch_node_labels", name, dict(labels))
        if not labels:
            return
        for index, node in enumerate(self.nodes):
            if node.name == name:
                # Merge, as the real patch does: a test that asserts the whole
                # label map would otherwise not notice a platform key going
                # missing, because the fake would have replaced the map too.
                self.nodes[index] = replace(node, labels={**node.labels, **labels})
                return
        raise K8sError(f"node {name!r} not found")

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
        key = f"{spec.namespace}/{spec.name}"
        self.services[key] = spec
        # A ClusterIP is assigned by the cluster, so the fake has to invent one.
        # Deterministic rather than random: a test asserting on this is asserting
        # that the proxy forwards to the Service it was told to, not on a real
        # cluster's numbering.
        if spec.service_type == "ClusterIP":
            self.service_ips.setdefault(key, f"10.43.0.{len(self.service_ips) + 2}")
        return spec

    def _allocate_node_port(self) -> int:
        self._next_node_port += 1
        return self._next_node_port

    def read_service(self, namespace: str, name: str) -> ServiceInfo | None:
        key = f"{namespace}/{name}"
        if key not in self.services:
            return None
        return ServiceInfo(name=name, namespace=namespace, cluster_ip=self.service_ips.get(key))

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

    def apply_pv(self, spec: PvSpec) -> None:
        self._record("apply_pv", spec.name)
        # Never rewrite: the host path and the claim reference are what make this
        # volume mean one specific directory. Re-applying over a bound volume
        # would repoint somebody's mounted data at a different place.
        if spec.name not in self.pvs:
            self.pvs[spec.name] = spec

    def read_pv(self, name: str) -> PvInfo | None:
        self._record("read_pv", name)
        scripted = self.pv_states.get(name)
        if scripted is not None:
            return scripted
        spec = self.pvs.get(name)
        if spec is None:
            return None
        # A volume with a claim reference is Bound to it and nothing else; one
        # without is still Available. Reporting "Bound" unconditionally would
        # hide the case the platform most needs to notice.
        return PvInfo(
            name=name,
            phase="Bound" if spec.claim_name else "Available",
            host_path=spec.host_path,
            capacity_gb=spec.capacity_gb,
            claim_namespace=spec.claim_namespace,
            claim_name=spec.claim_name,
        )

    def delete_pv(self, name: str) -> None:
        self._record("delete_pv", name)
        self.pvs.pop(name, None)
        self.pv_states.pop(name, None)

    def list_pvs(self, label_selector: str) -> list[PvInfo]:
        self._record("list_pvs", label_selector)
        wanted = _parse_selector(label_selector)
        found: list[PvInfo] = []
        # Sorted, so a sweep deletes in a predictable order and a test that asserts
        # on which volume went first is not at the mercy of dict ordering.
        for name in sorted(self.pvs):
            spec = self.pvs[name]
            if wanted and not _labels_match(spec.labels, wanted):
                continue
            scripted = self.pv_states.get(name)
            if scripted is not None:
                found.append(scripted)
                continue
            found.append(
                PvInfo(
                    name=name,
                    phase="Bound" if spec.claim_name else "Available",
                    host_path=spec.host_path,
                    capacity_gb=spec.capacity_gb,
                    claim_namespace=spec.claim_namespace,
                    claim_name=spec.claim_name,
                )
            )
        return found

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
