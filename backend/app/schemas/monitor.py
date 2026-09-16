"""Monitoring wire types (`app/api/v1/monitor.py`).

Everything here is a *view* over data the platform already holds, or a minted
pointer into Grafana. Nothing in this module returns a Prometheus sample: the
platform's records are the source of truth for what was consumed, and Grafana
answers the different question of what a container is doing right now.
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.base import ApiModel


class MonitorScopeRead(ApiModel):
    """
    What the caller's monitoring is currently limited to.

    Returned to the page so the filter is visible rather than implied. A user
    looking at a flat line deserves to know whether it is flat because nothing is
    happening or because they are filtered to a namespace with nothing in it.
    """

    namespaces: list[str] = Field(default_factory=list)
    pods: list[str] = Field(default_factory=list)
    #: No filter at all — an administrator. Distinct from `empty`.
    unrestricted: bool = False
    #: Filtered, and the filter matches nothing. Says "you have no live
    #: containers", which is a different sentence from "you may see everything".
    empty: bool = False


class MonitorNodeSummary(ApiModel):
    id: int
    name: str
    display_name: str
    status: str
    schedulable: bool
    gpu_count: int
    cpu_total_milli: int
    memory_total_mb: int


class GpuModelSummary(ApiModel):
    model: str
    total: int = 0
    allocated: int = 0


class InstanceStatusCount(ApiModel):
    status: str
    count: int


class MonitorInventory(ApiModel):
    """The platform's own view of the cluster. Not tenant-scoped."""

    nodes: list[MonitorNodeSummary] = Field(default_factory=list)
    nodes_ready: int = 0
    gpu_by_model: list[GpuModelSummary] = Field(default_factory=list)
    instances: list[InstanceStatusCount] = Field(default_factory=list)


class WatchedContainer(ApiModel):
    """A live instance the caller may watch, with the names Grafana filters on."""

    id: int
    name: str
    display_name: str
    #: The Kubernetes namespace, which is the workspace's — not the workspace id.
    namespace: str
    pod_name: str | None = None
    owner_username: str
    status: str
    gpu_type: str
    gpu_count: int
    node_name: str | None = None


class MonitorDashboardRead(ApiModel):
    """One dashboard the caller may open."""

    key: str
    uid: str
    title: str
    description: str
    icon: str


class MonitorEmbedRead(ApiModel):
    """
    A freshly minted, scope-locked Grafana URL.

    Short-lived by construction: the URL carries the scope, so it is re-minted
    every time the page needs one rather than handed out once and cached.
    """

    key: str
    url: str
    #: Seconds until `url` stops working. Surfaced so the UI can refresh before
    #: the iframe starts showing Grafana's own login page.
    expires_in: int


class MonitorOverview(ApiModel):
    """
    Everything the monitoring page needs on first paint.

    One response rather than four round-trips, because four requests that can
    disagree with each other produce a page that describes two different moments.
    """

    enabled: bool
    #: Why the Grafana panels are unavailable, when they are. Empty otherwise.
    disabled_reason: str | None = None
    scope: MonitorScopeRead
    inventory: MonitorInventory
    containers: list[WatchedContainer] = Field(default_factory=list)
    dashboards: list[MonitorDashboardRead] = Field(default_factory=list)
    #: Panels already scoped and signed, keyed by dashboard key. Only for the
    #: dashboards the caller may open; the rest are absent, not empty.
    embeds: dict[str, MonitorEmbedRead] = Field(default_factory=dict)
