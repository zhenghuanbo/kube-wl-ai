"""Turning an instance into the Kubernetes objects that realise it.

This module is the platform's *convention* layer: which ports an IDE listens on,
what its argv looks like, how an instance's home directory is mounted, and what
URL prefix an IDE is served under. It is deliberately pure — it takes plain
values and returns gateway dataclasses — so every rule here is testable without a
database or a cluster.

Two security decisions are encoded here rather than left to callers:

* **SSH gets its own Service.** Only the SSH Service is ``NodePort``; the IDE
  Service stays ``ClusterIP`` and is reached solely by the platform's own reverse
  proxy (``app/api/x/proxy.py``). Putting both ports on one NodePort Service
  would expose an unauthenticated IDE on every node's IP.

  Neither the proxy nor the ``Ingress`` it replaced authenticates anyone today,
  so the IDE is an open door to whoever can reach the platform — see the note on
  the proxy route. What the ``ClusterIP`` still buys is that the exposure is one
  known port on one known host instead of every node's address.
* **argv, never a shell string.** Commands are tuples. A user-supplied value can
  therefore never become a command, which is the plan's explicit requirement.
  The one apparent exception is :data:`TASK_SHELL`: a custom task exists to run
  a shell, so the user's script is handed to ``/bin/sh -lc`` as a single argv
  element. There is still no interpolation — the platform builds three fixed
  strings and the shell does the rest, inside the user's own namespace and
  under the user's own quota.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.k8s.gateway import (
    ContainerPort,
    JobSpec,
    PvcSpec,
    PvSpec,
    SecretSpec,
    ServicePort,
    ServiceSpec,
    VolumeMountSpec,
    WorkloadRefs,
    WorkloadSpec,
)
from app.k8s.naming import (
    INSTANCE_ID_LABEL,
    INSTANCE_LABEL,
    MANAGED_LABEL,
    MANAGED_LABEL_VALUE,
    STORAGE_ID_LABEL,
    STORAGE_KIND_LABEL,
    STORAGE_LABEL,
    TASK_ID_LABEL,
    TASK_LABEL,
    WORKSPACE_LABEL,
    storage_pv_name,
    storage_pvc_name,
    task_job_name,
    workload_name,
)
from app.models.enums import IdeType

#: sshd inside the instance image.
SSH_PORT = 22
#: Where the mounted ``authorized_keys`` fragment lives.
SSH_KEYS_PATH = "/etc/ssh/authorized_keys.d"

#: Listening port per IDE. ``none`` has no port because nothing is served.
IDE_PORTS: dict[IdeType, int] = {
    IdeType.JUPYTER: 8888,
    IdeType.VSCODE: 8080,
    IdeType.WEBSSH: 7681,
}

#: Container port names must be DNS labels, so they cannot simply be numbers.
SSH_PORT_NAME = "ssh"
IDE_PORT_NAME = "ide"

#: The most Services one instance can ever create: its SSH NodePort and its IDE
#: ClusterIP. The two are independent — either can be absent (`ssh_enabled=false`,
#: `ide_type=none`) but both together are the bound a namespace quota has to
#: cover, and a quota is a maximum rather than a prediction.
#:
#: It lives here, next to the code that creates the Services, because a workspace's
#: `count/services` allowance has to cover the instances its `count/deployments.apps`
#: allowance permits, unless an administrator has deliberately configured otherwise.
#: Sizing that allowance as one Service per instance is what capped a two-instance
#: workspace at a single usable instance: the first one's SSH + IDE pair consumed
#: the whole allowance, and the second instance's Services were rejected with
#: ``exceeded quota: count/services`` after its Deployment had already been
#: created. Both numbers now come from the workspace's quota profile, whose defaults
#: keep the 2:1 ratio.
SERVICES_PER_INSTANCE = 2


def ide_base_path(owner_username: str, instance_id: int) -> str:
    """The URL prefix an instance's web IDE is served under, e.g. ``/x/li/42/``.

    Keyed on the owner's username *and* the instance id: the username puts the
    instance under the person who owns it, the id keeps two instances of the same
    person apart. The trailing slash is load-bearing, not cosmetic — every IDE
    resolves its own assets relative to this path, so without it the browser
    resolves them one level up.

    This used to be ``/x/{id}/`` alone, back when the prefix was also the
    ``Ingress`` path. It now names the platform's own reverse-proxy route
    (``app/api/x/proxy.py``), which is why the username had to join: the route has
    to find the instance before it can forward anything, and "whose instance" is
    the natural key for a person-facing URL.
    """
    return f"/x/{owner_username}/{instance_id}/"


def ide_command(
    ide_type: IdeType, *, owner_username: str, instance_id: int
) -> tuple[str, ...] | None:
    """The argv that starts ``ide_type``, or ``None`` to use the image default.

    Two of the three IDEs have to be *told* their URL prefix, or every asset and
    websocket request they generate 404s behind the proxy — the single most common
    way a Jupyter reverse proxy is misconfigured:

    * **Jupyter** takes it as ``--ServerApp.base_url``.
    * **ttyd** takes it as ``--base-path``. It is passed here rather than left to
      the image's wrapper, which would otherwise guess ``/x/{id}/`` from
      ``WL_INSTANCE_ID`` and disagree with the route the browser is on.
    * **code-server** has no equivalent option (4.141.0 offers only
      ``--abs-proxy-base-path``, which concerns its own port-forwarding). As it
      happens its assets are all relative, so the proxy *strips* the prefix for
      this IDE instead — see :data:`IDE_STRIPS_PREFIX`. Told here rather than
      worked around: the argv cannot invent a flag the binary lacks.

    The IDEs run with authentication disabled *inside* the container. That is
    only safe because something in front is meant to authenticate them; today
    nothing does, so the proxy route is an open door for anyone who can reach the
    platform. Recorded here rather than left implicit — see the note on
    :func:`app.api.x.proxy.ide_proxy`.
    """
    if ide_type is IdeType.JUPYTER:
        base_url = ide_base_path(owner_username, instance_id)
        return (
            "jupyter",
            "lab",
            "--ip=0.0.0.0",
            f"--port={IDE_PORTS[IdeType.JUPYTER]}",
            f"--ServerApp.base_url={base_url}",
            "--ServerApp.token=",
            "--ServerApp.password=",
            "--ServerApp.disable_check_xsrf=True",
            "--no-browser",
        )
    if ide_type is IdeType.VSCODE:
        return (
            "code-server",
            "--bind-addr",
            f"0.0.0.0:{IDE_PORTS[IdeType.VSCODE]}",
            "--auth",
            "none",
            "--disable-telemetry",
        )
    if ide_type is IdeType.WEBSSH:
        return (
            "ttyd",
            "--port",
            str(IDE_PORTS[IdeType.WEBSSH]),
            # Explicit, so the image wrapper sees a caller-supplied base path and
            # does not add its own.
            "--base-path",
            ide_base_path(owner_username, instance_id),
            "--writable",
            "bash",
        )
    return None


#: Whether the reverse proxy must strip ``/x/{username}/{id}`` before forwarding.
#:
#: Not a preference — it follows from what each binary can be told:
#:
#: * ``False`` for jupyter and ttyd: they are started *with* the prefix and
#:   generate their asset URLs already carrying it, so the request must arrive
#:   intact. Stripping would leave their output pointing at paths the proxy
#:   cannot route.
#: * ``True`` for code-server: it cannot be told a prefix and always answers as if
#:   it owned ``/``. Its responses reference everything relatively, so if the
#:   prefix is stripped and the browser sits at a path ending in ``/``, the
#:   browser re-adds the prefix to every relative URL by itself.
#:
#: ``none`` has no IDE and therefore no opinion; it is absent from the mapping.
IDE_STRIPS_PREFIX: dict[IdeType, bool] = {
    IdeType.JUPYTER: False,
    IdeType.VSCODE: True,
    IdeType.WEBSSH: False,
}


@dataclass(frozen=True, slots=True)
class InstanceObjects:
    """Everything that has to exist in the cluster for one instance.

    No PVC: an instance owns no volume. Its home is the owner's *managed* storage
    volume, which outlives the instance and is created from the storage page — so
    deleting an instance must leave that claim alone, and the instance records no
    claim it is responsible for deleting.
    """

    refs: WorkloadRefs
    deployment: WorkloadSpec
    #: Kept apart rather than in one tuple: the caller records them in different
    #: columns, and which one is the NodePort is a security-relevant fact.
    ssh_service: ServiceSpec | None = None
    ide_service: ServiceSpec | None = None
    ssh_secret: SecretSpec | None = None

    @property
    def services(self) -> tuple[ServiceSpec, ...]:
        return tuple(
            service for service in (self.ssh_service, self.ide_service) if service is not None
        )


def build_instance_objects(
    *,
    instance_id: int,
    instance_name: str,
    owner_username: str,
    namespace: str,
    workspace_labels: dict[str, str],
    image: str,
    ide_type: IdeType,
    cpu_milli: int,
    memory_mb: int,
    gpu_type: str,
    gpu_count: int,
    home_pvc_name: str,
    home_mount_path: str,
    ssh_enabled: bool,
    authorized_keys: str | None,
    node_selector: dict[str, str],
    tolerations: tuple[dict[str, object], ...],
    extra_mounts: tuple[VolumeMountSpec, ...] = (),
) -> InstanceObjects:
    """Build the full object set for an instance.

    ``authorized_keys`` may be ``None`` when the owner has registered no key; in
    that case no Secret is created and the volume is omitted, so the container
    starts with an sshd that accepts nothing rather than one with a stale key.

    The home is the owner's managed storage volume, mounted at ``home_mount_path``
    (``/home/{username}``) and named in ``HOME``. It is mounted here rather than
    offered as a choice because there is no useful instance without it: the
    caller refuses to get this far if the owner has no such volume.
    """
    base = workload_name(instance_id, instance_name)
    labels = {
        **workspace_labels,
        INSTANCE_LABEL: instance_name,
        INSTANCE_ID_LABEL: str(instance_id),
    }
    # A selector must be stable for the life of the object: instance id and
    # workspace are immutable, a display name is not.
    selector = {
        INSTANCE_ID_LABEL: str(instance_id),
        "wl-workspace": workspace_labels.get("wl-workspace", ""),
    }

    ide_port = IDE_PORTS.get(ide_type)
    ports: list[ContainerPort] = []
    if ssh_enabled:
        ports.append(ContainerPort(name=SSH_PORT_NAME, container_port=SSH_PORT))
    if ide_port is not None:
        ports.append(ContainerPort(name=IDE_PORT_NAME, container_port=ide_port))

    ssh_secret_name = f"{base}-ssh" if ssh_enabled and authorized_keys else None

    deployment = WorkloadSpec(
        name=base,
        namespace=namespace,
        image=image,
        labels=labels,
        cpu_milli=cpu_milli,
        memory_mb=memory_mb,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        command=ide_command(ide_type, owner_username=owner_username, instance_id=instance_id),
        env={
            "WL_INSTANCE_ID": str(instance_id),
            "WL_WORKSPACE": workspace_labels.get("wl-workspace", ""),
            "HOME": home_mount_path,
        },
        ports=tuple(ports),
        pvc_name=home_pvc_name,
        mount_path=home_mount_path,
        ssh_secret_name=ssh_secret_name,
        extra_mounts=extra_mounts,
        node_selector=node_selector,
        tolerations=tolerations,
    )

    ssh_service: ServiceSpec | None = None
    if ssh_enabled:
        # The only Service exposed outside the cluster. NodePort is a deliberate
        # trade-off: it needs no extra load balancer, and the university cluster
        # already restricts node ports at the network edge.
        ssh_service = ServiceSpec(
            name=f"{base}-ssh",
            namespace=namespace,
            selector=selector,
            ports=(ServicePort(name=SSH_PORT_NAME, port=SSH_PORT, target_port=SSH_PORT),),
            service_type="NodePort",
            labels=labels,
        )
    ide_service: ServiceSpec | None = None
    if ide_port is not None:
        ide_service = ServiceSpec(
            name=f"{base}-ide",
            namespace=namespace,
            selector=selector,
            ports=(ServicePort(name=IDE_PORT_NAME, port=ide_port, target_port=ide_port),),
            # ClusterIP on purpose: the platform's own reverse proxy is the only
            # way in (``app/api/x/proxy.py``), and it reaches this Service by
            # ClusterIP. A NodePort here would publish an unauthenticated IDE on
            # every node address, which is worse than the proxy for exactly the
            # same reason an ingress would have been better — the difference is
            # only that the proxy exists.
            service_type="ClusterIP",
            labels=labels,
        )

    # No Ingress. The IDE used to be published through one at `/x/{id}/`, which
    # only works if the cluster has an ingress controller *and* the platform's
    # origin is that controller's host — so the user-facing URL changed hostname
    # and the browser left the platform's origin. The proxy route replaces it and
    # keeps one origin. `WorkloadRefs.ingress` stays in the model because
    # instances built before this change still have one to collect on deletion.

    ssh_secret = None
    if ssh_secret_name and authorized_keys:
        ssh_secret = SecretSpec(
            name=ssh_secret_name,
            namespace=namespace,
            data={"authorized_keys": authorized_keys},
            labels=labels,
        )

    refs = WorkloadRefs(
        deployment=base,
        services=tuple(
            service.name for service in (ssh_service, ide_service) if service is not None
        ),
        # No `pvc`: the home claim belongs to the storage volume, not to this
        # instance. Recording it here would be actively dangerous — the delete
        # path removes the claims a workload lists, and deleting an instance must
        # never delete the owner's storage.
        pvc=None,
        secret=ssh_secret_name,
    )

    return InstanceObjects(
        refs=refs,
        deployment=deployment,
        ssh_service=ssh_service,
        ide_service=ide_service,
        ssh_secret=ssh_secret,
    )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
#: The interpreter a task's script is handed to.
#:
#: Three argv elements, of which the third is the user's text. The platform
#: composes no part of the script and performs no interpolation, so nothing the
#: user writes can become a *platform* command — but they still get a real
#: shell, which is the entire point of a custom task.
TASK_SHELL: tuple[str, ...] = ("/bin/sh", "-lc")


@dataclass(frozen=True, slots=True)
class TaskObjects:
    """The objects that realise one custom task.

    No PVC: like an instance, a task owns no volume. Its home is the owner's
    managed storage volume, which the task mounts and must not delete.
    """

    refs: WorkloadRefs
    job: JobSpec


def build_task_objects(
    *,
    task_id: int,
    task_name: str,
    namespace: str,
    workspace_name: str,
    image: str,
    script: str,
    cpu_milli: int,
    memory_mb: int,
    gpu_type: str,
    gpu_count: int,
    home_pvc_name: str,
    home_mount_path: str,
    working_dir: str | None,
    env: dict[str, str],
    node_selector: dict[str, str],
    tolerations: tuple[dict[str, object], ...],
    max_runtime_seconds: int | None,
    ttl_seconds_after_finished: int,
    extra_mounts: tuple[VolumeMountSpec, ...] = (),
) -> TaskObjects:
    """Build the Job for one task.

    The home volume is always mounted, for the same reason as on an instance: the
    caller refuses to get here if the owner has no storage volume. Anything else
    (a group or global share) arrives in ``extra_mounts`` because the user chose
    it — an opt-in, not a default.
    """
    name = task_job_name(task_id, task_name)
    labels = {
        MANAGED_LABEL: MANAGED_LABEL_VALUE,
        WORKSPACE_LABEL: workspace_name,
        TASK_LABEL: task_name,
        TASK_ID_LABEL: str(task_id),
    }

    job = JobSpec(
        name=name,
        namespace=namespace,
        image=image,
        labels=labels,
        command=TASK_SHELL,
        args=(script,),
        cpu_milli=cpu_milli,
        memory_mb=memory_mb,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        env={
            **env,
            "WL_TASK_ID": str(task_id),
            "WL_WORKSPACE": workspace_name,
            "HOME": home_mount_path,
        },
        working_dir=working_dir,
        pvc_name=home_pvc_name,
        mount_path=home_mount_path,
        extra_mounts=extra_mounts,
        node_selector=node_selector,
        tolerations=tolerations,
        backoff_limit=0,
        # Kubernetes enforces the cap, so a task cannot outlive it even if the
        # backend is down when the deadline passes.
        active_deadline_seconds=max_runtime_seconds,
        ttl_seconds_after_finished=ttl_seconds_after_finished,
    )

    return TaskObjects(refs=WorkloadRefs(job=name), job=job)


# ---------------------------------------------------------------------------
# Standalone storage
# ---------------------------------------------------------------------------
#: Access modes, by how many pods must share the volume at once.
#:
#: ``ReadWriteMany`` is not available from every CSI driver, and a share that
#: silently fell back to ``ReadWriteOnce`` would work for the first user and fail
#: for everyone else. The mode is recorded on the row and reported honestly when
#: the cluster refuses it, rather than downgraded.
STORAGE_ACCESS_MODES: dict[str, tuple[str, ...]] = {
    "ReadWriteOnce": ("ReadWriteOnce",),
    "ReadOnlyMany": ("ReadOnlyMany",),
    "ReadWriteMany": ("ReadWriteMany",),
    "ReadWriteOncePod": ("ReadWriteOncePod",),
}


@dataclass(frozen=True, slots=True)
class StorageObjects:
    """The cluster objects behind one storage volume.

    ``pvc`` is optional because not every volume has a claim: a claim lives in a
    namespace, and a global share belongs to no namespace until a workload mounts
    it. The volume itself always exists — that is what registering a directory as
    a managed volume means.
    """

    refs: WorkloadRefs
    pv: PvSpec
    pvc: PvcSpec | None = None


def build_storage_objects(
    *,
    storage_id: int,
    storage_name: str,
    host_path: str,
    namespace: str | None,
    workspace_labels: dict[str, str],
    size_gb: int,
    access_mode: str | None = None,
    kind: str | None = None,
    extra_namespace: bool = False,
) -> StorageObjects:
    """Build the volume for a managed directory, and its claim when one can exist.

    No Deployment or Service: a volume exists whether or not anything is attached
    to it, which is the entire point of separating it from an instance.

    Statically provisioned, not dynamic. The directory already exists and already
    holds the data, so the volume's only job is to name it — which is also why the
    claim is pinned to the volume by name. With no provisioner to satisfy an
    unpinned claim, it waits forever, and "Pending" then means both "the cluster
    is slow" and "this will never work".

    ``extra_namespace`` says this claim is for a namespace that is *not* the
    volume's home — a global share being mounted from some lab's workspace. Since
    a PersistentVolume binds to exactly one claim, such a mount needs its own
    volume, so both objects are named after the namespace
    (:func:`app.k8s.naming.storage_pv_name`).
    """
    labels = {
        **workspace_labels,
        MANAGED_LABEL: MANAGED_LABEL_VALUE,
        STORAGE_LABEL: storage_name,
        STORAGE_ID_LABEL: str(storage_id),
    }
    if kind:
        labels[STORAGE_KIND_LABEL] = kind

    # One mode for both objects. Kubernetes binds a claim only to a volume that
    # offers a mode the claim asked for, so a mismatch here is a claim that never
    # binds and gives no reason why.
    modes = STORAGE_ACCESS_MODES.get(access_mode or "", ("ReadWriteOnce",))
    name_suffix = namespace if extra_namespace else None
    pv_name = storage_pv_name(storage_id, storage_name, namespace=name_suffix)

    pvc = None
    if namespace:
        pvc = PvcSpec(
            name=storage_pvc_name(storage_id, storage_name, namespace=name_suffix),
            namespace=namespace,
            size_gb=size_gb,
            # Explicitly *no* StorageClass. A static volume has none, and leaving
            # the field unset would silently mean "use the cluster default" — a
            # dynamic provisioner this platform is deliberately not relying on.
            storage_class="",
            access_modes=modes,
            labels=labels,
            volume_name=pv_name,
        )

    pv = PvSpec(
        name=pv_name,
        host_path=host_path,
        capacity_gb=size_gb,
        access_modes=modes,
        labels=labels,
        # Pre-bind where a claim exists, so the directory↔volume correspondence is
        # exact and no other claim can take this volume by being large enough.
        claim_namespace=pvc.namespace if pvc is not None else None,
        claim_name=pvc.name if pvc is not None else None,
    )
    return StorageObjects(
        refs=WorkloadRefs(pvc=pvc.name if pvc is not None else None), pv=pv, pvc=pvc
    )
