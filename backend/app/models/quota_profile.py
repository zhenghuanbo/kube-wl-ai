"""Compute quota profiles — the named ceilings an administrator hands out.

A profile is a reusable **ceiling**, not a live counter: it says what may be used,
and the usage it is compared against is always recomputed from the workloads that
exist right now (see ``app.services.quota_guard``). Nothing here is decremented,
which is precisely why there is no ``used_value`` column to drift out of date.

``-1`` (:data:`UNLIMITED`) means "do not check this dimension". It is deliberately
a number rather than ``NULL``: every dimension uses the same sentinel, and ``0``
has to keep meaning "none at all".
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, str_enum
from app.models.enums import QuotaProfileKind

#: Sentinel for "this dimension is not checked at all".
UNLIMITED = -1


class QuotaProfile(Base, TimestampMixin):
    """A named set of compute ceilings, for users or for workspaces.

    ``kind=user`` profiles carry the object counts as well, because "how many
    instances may one person hold" is a property of the person; ``kind=workspace``
    profiles carry the Kubernetes object counts instead, because those bound a
    namespace. The columns that do not apply to a kind stay ``NULL`` and are
    validated on the way in — see ``app.schemas.quota_profile``.

    The column defaults below only exist so that a row built by hand in a test is
    sane. Every profile the application creates is given the values from
    ``Settings`` explicitly (``app.services.quota_profiles``), so configuration —
    not this file — is what decides what a new tenant gets.
    """

    __tablename__ = "quota_profiles"
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_quota_profiles_kind_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[QuotaProfileKind] = mapped_column(
        str_enum(QuotaProfileKind, "quota_profile_kind"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)

    #: Compute ceilings, shared by both kinds. ``-1`` means "not checked".
    cpu_cores: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    memory_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=UNLIMITED)
    gpu: Mapped[int] = mapped_column(Integer, nullable=False, default=8)

    #: ``kind=user`` only: how many objects one person may hold at once.
    max_instances: Mapped[int | None] = mapped_column(Integer)
    max_applications: Mapped[int | None] = mapped_column(Integer)
    max_tasks: Mapped[int | None] = mapped_column(Integer)

    #: ``kind=workspace`` only: how many Kubernetes objects the namespace may hold.
    max_pods: Mapped[int | None] = mapped_column(Integer)
    max_deployments: Mapped[int | None] = mapped_column(Integer)
    max_services: Mapped[int | None] = mapped_column(Integer)
    max_jobs: Mapped[int | None] = mapped_column(Integer)

    #: Exactly one profile per kind is the default, and *every* user or workspace
    #: with no profile of its own follows it. The invariant is maintained in
    #: ``app.services.quota_profiles`` rather than by a partial unique index,
    #: matching how ``RuntimeLimit.is_default`` is already handled: SQLite and
    #: PostgreSQL want different partial-index syntax, and a second mechanism for
    #: the same rule is a second place for it to be wrong.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return (
            f"<QuotaProfile id={self.id} kind={self.kind} name={self.name!r} "
            f"cpu={self.cpu_cores} mem={self.memory_gb} gpu={self.gpu}>"
        )
