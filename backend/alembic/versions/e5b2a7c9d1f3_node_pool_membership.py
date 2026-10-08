"""Give every node a resource pool, and give the pools a default.

Until now a node's pool was inferred: the pool carried a ``k8s_label_selector``
and the scheduler matched it against whatever labels the node happened to have.
That made pool membership *overlapping* — a node matching two selectors belonged
to both — and *optional* — a node matching none belonged to nothing, and was
therefore hardware no workload could ever reach.

The requested model is a partition: every node in exactly one pool, chosen by the
platform rather than by a label an operator typed. Three things change here.

``compute_nodes.resource_group_id`` makes membership a fact the platform owns.
One nullable FK: nullable because a node discovered before any pool exists has
nowhere to go yet, and because deleting a pool must not delete hardware. The sync
homes such nodes to the default pool, so the value is expected to be set on every
node the platform manages.

``resource_groups.is_default`` guarantees the partition is *total*. Without a
catch-all, "every node is in a pool" is unenforceable — a new node, or a node
whose pool was deleted, would have no home. Exactly one pool may carry this flag;
the service enforces that, the same way it already does for runtime limits.

The two together replace ``k8s_label_selector`` as the scheduling target. The
selector column survives as an optional extra constraint ("this pool wants A100
nodes specifically"), but it is no longer what decides which pool a node is in.

Revision ID: e5b2a7c9d1f3
Revises: c3a17d5e8b04
Create Date: 2026-09-24 05:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e5b2a7c9d1f3"
down_revision: str | None = "c3a17d5e8b04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("resource_groups", schema=None) as batch_op:
        # Existing pools are all created equal: none of them is the default yet.
        # The service picks one when an administrator says so, or when the first
        # pool is created on an empty install.
        batch_op.add_column(
            sa.Column(
                "is_default",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        # Drop the server default again: the model carries a Python-side default
        # only, and leaving one here would be a permanent disagreement between
        # the schema and the metadata that `--autogenerate` would keep reporting.
        batch_op.alter_column(
            "is_default", existing_type=sa.Boolean(), server_default=None, nullable=False
        )

    with op.batch_alter_table("compute_nodes", schema=None) as batch_op:
        batch_op.add_column(sa.Column("resource_group_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_compute_nodes_resource_group_id_resource_groups"),
            "resource_groups",
            ["resource_group_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            batch_op.f("ix_compute_nodes_resource_group_id"), ["resource_group_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("compute_nodes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_compute_nodes_resource_group_id"))
        batch_op.drop_constraint(
            batch_op.f("fk_compute_nodes_resource_group_id_resource_groups"), type_="foreignkey"
        )
        batch_op.drop_column("resource_group_id")

    with op.batch_alter_table("resource_groups", schema=None) as batch_op:
        batch_op.drop_column("is_default")
