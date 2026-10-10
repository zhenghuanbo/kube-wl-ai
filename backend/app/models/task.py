"""Custom tasks — a shell command that runs to completion.

A task is one ``batch/v1`` Job: it starts, it runs, it exits. That is the whole
difference from an instance, and it drives every other decision here — a task
never has a Service, never an Ingress, never an SSH port, and its quota is
released the moment it reaches a terminal status.

**On the shell.** The platform's own argv is never built by string
interpolation; that rule (see ``app/k8s/workloads.py``) exists so a user-supplied
value cannot become a *platform* command. A custom task is different in kind:
the user is asking for a shell, inside their own container, in their own
namespace, under their own quota. So ``script`` is passed as the argument to
``/bin/sh -lc`` — an argv tuple of exactly three elements, of which the third is
the user's text. Nothing the platform composes is reinterpreted, and the shell
does what the user meant.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from app.models.enums import TaskStatus

if TYPE_CHECKING:
    from app.models.group import Group
    from app.models.resource import ResourceGroup, ResourceSpec
    from app.models.user import User
    from app.models.workspace import Workspace


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Unique per workspace, like instance names.
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)

    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    resource_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("resource_groups.id", ondelete="SET NULL")
    )
    resource_spec_id: Mapped[int | None] = mapped_column(
        ForeignKey("resource_specs.id", ondelete="SET NULL")
    )

    # --- snapshotted resource shape ---------------------------------------
    cpu_milli: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=2048)
    gpu_type: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- what to run -------------------------------------------------------
    image: Mapped[str] = mapped_column(String(512), nullable=False)
    #: The user's script. See the module docstring for why a shell is correct
    #: here and forbidden everywhere else.
    script: Mapped[str] = mapped_column(Text, nullable=False)
    working_dir: Mapped[str | None] = mapped_column(String(256))
    env: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    # --- lifecycle ---------------------------------------------------------
    status: Mapped[TaskStatus] = mapped_column(
        str_enum(TaskStatus, "task_status"), nullable=False, default=TaskStatus.PENDING, index=True
    )
    status_message: Mapped[str | None] = mapped_column(String(512))
    #: Higher runs first when the scheduler has to choose.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Wall-clock cap. Enforced by Kubernetes (``activeDeadlineSeconds``) rather
    #: than by a sweeper, so a task cannot outlive its limit even if the backend
    #: is down.
    max_runtime_seconds: Mapped[int | None] = mapped_column(Integer)
    #: After a terminal status, how long the Job object itself is kept. The row
    #: survives regardless; this only bounds the pod's lifetime.
    ttl_seconds_after_finished: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)

    k8s_job_name: Mapped[str | None] = mapped_column(String(63))
    pod_name: Mapped[str | None] = mapped_column(String(253))
    node_name: Mapped[str | None] = mapped_column(String(253))

    exit_code: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    workspace: Mapped[Workspace] = relationship(lazy="joined")
    group: Mapped[Group] = relationship(lazy="joined")
    owner: Mapped[User] = relationship(foreign_keys=[owner_user_id], lazy="joined")
    resource_group: Mapped[ResourceGroup | None] = relationship(lazy="joined")
    resource_spec: Mapped[ResourceSpec | None] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_tasks_workspace_name"),
        Index("ix_tasks_status_active", "status", "is_active"),
    )

    @property
    def is_finished(self) -> bool:
        from app.models.enums import TERMINAL_TASK_STATUSES

        return self.status in TERMINAL_TASK_STATUSES

    @property
    def has_gpu(self) -> bool:
        return self.gpu_count > 0 and self.gpu_type not in ("", "none")

    @property
    def duration_seconds(self) -> float | None:
        """Wall-clock runtime so far, or the final duration once finished."""
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.now(UTC)
        start = self.started_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        return (end - start).total_seconds()
