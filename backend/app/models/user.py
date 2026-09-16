"""The user identity model."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, str_enum
from app.models.enums import GroupRole, UserSource

if TYPE_CHECKING:
    from app.models.audit import AuditLog
    from app.models.group import Group, UserGroupLink
    from app.models.rbac import UserRole
    from app.models.ssh_key import SshKey
    from app.models.token import ApiToken, RefreshToken
    from app.models.workspace import WorkspaceMember


class User(Base, TimestampMixin):
    """A platform identity.

    Rows are JIT-provisioned on first successful LDAP/OIDC login; the
    break-glass local account has ``password_hash`` set.

    ``expires_at`` comes from the old system's ``ab_user.expire_time`` and is
    enforced at authentication time, not only at the UI layer.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(150), unique=True, index=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    source: Mapped[UserSource] = mapped_column(
        str_enum(UserSource, "user_source"), nullable=False, default=UserSource.LOCAL
    )
    #: Identifier in the upstream directory (LDAP DN, OIDC ``sub``).
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    #: Only set for local accounts; external identities are never stored here.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    group_links: Mapped[list[UserGroupLink]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    role_links: Mapped[list[UserRole]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    ssh_keys: Mapped[list[SshKey]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    api_tokens: Mapped[list[ApiToken]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    audit_logs: Mapped[list[AuditLog]] = relationship(back_populates="actor")
    #: A user belongs to at most one workspace, but this is a collection because
    #: the unique constraint — not the cardinality of the attribute — is what
    #: enforces that, and a collection keeps the relationship conventional.
    workspace_memberships: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # --- Derived state -------------------------------------------------------
    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= datetime.now(UTC)

    @property
    def is_usable(self) -> bool:
        """Whether the account may be used to authenticate right now."""
        return self.is_active and not self.is_expired

    @property
    def display_name(self) -> str:
        return self.full_name or self.username

    @property
    def primary_group(self) -> Group | None:
        for link in self.group_links:
            if link.is_primary:
                return link.group
        return self.group_links[0].group if self.group_links else None

    @property
    def group_ids(self) -> list[int]:
        return [link.group_id for link in self.group_links]

    @property
    def admin_group_ids(self) -> list[int]:
        """Groups in which this user holds ``group_admin`` (data-scope input)."""
        return [
            link.group_id
            for link in self.group_links
            if link.role_in_group == GroupRole.GROUP_ADMIN
        ]

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r}>"
