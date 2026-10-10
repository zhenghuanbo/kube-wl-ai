"""Workloads stop owning disks: drop the per-workload storage request.

Two decisions from the storage rework meet in this revision.

**A workload has no disk of its own.** The home an instance or a task sees is the
owner's *managed storage volume* — a directory registered by an administrator from
the storage page. That is now mounted at ``/home/{username}`` on every workload, so
``instances.storage_gb`` and ``tasks.storage_gb`` no longer describe anything that
gets created. They were the size of a temporary claim the platform provisioned per
workload, and that claim is gone; keeping the column would leave a field that reads
like a setting, is offered in the API and the UI, and has no effect.

``applications.default_storage_gb`` goes with them: it was the value copied into
``instances.storage_gb`` at launch, so it is a default for a field that no longer
exists.

**The columns' contents are deliberately discarded rather than migrated.** They hold
a *size request*, not data, and the data was never in them — it is in the volume
directory, which this revision does not touch. There is also nothing to convert them
to: the ceiling that used to bound this number
(``ResourceGroupLimit.max_storage_gb_per_user``) now bounds the capacity of a
*managed volume* when one is registered, and a volume is not per-workload. Inventing
a volume per existing instance would create directories nobody chose.

Pre-existing rows keep working. An instance created before this revision recorded its
temporary claim in ``instances.k8s_pvc``; that column is untouched, so deleting such
an instance still reclaims the claim it owns. New instances record nothing there, by
design — see ``app/services/instances.py``.

Revision ID: b7d2f4a8c915
Revises: a1c4e7f90b23
Create Date: 2026-09-24 07:05:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d2f4a8c915"
down_revision: str | None = "a1c4e7f90b23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("instances", schema=None) as batch_op:
        batch_op.drop_column("storage_gb")

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("storage_gb")

    with op.batch_alter_table("applications", schema=None) as batch_op:
        batch_op.drop_column("default_storage_gb")


def downgrade() -> None:
    """Restore the columns, with the defaults the models used to declare.

    The values cannot be restored — they were never recorded anywhere else — so
    every row comes back at its default. That is the honest outcome: the numbers
    described claims that were deleted along with the code that made them, and a
    downgrade recreates the *shape* of the old model, not its history.
    """
    with op.batch_alter_table("applications", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "default_storage_gb",
                sa.Integer(),
                nullable=False,
                server_default="10",
            )
        )

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "storage_gb",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )

    with op.batch_alter_table("instances", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "storage_gb",
                sa.Integer(),
                nullable=False,
                server_default="10",
            )
        )
