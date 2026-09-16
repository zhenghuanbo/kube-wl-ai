"""RBAC tables: roles, permissions and their assignments.

This is layer 1 of the permission model (`你能执行这个操作吗？`). Row-level
filtering lives in ``app.auth.policy`` and is not expressed in these tables.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, str_enum
from app.models.enums import RoleScope

if TYPE_CHECKING:
    from app.models.group import Group
    from app.models.user import User


class Role(Base, TimestampMixin):
    """A named bundle of permissions.

    ``code`` is deliberately a plain string rather than an enum column: the four
    business roles are seeded as ``is_builtin`` rows, but operators may add
    custom roles without a schema migration.
    """

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    permission_links: Mapped[list[RolePermission]] = relationship(
        back_populates="role",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Role id={self.id} code={self.code!r}>"


class Permission(Base, TimestampMixin):
    """A single ``resource:action`` capability."""

    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(96), unique=True, index=True, nullable=False)
    category: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<Permission code={self.code!r}>"


class RolePermission(Base):
    """Association between a role and a permission (composite primary key)."""

    __tablename__ = "role_permissions"

    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[int] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )

    role: Mapped[Role] = relationship(back_populates="permission_links")
    permission: Mapped[Permission] = relationship(lazy="joined")


class UserRole(Base, TimestampMixin):
    """Assignment of a role to a user, optionally scoped to one group.

    The ``scope``/``group_id`` pair is what makes ``group_admin`` group-bounded:
    a group-scoped assignment only contributes permissions while the caller is
    acting within that group (see ``app.auth.policy.resolve_scope``).
    """

    __tablename__ = "user_roles"
    __table_args__ = (
        CheckConstraint(
            "(scope = 'global' AND group_id IS NULL) OR (scope = 'group' AND group_id IS NOT NULL)",
            name="scope_group_consistency",
        ),
        UniqueConstraint("user_id", "role_id", "group_id", name="uq_user_roles_user_role_group"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope: Mapped[RoleScope] = mapped_column(
        str_enum(RoleScope, "role_scope"), nullable=False, default=RoleScope.GLOBAL
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )

    user: Mapped[User] = relationship(back_populates="role_links")
    role: Mapped[Role] = relationship(lazy="joined")
    group: Mapped[Group | None] = relationship(lazy="joined")

    def __repr__(self) -> str:
        return (
            f"<UserRole user_id={self.user_id} role_id={self.role_id} "
            f"scope={self.scope} group_id={self.group_id}>"
        )


__all__ = ["Permission", "Role", "RolePermission", "UserRole"]
