"""Authentication and identity payloads."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.auth import policy
from app.models.enums import GroupRole, RoleScope, UserSource
from app.models.user import User
from app.schemas.base import ApiModel


class LoginRequest(ApiModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=1, max_length=256)


class RefreshRequest(ApiModel):
    """The refresh token normally arrives as an httpOnly cookie.

    The body field exists for non-browser clients that cannot use cookies.
    """

    refresh_token: str | None = None


class DataScopeResponse(ApiModel):
    """The caller's row-level scope, as the policy layer resolved it.

    ``workspace_ids`` is part of the wire format because ``DataScope`` carries
    it and clients need it: it answers "which workspaces may I target" without a
    second request, and it is what ``scope_filter`` actually applies for a plain
    user. Omitting it here meant the value was computed, returned by
    ``to_dict()``, and then quietly dropped on the floor by Pydantic.
    """

    kind: str
    all_access: bool
    group_ids: list[int]
    workspace_ids: list[int]


class GroupMembership(ApiModel):
    """One of the caller's memberships, including their role inside the group."""

    group_id: int
    name: str
    display_name: str
    role_in_group: GroupRole
    is_primary: bool
    is_directory_managed: bool

    @classmethod
    def from_link(cls, link: object) -> GroupMembership:
        return cls(
            group_id=link.group_id,  # type: ignore[attr-defined]
            name=link.group.name,  # type: ignore[attr-defined]
            display_name=link.group.display_name,  # type: ignore[attr-defined]
            role_in_group=link.role_in_group,  # type: ignore[attr-defined]
            is_primary=link.is_primary,  # type: ignore[attr-defined]
            is_directory_managed=link.is_directory_managed,  # type: ignore[attr-defined]
        )


class RoleAssignment(ApiModel):
    code: str
    name: str
    scope: RoleScope
    group_id: int | None = None


class WorkspaceRef(ApiModel):
    """The single workspace a user belongs to (plan D7).

    Populated from Phase 2 onwards; ``None`` until then.
    """

    id: int
    name: str
    group_id: int
    group_name: str


class CurrentUserResponse(ApiModel):
    id: int
    username: str
    email: str | None
    full_name: str
    display_name: str
    source: UserSource
    is_active: bool
    is_superuser: bool
    expires_at: datetime | None
    last_login_at: datetime | None
    roles: list[str]
    groups: list[GroupMembership]
    role_assignments: list[RoleAssignment]
    workspace: WorkspaceRef | None = None

    @classmethod
    def from_user(cls, user: User, *, workspace: WorkspaceRef | None = None) -> CurrentUserResponse:
        return cls(
            id=user.id,
            username=user.username,
            email=user.email,
            full_name=user.full_name,
            display_name=user.display_name,
            source=user.source,
            is_active=user.is_active,
            is_superuser=user.is_superuser,
            expires_at=user.expires_at,
            last_login_at=user.last_login_at,
            roles=policy.effective_role_codes(user),
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
            workspace=workspace,
        )


class TokenResponse(ApiModel):
    """Issued on login and on every refresh.

    ``expires_in`` is what the frontend schedules its silent refresh against.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    expires_at: datetime
    user: CurrentUserResponse


class PermissionsResponse(ApiModel):
    """The complete authorisation state for the caller.

    The frontend uses this purely to decide what to render; the backend remains
    the only enforcement point.
    """

    permissions: list[str]
    roles: list[str]
    scope: DataScopeResponse
    is_global_admin: bool


class ProviderInfo(ApiModel):
    """Which login methods the UI should offer."""

    name: str
    description: str


class LoginOptionsResponse(ApiModel):
    providers: list[ProviderInfo]
    ldap_enabled: bool
    oidc_enabled: bool
