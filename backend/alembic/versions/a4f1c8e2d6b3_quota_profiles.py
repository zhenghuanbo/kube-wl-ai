"""Named compute quota profiles for users and workspaces.

Until now a compute ceiling lived in two unrelated places: a workspace's namespace
``ResourceQuota`` was **summed** from whichever resource groups the workspace
happened to be granted, and a per-user ceiling inside a pool lived on
``resource_group_limits``. Neither could be adjusted on its own, and the value a
workspace actually got was an accidental total that changed whenever a grant was
added or removed.

This adds ``quota_profiles``: one named set of ceilings, attachable to a user or to a
workspace, carrying exactly the dimensions the platform is asked to enforce —
``cpu_cores``, ``memory_gb``, ``gpu``, plus object counts. ``users.quota_profile_id``
and ``workspaces.quota_profile_id`` are nullable, and ``NULL`` means "follow this
kind's default profile", so promoting another profile to default moves everybody who
never asked for an individual one — which is the whole point of having a default.

Two defaults are *seeded here* rather than created at runtime, so a database
migrated from any earlier state starts from exactly the same place, and so an
operator reading this file can see what every tenant begins with: 24 cores,
unlimited memory, 8 accelerators, ten of each object per user, and the namespace
object counts that were previously hardcoded in ``_desired_quota``.

Units are **cores** and **GB** — what a user types into a form, and what
Kubernetes' own ``cpu: "8"`` / ``memory: "16Gi"`` mean. The translation to
milli-cores and MiB happens once, at the edge that builds a manifest
(``app.services.workspaces._desired_quota``), never in storage.

Revision ID: a4f1c8e2d6b3
Revises: b7d2f4a8c915
Create Date: 2026-10-01 10:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.auth.permissions import seed_rbac

# revision identifiers, used by Alembic.
revision: str = "a4f1c8e2d6b3"
down_revision: str | None = "b7d2f4a8c915"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ``-1`` means "do not check this dimension". Spelled out here rather than
#: imported from the application, because a migration has to keep describing the
#: data it created even after the application's constant is renamed or removed.
_UNLIMITED = -1

#: The columns a seeded row sets. ``created_at``/``updated_at`` are deliberately
#: absent: they carry server defaults, and letting the database fill them keeps this
#: migration from having to know how time is spelled on each backend.
_QUOTA_PROFILES = sa.table(
    "quota_profiles",
    sa.column("kind", sa.String),
    sa.column("name", sa.String),
    sa.column("display_name", sa.String),
    sa.column("description", sa.Text),
    sa.column("cpu_cores", sa.Integer),
    sa.column("memory_gb", sa.Integer),
    sa.column("gpu", sa.Integer),
    sa.column("max_instances", sa.Integer),
    sa.column("max_applications", sa.Integer),
    sa.column("max_tasks", sa.Integer),
    sa.column("max_pods", sa.Integer),
    sa.column("max_deployments", sa.Integer),
    sa.column("max_services", sa.Integer),
    sa.column("max_jobs", sa.Integer),
    sa.column("is_default", sa.Boolean),
    sa.column("is_active", sa.Boolean),
)

_SEED_ROWS: list[dict[str, object]] = [
    {
        "kind": "user",
        "name": "default",
        "display_name": "默认用户配额",
        "description": (
            "所有用户默认关联的算力配额。-1 表示该维度不做校验。"
        ),
        "cpu_cores": 24,
        "memory_gb": _UNLIMITED,
        "gpu": 8,
        "max_instances": 10,
        "max_applications": 10,
        "max_tasks": 10,
        "max_pods": None,
        "max_deployments": None,
        "max_services": None,
        "max_jobs": None,
        "is_default": True,
        "is_active": True,
    },
    {
        "kind": "workspace",
        "name": "default",
        "display_name": "默认工作空间配额",
        "description": (
            "工作空间默认关联的算力配额，直接下发为该命名空间的 ResourceQuota。"
            "-1 表示该维度不做校验，对应的 key 不会出现在 ResourceQuota 中。"
        ),
        "cpu_cores": 24,
        "memory_gb": _UNLIMITED,
        "gpu": 8,
        # A workspace profile counts namespace objects, not user objects.
        "max_instances": None,
        "max_applications": None,
        "max_tasks": None,
        "max_pods": 500,
        "max_deployments": 200,
        "max_services": 400,
        "max_jobs": 200,
        "is_default": True,
        "is_active": True,
    },
]


def upgrade() -> None:
    op.create_table(
        "quota_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("cpu_cores", sa.Integer(), nullable=False),
        sa.Column("memory_gb", sa.Integer(), nullable=False),
        sa.Column("gpu", sa.Integer(), nullable=False),
        # User-kind dimensions.
        sa.Column("max_instances", sa.Integer(), nullable=True),
        sa.Column("max_applications", sa.Integer(), nullable=True),
        sa.Column("max_tasks", sa.Integer(), nullable=True),
        # Workspace-kind dimensions.
        sa.Column("max_pods", sa.Integer(), nullable=True),
        sa.Column("max_deployments", sa.Integer(), nullable=True),
        sa.Column("max_services", sa.Integer(), nullable=True),
        sa.Column("max_jobs", sa.Integer(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quota_profiles")),
        # Unique per kind: "default" is a meaningful name for both kinds, and the
        # two live in one table.
        sa.UniqueConstraint("kind", "name", name="uq_quota_profiles_kind_name"),
    )
    op.create_index(op.f("ix_quota_profiles_kind"), "quota_profiles", ["kind"], unique=False)
    op.create_index(op.f("ix_quota_profiles_name"), "quota_profiles", ["name"], unique=False)

    # Nullable on purpose: NULL is the "follow the default" value, and existing rows
    # must keep working without being silently pinned to one profile.
    for table in ("users", "workspaces"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("quota_profile_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                batch_op.f(f"fk_{table}_quota_profile_id_quota_profiles"),
                "quota_profiles",
                ["quota_profile_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch_op.create_index(
                batch_op.f(f"ix_{table}_quota_profile_id"), ["quota_profile_id"]
            )

    op.bulk_insert(_QUOTA_PROFILES, _SEED_ROWS)

    # `seed_rbac` is idempotent and is how newly added permission codes reach the
    # catalogue; without it, `alembic upgrade` alone would leave the new
    # `quota_profile:*` codes ungranted to anyone but a freshly seeded role.
    result = seed_rbac(op.get_bind())
    print(f"[rbac seed] {result}")


def downgrade() -> None:
    for table in ("workspaces", "users"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(batch_op.f(f"ix_{table}_quota_profile_id"))
            batch_op.drop_constraint(
                batch_op.f(f"fk_{table}_quota_profile_id_quota_profiles"),
                type_="foreignkey",
            )
            batch_op.drop_column("quota_profile_id")

    op.drop_index(op.f("ix_quota_profiles_name"), table_name="quota_profiles")
    op.drop_index(op.f("ix_quota_profiles_kind"), table_name="quota_profiles")
    op.drop_table("quota_profiles")
