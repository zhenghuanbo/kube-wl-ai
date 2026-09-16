"""Workspaces — the tenancy boundary of the platform.

A workspace is exactly one Kubernetes namespace. It belongs to one group, may be
granted several resource groups, and holds at most one user (plan D7): a user
belongs to a single workspace and there is no workspace switcher, because the
underlying storage and namespace are what give the work its identity.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, str_enum
from app.models.enums import WorkspaceRole, WorkspaceStatus

if TYPE_CHECKING:
    from app.models.group import Group
    from app.models.resource import ResourceGroup
    from app.models.user import User


class Workspace(Base, TimestampMixin):
    """One Kubernetes namespace plus the platform metadata around it."""

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)

    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Derived as ``wl-{group}-{name}`` and immutable once created: renaming it
    #: would orphan every object living inside the namespace.
    k8s_namespace: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)

    status: Mapped[WorkspaceStatus] = mapped_column(
        str_enum(WorkspaceStatus, "workspace_status"),
        nullable=False,
        default=WorkspaceStatus.PENDING,
    )
    status_message: Mapped[str | None] = mapped_column(Text)

    #: Denormalised copies of the last applied Kubernetes objects. Kept so the
    #: UI can show "what the cluster should look like" without a live API call,
    #: and so drift can be detected by comparing with a fresh read.
    namespace_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quota_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    group: Mapped[Group] = relationship(lazy="joined")
    members: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    resource_grants: Mapped[list[WorkspaceResourceGrant]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (UniqueConstraint("group_id", "name", name="uq_workspaces_group_name"),)

    def __repr__(self) -> str:
        return f"<Workspace id={self.id} ns={self.k8s_namespace!r}>"


class WorkspaceMember(Base, TimestampMixin):
    """Membership of a user in a workspace.

    ``user_id`` is unique **on its own**, not per workspace: that single
    constraint is what enforces "one user, one workspace" at the database level,
    so no amount of application-layer bug can quietly give someone two.
    """

    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("user_id", name="uq_workspace_members_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_in_workspace: Mapped[WorkspaceRole] = mapped_column(
        str_enum(WorkspaceRole, "workspace_role"),
        nullable=False,
        default=WorkspaceRole.MEMBER,
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    workspace: Mapped[Workspace] = relationship(back_populates="members", lazy="joined")
    #: Eagerly loaded: every member listing renders the user, and a lazy load
    #: inside an async request raises MissingGreenlet.
    user: Mapped[User] = relationship(back_populates="workspace_memberships", lazy="joined")

    def __repr__(self) -> str:
        return f"<WorkspaceMember ws={self.workspace_id} user={self.user_id}>"


class WorkspaceResourceGrant(Base, TimestampMixin):
    """Grants one resource group to one workspace.

    This table is the **only** link between a tenant and a pool of hardware.
    There is deliberately no group↔resource-group table (plan §5.2): sharing a
    resource group means granting it to each workspace that needs it, which
    keeps the audit trail unambiguous about *who* got access and when.
    """

    __tablename__ = "workspace_resource_grants"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "resource_group_id", name="uq_workspace_resource_grants_pair"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource_group_id: Mapped[int] = mapped_column(
        ForeignKey("resource_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    granted_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    workspace: Mapped[Workspace] = relationship(back_populates="resource_grants")
    resource_group: Mapped[ResourceGroup] = relationship(lazy="joined")

    def __repr__(self) -> str:
        return f"<WorkspaceResourceGrant ws={self.workspace_id} rg={self.resource_group_id}>"
