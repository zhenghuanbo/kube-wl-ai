"""Storage request and response payloads."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.models.enums import StorageKind, StorageStatus
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

    workspace_id: int
    workspace_name: str | None = None
    group_id: int | None = None
    group_name: str | None = None
    owner_user_id: int | None = None
    owner_username: str | None = None
    owner_display_name: str | None = None

    capacity_gb: int
    used_gb: int
    access_mode: str | None = None
    storage_class: str | None = None
    default_mount_path: str

    #: Derived from both the kind and the row, so the UI never has to reimplement
    #: the rule "a dataset is always read-only".
    is_read_only: bool
    is_active: bool
    source: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_storage(cls, storage: object) -> StorageSummary:
        return cls(
            id=storage.id,  # type: ignore[attr-defined]
            name=storage.name,  # type: ignore[attr-defined]
            display_name=storage.display_name,  # type: ignore[attr-defined]
            description=storage.description,  # type: ignore[attr-defined]
            kind=storage.kind,  # type: ignore[attr-defined]
            status=storage.status,  # type: ignore[attr-defined]
            status_message=storage.status_message,  # type: ignore[attr-defined]
            workspace_id=storage.workspace_id,  # type: ignore[attr-defined]
            workspace_name=getattr(storage.workspace, "display_name", None),  # type: ignore[attr-defined]
            group_id=storage.group_id,  # type: ignore[attr-defined]
            group_name=getattr(storage.group, "display_name", None),  # type: ignore[attr-defined]
            owner_user_id=storage.owner_user_id,  # type: ignore[attr-defined]
            owner_username=getattr(storage.owner, "username", None),  # type: ignore[attr-defined]
            owner_display_name=getattr(storage.owner, "display_name", None),  # type: ignore[attr-defined]
            capacity_gb=storage.capacity_gb,  # type: ignore[attr-defined]
            used_gb=storage.used_gb,  # type: ignore[attr-defined]
            access_mode=storage.access_mode,  # type: ignore[attr-defined]
            storage_class=storage.storage_class,  # type: ignore[attr-defined]
            default_mount_path=storage.default_mount_path,  # type: ignore[attr-defined]
            is_read_only=storage.effective_read_only,  # type: ignore[attr-defined]
            is_active=storage.is_active,  # type: ignore[attr-defined]
            source=storage.source,  # type: ignore[attr-defined]
            created_at=storage.created_at,  # type: ignore[attr-defined]
            updated_at=storage.updated_at,  # type: ignore[attr-defined]
        )


class StorageDetail(StorageSummary):
    """A volume plus its cluster coordinates."""

    pvc_name: str | None = None
    k8s_namespace: str | None = None
    size_bytes: int | None = None
    checked_at: datetime | None = None
    #: Instances currently mounting it. Populated by the detail endpoint so the
    #: "delete" button can be disabled with a reason instead of failing on click.
    attached_instance_ids: list[int] = Field(default_factory=list)
    attached_instance_names: list[str] = Field(default_factory=list)


class StorageCreate(ApiModel):
    name: str = Field(min_length=1, max_length=MAX_STORAGE_NAME)
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    kind: StorageKind = StorageKind.PERSONAL_DATA
    workspace_id: int | None = None
    owner_user_id: int | None = None
    capacity_gb: int = Field(default=10, ge=1, le=MAX_CAPACITY_GB)
    access_mode: str | None = Field(default=None, max_length=32)
    storage_class: str | None = Field(default=None, max_length=64)
    is_read_only: bool | None = None
    source: str | None = Field(default=None, max_length=2000)
    group_id: int | None = None


class StorageUpdate(ApiModel):
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None
    is_read_only: bool | None = None
    source: str | None = Field(default=None, max_length=2000)


class StorageResize(ApiModel):
    """Grow a volume.

    Only the target size is accepted. There is no `shrink` flag, because
    Kubernetes cannot do it and offering the option would produce a form that
    fails on submit.
    """

    capacity_gb: int = Field(ge=1, le=MAX_CAPACITY_GB)


class StorageUsage(ApiModel):
    """Totals for the page header."""

    count: int
    capacity_gb: int
