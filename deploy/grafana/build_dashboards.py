#!/usr/bin/env python3
"""Generate the provisioned Grafana dashboards into this directory.

The four JSON files next to this script are the deliverable; this generator
exists because they share a great deal of structure and hand-maintaining four
near-identical JSON documents is how a dashboard quietly stops matching its
siblings — different variable names, a panel filtering on the wrong label, one
that forgot to filter at all.

The one rule that matters, and the reason the panels are generated rather than
written by hand: **every panel is filtered by the namespace (and, where relevant,
pod) variables, and those variables are the ones the backend's signed URL
pre-fills.** A panel that omits `namespace=~"$namespace"` shows the whole
institution to a user who was handed a URL for one lab. Grafana would render it
happily and nobody would see anything wrong.

Regenerate with:

    python3 deploy/grafana/build_dashboards.py

Nothing reads this at runtime. Provisioning loads the generated JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT = HERE / "dashboards"

#: Which provisioning folder each dashboard is written to. The split mirrors
#: `dashboards.yaml`, and is a convenience rather than a control: a Grafana
#: viewer can browse every folder. What actually restricts a dashboard is the
#: scope in the signed URL plus the filters `verify()` enforces.
FOLDERS = {
    "wl-my-containers": "user",
    "wl-lab-overview": "user",
    "wl-gpu-fleet": "admin",
    "wl-node-health": "admin",
}

#: Fixed uid so the dashboards can reference the provisioned datasource without
#: depending on what somebody named it in the Grafana UI. The matching
#: `datasources.yaml` defines it.
PROM_UID = "wl-prometheus"

#: How often a panel refreshes. Sixty seconds is the floor imposed by the default
#: `scrape_interval`; going faster only redraws the same sample.
REFRESH = "1m"


def ds() -> dict[str, str]:
    return {"type": "prometheus", "uid": PROM_UID}


def var_namespace(*, all_value: str = "$__all") -> dict[str, Any]:
    """
    The variable the backend locks for a normal user.

    `multi` + `includeAll` are both required. The backend sends a comma-separated
    list for a user with several namespaces, which only parses as a selection if
    the variable is multi-value — and an administrator receives *no* parameter at
    all, which only means "everything" if the variable includes an All option.
    """
    return {
        "name": "namespace",
        "label": "命名空间",
        "type": "query",
        "datasource": ds(),
        "query": {"query": "label_values(kube_namespace_labels, namespace)", "refId": "ns"},
        "refresh": 2,
        "multi": True,
        "includeAll": True,
        "allValue": all_value,
        "current": {"selected": True, "text": ["All"], "value": [all_value]},
        "sort": 1,
    }


def var_pod() -> dict[str, Any]:
    """Container-level variable, narrowed to the selected namespaces."""
    return {
        "name": "pod",
        "label": "容器",
        "type": "query",
        "datasource": ds(),
        "query": {
            "query": 'label_values(kube_pod_info{namespace=~"$namespace"}, pod)',
            "refId": "pod",
        },
        "refresh": 2,
        "multi": True,
        "includeAll": True,
        "allValue": "$__all",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]},
        "sort": 1,
    }


def target(expr: str, legend: str) -> dict[str, Any]:
    return {
        "datasource": ds(),
        "expr": expr,
        "legendFormat": legend,
        "refId": "A",
    }


def timeseries(
    *,
    panel_id: int,
    title: str,
    description: str,
    expr: str,
    legend: str,
    unit: str,
    x: int,
    y: int,
    w: int = 12,
    h: int = 8,
    stack: bool = False,
) -> dict[str, Any]:
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {"fillOpacity": 12, "lineWidth": 1, "stacking": {"mode": "normal"} if stack else {"mode": "none"}},
            },
            "overrides": [],
        },
        "targets": [target(expr, legend)],
    }


def stat(
    *,
    panel_id: int,
    title: str,
    description: str,
    expr: str,
    unit: str,
    x: int,
    y: int,
    w: int = 6,
    h: int = 4,
    thresholds: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": panel_id,
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "blue", "value": None}]},
            },
            "overrides": [],
        },
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
        "targets": [target(expr, "")],
    }


def table(
    *,
    panel_id: int,
    title: str,
    description: str,
    expr: str,
    x: int,
    y: int,
    w: int = 12,
    h: int = 8,
) -> dict[str, Any]:
    return {
        "id": panel_id,
        "type": "table",
        "title": title,
        "description": description,
        "datasource": ds(),
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": ds(), "expr": expr, "format": "table", "instant": True, "refId": "A"}],
    }


def dashboard(
    *,
    uid: str,
    title: str,
    description: str,
    tags: list[str],
    variables: list[dict[str, Any]],
    panels: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": tags,
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "refresh": REFRESH,
        "editable": False,
        "time": {"from": "now-24h", "to": "now"},
        "templating": {"list": variables},
        "panels": panels,
        "annotations": {"list": []},
    }


# ---------------------------------------------------------------------------
# The four dashboards
# ---------------------------------------------------------------------------
def lab_overview() -> dict[str, Any]:
    """One lab. Locked to the namespaces the caller's group owns."""
    ns = 'namespace=~"$namespace"'
    return dashboard(
        uid="wl-lab-overview",
        title="实验室总览",
        description="本组命名空间的资源使用与容器健康度。由 kube-wl-ai 提供。",
        tags=["kube-wl-ai", "lab"],
        variables=[var_namespace()],
        panels=[
            stat(
                panel_id=1,
                title="运行中的容器",
                description="当前处于 Running 阶段的容器数。",
                expr=f'count(kube_pod_status_phase{{{ns}, phase="Running"}} == 1)',
                unit="short",
                x=0,
                y=0,
            ),
            stat(
                panel_id=2,
                title="异常容器",
                description="NotReady 或 CrashLoopBackOff 的容器数。非零即需要关注。",
                expr=f'count(kube_pod_container_status_waiting_reason{{{ns}, reason=~"CrashLoopBackOff|ImagePullBackOff"}} == 1)',
                unit="short",
                x=6,
                y=0,
                thresholds=[
                    {"color": "green", "value": None},
                    {"color": "red", "value": 1},
                ],
            ),
            stat(
                panel_id=3,
                title="本组 GPU 申请数",
                description="各命名空间中容器声明的加速卡数量之和。",
                expr=f'sum(kube_pod_container_resource_requests{{{ns}, resource="nvidia_com_gpu"}})',
                unit="short",
                x=12,
                y=0,
            ),
            stat(
                panel_id=4,
                title="本组内存申请",
                description="请求内存之和，用于对照资源组上限。",
                expr=f'sum(kube_pod_container_resource_requests{{{ns}, resource="memory"}})',
                unit="bytes",
                x=18,
                y=0,
            ),
            timeseries(
                panel_id=5,
                title="CPU 使用",
                description="按容器聚合的 CPU 使用率。",
                expr=f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{ns}, container!="", container!="POD"}}[5m]))',
                legend="{{pod}}",
                unit="none",
                x=0,
                y=4,
                stack=True,
            ),
            timeseries(
                panel_id=6,
                title="内存使用",
                description="工作集大小，按容器聚合。",
                expr=f'sum by (pod) (container_memory_working_set_bytes{{{ns}, container!="", container!="POD"}})',
                legend="{{pod}}",
                unit="bytes",
                x=12,
                y=4,
                stack=True,
            ),
            table(
                panel_id=7,
                title="容器重启次数",
                description="重启次数是长跑任务最容易被忽视的失败信号。",
                expr=f'sort_desc(sum by (namespace, pod) (kube_pod_container_status_restarts_total{{{ns}}}))',
                x=0,
                y=12,
                w=24,
            ),
        ],
    )


def my_containers() -> dict[str, Any]:
    """The user's own containers. Locked to both namespace and pod."""
    ns = 'namespace=~"$namespace", pod=~"$pod"'
    return dashboard(
        uid="wl-my-containers",
        title="我的容器",
        description="本人容器的资源曲线与 GPU 占用。由 kube-wl-ai 提供。",
        tags=["kube-wl-ai", "user"],
        variables=[var_namespace(), var_pod()],
        panels=[
            stat(
                panel_id=1,
                title="CPU 使用",
                description="当前 CPU 使用率，按容器汇总。",
                expr=f"sum(rate(container_cpu_usage_seconds_total{{{ns}, container!=\"\", container!=\"POD\"}}[5m]))",
                unit="none",
                x=0,
                y=0,
            ),
            stat(
                panel_id=2,
                title="内存使用",
                description="当前工作集大小。",
                expr=f'sum(container_memory_working_set_bytes{{{ns}, container!="", container!="POD"}})',
                unit="bytes",
                x=6,
                y=0,
            ),
            stat(
                panel_id=3,
                title="GPU 利用率",
                description="DCGM 上报的 SM 利用率；无 GPU 的容器此面板为空。",
                expr=f' avg(DCGM_FI_DEV_GPU_UTIL{{{ns}}})',
                unit="percent",
                x=12,
                y=0,
            ),
            stat(
                panel_id=4,
                title="重启次数",
                description="累计重启次数。持续增长说明容器在崩溃重启。",
                expr=f"sum(kube_pod_container_status_restarts_total{{{ns}}})",
                unit="short",
                x=18,
                y=0,
                thresholds=[
                    {"color": "green", "value": None},
                    {"color": "orange", "value": 1},
                    {"color": "red", "value": 5},
                ],
            ),
            timeseries(
                panel_id=5,
                title="CPU 使用曲线",
                description="按容器的 CPU 使用率。",
                expr=f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{ns}, container!="", container!="POD"}}[5m]))',
                legend="{{pod}}",
                unit="none",
                x=0,
                y=4,
            ),
            timeseries(
                panel_id=6,
                title="内存使用曲线",
                description="按容器的工作集大小。",
                expr=f'sum by (pod) (container_memory_working_set_bytes{{{ns}, container!="", container!="POD"}})',
                legend="{{pod}}",
                unit="bytes",
                x=12,
                y=4,
            ),
            timeseries(
                panel_id=7,
                title="GPU 利用率曲线",
                description="按容器聚合的 SM 利用率。",
                expr=f'sum by (pod) (DCGM_FI_DEV_GPU_UTIL{{{ns}}})',
                legend="{{pod}}",
                unit="percent",
                x=0,
                y=12,
                w=24,
            ),
        ],
    )


def gpu_fleet() -> dict[str, Any]:
    """Institution-wide accelerator view. No namespace filter: admin only."""
    return dashboard(
        uid="wl-gpu-fleet",
        title="GPU 集群",
        description="全体加速卡的在位、分配与利用率。仅全局管理员可见。",
        tags=["kube-wl-ai", "admin"],
        variables=[],
        panels=[
            stat(
                panel_id=1,
                title="在位 GPU",
                description="DCGM 上报的 GPU 数量。",
                expr="count(DCGM_FI_DEV_GPU_UTIL)",
                unit="short",
                x=0,
                y=0,
            ),
            stat(
                panel_id=2,
                title="已分配 GPU",
                description="被容器申请的加速卡数量。",
                expr='sum(kube_pod_container_resource_requests{resource="nvidia_com_gpu"})',
                unit="short",
                x=6,
                y=0,
            ),
            stat(
                panel_id=3,
                title="平均利用率",
                description="全体 GPU 的 SM 利用率均值。",
                expr="avg(DCGM_FI_DEV_GPU_UTIL)",
                unit="percent",
                x=12,
                y=0,
            ),
            stat(
                panel_id=4,
                title="显存占用",
                description="已用显存占全部显存的比例。",
                expr="sum(DCGM_FI_DEV_FB_USED) / sum(DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE) * 100",
                unit="percent",
                x=18,
                y=0,
            ),
            timeseries(
                panel_id=5,
                title="利用率（按 GPU）",
                description="每张卡的 SM 利用率。",
                expr="DCGM_FI_DEV_GPU_UTIL",
                legend="{{Hostname}} GPU{{gpu}}",
                unit="percent",
                x=0,
                y=4,
                w=24,
            ),
            table(
                panel_id=6,
                title="按型号统计",
                description="各型号的在位与已分配数量。",
                expr='count by (modelName) (DCGM_FI_DEV_GPU_UTIL)',
                x=0,
                y=12,
                w=24,
            ),
        ],
    )


def node_health() -> dict[str, Any]:
    """Node-level view. Also admin only."""
    return dashboard(
        uid="wl-node-health",
        title="节点健康",
        description="节点状态、负载与显存压力。仅全局管理员可见。",
        tags=["kube-wl-ai", "admin"],
        variables=[],
        panels=[
            stat(
                panel_id=1,
                title="就绪节点",
                description="Ready 状态的节点数。",
                expr='count(kube_node_status_condition{condition="Ready", status="true"} == 1)',
                unit="short",
                x=0,
                y=0,
            ),
            stat(
                panel_id=2,
                title="不可调度节点",
                description="被封锁或 NotReady 的节点数。非零说明有人在排障。",
                expr='count(kube_node_spec_unschedulable == 1)',
                unit="short",
                x=6,
                y=0,
                thresholds=[
                    {"color": "green", "value": None},
                    {"color": "orange", "value": 1},
                ],
            ),
            timeseries(
                panel_id=3,
                title="节点 CPU 使用率",
                description="按节点的 CPU 使用率。",
                expr='100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)',
                legend="{{instance}}",
                unit="percent",
                x=0,
                y=4,
            ),
            timeseries(
                panel_id=4,
                title="节点内存使用率",
                description="按节点的内存使用率。",
                expr="(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100",
                legend="{{instance}}",
                unit="percent",
                x=12,
                y=4,
            ),
            timeseries(
                panel_id=5,
                title="GPU 显存压力",
                description="按 GPU 的显存占用比例。持续接近 100% 会开始 OOM。",
                expr="DCGM_FI_DEV_FB_USED / (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE) * 100",
                legend="{{Hostname}} GPU{{gpu}}",
                unit="percent",
                x=0,
                y=12,
                w=24,
            ),
        ],
    )


DASHBOARDS = (lab_overview, my_containers, gpu_fleet, node_health)


class ScopeViolation(Exception):
    """A panel that would render rows its viewer is not entitled to see."""


def targets_of(panel: dict[str, Any]) -> list[str]:
    return [str(t.get("expr", "")) for t in panel.get("targets", [])]


def verify(document: dict[str, Any]) -> None:
    """
    Refuse to emit a dashboard whose panels are not filtered by their variables.

    This is the check that makes the whole handoff safe, and it cannot be a
    runtime one: Grafana renders an unfiltered panel perfectly happily, so a
    missing `namespace=~"$namespace"` is invisible until somebody notices another
    lab's containers on their screen.

    The `pod` rule deserves its own sentence. A workspace *is* a Kubernetes
    namespace, shared by the whole group — so filtering "my containers" by
    namespace alone would show a user every colleague's containers in the same
    lab. The pod variable is what makes the dashboard the user's own, and a panel
    that drops it is a cross-tenant leak wearing the label "mine".
    """
    declared = {v["name"]: v for v in document["templating"]["list"]}
    if "namespace" not in declared:
        return  # an admin dashboard; the backend sends no filter and none is wanted

    # The backend sends the selection as a single comma-joined value
    # (`var-namespace=a,b`), which Grafana only splits when the variable is
    # multi-value. A user with two namespaces would otherwise get a filter that
    # matches the literal string "a,b" — nothing — and a silently empty panel.
    problems: list[str] = []
    for name in ("namespace", "pod"):
        if name in declared and not declared[name].get("multi"):
            problems.append(f"variable {name} is not multi-value")
    if problems:
        raise ScopeViolation(f"{document['uid']}: " + "; ".join(problems))

    for panel in document["panels"]:
        exprs = targets_of(panel)
        if not exprs:
            problems.append(f"panel {panel['id']} ({panel['title']}): no targets")
            continue
        for expr in exprs:
            if 'namespace=~"$namespace"' not in expr:
                problems.append(
                    f"panel {panel['id']} ({panel['title']}): missing namespace filter"
                )
            if "pod" in declared and 'pod=~"$pod"' not in expr:
                problems.append(f"panel {panel['id']} ({panel['title']}): missing pod filter")
    if problems:
        raise ScopeViolation(
            f"{document['uid']} would leak across tenants:\n  " + "\n  ".join(problems)
        )


def main() -> int:
    documents = [build() for build in DASHBOARDS]

    # Verified before anything is written, so a violation cannot leave a
    # half-updated dashboards directory behind.
    for document in documents:
        verify(document)

    for document in documents:
        folder = OUT / FOLDERS[document["uid"]]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{document['uid']}.json"
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        panels = len(document["panels"])
        print(f"wrote {path.relative_to(HERE.parent.parent)} ({panels} panels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
