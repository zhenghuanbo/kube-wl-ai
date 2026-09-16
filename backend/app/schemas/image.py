"""Wire shapes for the image catalogue."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.models.enums import ApplicationCategory, ImageKind, ImageVisibility
from app.models.image import Image
from app.schemas.base import ApiModel

MAX_REFERENCE = 512


class ImageSummary(ApiModel):
    """One catalogue entry, as the picker and the list page need it."""

    id: int
    reference: str
    display_name: str
    description: str | None = None
    registry: str
    project: str
    repository: str
    tag: str
    digest: str | None = None
    kind: ImageKind
    visibility: ImageVisibility
    category: ApplicationCategory
    framework: str | None = None
    framework_version: str | None = None
    python_version: str | None = None
    cuda_version: str | None = None
    os_version: str | None = None
    size_bytes: int | None = None
    layer_count: int | None = None
    #: True when the metadata came from the registry, rather than being typed in.
    verified: bool = False
    synced_at: datetime | None = None
    sync_error: str | None = None
    owner_user_id: int | None = None
    owner_username: str | None = None
    workspace_id: int | None = None
    group_id: int | None = None
    is_pinned: bool = False
    is_active: bool = True
    usage_count: int = 0
    created_at: datetime
    updated_at: datetime

    @field_validator("reference", mode="before")
    @classmethod
    def _stringify(cls, value: object) -> object:
        return str(value) if value is not None else value

    @classmethod
    def from_image(cls, image: Image) -> ImageSummary:
        return cls(
            id=image.id,
            reference=image.reference,
            display_name=image.display_name or image.reference,
            description=image.description,
            registry=image.registry_host,
            project=image.project,
            repository=image.repository,
            tag=image.tag,
            digest=image.digest,
            kind=ImageKind(image.kind),
            visibility=ImageVisibility(image.visibility),
            category=ApplicationCategory(image.category),
            framework=image.framework,
            framework_version=image.framework_version,
            python_version=image.python_version,
            cuda_version=image.cuda_version,
            os_version=image.os_version,
            size_bytes=image.size_bytes,
            layer_count=image.layer_count,
            # "Verified" means a sync has seen it; a hand-registered entry is
            # confirmed once, at creation, and then never checked again.
            verified=image.synced_at is not None,
            synced_at=image.synced_at,
            sync_error=image.sync_error,
            owner_user_id=image.owner_user_id,
            owner_username=image.owner.username if image.owner else None,
            workspace_id=image.workspace_id,
            group_id=image.group_id,
            is_pinned=image.is_pinned,
            is_active=image.is_active,
            usage_count=image.usage_count,
            created_at=image.created_at,
            updated_at=image.updated_at,
        )


class ImageDetail(ImageSummary):
    """The same entry with the history behind it."""

    #: Whether the caller may remove it, so the UI does not offer an action that
    #: will be refused. Advisory: the API re-checks on every request.
    can_delete: bool = True
    #: Why not, when `can_delete` is false.
    blocked_reason: str | None = None


class ImageRegister(ApiModel):
    """Add one reference to the catalogue."""

    reference: str = Field(min_length=1, max_length=MAX_REFERENCE)
    display_name: str = Field(default="", max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    category: ApplicationCategory = ApplicationCategory.OTHER
    kind: ImageKind = ImageKind.PERSONAL
    visibility: ImageVisibility = ImageVisibility.PRIVATE
    workspace_id: int | None = None
    group_id: int | None = None
    framework: str | None = Field(default=None, max_length=64)
    framework_version: str | None = Field(default=None, max_length=64)
    python_version: str | None = Field(default=None, max_length=32)
    cuda_version: str | None = Field(default=None, max_length=32)
    os_version: str | None = Field(default=None, max_length=64)
    is_pinned: bool = False

    @field_validator("reference")
    @classmethod
    def _no_whitespace(cls, value: str) -> str:
        # An image reference is a single token. Whitespace here is always a
        # paste accident, and the resulting `ImagePullBackOff` is exactly what
        # this catalogue exists to prevent, so reject it at the door.
        if any(character.isspace() for character in value):
            raise ValueError("镜像地址不能包含空格")
        return value.strip()


class ImageUpdate(ApiModel):
    display_name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    category: ApplicationCategory | None = None
    framework: str | None = Field(default=None, max_length=64)
    framework_version: str | None = Field(default=None, max_length=64)
    python_version: str | None = Field(default=None, max_length=32)
    cuda_version: str | None = Field(default=None, max_length=32)
    os_version: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None
    is_pinned: bool | None = None
    visibility: ImageVisibility | None = None
    kind: ImageKind | None = None


class ImageSyncRequest(ApiModel):
    """Trigger a cataloguing pass."""

    #: Restrict the pass to these Harbor projects. Omitted means every project
    #: the robot account can see.
    projects: list[str] = Field(default_factory=list, max_length=50)


class ImageSyncResult(ApiModel):
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    failed: int = 0
    errors: list[str] = Field(default_factory=list)
    total: int = 0


class RegistryStatusRead(ApiModel):
    reachable: bool
    version: str | None = None
    message: str | None = None
    host: str
    projects: list[str] = Field(default_factory=list)


class ImageUsage(ApiModel):
    """Totals for the header of the images page."""

    count: int
    total_bytes: int
    #: How many entries the caller could not remove, so the page can say so
    #: without fetching each one.
    in_use: int = 0
