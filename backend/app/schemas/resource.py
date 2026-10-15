"""Schemas for compute resources, specifications and quotas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.models.enums import NetworkType, ResourceGroupKind
from app.schemas.base import ApiModel

# ---------------------------------------------------------------------------
# Resource groups
# ---------------------------------------------------------------------------


class ResourceGroupSummary(ApiModel):
    id: int
    name: str
    display_name: str
    description: str | None = None
    kind: ResourceGroupKind
    group_id: int | None = None
    group_name: str | None = None
    network_type: NetworkType
    max_runtime_seconds: int | None = None
    storage_class: str | None = None
    is_active: bool
    #: The catch-all pool. Exactly one pool may carry this, and it is where nodes
    #: land when nothing else claims them.
    is_default: bool = False
    #: How many nodes are filed here. Derived from the node rows, and the number
    #: an operator actually wants when deciding whether a pool may be deleted.
    node_count: int = 0
    gpu_models: list[str] = Field(default_factory=list)
    spec_count: int = 0
    created_at: datetime
    updated_at: datetime


class ResourceGroupDetail(ResourceGroupSummary):
    k8s_label_selector: dict[str, Any] = Field(default_factory=dict)
    k8s_tolerations: list[dict[str, Any]] = Field(default_factory=list)
    limits: ResourceGroupLimitsRead | None = None
    specs: list[ResourceSpecRead] = Field(default_factory=list)


class ResourceGroupCreate(ApiModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(default="", max_length=128)
    description: str | None = None
    kind: ResourceGroupKind = ResourceGroupKind.PRIVATE
    group_id: int | None = None
    k8s_label_selector: dict[str, Any] = Field(default_factory=dict)
    k8s_tolerations: list[dict[str, Any]] = Field(default_factory=list)
    network_type: NetworkType = NetworkType.INTERNAL
    max_runtime_seconds: int | None = Field(default=None, ge=60)
    storage_class: str | None = Field(default=None, max_length=64)
    is_default: bool = False

    @field_validator("max_runtime_seconds")
    @classmethod
    def _check_runtime(cls, value: int | None) -> int | None:
        # Below a minute is almost certainly a mistake, and an unbounded value
        # would monopolise a GPU indefinitely.
        if value is not None and value < 60:
            raise ValueError("max_runtime_seconds must be at least 60")
        return value


class ResourceGroupUpdate(ApiModel):
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = None
    kind: ResourceGroupKind | None = None
    group_id: int | None = None
    k8s_label_selector: dict[str, Any] | None = None
    k8s_tolerations: list[dict[str, Any]] | None = None
    network_type: NetworkType | None = None
    max_runtime_seconds: int | None = Field(default=None, ge=60)
    storage_class: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None
    is_default: bool | None = None


class ResourceGroupLimitsRead(ApiModel):
    """Per-user ceilings inside one pool, in cores and GB.

    No workload counts: how many instances, applications or tasks one person may
    hold is a property of the person and lives on their quota profile, so that the
    question has one answer rather than two that can disagree.
    """

    max_gpu_per_user: int
    max_cpu_cores_per_user: int
    max_memory_gb_per_user: int
    max_storage_gb_per_user: int


class ResourceGroupLimitsUpdate(ApiModel):
    #: ``-1`` means "do not check this dimension"; ``0`` still means "none at all".
    max_gpu_per_user: int | None = Field(default=None, ge=-1, le=1024)
    max_cpu_cores_per_user: int | None = Field(default=None, ge=-1, le=4096)
    max_memory_gb_per_user: int | None = Field(default=None, ge=-1, le=65536)
    max_storage_gb_per_user: int | None = Field(default=None, ge=-1, le=65536)


# ---------------------------------------------------------------------------
# Resource specs
# ---------------------------------------------------------------------------


class ResourceSpecRead(ApiModel):
    id: int
    name: str
    display_name: str
    description: str | None = None
    cpu_milli: int
    memory_mb: int
    gpu_type: str
    gpu_count: int
    is_active: bool


class ResourceSpecCreate(ApiModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    display_name: str = Field(default="", max_length=128)
    description: str | None = None
    cpu_milli: int = Field(default=1000, ge=100, le=256_000)
    memory_mb: int = Field(default=2048, ge=128, le=4_194_304)
    gpu_type: str = Field(default="none", max_length=32)
    gpu_count: int = Field(default=0, ge=0, le=16)


class ResourceSpecUpdate(ApiModel):
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = None
    cpu_milli: int | None = Field(default=None, ge=100, le=256_000)
    memory_mb: int | None = Field(default=None, ge=128, le=4_194_304)
    gpu_type: str | None = Field(default=None, max_length=32)
    gpu_count: int | None = Field(default=None, ge=0, le=16)
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Runtime limits
# ---------------------------------------------------------------------------


class RuntimeLimitRead(ApiModel):
    id: int
    name: str
    display_name: str
    max_runtime_seconds: int | None = None
    is_default: bool
    is_active: bool


class RuntimeLimitCreate(ApiModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    display_name: str = Field(default="", max_length=128)
    max_runtime_seconds: int | None = Field(default=None, ge=60)
    is_default: bool = False


class RuntimeLimitUpdate(ApiModel):
    display_name: str | None = Field(default=None, max_length=128)
    max_runtime_seconds: int | None = Field(default=None, ge=60)
    is_default: bool | None = None
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Nodes and accelerators
# ---------------------------------------------------------------------------


class GpuDeviceRead(ApiModel):
    id: int
    index: int
    model: str
    memory_mb: int
    status: str
    uuid: str | None = None


class ComputeNodeRead(ApiModel):
    id: int
    name: str
    display_name: str
    status: str
    schedulable: bool
    cpu_total_milli: int
    memory_total_mb: int
    cpu_allocatable_milli: int
    memory_allocatable_mb: int
    labels: dict[str, Any] = Field(default_factory=dict)
    is_managed: bool
    #: The pool this node belongs to. ``resource_group_name`` is denormalised so a
    #: node table does not need a second request per row to show where it lives.
    resource_group_id: int | None = None
    resource_group_name: str | None = None
    gpu_count: int = 0
    gpu_models: list[str] = Field(default_factory=list)
    last_seen_at: datetime | None = None


class NodeSyncResult(ApiModel):
    nodes_seen: int
    nodes_created: int
    nodes_updated: int
    devices_seen: int
    #: Nodes whose pool label the sync had to rewrite.
    labels_patched: int = 0
    #: Nodes with no pool because no default is configured. Should be zero.
    nodes_unpooled: int = 0


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------


class QuotaRead(ApiModel):
    id: int
    scope_type: str
    scope_id: int
    resource: str
    limit_value: int
    used_value: int
    available: int
    unit: str
    is_active: bool


class QuotaUpdate(ApiModel):
    limit_value: int = Field(ge=0)
