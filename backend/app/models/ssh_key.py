"""SSH public keys belonging to a user (plan D8).

Keys are mounted into an instance as ``authorized_keys`` through a Secret; the
private key never reaches the platform.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class SshKey(Base, TimestampMixin):
    __tablename__ = "ssh_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The full public key line, e.g. ``ssh-ed25519 AAAA... user@host``.
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: SHA-256 fingerprint, used to prevent the same key being registered twice.
    fingerprint: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)

    user: Mapped[User] = relationship(back_populates="ssh_keys")

    def __repr__(self) -> str:
        return f"<SshKey id={self.id} user_id={self.user_id} name={self.name!r}>"
