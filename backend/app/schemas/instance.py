"""Instance request/response payloads.

Wire format is camelCase via :class:`ApiModel`; snake_case is also accepted on
input, so scripts and curl stay pleasant.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.k8s.naming import MAX_LABEL_LENGTH
from app.models.enums import IdeType, InstanceStatus
from app.schemas.base import ApiModel


class InstanceWorkspaceRef(ApiModel):
    id: int
    name: str
    display_name: str
    k8s_namespace: str


class InstanceOwnerRef(ApiModel):
    id: int
    username: str
    display_name: str


class InstanceSummary(ApiModel):
    """One row of the instance list."""

    id: int
    name: str
    display_name: str
    description: str | None = None
    workspace_id: int
    workspace_name: str | None = None
    owner_user_id: int
    owner_username: str | None = None
    owner_display_name: str | None = None
    resource_group_id: int | None = None
    resource_group_name: str | None = None
    image: str
    ide_type: IdeType
    status: InstanceStatus
    status_message: str | None = None
    cpu_milli: int
    memory_mb: int
    gpu_type: str
    gpu_count: int
    ssh_enabled: bool
    #: Present only once the Service has been allocated a port.
    ssh_node_port: int | None = None
    node_name: str | None = None
    started_at: datetime | None = None
    expires_at: datetime | None = None
    is_expired: bool = False
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_instance(cls, instance: object) -> InstanceSummary:
        workspace = getattr(instance, "workspace", None)
        owner = getattr(instance, "owner", None)
        pool = getattr(instance, "resource_group", None)
        return cls(
            id=instance.id,  # type: ignore[attr-defined]
            name=instance.name,  # type: ignore[attr-defined]
            display_name=instance.display_name or instance.name,  # type: ignore[attr-defined]
            description=instance.description,  # type: ignore[attr-defined]
            workspace_id=instance.workspace_id,  # type: ignore[attr-defined]
            workspace_name=workspace.k8s_namespace if workspace else None,
            owner_user_id=instance.owner_user_id,  # type: ignore[attr-defined]
            owner_username=owner.username if owner else None,
            owner_display_name=(owner.display_name or owner.username) if owner else None,
            resource_group_id=instance.resource_group_id,  # type: ignore[attr-defined]
            resource_group_name=pool.display_name if pool else None,
            image=instance.image,  # type: ignore[attr-defined]
            ide_type=instance.ide_type,  # type: ignore[attr-defined]
            status=instance.status,  # type: ignore[attr-defined]
            status_message=instance.status_message,  # type: ignore[attr-defined]
            cpu_milli=instance.cpu_milli,  # type: ignore[attr-defined]
            memory_mb=instance.memory_mb,  # type: ignore[attr-defined]
            gpu_type=instance.gpu_type,  # type: ignore[attr-defined]
            gpu_count=instance.gpu_count,  # type: ignore[attr-defined]
            ssh_enabled=instance.ssh_enabled,  # type: ignore[attr-defined]
            ssh_node_port=instance.ssh_node_port,  # type: ignore[attr-defined]
            node_name=instance.node_name,  # type: ignore[attr-defined]
            started_at=instance.started_at,  # type: ignore[attr-defined]
            expires_at=instance.expires_at,  # type: ignore[attr-defined]
            is_expired=instance.is_expired,  # type: ignore[attr-defined]
            is_active=instance.is_active,  # type: ignore[attr-defined]
            created_at=instance.created_at,  # type: ignore[attr-defined]
            updated_at=instance.updated_at,  # type: ignore[attr-defined]
        )


class InstanceDetail(InstanceSummary):
    """Adds everything needed to actually connect to the instance."""

    k8s_deployment: str | None = None
    k8s_ssh_service: str | None = None
    k8s_ide_service: str | None = None
    k8s_ingress: str | None = None
    k8s_pvc: str | None = None
    pod_name: str | None = None
    #: The exact command to paste, or ``None`` when SSH is unavailable.
    ssh_command: str | None = None
    #: Path the IDE is served under, relative to the platform origin.
    ide_path: str | None = None
    max_runtime_seconds: int | None = None


class InstanceCreate(ApiModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(default="", max_length=128)
    description: str | None = None
    #: Omitted means "the caller's own workspace". A group administrator
    #: creating for somebody else must pass it explicitly.
    workspace_id: int | None = None
    #: Defaults to the caller. Only honoured for administrators.
    owner_user_id: int | None = None
    resource_group_id: int
    resource_spec_id: int
    image: str = Field(min_length=1, max_length=512)
    ide_type: IdeType = IdeType.NONE
    ssh_enabled: bool = True
    #: How long the instance may run. Omitted means "the pool's maximum", which
    #: is itself ``None`` for a pool that sets no limit. A value above the
    #: pool's cap is rejected rather than clamped: the options are offered by
    #: the UI, so exceeding the cap is an attempt to bypass it.
    max_runtime_seconds: int | None = Field(default=None, ge=60)
    #: Extra volumes to mount, beyond the owner's home volume — which is always
    #: mounted and cannot be chosen, because an instance has no home without it.
    #: Nothing here is charged to a quota: a volume was accounted for when it was
    #: registered.
    storage_ids: list[int] = Field(default_factory=list, max_length=16)

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if len(value) > MAX_LABEL_LENGTH - 8:
            # The Kubernetes name carries a prefix and the instance id, so a
            # name near the label limit would be truncated into something the
            # user does not recognise.
            raise ValueError(f"name must be at most {MAX_LABEL_LENGTH - 8} characters")
        return value


class InstanceUpdate(ApiModel):
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = None


class InstanceAction(ApiModel):
    """Optional body for start/stop, so the reason lands in the audit trail."""

    reason: str | None = Field(default=None, max_length=255)


class SshKeyRead(ApiModel):
    id: int
    name: str
    public_key: str
    fingerprint: str
    created_at: datetime


class SshKeyCreate(ApiModel):
    name: str = Field(min_length=1, max_length=128)
    #: The full public key line, e.g. ``ssh-ed25519 AAAA... user@host``.
    public_key: str = Field(min_length=16, max_length=4096)


class IdeAccess(ApiModel):
    """Where to open an instance's web IDE.

    A path on the platform's own origin, carrying no credential. There is no
    ``expires_in`` any more: nothing expires, because nothing is issued — the
    route behind this path is not authenticated yet. See
    :func:`app.api.x.proxy.ide_proxy`.
    """

    url: str
