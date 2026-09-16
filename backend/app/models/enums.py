"""Enumerations shared by the ORM models.

All are ``StrEnum`` subclasses persisted as ``VARCHAR`` + ``CHECK`` (see
``app.db.base.str_enum``), so the stored values are the lower-case strings used
throughout the API and the frontend.
"""

from __future__ import annotations

from enum import StrEnum


class UserSource(StrEnum):
    """Where the identity came from."""

    LOCAL = "local"
    IPA = "ipa"
    OIDC = "oidc"


class GroupKind(StrEnum):
    """A lab group owns workspaces; the public group is the shared fallback."""

    LAB = "lab"
    PUBLIC = "public"


class GroupRole(StrEnum):
    """A user's standing *inside* one group."""

    MEMBER = "member"
    GROUP_ADMIN = "group_admin"


class RoleCode(StrEnum):
    """The four business roles (plan §5.3)."""

    OBSERVER = "observer"
    USER = "user"
    GROUP_ADMIN = "group_admin"
    GLOBAL_ADMIN = "global_admin"


class RoleScope(StrEnum):
    """Where a role assignment applies.

    ``GROUP`` scoped assignments carry a ``group_id`` — this is what makes
    ``group_admin`` inherently limited to one group.
    """

    GLOBAL = "global"
    GROUP = "group"
