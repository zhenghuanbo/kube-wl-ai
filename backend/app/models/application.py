"""Applications — reusable, publishable instance shapes.

An application is what a lab actually wants to hand out: *"PyTorch 2.4 with
JupyterLab, 4 cores, one A100"* plus the introduction that tells a new student
what it is for. It carries no cluster objects of its own; launching one creates
an :class:`~app.models.instance.Instance` in the launcher's workspace, which is
where the quota, the namespace and the lifecycle live.

Two design points are worth stating up front:

* **The launch parameters are defaults, not a contract.** The spec, image and
  resource shape recorded here pre-fill the launch form and are snapshotted onto
  the resulting instance, so a curator editing an application later cannot
  rewrite anything that is already running.
* **There is no separate ``app_templates`` table.** The plan listed one, but a
  template that only an administrator may author is exactly an application with
  ``is_curated=true`` and ``visibility=global``. Two nearly identical tables
  would drift apart, and every query would have to union them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, str_enum
from app.models.enums import ApplicationCategory, ApplicationVisibility, IdeType

if TYPE_CHECKING:
    from app.models.group import Group
    from app.models.resource import ResourceGroup, ResourceSpec
    from app.models.user import User
    from app.models.workspace import Workspace


class Application(Base, TimestampMixin):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Unique within the owning group, so two labs may both publish "pytorch".
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    #: One line, shown in the catalogue card.
    summary: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    #: Long-form markdown introduction: what it is, how to start, what to cite.
    readme: Mapped[str | None] = mapped_column(Text)
    #: An ``mdi-*`` icon name.
    icon: Mapped[str] = mapped_column(String(64), nullable=False, default="mdi-application")
    category: Mapped[ApplicationCategory] = mapped_column(
        str_enum(ApplicationCategory, "application_category"),
        nullable=False,
        default=ApplicationCategory.OTHER,
    )

    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Denormalised from the workspace so the data-scope filter needs no join.
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    # --- launch defaults ---------------------------------------------------
    image: Mapped[str] = mapped_column(String(512), nullable=False)
    ide_type: Mapped[IdeType] = mapped_column(
        str_enum(IdeType, "application_ide_type"), nullable=False, default=IdeType.NONE
    )
    #: Nullable: a curator may describe an application without pinning a pool,
    #: leaving the launcher to choose among the pools their workspace holds.
    resource_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("resource_groups.id", ondelete="SET NULL")
    )
    resource_spec_id: Mapped[int | None] = mapped_column(
        ForeignKey("resource_specs.id", ondelete="SET NULL")
    )
    default_cpu_milli: Mapped[int] = mapped_column(Integer, nullable=False, default=2000)
    default_memory_mb: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)
    default_gpu_type: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    default_gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    default_storage_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    #: Non-secret environment, e.g. ``{"OMP_NUM_THREADS": "8"}``. Secrets must go
    #: through a Secret, which this table deliberately has no way to express.
    env: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    #: Ports worth documenting, so the introduction can say "the dashboard is on
    #: 8501". Purely informational — routing is decided at launch time.
    ports: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)

    # --- publication -------------------------------------------------------
    visibility: Mapped[ApplicationVisibility] = mapped_column(
        str_enum(ApplicationVisibility, "application_visibility"),
        nullable=False,
        default=ApplicationVisibility.WORKSPACE,
        index=True,
    )
    #: Published by an administrator for everyone. Curated entries are readable
    #: by any authenticated user; ``visibility`` then governs who may edit.
    is_curated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: How many times it has been launched. Cheap social proof, and it tells a
    #: curator which entries are dead weight.
    launch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    workspace: Mapped[Workspace] = relationship(lazy="joined")
    group: Mapped[Group] = relationship(lazy="joined")
    owner: Mapped[User] = relationship(foreign_keys=[owner_user_id], lazy="joined")
    resource_group: Mapped[ResourceGroup | None] = relationship(lazy="joined")
    resource_spec: Mapped[ResourceSpec | None] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("group_id", "name", name="uq_applications_group_name"),
        Index("ix_applications_visibility_active", "visibility", "is_active"),
    )

    @property
    def is_global(self) -> bool:
        return self.visibility is ApplicationVisibility.GLOBAL or self.is_curated
