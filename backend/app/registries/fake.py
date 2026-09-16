"""An in-memory registry, for tests and for running without Harbor.

Not a stub that raises: the API has to be exercisable end to end on a laptop
with no registry, so this behaves like a small but honest Harbor. In particular
it keeps deletions and answers ``not_found`` for them afterwards, because "the
tag I just removed is gone" is exactly the behaviour the service's failure
handling depends on.
"""

from __future__ import annotations

from app.registries.registry import (
    RegistryError,
    RegistryProject,
    RegistryStatus,
    RegistryTag,
)


class FakeRegistryClient:
    """A registry that lives in a dictionary."""

    def __init__(self, url: str = "registry.local") -> None:
        self._host = url.split("://", 1)[-1].strip("/")
        #: Populated by tests, and by :meth:`seed` for a useful empty state.
        self.tags: dict[tuple[str, str], list[RegistryTag]] = {}
        self.projects: list[RegistryProject] = [RegistryProject("library", is_public=True)]
        self.reachable = True
        #: Set to make every call fail, so the "registry is down" path is
        #: reachable without unplugging anything.
        self.error: str | None = None
        self.deleted: list[tuple[str, str, str]] = []
        self.seed()

    @property
    def host(self) -> str:
        return self._host

    def seed(self) -> None:
        """A couple of plausible images, so a fresh install is not an empty page."""
        self.put(
            RegistryTag(
                "library",
                "base-notebook",
                "latest",
                digest="sha256:" + "0" * 64,
                size_bytes=1_200_000_000,
                layer_count=12,
            )
        )
        self.put(
            RegistryTag(
                "library",
                "pytorch",
                "2.4-cuda12",
                digest="sha256:" + "1" * 64,
                size_bytes=7_400_000_000,
                layer_count=24,
            )
        )

    def put(self, tag: RegistryTag) -> None:
        rows = self.tags.setdefault((tag.project, tag.repository), [])
        rows[:] = [row for row in rows if row.tag != tag.tag]
        rows.append(tag)

    def _guard(self) -> None:
        if self.error is not None:
            raise RegistryError(self.error)
        if not self.reachable:
            raise RegistryError("无法连接镜像仓库")

    def status(self) -> RegistryStatus:
        if self.error is not None or not self.reachable:
            return RegistryStatus(reachable=False, message=self.error or "无法连接镜像仓库")
        return RegistryStatus(
            reachable=True,
            version="fake-2.0",
            projects=[project.name for project in self.projects],
        )

    def list_projects(self) -> list[RegistryProject]:
        self._guard()
        return list(self.projects)

    def list_repositories(self, project: str) -> list[str]:
        self._guard()
        return sorted({repo for (owner, repo) in self.tags if owner == project})

    def list_tags(self, project: str, repository: str) -> list[RegistryTag]:
        self._guard()
        return list(self.tags.get((project, repository), []))

    def get_tag(self, project: str, repository: str, tag: str) -> RegistryTag | None:
        self._guard()
        for row in self.tags.get((project, repository), []):
            # A reference may be a tag *or* a digest; Harbor resolves both, so
            # the fake must too, or a valid pinned image looks nonexistent.
            if row.tag == tag or row.digest == tag:
                return row
        return None

    def delete_tag(self, project: str, repository: str, tag: str) -> None:
        self._guard()
        rows = self.tags.get((project, repository), [])
        remaining = [row for row in rows if row.tag != tag]
        if len(remaining) == len(rows):
            raise RegistryError("镜像仓库中不存在该资源", not_found=True)
        self.tags[(project, repository)] = remaining
        self.deleted.append((project, repository, tag))
