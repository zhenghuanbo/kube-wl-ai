"""Monitoring integration.

Currently just Grafana. Kept as a package rather than a module because the
Prometheus-backed measurement of *consumption* is a separate concern that will
arrive beside it (see `app/services/usage.py` for why it is not a replacement).
"""

from app.monitoring.grafana import (
    DASHBOARDS,
    DASHBOARDS_BY_KEY,
    MANAGED_TAG,
    DashboardSpec,
    MonitorScope,
    build_embed_url,
    build_grafana_token,
    dashboard_variables,
    dashboards_for,
)

__all__ = [
    "DASHBOARDS",
    "DASHBOARDS_BY_KEY",
    "MANAGED_TAG",
    "DashboardSpec",
    "MonitorScope",
    "build_embed_url",
    "build_grafana_token",
    "dashboard_variables",
    "dashboards_for",
]
