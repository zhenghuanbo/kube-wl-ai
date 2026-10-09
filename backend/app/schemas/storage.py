"""Storage request and response payloads.

``host_path`` is part of the read model rather than an internal detail. It is the
field everything else is derived from, so an administrator needs to see which
directory a volume is in order to judge whether it is the right one — and it is the
only way to tell a group's own directory from a share inside it at a glance.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.models.enums import StorageKind, StorageStatus
from app.models.storage import Storage
from app.schemas.base import ApiModel

MAX_STORAGE_NAME = 40
MAX_CAPACITY_GB = 65536


class StorageSummary(ApiModel):
    """One row of the storage list."""

    id: int
    name: str
    display_name: str
    description: str | None = None
    kind: StorageKind
    status: StorageStatus
    status_message: str | None = None

    #: The directory this volume *is*. Exposed because it is what makes two rows
    #: recognisably the same volume, and because "one volume per directory" is a
    #: rule an administrator has to be able to check by eye.
    host_path: str
    #: The named directory inside a group's own directory. Null means the volume
    #: *is* the group's directory — the one holding every member's home.
    dir_name: str | None = None
    is_group_level: bool = False

    #: Null for a global share: it belongs to no workspace, and its claims are
    #: created per namespace by whatever mounts it.
    workspace_id: int | None = None
    workspace_name: str | None = None
    group_id: int | None = None
    group_name: str | None = None
    owner_user_id: int | None = None
    owner_username: str | None = None
    owner_display_name: str | None = None

    capacity_gb: int
    used_gb: int
    access_mode: str | None = None
    #: Where a container sees it by default: ``/home/{username}`` for a user's
    #: volume, the standard share mount point for the share kinds.
    default_mount_path: str

    is_read_only: bool
    is_active: bool
    source: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_storage(cls, storage: Storage) -> StorageSummary:
        return cls(
            id=storage.id,
            name=storage.name,
            display_name=storage.display_name,
            description=storage.description,
            kind=storage.kind,
            status=storage.status,
            status_message=storage.status_message,
            host_path=storage.host_path,
            dir_name=storage.dir_name,
            is_group_level=storage.is_group_level,
            workspace_id=storage.workspace_id,
            workspace_name=getattr(storage.workspace, "display_name", None),
            group_id=storage.group_id,
            group_name=getattr(storage.group, "display_name", None),
            owner_user_id=storage.owner_user_id,
            owner_username=getattr(storage.owner, "username", None),
            owner_display_name=getattr(storage.owner, "display_name", None),
            capacity_gb=storage.capacity_gb,
            used_gb=storage.used_gb,
            access_mode=storage.access_mode,
            default_mount_path=storage.default_mount_path,
            is_read_only=storage.is_read_only,
            is_active=storage.is_active,
            source=storage.source,
            created_at=storage.created_at,
            updated_at=storage.updated_at,
        )


class StorageDetail(StorageSummary):
    """A volume plus its cluster coordinates."""

    #: Both null for a global share, which has no claim until something mounts it
    #: in a namespace.
    pvc_name: str | None = None
    k8s_namespace: str | None = None
    checked_at: datetime | None = None
    #: Instances currently mounting it. Populated by the detail endpoint so the
    #: "delete" button can be disabled with a reason instead of failing on click.
    attached_instance_ids: list[int] = Field(default_factory=list)
    attached_instance_names: list[str] = Field(default_factory=list)


class StorageCreate(ApiModel):
    """Register a directory as a managed volume.

    The directory is computed from the kind and these coordinates rather than being
    accepted from the caller: the layout is fixed, and a caller-chosen path would be
    a way to point a volume at a directory that is not theirs.
    """

    name: str = Field(min_length=1, max_length=MAX_STORAGE_NAME)
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    kind: StorageKind = StorageKind.USER_VOLUME
    #: Required for a user volume; the group is taken from the user, not from here.
    owner_user_id: int | None = None
    #: Required for a group share.
    group_id: int | None = None
    #: For a group share: the directory inside the group's own directory. Omit it to
    #: make the group's own directory the volume — mountable only by a group
    #: administrator, since it contains every member's home.
    dir_name: str | None = Field(default=None, max_length=255)
    #: Ignored for a global share, and validated against the group's own workspace
    #: otherwise: the claim has to live where the group's workloads run.
    workspace_id: int | None = None
    capacity_gb: int = Field(default=10, ge=1, le=MAX_CAPACITY_GB)
    access_mode: str | None = Field(default=None, max_length=32)
    is_read_only: bool | None = None
    source: str | None = Field(default=None, max_length=2000)


class StorageUpdate(ApiModel):
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None
    is_read_only: bool | None = None
    source: str | None = Field(default=None, max_length=2000)


class StorageResize(ApiModel):
    """Raise a volume's recorded capacity.

    Only the target size is accepted, and only upward. This changes the number the
    platform accounts for, not the cluster: a statically provisioned claim cannot be
    expanded at all, and a hostPath volume's size is not enforced by anything.
    """

    capacity_gb: int = Field(ge=1, le=MAX_CAPACITY_GB)


class StorageUsage(ApiModel):
    """Totals for the page header."""

    count: int
    capacity_gb: int
