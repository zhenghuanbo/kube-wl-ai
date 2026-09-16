"""What a registry must be able to answer.

Deliberately narrow. The platform needs to *list* what exists, ask about one
tag, and delete a tag it created. It never pushes: building and pushing is a CI
concern, and a control plane that can write to the registry is a control plane
that can overwrite a base image every lab depends on.

Like the Kubernetes gateway, this is a protocol with a real implementation and a
fake, so the API runs and can be exercised without a registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class RegistryError(Exception):
    """A registry refused or failed to answer.

    ``not_found`` is separated out because it is the one case that is routinely
    expected — a tag that was deleted outside the platform — and callers need to
    tell it apart from "the registry is down", which is a very different problem.
    """

    def __init__(self, message: str, *, not_found: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.not_found = not_found


@dataclass(frozen=True, slots=True)
class RegistryTag:
    """One tag as the registry reports it.

    Every field beyond the name is optional because registries differ in what
    they expose: a Docker Registry v2 endpoint gives a digest and little else,
    Harbor adds size, layer count and push time.
    """

    project: str
    repository: str
    tag: str
    digest: str | None = None
    size_bytes: int | None = None
    layer_count: int | None = None
    pushed_at: str | None = None


@dataclass(frozen=True, slots=True)
class RegistryProject:
    name: str
    is_public: bool = False
    repository_count: int | None = None


@dataclass(frozen=True, slots=True)
class RegistryStatus:
    """Whether the registry is reachable, for the admin page's health banner."""

    reachable: bool
    version: str | None = None
    message: str | None = None
    projects: list[str] = field(default_factory=list)


class RegistryClient(Protocol):
    """The operations the image catalogue performs against a registry."""

    @property
    def host(self) -> str:
        """Hostname that appears in an image reference, e.g. ``harbor.x.edu``."""
        ...

    def status(self) -> RegistryStatus:
        """Is the registry reachable, and what does it call itself."""
        ...

    def list_projects(self) -> list[RegistryProject]:
        """Projects, which map onto groups."""
        ...

    def list_repositories(self, project: str) -> list[str]:
        """Repository names inside one project."""
        ...

    def list_tags(self, project: str, repository: str) -> list[RegistryTag]:
        """Every tag in one repository, already deduplicated by the registry."""
        ...

    def get_tag(self, project: str, repository: str, tag: str) -> RegistryTag | None:
        """One tag, or ``None`` when it does not exist."""
        ...

    def delete_tag(self, project: str, repository: str, tag: str) -> None:
        """Remove one tag. Used only for images the platform itself registered."""
        ...
