"""Managed storage: a directory that outlives every container that mounts it.

A managed volume is not *allocated*, it is *named*. The directory already exists —
the layout is ``{storage_root}/{group}/{username}`` and so on, decided by whoever
provisions the filesystem — and the platform's job is to make that directory
mountable, ownable, and visible to the right people. ``Storage`` is therefore a
record of a directory plus the rules about who may use it, and ``host_path`` is
the one field that says which directory it is.

Three decisions are load-bearing:

**One row per directory.** ``host_path`` is unique. That is the whole "one volume
per directory" requirement expressed as a constraint: two rows claiming one
directory would be two volumes sharing an identity, and which one a pod got would
depend on which row happened to win.

**The kind decides the binding.** A user volume belongs to a user, a group share
to a group, a global share to nobody. The binding is checked against the
directory, so a row cannot claim to be a user volume while pointing at a group's
root.

**A group's own directory is a volume too.** ``{storage_root}/{group}`` contains
every member's home. That is exactly what makes it useful — it is what a group
administrator mounts to see what the group is consuming — and exactly why
mounting it is restricted to them.
"""

from __future__ import annotations

from datetime import datetime
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, str_enum
from app.models.enums import StorageKind, StorageStatus

if TYPE_CHECKING:
    from app.models.group import Group
    from app.models.user import User
    from app.models.workspace import Workspace

#: Where each kind of volume appears inside a container.
#:
#: A user's volume is mounted under the user's own name rather than at a fixed
#: path: the directory is theirs, and a mount point that says *whose* it is makes
#: a mistaken mount visible from inside the pod. The share kinds are not mounted
#: by default at all — a user chooses to attach them when starting a workload —
#: so those two constants are the defaults that choice uses.
USER_MOUNT_ROOT = "/home"
GROUP_SHARE_MOUNT_PATH = "/mnt/group_shared"
GLOBAL_SHARE_MOUNT_PATH = "/mnt/global_shared"


class Storage(Base, TimestampMixin):
    """A directory, plus the rules about who may use it."""

    __tablename__ = "storages"
    __table_args__ = (
        # The "one volume per directory" rule, as a constraint rather than a
        # convention — but only among *live* volumes.
        #
        # Partial on purpose. A retired volume keeps its `host_path` and its name
        # so the record survives, and if those kept occupying the directory then
        # deleting a volume would make its directory permanently unusable. Freeing
        # a directory is the point of deleting the volume, so the rule has to be
        # about the volumes that exist, not the ones that used to.
        Index(
            "uq_storages_host_path_live",
            "host_path",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active"),
        ),
        # A name is unique inside a workspace because the claim name is derived
        # from it: two rows mapping to one claim would fight on every reconcile.
        Index(
            "uq_storages_name_live",
            "workspace_id",
            "name",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active"),
        ),
        Index("ix_storages_owner_kind", "owner_user_id", "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)

    kind: Mapped[StorageKind] = mapped_column(
        str_enum(StorageKind, "storage_kind"), nullable=False, default=StorageKind.USER_VOLUME
    )
    status: Mapped[StorageStatus] = mapped_column(
        str_enum(StorageStatus, "storage_status"),
        nullable=False,
        default=StorageStatus.PENDING,
        index=True,
    )
    status_message: Mapped[str | None] = mapped_column(Text)

    #: The directory this volume *is*.
    #:
    #: Everything else — the PersistentVolume, the claim, the mount point — is
    #: derived from it. The unique constraint above is what turns "one volume per
    #: directory" from an intention into something the database refuses to break.
    host_path: Mapped[str] = mapped_column(String(512), nullable=False, index=True)

    #: The named directory inside the group's own directory, for a group share.
    #:
    #: ``None`` is a meaningful value rather than "unset": it marks the group's
    #: **own** directory as the volume, the one that contains every member's home
    #: and is therefore visible and mountable only to a group administrator.
    #: Recording it — instead of re-deriving it by comparing ``host_path`` with
    #: the group's directory — is what keeps the visibility rule a SQL predicate
    #: rather than a Python post-filter that would silently break pagination.
    #:
    #: Also ``None`` for user and global volumes, whose directory is fixed by the
    #: kind and has no name of its own.
    dir_name: Mapped[str | None] = mapped_column(String(255))

    # --- tenancy -------------------------------------------------------------
    #: The workspace whose namespace holds this volume's claim, and therefore
    #: which workloads can reach it. Null for a global share: it belongs to no
    #: single workspace, and is claimed separately in each namespace that uses it.
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), index=True
    )
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    # --- capacity ------------------------------------------------------------
    #: Accounting, not enforcement. A hostPath volume cannot limit a directory, so
    #: this reaches the cluster only so the claim's request can be satisfied at
    #: bind time. It is what the platform reports and charges against; the
    #: filesystem is what actually decides how much fits.
    capacity_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    used_gb: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- cluster -------------------------------------------------------------
    #: How many pods may hold it at once. ``ReadWriteMany`` for the share kinds,
    #: because a share only one pod can mount is not a share; a user's own volume
    #: is ``ReadWriteOnce``, the narrowest mode that still lets several of their
    #: own pods use it together on one node.
    access_mode: Mapped[str | None] = mapped_column(String(32))
    pvc_name: Mapped[str | None] = mapped_column(String(128))
    k8s_namespace: Mapped[str | None] = mapped_column(String(63))

    is_read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Where the data came from, for provenance.
    source: Mapped[str | None] = mapped_column(Text)
    #: When the platform last compared this row with the cluster. Written by every
    #: reconcile — it is the rotation key, so leaving it null would pin the same
    #: first page of rows in the queue forever.
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workspace: Mapped[Workspace | None] = relationship(lazy="joined")
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
    def is_individual(self) -> bool:
        """Whether the volume belongs to exactly one user."""
        return self.kind is StorageKind.USER_VOLUME

    @property
    def is_shared(self) -> bool:
        """Whether the volume is meant for more than its owner."""
        return self.kind in {StorageKind.GROUP_SHARE, StorageKind.GLOBAL_SHARE}

    @property
    def is_group_level(self) -> bool:
        """Whether this is a group's own directory rather than a share inside it.

        The distinction carries the access rule: this volume contains every
        member's home, so a group's members may not read it and only its
        administrators may mount it.
        """
        return self.kind is StorageKind.GROUP_SHARE and self.dir_name is None

    def mount_path_for(self, username: str | None = None) -> str:
        """Where a container sees this volume when nothing overrides it.

        ``username`` is the *owner's*, and is passed explicitly where the caller
        already knows it, so a read model does not depend on the owner row having
        been loaded.
        """
        if self.kind is StorageKind.GROUP_SHARE:
            return GROUP_SHARE_MOUNT_PATH
        if self.kind is StorageKind.GLOBAL_SHARE:
            return GLOBAL_SHARE_MOUNT_PATH

        who = username or (self.owner.username if self.owner is not None else None)
        if not who:
            # Only reachable once the owner row is gone, and the volume is then
            # unusable anyway; naming the id beats emitting an empty segment.
            who = f"user-{self.owner_user_id}"
        return f"{USER_MOUNT_ROOT}/{who}"

    @property
    def default_mount_path(self) -> str:
        """The mount path for display, resolved from whatever is loaded."""
        return self.mount_path_for()

    @property
    def available_gb(self) -> int:
        return max(self.capacity_gb - self.used_gb, 0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Storage {self.id} {self.name} {self.kind}>"


class InstanceStorage(Base, TimestampMixin):
    """An attachment of a volume to an instance.

    A join table with attributes rather than a plain many-to-many: the mount path
    and the read-only flag belong to *this* attachment, not to the volume. The
    same share may be mounted read-only in one instance and writable in another.

    This is why the mount path is stored rather than recomputed: several shares of
    one kind can be attached at once, and only the first gets the kind's default
    path — the rest are placed under it, and where each one actually landed has to
    survive a restart.
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

    storage: Mapped[Storage] = relationship(lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<InstanceStorage instance={self.instance_id} storage={self.storage_id}>"
