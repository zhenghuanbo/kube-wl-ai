"""Declarative base, naming conventions and shared column mixins."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import ClassVar

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint/index names make Alembic migrations reproducible.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def str_enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Portable enum column type.

    Stored as VARCHAR + CHECK constraint rather than a native database enum so
    that SQLite (used by the test suite) and PostgreSQL behave identically and
    adding a value does not require a type migration.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Base(DeclarativeBase):
    """Base class for all ORM models.

    ``eager_defaults`` makes SQLAlchemy fetch server-generated column values
    (``created_at`` / ``updated_at``) as part of the same INSERT/UPDATE via
    RETURNING. Without it those attributes are left expired after a flush, and
    the next access triggers a lazy load — which raises ``MissingGreenlet``
    under an async session rather than quietly working as it would in sync code.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # ClassVar is how SQLAlchemy documents this, but DeclarativeBase declares
    # the same name as an instance variable — hence the targeted ignore.
    __mapper_args__: ClassVar[dict[str, bool]] = {"eager_defaults": True}  # type: ignore[misc]


class TimestampMixin:
    """Adds timezone-aware ``created_at`` / ``updated_at`` columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
