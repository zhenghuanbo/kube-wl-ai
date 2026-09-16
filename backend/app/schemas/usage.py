"""Wire schemas for the usage centre.

Every measure is carried as an exact integer beside its display unit. The
integers are the truth and the floats are for humans: a chart that plots
``gpuHours`` is fine, but a client that sums floats across a month is not, and
sending only the float would leave it no alternative.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import Field

from app.models.enums import UsageKind
from app.schemas.base import ApiModel


class UsageTotalsRead(ApiModel):
    """Aggregated consumption over some window or bucket."""

    runtime_seconds: int = Field(description="负载处于运行状态的总秒数（不含副本数）")
    gpu_seconds: int = Field(description="GPU 秒：Σ 卡数 × 秒数")
    cpu_milli_seconds: int = Field(description="毫核秒：除以 1000 得核秒")
    memory_mb_seconds: int = Field(description="MB 秒：除以 1024 得 GB 秒")
    workload_count: int = Field(description="贡献用量的负载数量")

    gpu_hours: float = Field(description="GPU 卡时，由 gpuSeconds 折算")
    cpu_core_hours: float = Field(description="CPU 核时，由 cpuMilliSeconds 折算")
    memory_gb_hours: float = Field(description="内存 GB 时，由 memoryMbSeconds 折算")

    user_count: int = 0
    workspace_count: int = 0


class UsageDayRead(ApiModel):
    """One day of the time series. Days with no usage are present, with zeros."""

    day: date
    totals: UsageTotalsRead


class UsageSeriesRead(ApiModel):
    day_from: date
    day_to: date
    points: list[UsageDayRead]


class UsageBreakdownRowRead(ApiModel):
    """One bucket of a grouped report."""

    key: str
    label: str
    #: ``None`` for dimensions whose key is not a row id (GPU model, workload
    #: kind) — there is nothing to drill into.
    entity_id: int | None = None
    totals: UsageTotalsRead
    #: GPU-seconds split between instances and tasks, when the dimension is not
    #: itself the workload kind.
    kinds: dict[str, int] = Field(default_factory=dict)


class UsageBreakdownRead(ApiModel):
    by: str
    day_from: date
    day_to: date
    rows: list[UsageBreakdownRowRead]
    #: Whether the row list was cut off at ``limit``. A report that silently
    #: shows the top 50 of 900 labs is worse than one that says so.
    truncated: bool = False


class UsageBreakdownDimensionRead(ApiModel):
    key: str
    label: str


class UsageRollupRequest(ApiModel):
    day_from: date | None = None
    day_to: date | None = None


class UsageRollupResultRead(ApiModel):
    day_from: date
    day_to: date
    rows_written: int
    instances_scanned: int
    tasks_scanned: int
    runtime_seconds: int
    gpu_seconds: int
    outside_window: int


class UsageSummaryRead(ApiModel):
    """What the usage centre shows above the charts."""

    day_from: date
    day_to: date
    totals: UsageTotalsRead
    #: The same window's totals restricted to the caller alone. An administrator
    #: wants to know both "how much did the lab use" and "how much of that was
    #: me", and deriving the second on the client is impossible once the first
    #: has been aggregated.
    self_totals: UsageTotalsRead | None = None
    #: Whether a rollup has ever run for this window. All-zero numbers mean
    #: something very different when nothing was ever computed.
    covered: bool


class UsageKindRead(ApiModel):
    """The workload kinds a usage row can carry, for the filter control."""

    value: UsageKind
    label: str


class UsageMetaRead(ApiModel):
    """What the client needs in order to build the filter controls.

    Sent rather than hard-coded in the frontend so the two cannot drift: adding a
    breakdown dimension should light up in the UI without a second commit.
    """

    dimensions: list[UsageBreakdownDimensionRead]
    kinds: list[UsageKindRead]
    max_range_days: int
    default_rollup_lookback_days: int


def totals_to_read(totals: Any) -> UsageTotalsRead:
    """Convert the service's totals dataclass to the wire model.

    Spelled out rather than left to ``from_attributes``: the display-unit fields
    are computed properties, and relying on Pydantic to discover them would make
    a rename in the service fail here at runtime rather than at type-check time.
    """
    return UsageTotalsRead(
        runtime_seconds=totals.runtime_seconds,
        gpu_seconds=totals.gpu_seconds,
        cpu_milli_seconds=totals.cpu_milli_seconds,
        memory_mb_seconds=totals.memory_mb_seconds,
        workload_count=totals.workload_count,
        gpu_hours=totals.gpu_hours,
        cpu_core_hours=totals.cpu_core_hours,
        memory_gb_hours=totals.memory_gb_hours,
        user_count=totals.user_count,
        workspace_count=totals.workspace_count,
    )
