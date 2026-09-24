"""Drop the image classification and contents columns.

The catalogue used to carry two independent taxonomies: an
``ApplicationCategory`` copied from the application vocabulary, and a set of
free-form "contents" fields (framework / python / cuda / os).

Both turned out to describe the *application*, not the image. An image is a
tarball with a tag; whether the workload it runs is a Jupyter notebook or a
training job is decided when the instance is created, and the same
``pytorch:2.4-cuda12`` serves both. Keeping a second, hand-maintained copy of
that decision on the image only created a field to disagree with itself.

The contents fields were worse: nothing in the sync could populate them — Harbor
reports a media type and a config blob, not "this is PyTorch 2.4" — so every
synced row carried NULLs and only hand-registered rows ever had them. A filter
that matches only hand-registered rows is a filter that hides most of the
catalogue, which is exactly how it behaved.

The three things that actually distinguish images remain, and they are derived
from the registry layout rather than typed in: ``kind`` (official / shared /
personal) and ``visibility`` (global / group / workspace / private).

``requires_gpu`` on the model read ``cuda_version`` and had no callers; it goes
with the column.

Revision ID: c3a17d5e8b04
Revises: b7c41a9e5d02
Create Date: 2026-09-24 04:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c3a17d5e8b04"
down_revision: str | None = "b7c41a9e5d02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("images", schema=None) as batch_op:
        # The index has to go before the column it covers.
        batch_op.drop_index(batch_op.f("ix_images_framework"))
        batch_op.drop_column("framework")
        batch_op.drop_column("framework_version")
        batch_op.drop_column("python_version")
        batch_op.drop_column("cuda_version")
        batch_op.drop_column("os_version")
        batch_op.drop_column("category")


def downgrade() -> None:
    with op.batch_alter_table("images", schema=None) as batch_op:
        # `category` was NOT NULL with a default, so restoring it needs both; the
        # contents columns were nullable and come back empty.
        batch_op.add_column(
            sa.Column(
                "category",
                sa.String(length=32),
                nullable=False,
                server_default="other",
            )
        )
        batch_op.add_column(sa.Column("framework", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("framework_version", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("python_version", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("cuda_version", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("os_version", sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f("ix_images_framework"), ["framework"])
