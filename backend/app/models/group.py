"""User groups (a university lab) and the group-membership link table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, str_enum
from app.models.enums import GroupKind, GroupRole

if TYPE_CHECKING:
    from app.models.user import User


class Group(Base, TimestampMixin):
    """A lab / organisational unit.

    A group owns one or more workspaces and the quotas attached to them; it is
    the unit a ``group_admin`` is scoped to.
    """

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    #: The directory (LDAP) group this platform group mirrors, when the two
    #: names differ.
    #:
    #: Directory matching is otherwise by ``name``, but ``name`` is constrained
    #: to a lowercase ASCII slug because it becomes part of a Kubernetes
    #: namespace (``wl-{group}-{workspace}``), while a directory ``cn`` is
    #: arbitrary UTF-8. Without this column a group called ``Lab_Vision`` in the
    #: directory simply cannot be associated with anything here: the API refuses
    #: to create a group named ``Lab_Vision``, and matching on ``name`` would
    #: never find ``lab-vision``.
    #:
    #: ``NULL`` means "match on ``name``", which is exactly the behaviour that
    #: predates this column — so existing rows need no backfill and keep working.
    directory_name: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[GroupKind] = mapped_column(
        str_enum(GroupKind, "group_kind"), nullable=False, default=GroupKind.LAB
    )
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    member_links: Mapped[list[UserGroupLink]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Group id={self.id} name={self.name!r}>"

    @property
    def directory_key(self) -> str:
        """The name this group is matched against in the directory.

        One place decides this so the bulk reconcile and the per-login sync
        cannot drift apart on what "the same group" means.
        """
        return self.directory_name or self.name


class UserGroupLink(Base, TimestampMixin):
    """Membership of a user in a group, with the user's standing in that group.

    A user normally belongs to exactly one group (``is_primary``); the link
    table also carries the group-scoped ``group_admin`` flag so that being an
    admin of one lab never leaks into another.
    """

    __tablename__ = "user_groups"
    __table_args__ = (UniqueConstraint("user_id", "group_id", name="uq_user_groups_user_group"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_in_group: Mapped[GroupRole] = mapped_column(
        str_enum(GroupRole, "group_role"), nullable=False, default=GroupRole.MEMBER
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: True when the membership was derived from the directory (LDAP/OIDC).
    #: Directory sync only ever adds or removes rows it owns, so memberships an
    #: administrator granted by hand are never silently revoked.
    is_directory_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Eagerly loaded for the same reason as `group` below: the member list
    # always renders the user, and a lazy load would raise MissingGreenlet.
    user: Mapped[User] = relationship(back_populates="group_links", lazy="joined")
    # Eagerly loaded: the group name is needed wherever a membership is shown,
    # and a lazy load would raise MissingGreenlet inside async request handling.
    group: Mapped[Group] = relationship(back_populates="member_links", lazy="joined")

    def __repr__(self) -> str:
        return (
            f"<UserGroupLink user_id={self.user_id} group_id={self.group_id} "
            f"role={self.role_in_group}>"
        )
