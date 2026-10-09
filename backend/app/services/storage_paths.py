"""Where a managed volume lives on the filesystem.

This is its own module because the mapping from *who a volume belongs to* to
*which directory it is* is the whole point of the feature. The requirement is one
volume per directory; a directory that two rows can both claim is not an
assignment, it is a race — so the mapping is written once, here, and every path
the platform ever hands to the cluster comes out of this file.

Two rules hold for everything produced here:

**It is underneath ``settings.storage_root``.** Checked, not assumed. A bug in
this module would otherwise be a bug that can mount the host's ``/etc`` into a
user's container.

**Identifiers are validated, never escaped.** ``k8s.naming.slug`` exists to make
a *Kubernetes* name out of arbitrary text, and it is the wrong tool here: it maps
``Lab_Vision`` to ``lab-vision``, which is a directory that does not exist. A
directory is decided by whoever provisions the filesystem, so an identifier the
platform cannot use verbatim is refused with a message naming it, rather than
silently pointed somewhere else.
"""

from __future__ import annotations

import posixpath
import re

from app.core.config import Settings
from app.core.errors import BadRequestError
from app.models.enums import StorageKind

#: A single path component. Deliberately strict: the identifiers that reach here
#: are a group's ``name`` (already ``^[a-z0-9][a-z0-9-]*$``) and a user's
#: ``username``, so anything outside this set means a caller passed something it
#: should not have — not that the platform should be lenient.
#:
#: A leading underscore is allowed because the global-share segment is
#: ``_global``: an operator's constant rather than user input, and the underscore
#: is exactly what stops it colliding with a group of that name. A leading dot
#: stays out, which is how ``.`` and ``..`` are excluded — and a hidden directory
#: is not something this platform should ever be creating by accident.
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")

#: Components that mean something to the filesystem rather than naming a child.
_RESERVED_COMPONENTS = frozenset({".", ".."})


def _component(value: str, *, what: str) -> str:
    """Validate one path segment, or refuse it by name.

    The failure message names the offending value because the alternative — a
    generic "invalid name" — leaves an administrator with a group or user they
    cannot place and no indication of which one.
    """
    cleaned = value.strip()
    if (
        not cleaned
        or cleaned in _RESERVED_COMPONENTS
        or "/" in cleaned
        or "\\" in cleaned
        or not _COMPONENT_RE.match(cleaned)
    ):
        raise BadRequestError(
            f"{what}「{value}」不能用作目录名：只允许字母、数字、点、下划线和连字符，"
            "且必须以字母或数字开头",
            code="invalid_path_component",
        )
    return cleaned


def _join(root: str, *parts: str) -> str:
    """Join under ``root`` and prove the result stayed there.

    ``posixpath.normpath`` collapses any ``..`` before the containment check, so
    the check is on the path that would actually be used, not on the text that
    was passed in.
    """
    base = posixpath.normpath(root.strip())
    if not base.startswith("/"):
        raise BadRequestError(
            f"存储根目录必须是绝对路径（当前「{root}」）", code="invalid_storage_root"
        )
    candidate = posixpath.normpath(posixpath.join(base, *parts))
    if candidate != base and not candidate.startswith(base + "/"):
        raise BadRequestError(
            f"拒绝越出存储根目录的路径「{candidate}」", code="storage_path_escapes_root"
        )
    return candidate


def group_directory(settings: Settings, group_name: str) -> str:
    """The group's own directory: ``{root}/{group}``.

    This is the parent of every member's home, and is itself a valid volume — the
    group-level one, which only a group administrator may mount.
    """
    return _join(settings.storage_root, _component(group_name, what="用户组名"))


def user_volume_path(settings: Settings, *, group_name: str, username: str) -> str:
    """``{root}/{group}/{username}`` — one per user, and the user's only home."""
    return _join(
        settings.storage_root,
        _component(group_name, what="用户组名"),
        _component(username, what="用户名"),
    )


def group_share_path(settings: Settings, *, group_name: str, dir_name: str | None) -> str:
    """A group share: a named directory, or the group's directory itself.

    ``dir_name`` empty means the group level. That is not a shorthand for "a
    share called nothing" — it is a different volume with different reach (it
    contains every member's home) and different access rules, which is why the
    caller must be explicit about the distinction rather than defaulting into it.
    """
    group_dir = group_directory(settings, group_name)
    if dir_name is None or not dir_name.strip():
        return group_dir
    return _join(group_dir, _component(dir_name, what="共享目录名"))


def global_share_path(settings: Settings, *, name: str) -> str:
    """``{root}/_global/{name}``.

    The ``_global`` segment exists so a global share can never be named after a
    group: without it, a share called ``hpc`` would land on ``{root}/hpc`` — the
    directory holding every member of that group's home.
    """
    return _join(
        settings.storage_root,
        _component(settings.storage_global_segment, what="全局共享前缀"),
        _component(name, what="共享卷名"),
    )


def volume_path(
    settings: Settings,
    kind: StorageKind,
    *,
    group_name: str | None = None,
    username: str | None = None,
    dir_name: str | None = None,
    name: str,
) -> str:
    """The directory for a volume, by kind.

    The single dispatcher. Every caller goes through it so that "which kind needs
    which binding" is answered in one place — a kind that requires a group and
    was given none is a programming error here, not a silently mis-rooted path.
    """
    kind = StorageKind(str(kind))

    if kind is StorageKind.USER_VOLUME:
        if not group_name or not username:
            raise BadRequestError("用户存储卷必须指定用户及其所属组", code="storage_requires_owner")
        return user_volume_path(settings, group_name=group_name, username=username)

    if kind is StorageKind.GROUP_SHARE:
        if not group_name:
            raise BadRequestError("组共享存储卷必须指定用户组", code="storage_requires_group")
        return group_share_path(settings, group_name=group_name, dir_name=dir_name)

    return global_share_path(settings, name=name)
