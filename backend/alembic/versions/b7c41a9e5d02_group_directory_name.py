"""Group directory_name: associate a platform group with a directory group.

A platform group's ``name`` must be a lowercase ASCII slug, because it is
composed into a Kubernetes namespace (``wl-{group}-{workspace}``). A directory
``cn`` has no such constraint, so ``Lab_Vision`` or ``人工智能实验室`` could not be
matched against anything: creating a same-named group here was rejected by the
API, and matching on ``name`` would never have found the slugged equivalent.

``directory_name`` is the directory-side key, used for matching when it is set.
It is nullable and left NULL for every existing row, where NULL means "match on
``name``" — the behaviour that predates this column — so no backfill is needed.

The unique index covers the one case the database can enforce on its own: two
groups claiming the same explicit ``directory_name``. The subtle case, an
explicit ``directory_name`` colliding with some other group's ``name``, spans two
columns and is checked in the service layer.

Revision ID: b7c41a9e5d02
Revises: 3f0dbd1b4617
Create Date: 2026-09-20 19:05:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b7c41a9e5d02"
down_revision: str | None = "3f0dbd1b4617"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.add_column(sa.Column("directory_name", sa.String(length=128), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_groups_directory_name"), ["directory_name"], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table("groups", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_groups_directory_name"))
        batch_op.drop_column("directory_name")
