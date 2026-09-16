"""Daily resource consumption, per person, per day.

This is the usage centre's fact table and it exists for one reason: **a day is
the unit people ask about.** "How much of the A100 pool did the NLP lab use last
month?" cannot be answered from `instances.started_at` alone, because a job that
runs across midnight belongs to two days, and a month boundary cuts every
long-running environment in half.

Three decisions shape the table:

**Consumption is reserved time, not busy time.** An instance holding a GPU for
eight hours consumed eight GPU-hours whether or not the kernel was busy — the
card was unavailable to anyone else. That is the number a cluster owner needs,
and it is also the number the scheduler can account for exactly. Prometheus can
say how hard the card worked; that is a different (and useful) question, and it
belongs to the monitoring integration rather than here.

**Every measure is an exact integer.** Floats accumulate error, and a total that
disagrees with the sum of its parts by a rounding crumb is the kind of thing that
destroys trust in a report. So the columns are `gpu_seconds`,
`cpu_milli_seconds` and `memory_mb_seconds` — a millicore-second and a
mebibyte-second are whole numbers. Display units (GPU·h, core·h, GB·h) are
derived for the API and never stored.

**The row is a rollup, so it is replaceable.** Recomputing a day deletes that
day's rows and writes them again; there is no incremental bookkeeping to get
wrong. That is what makes the current day safe to recompute every few minutes
while it is still growing, and what makes a backfill after a fix possible at all.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import BigInteger, Date, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, str_enum
from app.models.enums import UsageKind


class UsageDaily(Base, TimestampMixin):
    __tablename__ = "usage_daily"

    id: Mapped[int] = mapped_column(primary_key=True)

    #: The UTC day the consumption happened in. Everything is UTC end to end —
    #: a local-time rollup would duplicate or drop an hour twice a year.
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # --- attribution ---------------------------------------------------------
    #: Who is charged. Every measure below is attributed to exactly one person,
    #: because a group's total has to equal the sum of its members' totals or
    #: nobody will believe either number.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Snapshots, not lookups. A workspace can be moved to another group and a
    #: user can leave one; last quarter's usage must keep reporting the
    #: arrangement that was actually in force then.
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )

    workload_kind: Mapped[UsageKind] = mapped_column(
        str_enum(UsageKind, "usage_kind"), nullable=False
    )
    #: Accelerator model, or ``"none"`` for CPU-only work. Not nullable: it is
    #: part of the uniqueness key, and a NULL there would defeat the constraint
    #: on both SQLite and PostgreSQL, where NULLs never compare equal.
    gpu_type: Mapped[str] = mapped_column(String(32), nullable=False, default="none")

    # --- measures ------------------------------------------------------------
    #: Wall-clock seconds the workload was live during this day, counted once
    #: regardless of replica count. Kept beside the multiplied measures so a
    #: reader can tell "one GPU for ten hours" from "ten GPUs for one hour".
    runtime_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Sum of ``gpu_count × seconds``. The headline number; GPU·h is this/3600.
    gpu_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Sum of ``cpu_milli × seconds`` — divide by 1000 for core-seconds.
    cpu_milli_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: Sum of ``memory_mb × seconds`` — divide by 1024 for GiB-seconds.
    memory_mb_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: How many workloads contributed. A count of 3 with 72 GPU-hours reads very
    #: differently from a count of 1, and the difference matters.
    workload_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        # The grain. Every column is NOT NULL so the constraint actually bites —
        # and it is what makes a recompute an idempotent replace rather than an
        # append that silently doubles every total.
        UniqueConstraint(
            "day",
            "user_id",
            "group_id",
            "workspace_id",
            "workload_kind",
            "gpu_type",
            name="uq_usage_daily_grain",
        ),
        Index("ix_usage_daily_day_group", "day", "group_id"),
        Index("ix_usage_daily_day_workspace", "day", "workspace_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<UsageDaily {self.day} user={self.user_id} "
            f"{self.workload_kind}={self.gpu_seconds}s gpu>"
        )
