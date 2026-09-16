"""The permission policy engine.

Three cooperating layers (plan §5.2)::

    L1 RBAC         effective_permissions() / has_permission()   - can you act?
    L2 Data scope   resolve_scope() / scope_filter()            - on which rows?
    L3 Object       ensure_object_access()                      - this exact row?

This module is deliberately free of FastAPI imports so it can be unit-tested as
plain functions; the dependency-injection wrappers live in ``app.core.deps``.

Everything the rest of the codebase needs is reachable through the four public
entry points named above. That is the seam that lets Casbin/OPA replace the
implementation later without touching a single caller.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, false, or_, select

from app.auth.permissions import ALL_PERMISSION_CODES
from app.core.errors import NotFoundError, PermissionDeniedError
from app.core.logging import get_logger
from app.models.enums import RoleCode, RoleScope
from app.models.group import Group, UserGroupLink
from app.models.instance import Instance
from app.models.rbac import UserRole
from app.models.resource import (
    ComputeNode,
    GpuDevice,
    ResourceGroup,
    ResourceSpec,
    RuntimeLimit,
)
from app.models.token import ApiToken, RefreshToken
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember, WorkspaceResourceGrant

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# L1 — functional permissions (RBAC)
# ---------------------------------------------------------------------------
def effective_permissions(user: User) -> frozenset[str]:
    """The union of permission codes granted by all of the user's roles.

    A superuser bypasses the mapping entirely. Group-scoped role assignments
    contribute here too: *whether* the user may perform the action is a function
    of their roles, *where* they may do it is decided by the data scope below.
    """
    if user.is_superuser:
        return ALL_PERMISSION_CODES

    codes: set[str] = set()
    for link in user.role_links:
        # `role` is non-nullable (FK), so no defensive check is needed here.
        codes.update(grant.permission.code for grant in link.role.permission_links)
    return frozenset(codes)


def has_permission(user: User, code: str) -> bool:
    return code in effective_permissions(user)


def require_permission(user: User, code: str) -> None:
    """Raise ``PermissionDeniedError`` unless the user holds ``code``."""
    if not has_permission(user, code):
        raise PermissionDeniedError(
            f"缺少权限：{code}",
            code="permission_denied",
            detail={"required_permission": code},
        )


def global_role_codes(user: User) -> frozenset[str]:
    """Role codes assigned at global scope (used for UI capability reporting)."""
    return frozenset(
        link.role.code
        for link in user.role_links
        if link.scope == RoleScope.GLOBAL and link.role is not None
    )


def is_global_admin(user: User) -> bool:
    return user.is_superuser or RoleCode.GLOBAL_ADMIN.value in global_role_codes(user)


#: Display/sort order — most privileged first, matching the role hierarchy.
_ROLE_ORDER: dict[str, int] = {
    RoleCode.GLOBAL_ADMIN.value: 0,
    RoleCode.GROUP_ADMIN.value: 1,
    RoleCode.USER.value: 2,
    RoleCode.OBSERVER.value: 3,
}


def effective_role_codes(user: User) -> list[str]:
    """The role codes that describe this user, for the UI.

    A global admin is reported as exactly that — listing their inherited
    capabilities alongside would only invite the frontend to re-derive
    permissions, which it must never do.
    """
    if is_global_admin(user):
        return [RoleCode.GLOBAL_ADMIN.value]

    codes = set(global_role_codes(user))
    if (
        any(
            link.role is not None and link.role.code == RoleCode.GROUP_ADMIN.value
            for link in user.role_links
        )
        or user.admin_group_ids
    ):
        codes.add(RoleCode.GROUP_ADMIN.value)

    return sorted(codes, key=lambda code: _ROLE_ORDER.get(code, 99))


# ---------------------------------------------------------------------------
# L2 — data scope (row level)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DataScope:
    """Which rows a user is allowed to see.

    ``all_access`` short-circuits everything. Otherwise a row is visible when it
    belongs to the user (``user_id``) **or** to one of ``group_ids`` — the union
    matters because a user can be a plain member globally while administering
    exactly one group.
    """

    user_id: int
    all_access: bool = False
    group_ids: frozenset[int] = frozenset()
    #: Workspaces the user is a *member* of. Separate from ``group_ids``: being
    #: in a lab does not let you see your colleagues' workspaces, but being in a
    #: workspace does let you see everything inside it.
    workspace_ids: frozenset[int] = frozenset()

    @property
    def kind(self) -> str:
        if self.all_access:
            return "all"
        return "group" if self.group_ids else "self"

    def to_dict(self) -> dict[str, Any]:
        """Wire representation for ``/auth/permissions``."""
        return {
            "kind": self.kind,
            "all_access": self.all_access,
            "group_ids": sorted(self.group_ids),
            "workspace_ids": sorted(self.workspace_ids),
        }


def resolve_scope(user: User) -> DataScope:
    """Compute the user's data scope.

    Group-admin authority comes from two places and both are honoured:
    a group-scoped ``group_admin`` role assignment, and the ``group_admin``
    flag on the group membership itself. They are kept in sync by the API, but
    a discrepancy must never silently *widen* access, so the union is only ever
    used to grant scope the user was explicitly given in one of the two.
    """
    if is_global_admin(user):
        return DataScope(user_id=user.id, all_access=True)

    admin_groups: set[int] = set(user.admin_group_ids)
    for link in user.role_links:
        if (
            link.scope == RoleScope.GROUP
            and link.group_id is not None
            and link.role is not None
            and link.role.code in {RoleCode.GROUP_ADMIN.value, RoleCode.GLOBAL_ADMIN.value}
        ):
            admin_groups.add(link.group_id)

    member_workspaces = frozenset(link.workspace_id for link in user.workspace_memberships)
    return DataScope(
        user_id=user.id,
        group_ids=frozenset(admin_groups),
        workspace_ids=member_workspaces,
    )


# ---------------------------------------------------------------------------
# Model → scope column mapping
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ScopeRule:
    """Where to find the ownership columns on a model.

    ``owner``    — column holding the owning user id
    ``group``    — column holding a group id directly
    ``workspace``— column holding a workspace id (its group is derived)
    ``via_user`` — the model *is* a user link table; match on its ``user_id``
    """

    owner: str | None = None
    group: str | None = None
    workspace: str | None = None
    via_user: str | None = None
    #: Column holding a group id the caller is a *member* of (not just admin of).
    member_of: str | None = None
    #: Column holding a workspace id the caller is a *member* of. Applies at
    #: every scope, which is what lets a plain user see their own workspace's
    #: objects without being an administrator of anything.
    member_workspace: str | None = None
    #: Catalogue data with no per-tenant rows (resource groups, specs, nodes).
    #: Readable by any authenticated user; the *grant* decides what they may
    #: actually schedule on, and that is an object-level check.
    public: bool = False


#: Explicit rules. Models added in later phases must be registered here; the
#: fallback inference below is a safety net, not a substitute.
SCOPE_RULES: dict[type[Any], ScopeRule] = {
    # A user always sees itself; a group admin additionally sees their group's
    # members (`owner` and `via_user` are OR'd together in `scope_filter`).
    User: ScopeRule(owner="id", via_user="id"),
    Group: ScopeRule(group="id", member_of="id"),
    UserGroupLink: ScopeRule(owner="user_id", group="group_id"),
    UserRole: ScopeRule(owner="user_id", group="group_id"),
    RefreshToken: ScopeRule(owner="user_id"),
    ApiToken: ScopeRule(owner="user_id"),
    # --- Phase 2 ---------------------------------------------------------
    # A user sees the workspace they belong to; a group admin sees every
    # workspace of the groups they administer.
    Workspace: ScopeRule(group="group_id", member_workspace="id"),
    WorkspaceMember: ScopeRule(
        owner="user_id", workspace="workspace_id", member_workspace="workspace_id"
    ),
    WorkspaceResourceGrant: ScopeRule(workspace="workspace_id", member_workspace="workspace_id"),
    # Catalogue and infrastructure rows: readable by anyone authenticated, and
    # the endpoints that expose them are gated by `require()`.
    ResourceGroup: ScopeRule(public=True),
    ResourceSpec: ScopeRule(public=True),
    RuntimeLimit: ScopeRule(public=True),
    ComputeNode: ScopeRule(public=True),
    GpuDevice: ScopeRule(public=True),
    # --- Phase 3 ---------------------------------------------------------
    # An instance is visible to its owner, to their group's administrators, and
    # to fellow members of the same workspace.
    Instance: ScopeRule(
        owner="owner_user_id", workspace="workspace_id", member_workspace="workspace_id"
    ),
}

#: Column names consulted when a model has no explicit rule.
_INFERRED_OWNER_COLUMNS = ("owner_user_id", "user_id", "created_by")
_INFERRED_GROUP_COLUMNS = ("group_id",)
_INFERRED_WORKSPACE_COLUMNS = ("workspace_id",)

_warned_models: set[str] = set()

#: Model used to resolve "which workspaces belong to these groups". Registered
#: here at import time; the hook remains so tests and later phases can swap it.
_workspace_model: Any | None = Workspace


def register_workspace_model(model: type[Any]) -> None:
    """Tell the policy engine which model resolves workspace → group."""
    global _workspace_model
    _workspace_model = model


def register_scope_rule(model: type[Any], rule: ScopeRule) -> None:
    """Register (or override) the scope rule for a model."""
    SCOPE_RULES[model] = rule


def _rule_for(model: type[Any]) -> ScopeRule | None:
    rule = SCOPE_RULES.get(model)
    if rule is not None:
        return rule

    columns = getattr(model, "__table__", None)
    if columns is None:
        return None
    names = set(columns.columns.keys())

    owner = next((name for name in _INFERRED_OWNER_COLUMNS if name in names), None)
    group = next((name for name in _INFERRED_GROUP_COLUMNS if name in names), None)
    workspace = next((name for name in _INFERRED_WORKSPACE_COLUMNS if name in names), None)

    if owner is None and group is None and workspace is None:
        return None

    inferred = ScopeRule(owner=owner, group=group, workspace=workspace)
    logger.debug("scope_rule_inferred", model=model.__name__, rule=inferred)
    return inferred


def scope_filter(stmt: Select[Any], model: type[Any], user: User) -> Select[Any]:
    """Restrict a SELECT to the rows this user may see.

    This *must* be applied to every list/lookup query. When no rule can be
    determined the statement is restricted to nothing (fail closed) and a
    warning is logged — silently returning everything would be a data leak.
    """
    scope = resolve_scope(user)
    if scope.all_access:
        return stmt

    rule = _rule_for(model)
    if rule is None:
        if model.__name__ not in _warned_models:
            _warned_models.add(model.__name__)
            logger.warning("scope_filter_no_rule", model=model.__name__)
        return stmt.where(false())

    if rule.public:
        return stmt

    clauses: list[Any] = []

    if rule.owner:
        clauses.append(getattr(model, rule.owner) == scope.user_id)

    if rule.group and scope.group_ids:
        clauses.append(getattr(model, rule.group).in_(scope.group_ids))

    if rule.workspace and scope.group_ids:
        if _workspace_model is None:
            # No workspace model registered yet (pre-Phase 2) — fail closed.
            logger.warning("scope_workspace_model_missing", model=model.__name__)
            return stmt.where(false())
        clauses.append(
            getattr(model, rule.workspace).in_(
                select(_workspace_model.id).where(_workspace_model.group_id.in_(scope.group_ids))
            )
        )

    if rule.via_user and scope.group_ids:
        clauses.append(
            getattr(model, rule.via_user).in_(
                select(UserGroupLink.user_id).where(UserGroupLink.group_id.in_(scope.group_ids))
            )
        )

    if rule.member_of:
        # Applies regardless of administrative scope: belonging to a group is
        # always enough to see it, which is what makes "my lab" work.
        clauses.append(
            getattr(model, rule.member_of).in_(
                select(UserGroupLink.group_id).where(UserGroupLink.user_id == scope.user_id)
            )
        )

    if rule.member_workspace:
        # Unconditional, like `member_of`: belonging to a workspace is always
        # enough to see what lives in it.
        clauses.append(
            getattr(model, rule.member_workspace).in_(
                select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == scope.user_id)
            )
        )

    if not clauses:
        # e.g. a group-scoped rule for a user who administers no group.
        return stmt.where(false())

    return stmt.where(or_(*clauses))


# ---------------------------------------------------------------------------
# L3 — object level
# ---------------------------------------------------------------------------
def ensure_object_access(
    user: User,
    obj: Any,
    *,
    group_id: int | None = None,
    scope: DataScope | None = None,
    not_found: bool = True,
) -> None:
    """Verify the caller may act on one specific row.

    ``group_id`` lets a caller pass an already-resolved group (typically derived
    from a workspace) so no extra query is needed here.

    By default a row outside the caller's scope raises ``NotFoundError`` rather
    than ``ForbiddenError``: resource ids are sequential and guessing them must
    not reveal which ones exist. Pass ``not_found=False`` for actions where the
    distinction is useful to the user.
    """
    scope = scope or resolve_scope(user)
    if scope.all_access:
        return

    rule = _rule_for(type(obj))

    allowed = False
    if rule is not None:
        if rule.owner and getattr(obj, rule.owner, None) == scope.user_id:
            allowed = True
        if not allowed and rule.group and getattr(obj, rule.group, None) in scope.group_ids:
            allowed = True
        if not allowed and rule.via_user and getattr(obj, rule.via_user, None) == scope.user_id:
            allowed = True
        # Membership of the workspace the row lives in, at any administrative
        # scope — the same rule `scope_filter` applies to lists, so a row that
        # appears in a listing is always fetchable by id.
        if not allowed and rule.member_workspace:
            row_workspace = getattr(obj, rule.member_workspace, None)
            if row_workspace is not None and row_workspace in scope.workspace_ids:
                allowed = True

    if not allowed and group_id is not None and group_id in scope.group_ids:
        allowed = True

    if allowed:
        return

    if not_found:
        raise NotFoundError()
    raise PermissionDeniedError("无权访问该资源")


def ensure_group_access(user: User, group_id: int, *, scope: DataScope | None = None) -> None:
    """Verify the caller may act within ``group_id`` (e.g. create a workspace)."""
    scope = scope or resolve_scope(user)
    if scope.all_access or group_id in scope.group_ids:
        return
    raise PermissionDeniedError("无权操作该用户组", detail={"group_id": group_id})


def ensure_group_visible(user: User, group_id: int, *, scope: DataScope | None = None) -> None:
    """Verify the caller may *see* a group and its members.

    Being a member is enough to see the group's roster — unlike
    :func:`ensure_group_access`, which is about administering it.
    """
    scope = scope or resolve_scope(user)
    if scope.all_access or group_id in scope.group_ids or group_id in user.group_ids:
        return
    # 404 rather than 403: group ids are sequential and must not be probed.
    raise NotFoundError()


def ensure_owner_or_admin(user: User, owner_user_id: int) -> None:
    """For actions only the owner (or an administrator) may perform."""
    scope = resolve_scope(user)
    if scope.all_access or scope.user_id == owner_user_id:
        return
    raise PermissionDeniedError("仅资源所有者或管理员可执行该操作")


#: Type alias for the callables handed to FastAPI's dependency system.
PermissionCheck = Callable[[User], None]
