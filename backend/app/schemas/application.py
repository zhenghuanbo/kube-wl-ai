"""Application request and response payloads."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.models.enums import ApplicationCategory, ApplicationVisibility, IdeType
from app.schemas.base import ApiModel

MAX_APP_NAME = 64


class ApplicationOwnerRef(ApiModel):
    id: int
    username: str
    display_name: str


class ApplicationSummary(ApiModel):
    """One catalogue card."""

    id: int
    name: str
    display_name: str
    summary: str = ""
    icon: str
    category: ApplicationCategory
    workspace_id: int
    workspace_name: str | None = None
    group_id: int
    group_name: str | None = None
    owner_user_id: int
    owner_username: str | None = None
    owner_display_name: str | None = None
    image: str
    ide_type: IdeType
    resource_group_id: int | None = None
    resource_group_name: str | None = None
    default_cpu_milli: int
    default_memory_mb: int
    default_gpu_type: str
    default_gpu_count: int
    default_storage_gb: int
    visibility: ApplicationVisibility
    is_curated: bool
    is_active: bool
    launch_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_application(cls, app: object) -> ApplicationSummary:
        workspace = getattr(app, "workspace", None)
        group = getattr(app, "group", None)
        owner = getattr(app, "owner", None)
        pool = getattr(app, "resource_group", None)
        return cls(
            id=app.id,  # type: ignore[attr-defined]
            name=app.name,  # type: ignore[attr-defined]
            display_name=app.display_name or app.name,  # type: ignore[attr-defined]
            summary=app.summary,  # type: ignore[attr-defined]
            icon=app.icon,  # type: ignore[attr-defined]
            category=app.category,  # type: ignore[attr-defined]
            workspace_id=app.workspace_id,  # type: ignore[attr-defined]
            workspace_name=workspace.k8s_namespace if workspace else None,
            group_id=app.group_id,  # type: ignore[attr-defined]
            group_name=group.name if group else None,
            owner_user_id=app.owner_user_id,  # type: ignore[attr-defined]
            owner_username=owner.username if owner else None,
            owner_display_name=owner.display_name if owner else None,
            image=app.image,  # type: ignore[attr-defined]
            ide_type=app.ide_type,  # type: ignore[attr-defined]
            resource_group_id=app.resource_group_id,  # type: ignore[attr-defined]
            resource_group_name=pool.name if pool else None,
            default_cpu_milli=app.default_cpu_milli,  # type: ignore[attr-defined]
            default_memory_mb=app.default_memory_mb,  # type: ignore[attr-defined]
            default_gpu_type=app.default_gpu_type,  # type: ignore[attr-defined]
            default_gpu_count=app.default_gpu_count,  # type: ignore[attr-defined]
            default_storage_gb=app.default_storage_gb,  # type: ignore[attr-defined]
            visibility=app.visibility,  # type: ignore[attr-defined]
            is_curated=app.is_curated,  # type: ignore[attr-defined]
            is_active=app.is_active,  # type: ignore[attr-defined]
            launch_count=app.launch_count,  # type: ignore[attr-defined]
            created_at=app.created_at,  # type: ignore[attr-defined]
            updated_at=app.updated_at,  # type: ignore[attr-defined]
        )


class ApplicationDetail(ApplicationSummary):
    """The catalogue card plus the introduction and the launch defaults."""

    readme: str | None = None
    resource_spec_id: int | None = None
    env: dict[str, str] = Field(default_factory=dict)
    ports: list[dict[str, object]] = Field(default_factory=list)

    @classmethod
    def from_application(cls, app: object) -> ApplicationDetail:
        summary = ApplicationSummary.from_application(app)
        return cls(
            **summary.model_dump(),
            readme=app.readme,  # type: ignore[attr-defined]
            resource_spec_id=app.resource_spec_id,  # type: ignore[attr-defined]
            env=dict(app.env or {}),  # type: ignore[attr-defined]
            ports=list(app.ports or []),  # type: ignore[attr-defined]
        )


class ApplicationCreate(ApiModel):
    name: str = Field(min_length=1, max_length=MAX_APP_NAME, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(default="", max_length=128)
    summary: str = Field(default="", max_length=255)
    readme: str | None = None
    icon: str = Field(default="mdi-application", max_length=64)
    category: ApplicationCategory = ApplicationCategory.OTHER
    workspace_id: int | None = None
    image: str = Field(min_length=1, max_length=512)
    ide_type: IdeType = IdeType.NONE
    resource_group_id: int | None = None
    resource_spec_id: int | None = None
    default_cpu_milli: int = Field(default=2000, ge=100, le=512000)
    default_memory_mb: int = Field(default=4096, ge=128, le=4194304)
    default_gpu_type: str = Field(default="none", max_length=32)
    default_gpu_count: int = Field(default=0, ge=0, le=64)
    default_storage_gb: int = Field(default=10, ge=0, le=2048)
    env: dict[str, str] = Field(default_factory=dict)
    ports: list[dict[str, object]] = Field(default_factory=list)
    visibility: ApplicationVisibility = ApplicationVisibility.WORKSPACE


class ApplicationUpdate(ApiModel):
    display_name: str | None = Field(default=None, max_length=128)
    summary: str | None = Field(default=None, max_length=255)
    readme: str | None = None
    icon: str | None = Field(default=None, max_length=64)
    category: ApplicationCategory | None = None
    image: str | None = Field(default=None, max_length=512)
    ide_type: IdeType | None = None
    resource_group_id: int | None = None
    resource_spec_id: int | None = None
    default_cpu_milli: int | None = Field(default=None, ge=100, le=512000)
    default_memory_mb: int | None = Field(default=None, ge=128, le=4194304)
    default_gpu_type: str | None = Field(default=None, max_length=32)
    default_gpu_count: int | None = Field(default=None, ge=0, le=64)
    default_storage_gb: int | None = Field(default=None, ge=0, le=2048)
    env: dict[str, str] | None = None
    ports: list[dict[str, object]] | None = None
    visibility: ApplicationVisibility | None = None
    is_active: bool | None = None


class ApplicationLaunch(ApiModel):
    """Launch an application as an instance in the caller's workspace.

    Every field is optional and falls back to the application's default, so the
    common case is a bare ``{}``.
    """

    name: str | None = Field(default=None, max_length=56)
    workspace_id: int | None = None
    owner_user_id: int | None = None
    resource_group_id: int | None = None
    resource_spec_id: int | None = None
    storage_gb: int | None = Field(default=None, ge=0, le=2048)
    max_runtime_seconds: int | None = Field(default=None, ge=60)
    ssh_enabled: bool = True


class WorkloadEventRead(ApiModel):
    """One entry of the workload timeline."""

    id: int
    event_type: str
    message: str | None = None
    from_status: str | None = None
    to_status: str | None = None
    actor_username: str | None = None
    created_at: datetime

    @classmethod
    def from_event(cls, event: object) -> WorkloadEventRead:
        return cls(
            id=event.id,  # type: ignore[attr-defined]
            event_type=event.event_type.value,  # type: ignore[attr-defined]
            message=event.message,  # type: ignore[attr-defined]
            from_status=event.from_status,  # type: ignore[attr-defined]
            to_status=event.to_status,  # type: ignore[attr-defined]
            actor_username=event.actor_username,  # type: ignore[attr-defined]
            created_at=event.created_at,  # type: ignore[attr-defined]
        )
