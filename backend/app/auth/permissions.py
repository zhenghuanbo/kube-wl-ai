"""Permission catalogue and RBAC seeding.

This module is the **single source of truth** for permission codes and the
role→permission mapping. `sync_rbac` is idempotent: it is called from the
initial Alembic migration and may be re-run at any time (admin CLI, startup
task) to pick up newly added permissions without a schema migration.

Layer 1 of the permission model only — row-level data scope lives in
``app.auth.policy``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlalchemy import Connection, Table, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import RoleCode
from app.models.rbac import Permission, Role, RolePermission


@dataclass(frozen=True, slots=True)
class PermissionSpec:
    code: str
    category: str
    description: str


def _codes(specs: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in specs.split() if part.strip())


# --- Catalogue ---------------------------------------------------------------
# Codes follow the `resource:action` convention and are used verbatim by both
# the backend policy layer and the frontend `usePermissions()` composable.
PERMISSIONS: tuple[PermissionSpec, ...] = (
    # Identity ---------------------------------------------------------------
    PermissionSpec("user:read", "user", "查看用户"),
    PermissionSpec("user:create", "user", "创建用户"),
    PermissionSpec("user:update", "user", "修改用户"),
    PermissionSpec("user:delete", "user", "删除用户"),
    PermissionSpec("user:assign_role", "user", "分配角色"),
    # Groups ------------------------------------------------------------------
    PermissionSpec("group:read", "group", "查看用户组"),
    PermissionSpec("group:create", "group", "创建用户组"),
    PermissionSpec("group:update", "group", "修改用户组"),
    PermissionSpec("group:delete", "group", "删除用户组"),
    PermissionSpec("group:manage_member", "group", "管理组成员"),
    # RBAC catalogue (read-only: the catalogue is seeded, not edited at runtime)
    PermissionSpec("role:read", "rbac", "查看角色及其权限"),
    PermissionSpec("permission:read", "rbac", "查看权限清单"),
    # Workspaces --------------------------------------------------------------
    PermissionSpec("workspace:read", "workspace", "查看工作空间"),
    PermissionSpec("workspace:create", "workspace", "创建工作空间"),
    PermissionSpec("workspace:update", "workspace", "修改工作空间"),
    PermissionSpec("workspace:delete", "workspace", "删除工作空间"),
    PermissionSpec("workspace:assign_member", "workspace", "分配工作空间成员"),
    PermissionSpec("workspace:grant_resource", "workspace", "授权资源组给工作空间"),
    # Compute resources -------------------------------------------------------
    PermissionSpec("resource_group:read", "resource", "查看资源组"),
    PermissionSpec("resource_group:create", "resource", "创建资源组"),
    PermissionSpec("resource_group:update", "resource", "修改资源组"),
    PermissionSpec("resource_group:delete", "resource", "删除资源组"),
    PermissionSpec("resource_spec:read", "resource", "查看资源规格"),
    PermissionSpec("resource_spec:create", "resource", "创建资源规格"),
    PermissionSpec("resource_spec:update", "resource", "修改资源规格"),
    PermissionSpec("resource_spec:delete", "resource", "删除资源规格"),
    PermissionSpec("runtime_limit:read", "resource", "查看时长限制"),
    PermissionSpec("runtime_limit:create", "resource", "创建时长限制"),
    PermissionSpec("runtime_limit:update", "resource", "修改时长限制"),
    PermissionSpec("runtime_limit:delete", "resource", "删除时长限制"),
    PermissionSpec("node:read", "resource", "查看计算节点"),
    PermissionSpec("node:sync", "resource", "同步集群节点"),
    PermissionSpec("quota:read", "resource", "查看配额"),
    PermissionSpec("quota:update", "resource", "调整配额"),
    #: Compute quota profiles. Readable by a group administrator so a workspace's
    #: ceiling can be explained to whoever asks; every write is global-admin only,
    #: because these rows decide what *every* tenant is allowed.
    PermissionSpec("quota_profile:read", "resource", "查看配额模板"),
    PermissionSpec("quota_profile:create", "resource", "创建配额模板"),
    PermissionSpec("quota_profile:update", "resource", "编辑配额模板"),
    PermissionSpec("quota_profile:delete", "resource", "删除配额模板"),
    #: Attaching a template to a user or a workspace is the step that changes
    #: somebody's ceiling without changing the template, so it is its own code
    #: rather than riding on `user:update` or `workspace:update` — a group
    #: administrator may rename a workspace without being able to re-quota it.
    PermissionSpec("quota_profile:assign", "resource", "分配配额模板"),
    # Images ------------------------------------------------------------------
    PermissionSpec("image:read", "image", "查看镜像"),
    PermissionSpec("image:create", "image", "登记/制作镜像"),
    PermissionSpec("image:share", "image", "共享镜像"),
    PermissionSpec("image:delete", "image", "删除镜像"),
    # Storage -----------------------------------------------------------------
    PermissionSpec("storage:read", "storage", "查看存储"),
    #: One code for every mutation. There is deliberately no per-user storage code
    #: any more: a volume is a directory in an installation-wide layout provisioned
    #: outside the platform, so placing one is not something a tenant may do.
    PermissionSpec("storage:manage", "storage", "创建、修改、删除存储卷"),
    # Instances ---------------------------------------------------------------
    PermissionSpec("instance:read", "instance", "查看容器实例"),
    PermissionSpec("instance:create", "instance", "创建容器实例"),
    PermissionSpec("instance:update", "instance", "修改容器实例"),
    PermissionSpec("instance:delete", "instance", "删除容器实例"),
    PermissionSpec("instance:start", "instance", "启动容器实例"),
    PermissionSpec("instance:stop", "instance", "停止容器实例"),
    PermissionSpec("instance:exec", "instance", "进入实例终端"),
    # Applications ------------------------------------------------------------
    PermissionSpec("application:read", "application", "查看应用程序"),
    PermissionSpec("application:create", "application", "创建应用程序"),
    PermissionSpec("application:update", "application", "修改应用程序"),
    PermissionSpec("application:delete", "application", "删除应用程序"),
    PermissionSpec("application:stop", "application", "停止应用程序"),
    # Tasks -------------------------------------------------------------------
    PermissionSpec("task:read", "task", "查看任务"),
    PermissionSpec("task:create", "task", "提交任务"),
    PermissionSpec("task:cancel", "task", "取消任务"),
    PermissionSpec("task:logs", "task", "查看任务日志"),
    # Usage -------------------------------------------------------------------
    # One read code, not three. How *much* usage a caller sees is a data-scope
    # question (own rows → whole groups → everything), and the scope already
    # derives that from the same group/role facts. A `read_group` / `read_all`
    # pair sat here and was checked by nothing: the endpoints gate on
    # `read_self` and `scope_filter` does the tiering, so granting `read_all` to
    # a plain user changed nothing and a group admin holding only `read_self`
    # still saw their whole group. Monitoring keeps its three codes because they
    # genuinely gate different dashboards; usage has one page, so breadth belongs
    # to the scope.
    PermissionSpec("usage:read_self", "usage", "查看用量（范围由数据权限决定）"),
    PermissionSpec("usage:export", "usage", "导出用量数据"),
    PermissionSpec("usage:manage", "usage", "重算用量聚合"),
    # Monitoring --------------------------------------------------------------
    PermissionSpec("monitor:read_self", "monitor", "查看本人监控"),
    PermissionSpec("monitor:read_group", "monitor", "查看本组监控"),
    PermissionSpec("monitor:read_all", "monitor", "查看全部监控"),
    # API tokens (self-service) -----------------------------------------------
    # These are self-service and granted to every role, including observer. The
    # reason is that a token cannot exceed its owner: `effective_permissions`
    # intersects the token's declared scopes with the user's *current* roles, so
    # an observer's token reads and nothing more. Gating the self-service
    # endpoints behind a privilege would mean only administrators could automate
    # anything, which is the opposite of what an open API is for.
    PermissionSpec("api_token:read", "api_token", "查看本人的 API 令牌"),
    PermissionSpec("api_token:create", "api_token", "创建 API 令牌"),
    PermissionSpec("api_token:delete", "api_token", "吊销 API 令牌"),
    # System ------------------------------------------------------------------
    PermissionSpec("audit:read", "system", "查看审计日志"),
    PermissionSpec("system:config", "system", "系统配置"),
)

ALL_PERMISSION_CODES: frozenset[str] = frozenset(spec.code for spec in PERMISSIONS)


# --- Role definitions --------------------------------------------------------
ROLE_DEFINITIONS: dict[RoleCode, tuple[str, str]] = {
    RoleCode.OBSERVER: ("观察用户", "只读访问，不能提交任何任务或修改任何资源"),
    RoleCode.USER: ("普通用户", "管理自己的实例、任务、镜像与存储"),
    RoleCode.GROUP_ADMIN: ("组管理员", "在所属用户组内管理成员、工作空间与资源授权"),
    RoleCode.GLOBAL_ADMIN: ("全局管理员", "平台超级管理员，拥有全部权限"),
}


# --- Role → permission mapping ----------------------------------------------
# `observer` is intentionally read-only on *shared* resources only: it has no
# access to user/group/workspace/quota/audit APIs, matching the UI matrix in
# plan §6.
# Directory reads are safe for every role because the data-scope layer limits
# the rows: a plain user ends up seeing only themselves and the groups they
# belong to, which is what makes "my lab's members" work without exposing the
# rest of the institution.
# NOTE: `_codes` splits this literal on whitespace and does **not** strip
# comments — never put a `#` line inside it.
#
# `workspace:read` is granted to every role because a user must be able to see
# the one workspace they belong to; the data-scope layer narrows the result to
# exactly that. `quota:read` joins it for normal users so they can see their own
# consumption.
#
# `usage:export` is a group-admin capability rather than a user one. Reading a
# figure on a page and walking away with a file of every colleague's daily
# consumption are different acts, and the export is the one that ends up
# attached to a grant application. `usage:manage` is global-admin only: it
# rewrites rows for every tenant at once and there is no group-scoped version of
# that act.
_OBSERVER = frozenset(
    _codes(
        """
        instance:read application:read task:read image:read storage:read
        usage:read_self monitor:read_self
        user:read group:read
        workspace:read
        api_token:read api_token:create api_token:delete
        """
    )
)

# A normal user additionally sees the catalogue they need to build workloads
# (resource groups / specs / limits are read-only for them).
#
# Storage is deliberately absent: they hold `storage:read` from `_OBSERVER` and
# nothing more. Their own volume is mounted into everything they run, so they never
# need to create one, and a group share is placed for them rather than by them.
_USER = _OBSERVER | frozenset(
    _codes(
        """
        instance:create instance:update instance:delete instance:start
        instance:stop instance:exec
        application:create application:update application:delete application:stop
        task:create task:cancel task:logs
        image:create image:delete
        resource_group:read resource_spec:read runtime_limit:read
        quota:read
        """
    )
)

# A group administrator governs people, workspaces and their group's images — but
# not storage. Even the group's *own* directory, which is the volume they exist to
# be able to mount, is created for them by a global administrator: `storage:manage`
# is deliberately not granted here, because the directory layout is provisioned
# outside the platform and changing it is not a per-group decision.
_GROUP_ADMIN = _USER | frozenset(
    _codes(
        """
        user:read group:read
        workspace:read workspace:create workspace:update
        workspace:assign_member workspace:grant_resource
        image:share
        resource_group:read resource_spec:read runtime_limit:read
        node:read quota:read quota_profile:read
        usage:export monitor:read_group audit:read
        role:read permission:read
        """
    )
)

ROLE_PERMISSIONS: dict[RoleCode, frozenset[str]] = {
    RoleCode.OBSERVER: _OBSERVER,
    RoleCode.USER: _USER,
    RoleCode.GROUP_ADMIN: _GROUP_ADMIN,
    RoleCode.GLOBAL_ADMIN: ALL_PERMISSION_CODES,
}


@dataclass(slots=True)
class SyncResult:
    roles_created: int = 0
    roles_updated: int = 0
    permissions_created: int = 0
    permissions_updated: int = 0
    permissions_removed: int = 0
    grants_added: int = 0
    grants_removed: int = 0

    def __str__(self) -> str:
        return (
            f"roles +{self.roles_created}/~{self.roles_updated}, "
            f"permissions +{self.permissions_created}/~{self.permissions_updated}"
            f"/-{self.permissions_removed}, "
            f"grants +{self.grants_added}/-{self.grants_removed}"
        )


def seed_rbac(connection: Connection) -> SyncResult:
    """Reconcile the RBAC tables with this module's catalogue.

    Implemented against a SQLAlchemy Core :class:`Connection` so the exact same
    code runs from the Alembic data migration and from the running application.
    It never commits: the caller owns the transaction.

    Safe to run repeatedly — existing rows are updated in place, missing rows are
    created, grants no longer in the mapping are removed, and permissions retired
    from the catalogue are deleted.
    """
    result = SyncResult()

    # `__table__` is typed as FromClause on declarative classes; the concrete
    # Table is what the Core statement builders need.
    roles_t = cast("Table", Role.__table__)
    permissions_t = cast("Table", Permission.__table__)
    grants_t = cast("Table", RolePermission.__table__)

    # --- Roles ---------------------------------------------------------------
    existing_roles = {row["code"]: row for row in connection.execute(select(roles_t)).mappings()}
    missing_roles = [
        {
            "code": role_code.value,
            "name": name,
            "description": description,
            "is_builtin": True,
        }
        for role_code, (name, description) in ROLE_DEFINITIONS.items()
        if role_code.value not in existing_roles
    ]
    if missing_roles:
        connection.execute(insert(roles_t), missing_roles)
        result.roles_created = len(missing_roles)

    for role_code, (name, description) in ROLE_DEFINITIONS.items():
        role_row = existing_roles.get(role_code.value)
        if role_row is None:
            continue
        if (role_row["name"], role_row["description"], role_row["is_builtin"]) != (
            name,
            description,
            True,
        ):
            connection.execute(
                update(roles_t)
                .where(roles_t.c.id == role_row["id"])
                .values(name=name, description=description, is_builtin=True)
            )
            result.roles_updated += 1

    role_ids: dict[str, int] = {
        row["code"]: row["id"] for row in connection.execute(select(roles_t)).mappings()
    }

    # --- Permissions ---------------------------------------------------------
    existing_perms = {
        row["code"]: row for row in connection.execute(select(permissions_t)).mappings()
    }
    missing_perms = [
        {
            "code": spec.code,
            "category": spec.category,
            "description": spec.description,
        }
        for spec in PERMISSIONS
        if spec.code not in existing_perms
    ]
    if missing_perms:
        connection.execute(insert(permissions_t), missing_perms)
        result.permissions_created = len(missing_perms)

    for spec in PERMISSIONS:
        perm_row = existing_perms.get(spec.code)
        if perm_row is None:
            continue
        if (perm_row["category"], perm_row["description"]) != (
            spec.category,
            spec.description,
        ):
            connection.execute(
                update(permissions_t)
                .where(permissions_t.c.id == perm_row["id"])
                .values(category=spec.category, description=spec.description)
            )
            result.permissions_updated += 1

    # --- Retired permissions -------------------------------------------------
    #
    # The catalogue in this module is the source of truth for *which* codes exist;
    # the table only records what they are called. Nothing else creates a
    # `Permission` row — `GET /rbac/permissions` is read-only — so a row whose code
    # is no longer in the catalogue is not somebody's extension, it is a leftover
    # from a code that was deliberately removed.
    #
    # Leaving it would be worse than untidy: the admin permission matrix is built
    # from this table, so a retired code keeps being offered for granting, and the
    # API would honour it if it were granted — no endpoint requires it any more, so
    # it would grant nothing. A code that can be handed out and does nothing is
    # exactly the kind of quiet lie this catalogue exists to prevent.
    #
    # Grants are reconciled below and the FK cascades, so deleting the row also
    # removes any grant of it.
    retired_ids = [
        row["id"] for code, row in existing_perms.items() if code not in ALL_PERMISSION_CODES
    ]
    if retired_ids:
        connection.execute(delete(permissions_t).where(permissions_t.c.id.in_(retired_ids)))
        result.permissions_removed = len(retired_ids)

    perm_ids: dict[str, int] = {
        row["code"]: row["id"] for row in connection.execute(select(permissions_t)).mappings()
    }

    # --- Role → permission grants -------------------------------------------
    granted: dict[int, set[int]] = {}
    for role_id, permission_id in connection.execute(
        select(grants_t.c.role_id, grants_t.c.permission_id)
    ).all():
        granted.setdefault(role_id, set()).add(permission_id)

    for role_code, codes in ROLE_PERMISSIONS.items():
        unknown = codes - ALL_PERMISSION_CODES
        if unknown:  # pragma: no cover - guarded by the module-level literal
            raise ValueError(f"role {role_code} references unknown permissions: {sorted(unknown)}")

        target_role_id = role_ids[role_code.value]
        desired = {perm_ids[code] for code in codes}
        current_grants = granted.get(target_role_id, set())

        to_add = desired - current_grants
        if to_add:
            connection.execute(
                insert(grants_t),
                [{"role_id": target_role_id, "permission_id": pid} for pid in to_add],
            )
            result.grants_added += len(to_add)

        to_remove = current_grants - desired
        if to_remove:
            connection.execute(
                delete(grants_t).where(
                    grants_t.c.role_id == target_role_id,
                    grants_t.c.permission_id.in_(to_remove),
                )
            )
            result.grants_removed += len(to_remove)

    return result


async def sync_rbac(session: AsyncSession) -> SyncResult:
    """Async wrapper around :func:`seed_rbac` for application use.

    Runs on the session's own connection so the caller keeps transaction
    control (commit/rollback remains the caller's responsibility).
    """
    return await session.run_sync(lambda sync_session: seed_rbac(sync_session.connection()))
