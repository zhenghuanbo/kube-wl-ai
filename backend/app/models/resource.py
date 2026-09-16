"""Compute resources, their specifications, limits and quotas.

The shape here encodes one decision worth stating plainly: **hardware is pooled
into resource groups, and a workspace must be granted a group before it can use
it.** A resource group is therefore both a scheduling target (a node label
selector) and an authorisation unit, which is why the grant table lives with the
workspace models.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, str_enum
from app.models.enums import (
    GpuDeviceStatus,
    NetworkType,
    NodeStatus,
    QuotaResource,
    QuotaScope,
    ResourceGroupKind,
)

if TYPE_CHECKING:
    from app.models.group import Group

#: Accelerator models the platform knows how to label and display.
#:
#: Stored as a free ``VARCHAR`` rather than a database enum on purpose: new
#: silicon ships faster than migrations, and ``str_enum`` emits a CHECK
#: constraint that would turn "add a B200 node" into a schema change. The known
#: set is enforced at the API schema layer instead.
GPU_MODELS: tuple[str, ...] = ("v100", "a100", "a800", "h100", "h800", "a10", "l40s", "none")


class ResourceGroup(Base, TimestampMixin):
    """A pool of interchangeable nodes, and the permission to schedule onto it."""

    __tablename__ = "resource_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)

    kind: Mapped[ResourceGroupKind] = mapped_column(
        str_enum(ResourceGroupKind, "resource_group_kind"),
        nullable=False,
        default=ResourceGroupKind.PRIVATE,
    )
    #: Owning group for a private pool; ``None`` means platform-wide.
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), index=True
    )

    #: Node label selector used to build ``nodeSelector`` when scheduling.
    k8s_label_selector: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: Tolerations applied alongside the selector, so dedicated GPU pools that
    #: carry a taint remain usable by the workloads meant for them.
    k8s_tolerations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    network_type: Mapped[NetworkType] = mapped_column(
        str_enum(NetworkType, "network_type"), nullable=False, default=NetworkType.INTERNAL
    )
    #: Hard ceiling on how long a single workload may run here.
    max_runtime_seconds: Mapped[int | None] = mapped_column(Integer)
    #: Where the workspace's persistent volumes for this pool should land.
    storage_class: Mapped[str | None] = mapped_column(String(64))

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    group: Mapped[Group | None] = relationship(lazy="joined")
    specs: Mapped[list[ResourceSpec]] = relationship(
        secondary="resource_group_specs",
        lazy="selectin",
        viewonly=True,
    )
    limits: Mapped[ResourceGroupLimit | None] = relationship(
        back_populates="resource_group",
        cascade="all, delete-orphan",
        uselist=False,
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<ResourceGroup id={self.id} name={self.name!r} kind={self.kind}>"


class ResourceSpec(Base, TimestampMixin):
    """A named CPU / memory / GPU shape a user can pick when launching."""

    __tablename__ = "resource_specs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)

    cpu_milli: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=2048)
    #: See :data:`GPU_MODELS` — a model name, never an integer count alone.
    gpu_type: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return (
            f"<ResourceSpec id={self.id} name={self.name!r} "
            f"cpu={self.cpu_milli}m mem={self.memory_mb}MB gpu={self.gpu_count}x{self.gpu_type}>"
        )


class ResourceGroupSpec(Base):
    """Which specs a resource group offers. Pure association, no payload."""

    __tablename__ = "resource_group_specs"
    __table_args__ = (
        UniqueConstraint("resource_group_id", "resource_spec_id", name="uq_resource_group_specs"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_group_id: Mapped[int] = mapped_column(
        ForeignKey("resource_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource_spec_id: Mapped[int] = mapped_column(
        ForeignKey("resource_specs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class RuntimeLimit(Base, TimestampMixin):
    """A named maximum runtime offered to users (e.g. 4h / 24h / 7d)."""

    __tablename__ = "runtime_limits"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    #: ``None`` means unlimited — expressed as NULL rather than a magic number.
    max_runtime_seconds: Mapped[int | None] = mapped_column(Integer)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return f"<RuntimeLimit id={self.id} name={self.name!r} max={self.max_runtime_seconds}>"


class ResourceGroupLimit(Base, TimestampMixin):
    """Per-user ceilings inside one resource group.

    Modelled per resource group rather than globally because a pool of eight
    A100s and a pool of two H100s need very different fairness rules; a single
    platform-wide number would either strangle one or overrun the other.
    """

    __tablename__ = "resource_group_limits"
    __table_args__ = (UniqueConstraint("resource_group_id", name="uq_resource_group_limits_group"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_group_id: Mapped[int] = mapped_column(
        ForeignKey("resource_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )

    max_concurrent_instances: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    max_gpu_per_instance: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_gpu_per_user: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    max_cpu_milli_per_user: Mapped[int] = mapped_column(Integer, nullable=False, default=8000)
    max_memory_mb_per_user: Mapped[int] = mapped_column(Integer, nullable=False, default=16384)
    max_storage_gb_per_user: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    resource_group: Mapped[ResourceGroup] = relationship(back_populates="limits")

    def __repr__(self) -> str:
        return f"<ResourceGroupLimit rg={self.resource_group_id}>"


class ComputeNode(Base, TimestampMixin):
    """A cluster node, cached so scheduling decisions do not hit the API server."""

    __tablename__ = "compute_nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Kubernetes node name; the join key back to the cluster.
    name: Mapped[str] = mapped_column(String(253), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    hostname: Mapped[str | None] = mapped_column(String(253))

    status: Mapped[NodeStatus] = mapped_column(
        str_enum(NodeStatus, "node_status"), nullable=False, default=NodeStatus.UNKNOWN
    )
    schedulable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    cpu_total_milli: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    memory_total_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cpu_allocatable_milli: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    memory_allocatable_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    labels: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: Platform-owned nodes must be distinguishable: the backend is only allowed
    #: to touch namespaces it created, and operators need to know which nodes it
    #: is accounting for.
    is_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    gpu_devices: Mapped[list[GpuDevice]] = relationship(
        back_populates="node",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="GpuDevice.index",
    )

    def __repr__(self) -> str:
        return f"<ComputeNode id={self.id} name={self.name!r} status={self.status}>"


class GpuDevice(Base, TimestampMixin):
    """One physical accelerator, so allocation is trackable per card."""

    __tablename__ = "gpu_devices"
    __table_args__ = (
        UniqueConstraint("node_id", "index", name="uq_gpu_devices_node_index"),
        Index("ix_gpu_devices_model_status", "model", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(
        ForeignKey("compute_nodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Device index within the node, as reported by the device plugin.
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[GpuDeviceStatus] = mapped_column(
        str_enum(GpuDeviceStatus, "gpu_device_status"),
        nullable=False,
        default=GpuDeviceStatus.AVAILABLE,
    )
    uuid: Mapped[str | None] = mapped_column(String(128), index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    node: Mapped[ComputeNode] = relationship(back_populates="gpu_devices")

    def __repr__(self) -> str:
        return f"<GpuDevice node={self.node_id} idx={self.index} model={self.model!r}>"


class Quota(Base, TimestampMixin):
    """A counter and its ceiling, at one scope.

    ``used_value`` is stored rather than recomputed so that a quota check is a
    single row read on the hot path; the reconcile loop is responsible for
    correcting drift against reality.
    """

    __tablename__ = "quotas"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", "resource", name="uq_quotas_scope_resource"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scope_type: Mapped[QuotaScope] = mapped_column(
        str_enum(QuotaScope, "quota_scope"), nullable=False
    )
    #: Id of the group / user / workspace, interpreted via ``scope_type``.
    scope_id: Mapped[int] = mapped_column(Integer, nullable=False)

    resource: Mapped[str] = mapped_column(String(48), nullable=False)
    limit_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Presentation only (``m`` / ``MB`` / ``GB`` / ``card``); never used in maths.
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="")

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    @property
    def available(self) -> int:
        return max(self.limit_value - self.used_value, 0)

    @property
    def is_exhausted(self) -> bool:
        return self.used_value >= self.limit_value

    def __repr__(self) -> str:
        return (
            f"<Quota {self.scope_type}:{self.scope_id} {self.resource} "
            f"{self.used_value}/{self.limit_value}>"
        )


#: Convenience alias so callers can build resource keys without importing enums.
RESOURCE_KEY_CPU = QuotaResource.CPU.value
RESOURCE_KEY_MEMORY = QuotaResource.MEMORY.value
RESOURCE_KEY_GPU = QuotaResource.GPU.value
RESOURCE_KEY_STORAGE = QuotaResource.STORAGE.value
RESOURCE_KEY_INSTANCE = QuotaResource.INSTANCE.value
RESOURCE_KEY_TASK = QuotaResource.TASK.value


def gpu_resource_key(model: str) -> str:
    """Resource key for one accelerator model, e.g. ``gpu:a100``.

    Model-qualified keys are what make "2× A100 but no H100" expressible; a bare
    ``gpu`` key would let a user burn the scarce pool by accident.
    """
    return f"{RESOURCE_KEY_GPU}:{model}"
