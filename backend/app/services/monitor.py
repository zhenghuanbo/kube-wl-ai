"""Monitoring: what the caller may watch, and what is actually running.

This module answers two questions and keeps them apart.

**What may they watch?** `resolve_scope` turns the caller's data scope into the
Kubernetes namespaces and pod names their dashboards will be filtered by. It is
derived from `Instance` rows through the *same* `policy.scope_filter` the REST API
uses, so a monitoring panel and a container list can never disagree about who sees
what.

**What is running?** `inventory` summarises the platform's own view — nodes,
accelerators, the shape of the instance population — without asking Prometheus or
the API server. That is deliberate: the inventory is what the *platform*
scheduled, it is available when the cluster is unreachable, and it needs no new
labels on anybody's metrics.

Whether the caller's scope is "unrestricted" matters more than it looks. An
administrator with no filter and a user with no running containers both produce
zero namespaces, and rendering them the same way would show an administrator an
empty dashboard. `MonitorScope.unrestricted` keeps them distinct all the way
through to the Grafana variable list.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import policy
from app.models import ComputeNode, GpuDevice, Instance, NodeStatus, User, Workspace
from app.models.enums import GpuDeviceStatus, InstanceStatus
from app.monitoring.grafana import MonitorScope
from app.schemas.monitor import (
    GpuModelSummary,
    InstanceStatusCount,
    MonitorInventory,
    MonitorNodeSummary,
    MonitorScopeRead,
    WatchedContainer,
)

#: States in which the platform still intends a pod to exist.
#:
#: `stopped` and `failed` are the terminal pair and are excluded: a stopped
#: instance keeps its row (and its name) but has nothing to scrape, so offering it
#: in a pod picker produces a panel that is eternally blank. Everything else is
#: included even when the pod may already be gone — an `error` instance is
#: precisely the one somebody wants to look at.
LIVE_INSTANCE_STATUSES: tuple[InstanceStatus, ...] = (
    InstanceStatus.PENDING,
    InstanceStatus.CREATING,
    InstanceStatus.RUNNING,
    InstanceStatus.STOPPING,
    InstanceStatus.DELETING,
    InstanceStatus.ERROR,
)


@dataclass(frozen=True, slots=True)
class WatchedInstance:
    id: int
    name: str
    display_name: str
    namespace: str
    pod_name: str | None
    owner_username: str
    status: InstanceStatus
    gpu_type: str
    gpu_count: int
    node_name: str | None


def _live_query() -> Select[Any]:
    """Live instances joined to the two names the UI and Grafana need."""
    return (
        select(Instance, Workspace.k8s_namespace, User.username)
        .join(Workspace, Workspace.id == Instance.workspace_id)
        .join(User, User.id == Instance.owner_user_id)
        .where(Instance.status.in_(LIVE_INSTANCE_STATUSES))
    )


async def watched_instances(
    session: AsyncSession, actor: User, *, limit: int = 500
) -> list[WatchedInstance]:
    """The caller's live containers, newest first. Scope-filtered."""
    stmt = _live_query().order_by(Instance.id.desc()).limit(limit)
    stmt = policy.scope_filter(stmt, Instance, actor)

    return [
        WatchedInstance(
            id=instance.id,
            name=instance.name,
            display_name=instance.display_name,
            namespace=str(namespace),
            pod_name=instance.pod_name,
            owner_username=str(username),
            status=instance.status,
            gpu_type=instance.gpu_type,
            gpu_count=instance.gpu_count,
            node_name=instance.node_name,
        )
        for instance, namespace, username in (await session.execute(stmt)).all()
    ]


async def resolve_scope(session: AsyncSession, actor: User) -> MonitorScope:
    """
    The namespaces and pods the caller's dashboards are locked to.

    An unrestricted caller gets empty tuples *and* the flag, never a wildcard:
    the dashboard is left to fall back to its own "All" default. Passing a
    literal `$__all` here would be worse than useless — Grafana accepts it only
    for a multi-value variable, and for a single-value one it silently selects
    nothing, so the administrator would see an empty panel and no error.
    """
    if policy.resolve_scope(actor).all_access:
        return MonitorScope(namespaces=(), pods=(), unrestricted=True)

    rows = await watched_instances(session, actor)
    return MonitorScope(
        namespaces=tuple(sorted({row.namespace for row in rows})),
        pods=tuple(sorted({row.pod_name for row in rows if row.pod_name})),
        unrestricted=False,
    )


async def scope_read(session: AsyncSession, actor: User) -> MonitorScopeRead:
    """The scope as the page shows it, so a user can see what they are filtered to."""
    scope = await resolve_scope(session, actor)
    return MonitorScopeRead(
        namespaces=list(scope.namespaces),
        pods=list(scope.pods),
        unrestricted=scope.unrestricted,
        empty=scope.is_empty,
    )


async def inventory(session: AsyncSession) -> MonitorInventory:
    """
    The platform's own view of the cluster.

    Not scope-filtered, and served only to callers holding a monitoring
    permission: node names, accelerator counts and the shape of the instance
    population are infrastructure facts, not tenant data. The per-tenant detail
    lives in `watched_instances`, which *is* filtered.
    """
    nodes = list((await session.scalars(select(ComputeNode).order_by(ComputeNode.name))).all())

    gpu_rows = (
        await session.execute(
            select(GpuDevice.node_id, GpuDevice.model, GpuDevice.status, func.count())
            .group_by(GpuDevice.node_id, GpuDevice.model, GpuDevice.status)
            .order_by(GpuDevice.model)
        )
    ).all()

    by_model: dict[str, GpuModelSummary] = {}
    gpus_per_node: dict[int, int] = {}
    for node_id, model, status, count in gpu_rows:
        entry = by_model.setdefault(str(model), GpuModelSummary(model=str(model)))
        entry.total += int(count)
        if status == GpuDeviceStatus.ALLOCATED:
            entry.allocated += int(count)
        gpus_per_node[int(node_id)] = gpus_per_node.get(int(node_id), 0) + int(count)

    status_rows = (
        await session.execute(
            select(Instance.status, func.count())
            .group_by(Instance.status)
            .order_by(Instance.status)
        )
    ).all()

    return MonitorInventory(
        nodes=[
            MonitorNodeSummary(
                id=node.id,
                name=node.name,
                display_name=node.display_name or node.name,
                status=node.status.value,
                schedulable=node.schedulable,
                gpu_count=gpus_per_node.get(node.id, 0),
                cpu_total_milli=node.cpu_total_milli,
                memory_total_mb=node.memory_total_mb,
            )
            for node in nodes
        ],
        nodes_ready=sum(1 for node in nodes if node.status == NodeStatus.READY),
        gpu_by_model=sorted(by_model.values(), key=lambda item: item.model),
        instances=[
            InstanceStatusCount(status=str(status), count=int(count))
            for status, count in status_rows
        ],
    )


def to_watched_read(rows: list[WatchedInstance]) -> list[WatchedContainer]:
    return [
        WatchedContainer(
            id=row.id,
            name=row.name,
            display_name=row.display_name,
            namespace=row.namespace,
            pod_name=row.pod_name,
            owner_username=row.owner_username,
            status=row.status.value,
            gpu_type=row.gpu_type,
            gpu_count=row.gpu_count,
            node_name=row.node_name,
        )
        for row in rows
    ]
