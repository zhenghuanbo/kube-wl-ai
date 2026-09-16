"""Audit trail.

Every mutating request and every denied attempt is recorded. ``actor_username``
is denormalised so the trail survives user deletion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    actor_username: Mapped[str] = mapped_column(String(150), nullable=False, default="anonymous")
    #: Permission-style code, e.g. ``instance:create`` or ``auth:login``.
    action: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    object_type: Mapped[str | None] = mapped_column(String(64), index=True)
    object_id: Mapped[str | None] = mapped_column(String(64), index=True)
    #: ``success`` | ``denied`` | ``error``
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success", index=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)

    actor: Mapped[User | None] = relationship(back_populates="audit_logs")

    def __repr__(self) -> str:
        return f"<AuditLog id={self.id} action={self.action!r} status={self.status!r}>"
