"""Task request and response payloads."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.k8s.naming import MAX_LABEL_LENGTH
from app.models.enums import TaskStatus
from app.schemas.base import ApiModel

#: A task name ends up inside a Job name, which carries the ``wl-job-`` prefix
#: and a numeric suffix. Reserving that margin here means the failure is a clear
#: 422 rather than a silently truncated name.
MAX_TASK_NAME = MAX_LABEL_LENGTH - 12


class TaskWorkspaceRef(ApiModel):
    id: int
    name: str
    k8s_namespace: str


class TaskOwnerRef(ApiModel):
    id: int
    username: str
    display_name: str


class TaskSummary(ApiModel):
    """One row of the task list."""

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
    cpu_milli: int
    memory_mb: int
    gpu_type: str
    gpu_count: int
    status: TaskStatus
    status_message: str | None = None
    priority: int
    max_runtime_seconds: int | None = None
    node_name: str | None = None
    exit_code: int | None = None
    #: Wall clock, computed rather than stored: a stored copy would need a
    #: writer on every path and would be wrong whenever one forgot.
    duration_seconds: float | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_task(cls, task: object) -> TaskSummary:
        workspace = getattr(task, "workspace", None)
        owner = getattr(task, "owner", None)
        pool = getattr(task, "resource_group", None)
        return cls(
            id=task.id,  # type: ignore[attr-defined]
            name=task.name,  # type: ignore[attr-defined]
            display_name=task.display_name or task.name,  # type: ignore[attr-defined]
            description=task.description,  # type: ignore[attr-defined]
            workspace_id=task.workspace_id,  # type: ignore[attr-defined]
            workspace_name=workspace.k8s_namespace if workspace else None,
            owner_user_id=task.owner_user_id,  # type: ignore[attr-defined]
            owner_username=owner.username if owner else None,
            owner_display_name=owner.display_name if owner else None,
            resource_group_id=task.resource_group_id,  # type: ignore[attr-defined]
            resource_group_name=pool.name if pool else None,
            image=task.image,  # type: ignore[attr-defined]
            cpu_milli=task.cpu_milli,  # type: ignore[attr-defined]
            memory_mb=task.memory_mb,  # type: ignore[attr-defined]
            gpu_type=task.gpu_type,  # type: ignore[attr-defined]
            gpu_count=task.gpu_count,  # type: ignore[attr-defined]
            status=task.status,  # type: ignore[attr-defined]
            status_message=task.status_message,  # type: ignore[attr-defined]
            priority=task.priority,  # type: ignore[attr-defined]
            max_runtime_seconds=task.max_runtime_seconds,  # type: ignore[attr-defined]
            node_name=task.node_name,  # type: ignore[attr-defined]
            exit_code=task.exit_code,  # type: ignore[attr-defined]
            duration_seconds=task.duration_seconds,  # type: ignore[attr-defined]
            started_at=task.started_at,  # type: ignore[attr-defined]
            finished_at=task.finished_at,  # type: ignore[attr-defined]
            is_active=task.is_active,  # type: ignore[attr-defined]
            created_at=task.created_at,  # type: ignore[attr-defined]
            updated_at=task.updated_at,  # type: ignore[attr-defined]
        )


class TaskDetail(TaskSummary):
    """The task list row plus everything only the detail view needs."""

    script: str
    working_dir: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    k8s_job_name: str | None = None
    pod_name: str | None = None
    ttl_seconds_after_finished: int = 3600

    @classmethod
    def from_task(cls, task: object) -> TaskDetail:
        summary = TaskSummary.from_task(task)
        return cls(
            **summary.model_dump(),
            script=task.script,  # type: ignore[attr-defined]
            working_dir=task.working_dir,  # type: ignore[attr-defined]
            env=dict(task.env or {}),  # type: ignore[attr-defined]
            k8s_job_name=task.k8s_job_name,  # type: ignore[attr-defined]
            pod_name=task.pod_name,  # type: ignore[attr-defined]
            ttl_seconds_after_finished=task.ttl_seconds_after_finished,  # type: ignore[attr-defined]
        )


class TaskCreate(ApiModel):
    name: str = Field(min_length=1, max_length=MAX_TASK_NAME, pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str = Field(default="", max_length=128)
    description: str | None = None
    workspace_id: int | None = None
    owner_user_id: int | None = None
    resource_group_id: int
    resource_spec_id: int
    image: str = Field(min_length=1, max_length=512)
    #: A shell script. Run as ``/bin/sh -lc <script>`` inside the container —
    #: see ``app/models/task.py`` for why a shell is right here and nowhere else.
    script: str = Field(min_length=1)
    working_dir: str | None = Field(default=None, max_length=256)
    env: dict[str, str] = Field(default_factory=dict)
    #: Extra volumes to mount, beyond the owner's home volume — which is always
    #: mounted, because a task has no home without it. Nothing is mounted by
    #: default: a task that silently saw a colleague's shared directory would be a
    #: surprise rather than a convenience.
    storage_ids: list[int] = Field(default_factory=list, max_length=16)
    priority: int = Field(default=0, ge=-100, le=100)
    max_runtime_seconds: int | None = Field(default=None, ge=60)
    ttl_seconds_after_finished: int = Field(default=3600, ge=60, le=604800)

    @field_validator("name")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.lower()


class TaskUpdate(ApiModel):
    """Only the fields that can honestly change after submission."""

    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = None
    priority: int | None = Field(default=None, ge=-100, le=100)


class TaskLogs(ApiModel):
    task_id: int
    pod_name: str | None = None
    logs: str
    previous: bool = False
