"""Drop ``quotas.used_value``.

The column existed to make a quota check a single row read, with the reconcile loop
correcting any drift. Nothing ever incremented it, so every reader — including
``GET /quotas`` — reported "used: 0". A counter that is never written is worse than no
counter at all, because it looks authoritative: the workspace page said an empty
namespace was full, and a workspace 16 GiB into a 16 GiB ceiling said it was empty.

Usage is computed from the workload tables instead (``app.services.quota_guard``).
That cannot drift from reality because it *is* derived from reality, and it is the
same computation the admission gate uses, so "how much is in use" and "does this fit"
can no longer disagree.

Revision ID: d1a7f3b95c28
Revises: c8b3e5a71f42
Create Date: 2026-10-02 11:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "d1a7f3b95c28"
down_revision = "c8b3e5a71f42"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("quotas", schema=None) as batch_op:
        batch_op.drop_column("used_value")


def downgrade() -> None:
    # Restored as zero for every row, which loses nothing: the real consumption was
    # never stored, so any other value would be invented rather than recovered. A
    # caller on the old code sees the same always-zero counter it would have seen
    # before this revision existed.
    with op.batch_alter_table("quotas", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("used_value", sa.Integer(), nullable=False, server_default="0")
        )
