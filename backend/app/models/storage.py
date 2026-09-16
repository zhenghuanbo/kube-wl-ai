"""Persistent storage: the volume that outlives the container.

An instance's scratch PVC is deleted with the instance, which is right for
scratch and wrong for everything else. This module models the other half: a
volume with its own identity that an instance *attaches* to.

Two decisions are load-bearing:

**Mount, don't copy.** A dataset is attached to many pods at once, so the mount
is read-only at the Kubernetes level, not just hidden in the UI. A training job
that can write to a shared dataset will eventually do so, and the damage is
silent and unrecoverable.

**One home per user.** ``PERSONAL_HOME`` is unique per (workspace, owner). It is
the reason an instance becomes disposable — code, environments and checkpoints
live in the home, so deleting the container costs nothing. Allowing two would
reintroduce exactly the ambiguity it exists to remove.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
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
from app.models.enums import StorageKind, StorageStatus

if TYPE_CHECKING:
    from app.models.group import Group
    from app.models.user import User
    from app.models.workspace import Workspace

#: Default mount point per kind.
#:
#: A user should never have to remember a path. The instance mounts its home at
#: the image's home directory and everything else under ``/mnt/<name>``, so the
#: same storage looks the same from every image.
DEFAULT_MOUNT_POINTS: dict[str, str] = {
    StorageKind.PERSONAL_HOME.value: "/home/jovyan",
    StorageKind.PERSONAL_DATA.value: "/mnt/data",
    StorageKind.GROUP_SHARE.value: "/mnt/share",
    StorageKind.DATASET.value: "/mnt/dataset",
}

#: Kinds that are read-only wherever they are mounted.
#:
#: Datasets are shared, often university-wide, and are usually the only copy of
#: something expensive to reproduce.
READ_ONLY_KINDS: frozenset[str] = frozenset({StorageKind.DATASET.value})


class Storage(Base, TimestampMixin):
    """A PersistentVolumeClaim with a name, an owner and a purpose."""

    __tablename__ = "storages"
    __table_args__ = (
        # Names are unique inside a namespace, which is what the PVC name is
        # derived from — two rows mapping to one PVC would fight on reconcile.
        UniqueConstraint("workspace_id", "name", name="uq_storages_workspace_name"),
        Index("ix_storages_owner_kind", "owner_user_id", "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)

    kind: Mapped[StorageKind] = mapped_column(
        str_enum(StorageKind, "storage_kind"), nullable=False, default=StorageKind.PERSONAL_DATA
    )
    status: Mapped[StorageStatus] = mapped_column(
        str_enum(StorageStatus, "storage_status"),
        nullable=False,
        default=StorageStatus.PENDING,
        index=True,
    )
    status_message: Mapped[str | None] = mapped_column(Text)

    # --- tenancy -------------------------------------------------------------
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), index=True
    )
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    # --- capacity ------------------------------------------------------------
    capacity_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    used_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- cluster -------------------------------------------------------------
    #: ``ReadOnlyMany``/``ReadWriteMany`` matter for datasets: a dataset only one
    #: pod may read at a time is not a dataset. Left nullable so the resource
    #: group's storage class can decide.
    access_mode: Mapped[str | None] = mapped_column(String(32))
    storage_class: Mapped[str | None] = mapped_column(String(64))
    #: Informational only. A PersistentVolumeClaim has no `medium` field — that
    #: belongs to emptyDir — so this records which StorageClass backs the volume
    #: and is never sent to the cluster. It exists so the UI can warn that a
    #: tmpfs-backed class behaves like RAM: contents vanish on pod restart and
    #: the usage is memory the quota does not count.
    medium: Mapped[str | None] = mapped_column(String(32))
    pvc_name: Mapped[str | None] = mapped_column(String(128))
    k8s_namespace: Mapped[str | None] = mapped_column(String(63))

    is_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Where a dataset or share was imported from, for provenance.
    source: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workspace: Mapped[Workspace] = relationship(lazy="joined")
    group: Mapped[Group | None] = relationship(lazy="joined")
    owner: Mapped[User | None] = relationship(
        lazy="joined", foreign_keys=[owner_user_id], join_depth=1
    )

    @property
    def is_live(self) -> bool:
        """Whether the volume still holds capacity."""
        return self.status in {
            StorageStatus.PENDING,
            StorageStatus.BOUND,
            StorageStatus.RESIZING,
            StorageStatus.FAILED,
        }

    @property
    def is_shared(self) -> bool:
        """Whether more than one person is meant to see it."""
        return self.kind in {StorageKind.GROUP_SHARE, StorageKind.DATASET}

    @property
    def default_mount_path(self) -> str:
        return DEFAULT_MOUNT_POINTS.get(str(self.kind), f"/mnt/{self.name}")

    @property
    def effective_read_only(self) -> bool:
        """Read-only if the kind says so *or* the row was marked so.

        Both sources matter: a dataset is read-only by nature, and an
        administrator may additionally freeze a share that is being retired.
        """
        return self.is_read_only or str(self.kind) in READ_ONLY_KINDS

    @property
    def available_gb(self) -> int:
        return max(self.capacity_gb - self.used_gb, 0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Storage {self.id} {self.name} {self.kind}>"


class InstanceStorage(Base, TimestampMixin):
    """An attachment of a volume to an instance.

    A join table with attributes rather than a plain many-to-many: the mount
    path and the read-only flag belong to *this* attachment, not to the volume.
    The same dataset may be mounted read-only in one instance and (by an
    administrator) writable in another.
    """

    __tablename__ = "instance_storages"
    __table_args__ = (
        UniqueConstraint("instance_id", "storage_id", name="uq_instance_storages_pair"),
        # One volume cannot appear twice at the same path inside a container.
        UniqueConstraint("instance_id", "mount_path", name="uq_instance_storages_mount"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instance_id: Mapped[int] = mapped_column(
        ForeignKey("instances.id", ondelete="CASCADE"), nullable=False, index=True
    )
    storage_id: Mapped[int] = mapped_column(
        ForeignKey("storages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mount_path: Mapped[str] = mapped_column(String(255), nullable=False)
    is_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Sub-directory of the volume to mount, so one share can hold per-user
    #: folders without everyone's files appearing at the root of their view.
    sub_path: Mapped[str | None] = mapped_column(String(255))

    storage: Mapped[Storage] = relationship(lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<InstanceStorage instance={self.instance_id} storage={self.storage_id}>"
