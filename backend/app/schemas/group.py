"""User-group management payloads."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from app.models.enums import GroupKind, GroupRole
from app.models.group import Group, UserGroupLink
from app.schemas.base import ApiModel


def _blank_means_absent(value: str | None) -> str | None:
    """Treat ``""`` as "no directory name" rather than as a name of spaces.

    The distinction matters because ``None`` is meaningful here: it is what
    reverts a group to matching the directory on ``name``. A form that submits an
    empty text input should produce that, not a group whose directory name is the
    empty string.
    """
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


class GroupSummary(ApiModel):
    id: int
    name: str
    directory_name: str | None
    display_name: str
    description: str | None
    kind: GroupKind
    is_active: bool
    owner_user_id: int | None
    member_count: int = 0
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_group(cls, group: Group, *, member_count: int | None = None) -> GroupSummary:
        return cls(
            id=group.id,
            name=group.name,
            directory_name=group.directory_name,
            display_name=group.display_name,
            description=group.description,
            kind=group.kind,
            is_active=group.is_active,
            owner_user_id=group.owner_user_id,
            member_count=member_count if member_count is not None else len(group.member_links),
            created_at=group.created_at,
            updated_at=group.updated_at,
        )


class GroupCreate(ApiModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    #: The directory group to mirror. Omit when the directory name is already
    #: identical to ``name``, which is the common case.
    directory_name: str | None = Field(default=None, max_length=128)
    display_name: str = Field(default="", max_length=128)
    description: str | None = None
    kind: GroupKind = GroupKind.LAB
    owner_user_id: int | None = None

    _normalise_directory_name = field_validator("directory_name")(_blank_means_absent)


class GroupUpdate(ApiModel):
    #: Explicit ``null`` clears it, reverting the group to matching on ``name``;
    #: omitting the field leaves it alone.
    directory_name: str | None = Field(default=None, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = None
    kind: GroupKind | None = None
    owner_user_id: int | None = None
    is_active: bool | None = None

    _normalise_directory_name = field_validator("directory_name")(_blank_means_absent)


class GroupMemberRead(ApiModel):
    """A member row — the user summary plus their standing in this group."""

    user_id: int
    username: str
    full_name: str
    display_name: str
    email: str | None
    is_active: bool
    role_in_group: GroupRole
    is_primary: bool
    is_directory_managed: bool
    joined_at: datetime

    @classmethod
    def from_link(cls, link: UserGroupLink) -> GroupMemberRead:
        user = link.user
        return cls(
            user_id=link.user_id,
            username=user.username,
            full_name=user.full_name,
            display_name=user.display_name,
            email=user.email,
            is_active=user.is_active,
            role_in_group=link.role_in_group,
            is_primary=link.is_primary,
            is_directory_managed=link.is_directory_managed,
            joined_at=link.created_at,
        )


class GroupMemberAdd(ApiModel):
    user_id: int
    role_in_group: GroupRole = GroupRole.MEMBER
    is_primary: bool = False


class GroupMemberUpdate(ApiModel):
    role_in_group: GroupRole | None = None
    is_primary: bool | None = None


class GroupSyncRequest(ApiModel):
    """Ask the backend to refresh memberships from the directory.

    ``dry_run`` reports what would change without writing anything.
    """

    dry_run: bool = False


class GroupSyncResult(ApiModel):
    directory_groups: list[str] = Field(default_factory=list)
    matched_groups: list[str] = Field(default_factory=list)
    #: Directory groups with no platform counterpart. This is the actionable
    #: part of a reconcile: each name here is a group somebody must create (or
    #: point at with ``directoryName``) before its members can be mapped.
    unmatched_groups: list[str] = Field(default_factory=list)
    memberships_added: int = 0
    memberships_removed: int = 0
    memberships_updated: int = 0
    #: ``users_created`` used to live here and was always 0. The reconcile
    #: deliberately never creates accounts — directory users are provisioned on
    #: first login, and members it cannot find are reported in ``errors`` — so
    #: the field only invited the question "why is this never non-zero".
    errors: list[str] = Field(default_factory=list)
    dry_run: bool = False
