"""An append-only timeline of what happened to a workload.

Status alone loses history: an instance that is ``running`` now may have been
stopped twice and rescheduled once, and "why was my job killed at 3am?" is
answerable only from a record. Kubernetes events are not a substitute — they
expire after an hour by default, and they say nothing about who asked for what.

The table is intentionally generic over workload kind. Four nullable foreign
keys would make every query filter on three NULLs and would make "show me
everything that happened in this workspace today" impossible to write.
"""

from __future__ import annotations

from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, str_enum
from app.models.enums import WorkloadEventType, WorkloadKind


class WorkloadEvent(Base, TimestampMixin):
    __tablename__ = "workload_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    workload_kind: Mapped[WorkloadKind] = mapped_column(
        str_enum(WorkloadKind, "workload_kind"), nullable=False
    )
    workload_id: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[WorkloadEventType] = mapped_column(
        str_enum(WorkloadEventType, "workload_event_type"), nullable=False
    )
    #: Human-readable detail, e.g. the cluster error that caused a failure.
    message: Mapped[str | None] = mapped_column(String(512))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    #: Who asked for it. Null for events the reconciler produced on its own —
    #: that distinction is exactly what makes a timeline readable.
    actor_user_id: Mapped[int | None] = mapped_column(Integer)
    actor_username: Mapped[str | None] = mapped_column(String(150))

    __table_args__ = (
        Index(
            "ix_workload_events_target",
            "workload_kind",
            "workload_id",
            "created_at",
        ),
    )
