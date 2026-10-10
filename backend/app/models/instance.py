"""Container instances — a developer's running environment.

An instance is one ``Deployment(replicas=1)`` plus a ``Service``, an optional
``Ingress`` for the web IDE, a ``PVC`` for the home directory and a ``Secret``
holding the owner's ``authorized_keys``. Everything lives in the owner's
workspace namespace, so the namespace quota is what bounds it.

Resource shape is **snapshotted** onto the row rather than read through the
spec: an administrator editing or retiring a spec must not silently rewrite what
a running instance was created as, and usage accounting needs the figure that
was actually requested.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
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
from app.models.enums import IdeType, InstanceStatus

if TYPE_CHECKING:
    from app.models.resource import ResourceGroup, ResourceSpec
    from app.models.user import User
    from app.models.workspace import Workspace


class Instance(Base, TimestampMixin):
    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Unique per workspace, not globally: two labs may both have an "alice-dev".
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)

    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The person the instance belongs to. Always a member of ``workspace_id``;
    #: enforced in the service, since a cross-row check cannot be a constraint.
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    #: Which pool it was scheduled against, and which shape within that pool.
    resource_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("resource_groups.id", ondelete="SET NULL"), index=True
    )
    resource_spec_id: Mapped[int | None] = mapped_column(
        ForeignKey("resource_specs.id", ondelete="SET NULL"), index=True
    )

    # --- requested shape, snapshotted ---------------------------------------
    cpu_milli: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_mb: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Plain string, not an enum: adding an accelerator model must not be a
    #: schema migration (same rationale as ``ResourceSpec.gpu_type``).
    gpu_type: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    gpu_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    image: Mapped[str] = mapped_column(String(512), nullable=False)
    ide_type: Mapped[IdeType] = mapped_column(
        str_enum(IdeType, "ide_type"), nullable=False, default=IdeType.NONE
    )

    status: Mapped[InstanceStatus] = mapped_column(
        str_enum(InstanceStatus, "instance_status"),
        nullable=False,
        default=InstanceStatus.PENDING,
        index=True,
    )
    status_message: Mapped[str | None] = mapped_column(Text)

    # --- Kubernetes object names --------------------------------------------
    #: Stored rather than derived so a rename of the instance row cannot orphan
    #: the objects; the names are immutable once applied.
    k8s_deployment: Mapped[str | None] = mapped_column(String(63))
    #: SSH and the IDE are separate Services — only SSH is a NodePort — so they
    #: get separate columns. A single comma-joined string would silently
    #: overflow its column for long instance names.
    k8s_ssh_service: Mapped[str | None] = mapped_column(String(63))
    k8s_ide_service: Mapped[str | None] = mapped_column(String(63))
    k8s_ingress: Mapped[str | None] = mapped_column(String(63))
    k8s_pvc: Mapped[str | None] = mapped_column(String(63))
    k8s_secret: Mapped[str | None] = mapped_column(String(63))

    node_name: Mapped[str | None] = mapped_column(String(253))
    pod_name: Mapped[str | None] = mapped_column(String(253))
    #: NodePort allocated for SSH. Recorded so the UI can print the exact
    #: ``ssh -p`` command without a live service read.
    ssh_node_port: Mapped[int | None] = mapped_column(Integer)
    ssh_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- lifecycle -----------------------------------------------------------
    max_runtime_seconds: Mapped[int | None] = mapped_column(Integer)
    #: NULL means "no deadline", which is a legitimate state when the pool sets
    #: no runtime limit — not an error to be papered over with a sentinel.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Set when the owner deletes the row: the object is gone from the platform's
    #: listings but the record survives for usage history.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    workspace: Mapped[Workspace] = relationship(lazy="joined")
    owner: Mapped[User] = relationship(foreign_keys=[owner_user_id], lazy="joined")
    resource_group: Mapped[ResourceGroup | None] = relationship(lazy="joined")
    resource_spec: Mapped[ResourceSpec | None] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_instances_workspace_name"),
        Index("ix_instances_status_active", "status", "is_active"),
    )

    # --- derived state -------------------------------------------------------
    @property
    def is_expired(self) -> bool:
        """True when a deadline was set and has passed."""
        if self.expires_at is None:
            return False
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires <= datetime.now(UTC)

    @property
    def is_running(self) -> bool:
        return self.status == InstanceStatus.RUNNING

    @property
    def has_gpu(self) -> bool:
        return self.gpu_count > 0 and self.gpu_type not in ("", "none")

    def __repr__(self) -> str:
        return f"<Instance id={self.id} name={self.name!r} status={self.status}>"
