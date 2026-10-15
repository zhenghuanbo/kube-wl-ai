"""Compute-resource endpoints: pools, specs, runtime limits, nodes and quotas.

The catalogue (pools, specs, duration options) is readable by any authenticated
user — a user must be able to see what they may ask for. *Using* a pool is a
separate, object-level decision made against the workspace's grants, which is
what keeps this a real boundary rather than a listing filter.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.auth import policy
from app.auth.policy import PermissionCheck
from app.core.deps import CurrentUser, DbSession, require
from app.core.errors import NotFoundError
from app.k8s import get_gateway
from app.models.enums import QuotaScope
from app.models.resource import ComputeNode, ResourceSpec, RuntimeLimit
from app.schemas.base import ApiMessage
from app.schemas.common import Page, PageMeta
from app.schemas.resource import (
    ComputeNodeRead,
    GpuDeviceRead,
    NodeSyncResult,
    QuotaRead,
    ResourceGroupCreate,
    ResourceGroupDetail,
    ResourceGroupLimitsRead,
    ResourceGroupLimitsUpdate,
    ResourceGroupSummary,
    ResourceGroupUpdate,
    ResourceSpecCreate,
    ResourceSpecRead,
    ResourceSpecUpdate,
    RuntimeLimitCreate,
    RuntimeLimitRead,
    RuntimeLimitUpdate,
)
from app.services import quota_guard
from app.services import resources as resource_service
from app.services.workspaces import get_workspace

router = APIRouter(tags=["resources"])

CanReadPool = Depends(require("resource_group:read"))
CanManagePool = Depends(require("resource_group:update"))
CanCreatePool = Depends(require("resource_group:create"))
CanDeletePool = Depends(require("resource_group:delete"))
CanReadSpec = Depends(require("resource_spec:read"))
CanCreateSpec = Depends(require("resource_spec:create"))
CanUpdateSpec = Depends(require("resource_spec:update"))
CanDeleteSpec = Depends(require("resource_spec:delete"))
CanReadLimit = Depends(require("runtime_limit:read"))
CanManageLimit = Depends(require("runtime_limit:update"))
CanCreateLimit = Depends(require("runtime_limit:create"))
CanReadNode = Depends(require("node:read"))
CanSyncNode = Depends(require("node:sync"))
CanReadQuota = Depends(require("quota:read"))


def _pages(total: int, page_size: int) -> int:
    return (total + page_size - 1) // page_size if page_size else 0


# ---------------------------------------------------------------------------
# Resource groups
# ---------------------------------------------------------------------------
@router.get("/resource-groups", response_model=Page[ResourceGroupSummary])
async def list_resource_groups(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanReadPool],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, alias="pageSize")] = 50,
    kind: Annotated[str | None, Query(max_length=16)] = None,
    is_active: Annotated[bool | None, Query(alias="isActive")] = None,
    search: Annotated[str | None, Query(max_length=150)] = None,
) -> Page[ResourceGroupSummary]:
    result = await resource_service.list_resource_groups(
        session,
        actor,
        kind=kind,
        is_active=is_active,
        search=search,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return Page[ResourceGroupSummary](
        items=list(result.items),
        meta=PageMeta(
            page=page,
            page_size=page_size,
            total=result.total,
            pages=_pages(result.total, page_size),
        ),
    )


@router.post(
    "/resource-groups", response_model=ResourceGroupDetail, status_code=status.HTTP_201_CREATED
)
async def create_resource_group(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanCreatePool],
    payload: ResourceGroupCreate,
) -> ResourceGroupDetail:
    group = await resource_service.create_resource_group(session, actor, payload)
    return resource_service.group_to_detail(group, [])


@router.get("/resource-groups/{group_id}", response_model=ResourceGroupDetail)
async def get_resource_group(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanReadPool],
    group_id: int,
) -> ResourceGroupDetail:
    group = await resource_service.get_resource_group(session, actor, group_id)
    specs = (await resource_service.specs_for_groups(session, [group.id])).get(group.id, [])
    counts = await resource_service.node_counts_for_groups(session, [group.id])
    return resource_service.group_to_detail(group, specs, node_count=counts.get(group.id, 0))


@router.patch("/resource-groups/{group_id}", response_model=ResourceGroupDetail)
async def update_resource_group(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanManagePool],
    group_id: int,
    payload: ResourceGroupUpdate,
) -> ResourceGroupDetail:
    group = await resource_service.get_resource_group(session, actor, group_id)
    group = await resource_service.update_resource_group(session, actor, group, payload)
    specs = (await resource_service.specs_for_groups(session, [group.id])).get(group.id, [])
    counts = await resource_service.node_counts_for_groups(session, [group.id])
    return resource_service.group_to_detail(group, specs, node_count=counts.get(group.id, 0))


@router.delete("/resource-groups/{group_id}", response_model=ApiMessage)
async def delete_resource_group(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanDeletePool],
    group_id: int,
) -> ApiMessage:
    group = await resource_service.get_resource_group(session, actor, group_id)
    await resource_service.delete_resource_group(session, actor, group)
    return ApiMessage(message=f"资源组 {group.name} 已删除")


@router.put("/resource-groups/{group_id}/limits", response_model=ResourceGroupLimitsRead)
async def update_resource_group_limits(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanManagePool],
    group_id: int,
    payload: ResourceGroupLimitsUpdate,
) -> ResourceGroupLimitsRead:
    group = await resource_service.get_resource_group(session, actor, group_id)
    limit = await resource_service.update_group_limits(session, actor, group, payload)
    return ResourceGroupLimitsRead.model_validate(limit, from_attributes=True)


@router.put("/resource-groups/{group_id}/specs", response_model=list[ResourceSpecRead])
async def set_resource_group_specs(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanManagePool],
    group_id: int,
    spec_ids: list[int],
) -> list[ResourceSpecRead]:
    """Replace which specs a pool offers."""
    group = await resource_service.get_resource_group(session, actor, group_id)
    specs = await resource_service.set_group_specs(session, actor, group, spec_ids)
    return [resource_service.spec_to_read(spec) for spec in specs]


@router.put("/resource-groups/{group_id}/nodes", response_model=NodeSyncResult)
async def set_resource_group_nodes(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanManagePool],
    group_id: int,
    node_ids: list[int],
) -> NodeSyncResult:
    """Replace which nodes belong to a pool.

    Nodes dropped from the list go to the default pool, and each move rewrites the
    node's ``wl-pool`` label so the scheduler and the database agree. The response
    reports how many labels had to change, which is the number worth showing an
    operator after a bulk move.
    """
    group = await resource_service.get_resource_group(session, actor, group_id)
    outcome = await resource_service.set_group_nodes(session, actor, group, node_ids, get_gateway())
    return NodeSyncResult(
        nodes_seen=len(node_ids),
        nodes_created=0,
        nodes_updated=0,
        devices_seen=0,
        labels_patched=outcome.labels_patched,
        nodes_unpooled=outcome.nodes_unpooled,
    )


# ---------------------------------------------------------------------------
# Resource specs
# ---------------------------------------------------------------------------
@router.get("/resource-specs", response_model=list[ResourceSpecRead])
async def list_resource_specs(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanReadSpec],
    gpu_only: Annotated[bool, Query(alias="gpuOnly")] = False,
    is_active: Annotated[bool | None, Query(alias="isActive")] = None,
    search: Annotated[str | None, Query(max_length=150)] = None,
) -> list[ResourceSpecRead]:
    specs = await resource_service.list_resource_specs(
        session, actor, gpu_only=gpu_only, is_active=is_active, search=search
    )
    return [resource_service.spec_to_read(spec) for spec in specs]


@router.post(
    "/resource-specs", response_model=ResourceSpecRead, status_code=status.HTTP_201_CREATED
)
async def create_resource_spec(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanCreateSpec],
    payload: ResourceSpecCreate,
) -> ResourceSpecRead:
    spec = await resource_service.create_resource_spec(session, actor, payload)
    return resource_service.spec_to_read(spec)


@router.patch("/resource-specs/{spec_id}", response_model=ResourceSpecRead)
async def update_resource_spec(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanUpdateSpec],
    spec_id: int,
    payload: ResourceSpecUpdate,
) -> ResourceSpecRead:
    spec = await _require_spec(session, spec_id)
    spec = await resource_service.update_resource_spec(session, actor, spec, payload)
    return resource_service.spec_to_read(spec)


@router.delete("/resource-specs/{spec_id}", response_model=ApiMessage)
async def delete_resource_spec(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanDeleteSpec],
    spec_id: int,
) -> ApiMessage:
    spec = await _require_spec(session, spec_id)
    await resource_service.delete_resource_spec(session, actor, spec)
    return ApiMessage(message=f"资源规格 {spec.name} 已删除")


async def _require_spec(session: DbSession, spec_id: int) -> ResourceSpec:
    spec = await session.get(ResourceSpec, spec_id)
    if spec is None:
        raise NotFoundError("资源规格不存在", code="resource_spec_not_found")
    return spec


# ---------------------------------------------------------------------------
# Runtime limits
# ---------------------------------------------------------------------------
@router.get("/runtime-limits", response_model=list[RuntimeLimitRead])
async def list_runtime_limits(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanReadLimit],
    is_active: Annotated[bool | None, Query(alias="isActive")] = None,
) -> list[RuntimeLimitRead]:
    limits = await resource_service.list_runtime_limits(session, actor, is_active=is_active)
    return [resource_service.limit_to_read(limit) for limit in limits]


@router.post(
    "/runtime-limits", response_model=RuntimeLimitRead, status_code=status.HTTP_201_CREATED
)
async def create_runtime_limit(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanCreateLimit],
    payload: RuntimeLimitCreate,
) -> RuntimeLimitRead:
    limit = await resource_service.create_runtime_limit(session, actor, payload)
    return resource_service.limit_to_read(limit)


@router.patch("/runtime-limits/{limit_id}", response_model=RuntimeLimitRead)
async def update_runtime_limit(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanManageLimit],
    limit_id: int,
    payload: RuntimeLimitUpdate,
) -> RuntimeLimitRead:
    limit = await _require_runtime_limit(session, limit_id)
    limit = await resource_service.update_runtime_limit(session, actor, limit, payload)
    return resource_service.limit_to_read(limit)


@router.delete("/runtime-limits/{limit_id}", response_model=ApiMessage)
async def delete_runtime_limit(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanManageLimit],
    limit_id: int,
) -> ApiMessage:
    limit = await _require_runtime_limit(session, limit_id)
    await resource_service.delete_runtime_limit(session, actor, limit)
    return ApiMessage(message=f"时长限制 {limit.name} 已删除")


async def _require_runtime_limit(session: DbSession, limit_id: int) -> RuntimeLimit:
    limit = await session.get(RuntimeLimit, limit_id)
    if limit is None:
        raise NotFoundError("时长限制不存在", code="runtime_limit_not_found")
    return limit


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
@router.get("/nodes", response_model=list[ComputeNodeRead])
async def list_nodes(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanReadNode],
) -> list[ComputeNodeRead]:
    nodes = await resource_service.list_nodes(session, actor)
    return [resource_service.node_to_read(node) for node in nodes]


@router.post("/nodes/sync", response_model=NodeSyncResult)
async def sync_nodes(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanSyncNode],
) -> NodeSyncResult:
    """Pull the node inventory from the cluster and reconcile it.

    Also re-derives each node's pool and rewrites the ``wl-pool`` label to match,
    so this is what keeps the cluster and the database telling the same story.
    """
    outcome = await resource_service.sync_nodes(session, actor, get_gateway())
    return NodeSyncResult(
        nodes_seen=outcome.nodes_seen,
        nodes_created=outcome.nodes_created,
        nodes_updated=outcome.nodes_updated,
        devices_seen=outcome.devices_seen,
        labels_patched=outcome.labels_patched,
        nodes_unpooled=outcome.nodes_unpooled,
    )


@router.get("/nodes/{node_id}/gpus", response_model=list[GpuDeviceRead])
async def list_node_gpus(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanReadNode],
    node_id: int,
) -> list[GpuDeviceRead]:
    node = await session.get(ComputeNode, node_id)
    if node is None:
        raise NotFoundError("计算节点不存在", code="node_not_found")
    return [
        GpuDeviceRead.model_validate(device, from_attributes=True) for device in node.gpu_devices
    ]


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------
@router.get("/quotas", response_model=list[QuotaRead])
async def list_quotas(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanReadQuota],
    scope_type: Annotated[str, Query(alias="scopeType")],
    scope_id: Annotated[int, Query(alias="scopeId")],
) -> list[QuotaRead]:
    """Quotas at one scope, with the usage measured for each.

    A caller may always read their own; anything else requires the scope to be
    one they administer, which :func:`_ensure_scope_visible` enforces.
    """
    resolved = QuotaScope(scope_type)
    await _ensure_scope_visible(session, actor, resolved, scope_id)
    quotas = await resource_service.list_quotas(session, scope_type=resolved, scope_id=scope_id)
    used = await quota_guard.scope_usage(session, resolved, scope_id)
    return [
        QuotaRead.model_validate(
            resource_service.quota_to_dict(quota, used_value=used.get(quota.resource, 0))
        )
        for quota in quotas
    ]


async def _ensure_scope_visible(
    session: DbSession, actor: CurrentUser, scope_type: QuotaScope, scope_id: int
) -> None:
    """Prove the caller may see quotas at this scope.

    Kept explicit rather than pushed into ``scope_filter`` because the table is
    polymorphic: ``scope_id`` means a different entity depending on
    ``scope_type``, so no single column rule could express it.
    """
    if scope_type == QuotaScope.USER:
        if actor.id != scope_id and not policy.is_global_admin(actor):
            raise NotFoundError()
        return

    if scope_type == QuotaScope.GROUP:
        policy.ensure_group_visible(actor, scope_id)
        return

    # Workspace: reuse the workspace object-level check.
    await get_workspace(session, actor, scope_id)
