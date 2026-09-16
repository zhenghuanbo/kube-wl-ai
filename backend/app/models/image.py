"""Container images: the catalogue behind every image picker.

Before this existed, every form asked the user to type an image reference from
memory. That produces typos that only surface as an ``ImagePullBackOff`` minutes
later, and it makes "which images do we actually support?" unanswerable.

Two decisions:

**A reference is not an identity.** ``registry/p/pytorch:2.4`` can be repushed
under the same tag, so the row carries the ``digest`` as well. The picker shows
the tag because that is what people recognise; anything that needs to be
reproducible should pin the digest, and the API accepts either.

**The catalogue is a cache, not the source of truth.** Harbor is. A row records
what Harbor said at ``synced_at``, and a sync is allowed to delete rows that
Harbor no longer reports — with one exception, ``is_pinned``. A curated image
that vanishes from a registry listing is usually a misconfigured project rather
than a decision to stop offering it, and silently dropping it from the picker
would break every saved application that referenced it.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, str_enum
from app.models.enums import ApplicationCategory, ImageKind, ImageVisibility

if TYPE_CHECKING:
    from app.models.group import Group
    from app.models.user import User
    from app.models.workspace import Workspace


class Image(Base, TimestampMixin):
    """One tagged image in one registry."""

    __tablename__ = "images"
    __table_args__ = (
        # The natural key: a tag is unique within a repository.
        UniqueConstraint("registry", "project", "repository", "tag", name="uq_images_reference"),
        Index("ix_images_kind_active", "kind", "is_active"),
        Index("ix_images_owner", "owner_user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- reference -----------------------------------------------------------
    #: Host of the registry, e.g. ``harbor.example.edu``. No scheme: it is part
    #: of the image reference, and ``https://`` in a reference is a pull error.
    #:
    #: Named ``registry_host`` rather than ``registry`` because SQLAlchemy's
    #: ``DeclarativeBase`` already owns a ``registry`` attribute — the mapper
    #: registry itself. Shadowing it breaks the class at import time, and the
    #: column name stays ``registry`` so the database still reads naturally.
    registry_host: Mapped[str] = mapped_column("registry", String(255), nullable=False, index=True)
    #: Harbor project. Maps onto the ``group`` for group-scoped catalogues.
    project: Mapped[str] = mapped_column(String(255), nullable=False, default="library")
    repository: Mapped[str] = mapped_column(String(255), nullable=False)
    tag: Mapped[str] = mapped_column(String(128), nullable=False, default="latest")
    #: ``sha256:...`` as reported by the registry. Empty until first synced.
    digest: Mapped[str | None] = mapped_column(String(128))

    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    description: Mapped[str | None] = mapped_column(Text)

    # --- classification ------------------------------------------------------
    kind: Mapped[ImageKind] = mapped_column(
        str_enum(ImageKind, "image_kind"), nullable=False, default=ImageKind.PERSONAL, index=True
    )
    visibility: Mapped[ImageVisibility] = mapped_column(
        str_enum(ImageVisibility, "image_visibility"),
        nullable=False,
        default=ImageVisibility.PRIVATE,
    )
    #: What the image is for; drives the same picker grouping as applications.
    category: Mapped[ApplicationCategory] = mapped_column(
        str_enum(ApplicationCategory, "image_category"),
        nullable=False,
        default=ApplicationCategory.OTHER,
    )

    # --- contents ------------------------------------------------------------
    #: Free-form, but the common ones are worth their own column: a user
    #: choosing an image is usually answering "does this have PyTorch and which
    #: CUDA does it need?".
    framework: Mapped[str | None] = mapped_column(String(64), index=True)
    framework_version: Mapped[str | None] = mapped_column(String(64))
    python_version: Mapped[str | None] = mapped_column(String(32))
    cuda_version: Mapped[str | None] = mapped_column(String(32))
    os_version: Mapped[str | None] = mapped_column(String(64))

    # --- registry metadata ---------------------------------------------------
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    #: Number of layers, exposed by Harbor; a cheap proxy for "is this a real
    #: image or a stub someone pushed by accident".
    layer_count: Mapped[int | None] = mapped_column(Integer)
    #: Empty means the sync has never seen it.
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Set when the registry reported an error for this reference, so the UI can
    #: say *why* an entry is stale instead of just showing an old timestamp.
    sync_error: Mapped[str | None] = mapped_column(Text)

    # --- tenancy -------------------------------------------------------------
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"), index=True
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), index=True
    )
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    #: Curated entries survive a sync that no longer reports them.
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: How many instances, tasks and applications reference this image. Denormalised
    #: so "may I delete this?" is answerable without three counts per row.
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    workspace: Mapped[Workspace | None] = relationship(lazy="joined")
    group: Mapped[Group | None] = relationship(lazy="joined")
    owner: Mapped[User | None] = relationship(
        lazy="joined", foreign_keys=[owner_user_id], join_depth=1
    )

    @property
    def reference(self) -> str:
        """The string to hand Kubernetes: ``registry/project/repository:tag``."""
        return f"{self.registry_host}/{self.project}/{self.repository}:{self.tag}"

    @property
    def pinned_reference(self) -> str:
        """The digest-pinned reference, falling back to the tag when unknown.

        A caller that needs reproducibility should use this; a caller that just
        wants to pull should use :attr:`reference`.
        """
        if self.digest:
            return f"{self.registry_host}/{self.project}/{self.repository}@{self.digest}"
        return self.reference

    @property
    def requires_gpu(self) -> bool:
        """Whether the image was built against CUDA.

        Inferred from the CUDA tag rather than stored as its own flag: one fact,
        one place, and the tag is what the image's author actually controls.
        """
        return bool(self.cuda_version)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Image {self.id} {self.reference}>"
