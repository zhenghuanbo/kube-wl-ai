"""System information schemas.

See `app/services/system.py` for what is and is not reported, and why.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.base import ApiModel

WarningSeverity = Literal["critical", "warning", "info"]


class SystemFeature(ApiModel):
    """One integration, and whether it is actually usable."""

    key: str
    title: str
    enabled: bool
    detail: str
    #: The environment variable or values key that controls it. Present so the
    #: page can tell an administrator what to change without them reading source.
    config_key: str


class SystemWarning(ApiModel):
    """A concrete misconfiguration.

    Three fields beyond the code, because a warning that only names a setting
    makes the reader go and discover whether it matters: `detail` says what it
    causes, `remedy` says what to change.
    """

    code: str
    severity: WarningSeverity
    title: str
    detail: str
    remedy: str


class SystemCounts(ApiModel):
    users: int
    active_users: int
    groups: int
    workspaces: int
    nodes: int
    gpus: int
    instances: int
    running_instances: int


class SystemInfo(ApiModel):
    """Runtime state an administrator needs in order to diagnose a deployment.

    Every field is a boolean, a count, a duration, or a string an operator could
    read from the ConfigMap. Credentials are absent by construction — see the
    module docstring on the service.
    """

    app_name: str
    app_version: str
    environment: str
    debug: bool

    auth_providers: list[str] = Field(default_factory=list)
    features: list[SystemFeature] = Field(default_factory=list)
    warnings: list[SystemWarning] = Field(default_factory=list)

    access_token_minutes: int
    refresh_token_days: int

    login_rate_limit_enabled: bool
    login_rate_limit_window_seconds: int
    login_rate_limit_attempts_per_ip: int
    login_rate_limit_attempts_per_username: int

    counts: SystemCounts

    ssh_mode: str
    ssh_host: str | None = None

    #: Role → storage class name, with `null` for "not configured". A `null`
    #: here is the cause of a runtime failure the UI would otherwise report as a
    #: generic storage error, so it is worth surfacing on its own.
    storage_classes: dict[str, str | None] = Field(default_factory=dict)
