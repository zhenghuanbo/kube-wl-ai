"""Resource-group per-user ceilings in cores and GB, and no workload counts.

Two unit changes and three deletions, all on ``resource_group_limits``.

**Units.** ``max_cpu_milli_per_user`` (8000) becomes ``max_cpu_cores_per_user`` (8)
and ``max_memory_mb_per_user`` (16384) becomes ``max_memory_gb_per_user`` (16). A
quota is a number an administrator types into a form and compares against what a
user asked for, and both of those are usually said in cores and gigabytes — the
milli-core and mebibyte spellings are Kubernetes' wire format, not a human's. The
conversion to those units now happens once, where a manifest is built.

A fractional value cannot survive an integer column, so it rounds **up**: dropping
the fraction would silently *tighten* a ceiling somebody deliberately set, and the
extra sub-core allowance is bounded by the pool's own hardware. In practice every
existing row is exact (8000 -> 8, 80000 -> 80, 16384 -> 16).

**Deletions.** ``max_concurrent_instances``, ``max_concurrent_tasks`` and
``max_gpu_per_instance`` go. Counting how many workloads one person may hold is a
property of the person, so it moves to the user quota profile
(``max_instances`` / ``max_applications`` / ``max_tasks``); keeping a second count
here would give one question two answers. ``max_gpu_per_instance`` was a
*catalogue* concern — which shapes a user may request — and is already expressed by
the resource specs offered in the group, so it was a second, rarely-updated copy of
that.

**Consequence.** The workspace rows in ``quotas`` for ``instance`` and ``task``
were derived from the two deleted count columns and now have no source, so they are
removed. The gate checks counts at the user layer instead. ``upsert_quotas`` also
learns to prune keys it no longer produces, so a row like this cannot outlive its
source again.

Revision ID: c8b3e5a71f42
Revises: a4f1c8e2d6b3
Create Date: 2026-10-02 09:15:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.auth.permissions import seed_rbac

# revision identifiers, used by Alembic.
revision: str = "c8b3e5a71f42"
down_revision: str | None = "a4f1c8e2d6b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "resource_group_limits"


def upgrade() -> None:
    # Step 1 — add the new columns nullable, because a NOT NULL column cannot be
    # added to a populated table without inventing a value for every row.
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.add_column(sa.Column("max_cpu_cores_per_user", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("max_memory_gb_per_user", sa.Integer(), nullable=True))

    # Step 2 — convert, rounding up. Integer division on both SQLite and
    # PostgreSQL truncates, so the addition of one unit's worth is what makes it a
    # ceiling rather than a floor: (8000 + 999) / 1000 == 8, (1500 + 999) / 1000 == 2.
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} "
            "SET max_cpu_cores_per_user = (max_cpu_milli_per_user + 999) / 1000, "
            "    max_memory_gb_per_user = (max_memory_mb_per_user + 1023) / 1024"
        )
    )

    # Step 3 — tighten to NOT NULL and drop what is gone. In one block, because
    # batch mode recreates the table once per block and there is no reason to copy
    # it twice.
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.alter_column(
            "max_cpu_cores_per_user", existing_type=sa.Integer(), nullable=False
        )
        batch_op.alter_column(
            "max_memory_gb_per_user", existing_type=sa.Integer(), nullable=False
        )
        batch_op.drop_column("max_cpu_milli_per_user")
        batch_op.drop_column("max_memory_mb_per_user")
        batch_op.drop_column("max_concurrent_instances")
        batch_op.drop_column("max_concurrent_tasks")
        batch_op.drop_column("max_gpu_per_instance")

    # Step 4 — retire the workspace quota rows whose only source was just deleted.
    # Leaving them would enforce a ceiling that nothing can now recompute, and the
    # next `apply_cluster_state` would keep it alive by pruning around it.
    op.execute(
        sa.text(
            "DELETE FROM quotas WHERE scope_type = 'workspace' "
            "AND resource IN ('instance', 'task')"
        )
    )

    # Step 5 — `quota_profile:assign` is a new code, and a code that reaches the
    # catalogue without a grant would leave even the global administrator unable to
    # attach a template. `seed_rbac` is idempotent: re-running it in this revision
    # adds only what is genuinely new since the previous one.
    result = seed_rbac(op.get_bind())
    print(f"[rbac seed] {result}")


def downgrade() -> None:
    # The three dropped columns come back with the defaults the model used to
    # carry; the exact previous values are not recoverable, and pretending
    # otherwise by inventing them would be worse than the documented default.
    #
    # The two workspace `quotas` rows deleted by `upgrade` are not restored either:
    # their values were the *sum of per-user count columns*, not something anyone
    # recorded, and the next `apply_cluster_state` recomputes them under whichever
    # code is running.
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "max_concurrent_instances", sa.Integer(), nullable=False, server_default="2"
            )
        )
        batch_op.add_column(
            sa.Column("max_concurrent_tasks", sa.Integer(), nullable=False, server_default="4")
        )
        batch_op.add_column(
            sa.Column("max_gpu_per_instance", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(sa.Column("max_cpu_milli_per_user", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("max_memory_mb_per_user", sa.Integer(), nullable=True))

    # Multiply rather than divide: it is the exact inverse, so a
    # downgrade/upgrade round trip returns the value it started with.
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} "
            "SET max_cpu_milli_per_user = max_cpu_cores_per_user * 1000, "
            "    max_memory_mb_per_user = max_memory_gb_per_user * 1024"
        )
    )

    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.alter_column(
            "max_cpu_milli_per_user", existing_type=sa.Integer(), nullable=False
        )
        batch_op.alter_column(
            "max_memory_mb_per_user", existing_type=sa.Integer(), nullable=False
        )
        batch_op.drop_column("max_cpu_cores_per_user")
        batch_op.drop_column("max_memory_gb_per_user")
        for column in (
            "max_concurrent_instances",
            "max_concurrent_tasks",
            "max_gpu_per_instance",
        ):
            batch_op.alter_column(column, existing_type=sa.Integer(), server_default=None)
