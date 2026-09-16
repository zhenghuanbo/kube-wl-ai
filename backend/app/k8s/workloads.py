"""Turning an instance into the Kubernetes objects that realise it.

This module is the platform's *convention* layer: which ports an IDE listens on,
what its argv looks like, how an instance's home directory is mounted, and how
the ingress path is chosen. It is deliberately pure — it takes plain values and
returns gateway dataclasses — so every rule here is testable without a database
or a cluster.

Two security decisions are encoded here rather than left to callers:

* **SSH gets its own Service.** Only the SSH Service is ``NodePort``; the IDE
  Service stays ``ClusterIP`` and is reachable solely through the ingress, which
  the backend authenticates. Putting both ports on one NodePort Service would
  expose an unauthenticated IDE on every node's IP.
* **argv, never a shell string.** Commands are tuples. A user-supplied value can
  therefore never become a command, which is the plan's explicit requirement.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.k8s.gateway import (
    HOME_MOUNT_PATH,
    ContainerPort,
    IngressSpec,
    PvcSpec,
    SecretSpec,
    ServicePort,
    ServiceSpec,
    WorkloadRefs,
    WorkloadSpec,
)
from app.k8s.naming import INSTANCE_ID_LABEL, INSTANCE_LABEL, workload_name
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


def ide_command(ide_type: IdeType, *, instance_id: int) -> tuple[str, ...] | None:
    """The argv that starts ``ide_type``, or ``None`` to use the image default.

    Jupyter needs its base URL to match the ingress prefix, or every asset and
    websocket request 404s behind the proxy — which is the single most common
    way a Jupyter ingress is misconfigured.

    The IDEs run with authentication disabled *inside* the container because the
    ingress in front of them is authenticated by the backend. Enabling a second
    password here would mean shipping one to the user and rotating it, for no
    additional protection.
    """
    if ide_type is IdeType.JUPYTER:
        base_url = f"/x/{instance_id}/"
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
            "--writable",
            "bash",
        )
    return None


def ingress_path(instance_id: int) -> str:
    """The ingress prefix for an instance, e.g. ``/x/42/``.

    Keyed on the instance id rather than its name so the path cannot collide
    across workspaces, and cannot be guessed from a display name.
    """
    return f"/x/{instance_id}/"


@dataclass(frozen=True, slots=True)
class InstanceObjects:
    """Everything that has to exist in the cluster for one instance."""

    refs: WorkloadRefs
    deployment: WorkloadSpec
    #: Kept apart rather than in one tuple: the caller records them in different
    #: columns, and which one is the NodePort is a security-relevant fact.
    ssh_service: ServiceSpec | None = None
    ide_service: ServiceSpec | None = None
    ingress: IngressSpec | None = None
    pvc: PvcSpec | None = None
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
    namespace: str,
    workspace_labels: dict[str, str],
    image: str,
    ide_type: IdeType,
    cpu_milli: int,
    memory_mb: int,
    gpu_type: str,
    gpu_count: int,
    storage_gb: int,
    ssh_enabled: bool,
    authorized_keys: str | None,
    node_selector: dict[str, str],
    tolerations: tuple[dict[str, object], ...],
    ingress_host: str,
    storage_class: str | None,
) -> InstanceObjects:
    """Build the full object set for an instance.

    ``authorized_keys`` may be ``None`` when the owner has registered no key; in
    that case no Secret is created and the volume is omitted, so the container
    starts with an sshd that accepts nothing rather than one with a stale key.
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
    pvc_name = f"{base}-home"

    deployment = WorkloadSpec(
        name=base,
        namespace=namespace,
        image=image,
        labels=labels,
        cpu_milli=cpu_milli,
        memory_mb=memory_mb,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        command=ide_command(ide_type, instance_id=instance_id),
        env={
            "WL_INSTANCE_ID": str(instance_id),
            "WL_WORKSPACE": workspace_labels.get("wl-workspace", ""),
            "HOME": HOME_MOUNT_PATH,
        },
        ports=tuple(ports),
        pvc_name=pvc_name,
        mount_path=HOME_MOUNT_PATH,
        ssh_secret_name=ssh_secret_name,
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
            # ClusterIP on purpose: the ingress is the only way in, and the
            # backend authenticates it. A NodePort here would publish an
            # unauthenticated IDE on every node address.
            service_type="ClusterIP",
            labels=labels,
        )

    ingress = None
    if ide_port is not None:
        ingress = IngressSpec(
            name=f"{base}-ide",
            namespace=namespace,
            host=ingress_host,
            path=ingress_path(instance_id),
            service_name=f"{base}-ide",
            service_port=ide_port,
            annotations={
                # Websockets are not optional for any of these IDEs, and the
                # timeouts are raised because a Jupyter kernel can sit idle.
                "nginx.ingress.kubernetes.io/proxy-read-timeout": "3600",
                "nginx.ingress.kubernetes.io/proxy-send-timeout": "3600",
                "nginx.ingress.kubernetes.io/proxy-body-size": "0",
            },
            labels=labels,
        )

    pvc = PvcSpec(
        name=pvc_name,
        namespace=namespace,
        size_gb=storage_gb,
        storage_class=storage_class,
        labels=labels,
    )

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
        ingress=f"{base}-ide" if ingress else None,
        pvc=pvc_name,
        secret=ssh_secret_name,
    )

    return InstanceObjects(
        refs=refs,
        deployment=deployment,
        ssh_service=ssh_service,
        ide_service=ide_service,
        ingress=ingress,
        pvc=pvc,
        ssh_secret=ssh_secret,
    )
