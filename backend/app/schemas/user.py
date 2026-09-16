"""User management payloads."""

from __future__ import annotations

from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.auth import policy
from app.auth.passwords import MIN_PASSWORD_LENGTH
from app.models.enums import GroupRole, RoleScope, UserSource
from app.models.user import User
from app.schemas.auth import GroupMembership, RoleAssignment
from app.schemas.base import ApiModel


class UserSummary(ApiModel):
    """A row in the user list."""

    id: int
    username: str
    full_name: str
    display_name: str
    email: str | None
    source: UserSource
    is_active: bool
    is_superuser: bool
    expires_at: datetime | None
    last_login_at: datetime | None
    roles: list[str]
    primary_group_id: int | None = None
    primary_group_name: str | None = None

    @classmethod
    def from_user(cls, user: User) -> UserSummary:
        primary = user.primary_group
        return cls(
            id=user.id,
            username=user.username,
            full_name=user.full_name,
            display_name=user.display_name,
            email=user.email,
            source=user.source,
            is_active=user.is_active,
            is_superuser=user.is_superuser,
            expires_at=user.expires_at,
            last_login_at=user.last_login_at,
            roles=policy.effective_role_codes(user),
            primary_group_id=primary.id if primary else None,
            primary_group_name=primary.display_name if primary else None,
        )


class UserDetail(UserSummary):
    external_id: str | None
    created_at: datetime
    updated_at: datetime
    groups: list[GroupMembership]
    role_assignments: list[RoleAssignment]

    @classmethod
    def from_user(cls, user: User) -> UserDetail:
        summary = UserSummary.from_user(user)
        return cls(
            **summary.model_dump(),
            external_id=user.external_id,
            created_at=user.created_at,
            updated_at=user.updated_at,
            groups=[GroupMembership.from_link(link) for link in user.group_links],
            role_assignments=[
                RoleAssignment(
                    code=link.role.code,
                    name=link.role.name,
                    scope=link.scope,
                    group_id=link.group_id,
                )
                for link in user.role_links
            ],
        )


class UserCreate(ApiModel):
    """Create a local account.

    Directory-backed users are created automatically on first login; this
    endpoint exists for the break-glass accounts an administrator must be able
    to issue without the directory being reachable.
    """

    username: str = Field(min_length=1, max_length=150, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    email: EmailStr | None = None
    full_name: str = Field(default="", max_length=255)
    password: str | None = Field(default=None, min_length=MIN_PASSWORD_LENGTH, max_length=256)
    is_active: bool = True
    expires_at: datetime | None = None
    role_codes: list[str] = Field(default_factory=lambda: ["user"])
    group_ids: list[int] = Field(default_factory=list)

    @field_validator("role_codes")
    @classmethod
    def _no_duplicate_roles(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class UserUpdate(ApiModel):
    """Partial update; only the fields present are applied."""

    email: EmailStr | None = None
    full_name: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None
    expires_at: datetime | None = None
    password: str | None = Field(default=None, min_length=MIN_PASSWORD_LENGTH, max_length=256)


class UserStatusUpdate(ApiModel):
    is_active: bool


class PasswordReset(ApiModel):
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=256)


class UserRoleGrant(ApiModel):
    """Grant a role globally or within one group."""

    role_code: str = Field(min_length=1, max_length=64)
    scope: RoleScope = RoleScope.GLOBAL
    group_id: int | None = None


class UserGroupsUpdate(ApiModel):
    """Replace the user's group memberships.

    ``primary_group_id`` must be one of ``group_ids``; membership flags for
    directory-managed rows are preserved by the service.
    """

    group_ids: list[int]
    primary_group_id: int | None = None
    role_in_group: GroupRole = GroupRole.MEMBER


class UserSelfUpdate(ApiModel):
    """What a user may change about themselves."""

    email: EmailStr | None = None
    full_name: str | None = Field(default=None, max_length=255)
