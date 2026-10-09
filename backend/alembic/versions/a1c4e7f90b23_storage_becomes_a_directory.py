"""Storage becomes a directory: static volumes, and three kinds instead of five.

The model before this revision described a volume as *a claim the platform
provisioned*. That is the wrong shape for the requirement, which is one volume per
directory in a layout decided outside the platform:

    /storage/kube-wl/{group}/{username}     用户存储卷
    /storage/kube-wl/{group}/{dir_name}     组共享存储卷
    /storage/kube-wl/{group}                组本级存储卷
    /storage/kube-wl/_global/{name}         全局共享存储卷

So a volume stops being something allocated and becomes something *named*. Four
consequences, in the order they matter.

``host_path`` is the new identity, and it is unique — but only among live rows. The
uniqueness is the "one volume per directory" rule written as a constraint, and the
partiality is what lets a retired volume keep its record without making its
directory permanently unusable.

``dir_name`` distinguishes a share *inside* a group's directory from the group's
directory itself. Null means the latter, and that is not "unset": the group's own
directory contains every member's home, so it is visible and mountable only to a
group administrator. Storing the distinction keeps that access rule a SQL predicate
rather than a post-filter that would silently break pagination.

``workspace_id`` becomes nullable, because a global share belongs to no workspace.
Its claims are created per namespace by whatever mounts it, which is a consequence of
PersistentVolumeClaims being namespaced and a PersistentVolume binding to exactly one
of them.

Three kinds replace five. ``personal_home`` and ``personal_data`` were two halves of
one idea — the volume a user's work lives in — and were never both usable;
``dataset`` and the never-used ``object`` are covered by ``global_share``. Nothing
implemented the read-only guarantee that ``dataset`` existed to provide, so the flag
survives as a plain administrator choice.

**This revision refuses to run against existing volumes.** There is no honest mapping
from "a claim with no directory" to "a directory": the directory is exactly the
information the old model did not have, and inventing one would point a user's volume
at a path nobody chose. On an installation with volumes, the migration stops and says
so; with none — the expected case, since this is the layout being introduced — it is a
pure schema change.

Revision ID: a1c4e7f90b23
Revises: e5b2a7c9d1f3
Create Date: 2026-09-24 06:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.auth.permissions import seed_rbac

# revision identifiers, used by Alembic.
revision: str = "a1c4e7f90b23"
down_revision: str | None = "e5b2a7c9d1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _refuse_if_volumes_exist() -> None:
    """Stop rather than guess where somebody's data lives.

    A row here means an administrator had a volume under the old model. Its
    directory is not recorded anywhere and cannot be derived, so there is no
    migration that would not be a fabrication. Failing loudly leaves the operator
    in control of the decision; deleting the rows, or pointing them somewhere
    arbitrary, would each look like success while losing the thing the record was
    for.
    """
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM storages")).scalar() or 0
    if count:
        raise RuntimeError(
            f"storages 表中有 {count} 条旧存储记录，无法自动迁移到「目录即存储卷」模型："
            "旧模型没有记录存储目录，迁移无法推断。"
            "请先导出这些记录，确认每个存储卷对应的存储目录，"
            "清空 storages 与 instance_storages 后再执行迁移，"
            "然后用新的接口按目录重新创建存储卷。"
        )


def upgrade() -> None:
    _refuse_if_volumes_exist()

    with op.batch_alter_table("storages", schema=None) as batch_op:
        # The directory a volume *is*. Not nullable: a volume without one is not a
        # volume, and the whole model rests on this column being there.
        batch_op.add_column(sa.Column("host_path", sa.String(length=512), nullable=False))
        # Null means "the group's own directory" for a group share.
        batch_op.add_column(sa.Column("dir_name", sa.String(length=255), nullable=True))

        # A global share belongs to no workspace, so this can no longer be required.
        batch_op.alter_column("workspace_id", existing_type=sa.Integer(), nullable=True)

        # Superseded by `host_path`: the storage class was only ever consulted to
        # ask a provisioner for space, and a static volume has no class. `medium`
        # was informational and never written; `size_bytes` was never written either,
        # because the size of a directory is not something Kubernetes reports.
        batch_op.drop_column("storage_class")
        batch_op.drop_column("medium")
        batch_op.drop_column("size_bytes")

        # Replaced by the two partial indexes below, which additionally allow a
        # retired volume to keep its name and its directory on the record.
        batch_op.drop_constraint(batch_op.f("uq_storages_workspace_name"), type_="unique")
        batch_op.create_index(batch_op.f("ix_storages_host_path"), ["host_path"], unique=False)
        batch_op.create_index(
            "uq_storages_host_path_live",
            ["host_path"],
            unique=True,
            sqlite_where=sa.text("is_active = 1"),
            postgresql_where=sa.text("is_active"),
        )
        batch_op.create_index(
            "uq_storages_name_live",
            ["workspace_id", "name"],
            unique=True,
            sqlite_where=sa.text("is_active = 1"),
            postgresql_where=sa.text("is_active"),
        )

    with op.batch_alter_table("instance_storages", schema=None) as batch_op:
        # One directory is one volume, so "mount part of a volume" is not a thing
        # the platform needs to express. Where several shares of a kind are attached
        # at once, each gets its own mount point instead — which the `mount_path`
        # column already records.
        batch_op.drop_column("sub_path")

    # The permission catalogue changed shape: `storage:create`, `storage:resize` and
    # `storage:delete` are gone, and `storage:manage` is no longer granted to group
    # administrators. `seed_rbac` reconciles roles and grants against this module's
    # catalogue and removes what is no longer declared, which is exactly the update
    # needed — leaving stale grants behind would keep a right that no longer exists.
    print(f"[rbac seed] {seed_rbac(op.get_bind())}")


def downgrade() -> None:
    with op.batch_alter_table("instance_storages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("sub_path", sa.String(length=255), nullable=True))

    with op.batch_alter_table("storages", schema=None) as batch_op:
        batch_op.drop_index("uq_storages_name_live")
        batch_op.drop_index("uq_storages_host_path_live")
        batch_op.drop_index(batch_op.f("ix_storages_host_path"))
        batch_op.create_unique_constraint(
            batch_op.f("uq_storages_workspace_name"), ["workspace_id", "name"]
        )

        batch_op.add_column(sa.Column("size_bytes", sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column("medium", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("storage_class", sa.String(length=64), nullable=True))

        # Rows cannot survive this direction either: the kind values written under
        # the new model (`user_volume`, `global_share`) do not exist in the old
        # vocabulary, so any that exist must go before the column can be reused.
        op.get_bind().execute(sa.text("DELETE FROM storages"))

        batch_op.alter_column("workspace_id", existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column("dir_name")
        batch_op.drop_column("host_path")

    print(f"[rbac seed] {seed_rbac(op.get_bind())}")
