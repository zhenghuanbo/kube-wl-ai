"""Harbor's implementation of :class:`RegistryClient`.

Harbor's API is used rather than the plain Docker Registry v2 API because the
platform needs project listings and layer counts, and because a robot account
scoped to a project is the right credential shape: it can read the projects it
was granted and nothing else. The v2 API has no notion of projects at all.

Two details that are easy to get wrong:

* Harbor paginates with ``page``/``page_size`` and caps ``page_size`` at 100. A
  repository with more tags than that needs more than one request, so every
  listing here loops until a short page arrives rather than trusting the first.
* Deleting a tag is a *repository* operation with a reference parameter, not a
  ``DELETE`` on a tag resource. Sending it as ``/tags/{tag}`` returns 405.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

import httpx

from app.core.logging import get_logger
from app.registries.registry import (
    RegistryError,
    RegistryProject,
    RegistryStatus,
    RegistryTag,
)

logger = get_logger(__name__)

#: Harbor's maximum. Asking for more is silently clamped, which would make the
#: pagination loop below terminate early and lose tags.
PAGE_SIZE = 100
#: A repository with thousands of tags would otherwise generate thousands of
#: requests. Deep enough for real use, bounded so a misconfiguration cannot hang.
MAX_PAGES = 50
DEFAULT_TIMEOUT = 10.0


def _host_from_url(url: str) -> str:
    """Reduce a URL to the bare host used inside an image reference.

    ``https://harbor.example.edu/`` and ``harbor.example.edu`` must produce the
    same string: the scheme belongs to the API call, never to the reference, and
    a reference carrying ``https://`` is a pull error that only shows up much
    later as an ``ImagePullBackOff``.
    """
    return url.split("://", 1)[-1].strip("/")


def _repository_path(repository: str) -> str:
    """Percent-encode a repository name for a Harbor API path.

    Harbor documents the parameter as *"The name of the repository. If it
    contains slash, encode it twice over with URL encoding. e.g. a/b -> a%2Fb ->
    a%252Fb"*, and the platform's scheme makes that the common case rather than
    the exception: ``{group}/{user}/{image}`` is a nested repository.

    Twice, not once, because Harbor's router decodes the path once before
    matching and the handler decodes the captured parameter again. Sending a
    single encoding — or a raw slash, which reads as extra path segments and
    answers 404 — fails every tag listing, verification and delete for nested
    repositories.
    """
    return quote(quote(repository, safe=""), safe="")


class RealHarborClient:
    """Talks to a live Harbor installation."""

    def __init__(
        self,
        url: str,
        *,
        username: str = "",
        password: str = "",
        verify: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not url:
            raise RegistryError("未配置 Harbor 地址")
        self._url = url if "://" in url else f"https://{url}"
        self._host = _host_from_url(self._url)
        self._username = username
        self._password = password
        self._timeout = timeout
        self._client = httpx.Client(base_url=self._url, verify=verify, timeout=timeout)

    @property
    def host(self) -> str:
        return self._host

    # --- plumbing ----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        if not self._username:
            return {}
        raw = f"{self._username}:{self._password}".encode()
        return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """One request, with every failure translated into :class:`RegistryError`.

        Nothing below this line is allowed to leak an ``httpx`` exception: the
        service layer decides what a failure *means*, and it cannot do that if it
        has to know which of half a dozen transport errors it is looking at.
        """
        try:
            response = self._client.request(method, path, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise RegistryError(f"无法连接镜像仓库：{exc}") from exc

        if response.status_code == 404:
            raise RegistryError("镜像仓库中不存在该资源", not_found=True)
        if response.status_code in (401, 403):
            raise RegistryError("镜像仓库拒绝了请求，请检查 robot 账号权限")
        if response.status_code >= 400:
            raise RegistryError(f"镜像仓库返回 {response.status_code}：{response.text[:200]}")
        if not response.content:
            return None
        return response.json()

    def _paginate(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Collect every page of a Harbor collection endpoint."""
        collected: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            payload = self._request(
                "GET", path, params={**params, "page": page, "page_size": PAGE_SIZE}
            )
            if not payload:
                break
            collected.extend(payload)
            if len(payload) < PAGE_SIZE:
                break
        else:
            # The `else` on a `for` runs when the loop was never broken, i.e. we
            # hit MAX_PAGES with a full final page. Say so rather than silently
            # presenting a truncated catalogue as complete.
            logger.warning("harbor_pagination_truncated", path=path, pages=MAX_PAGES)
        return collected

    # --- read paths --------------------------------------------------------
    def status(self) -> RegistryStatus:
        try:
            payload = self._request("GET", "/api/v2.0/systeminfo")
        except RegistryError as exc:
            return RegistryStatus(reachable=False, message=exc.message)
        projects = [project.name for project in self.list_projects()] if payload is not None else []
        return RegistryStatus(
            reachable=True,
            version=str((payload or {}).get("harbor_version") or "") or None,
            projects=projects,
        )

    def list_projects(self) -> list[RegistryProject]:
        rows = self._paginate("/api/v2.0/projects")
        return [
            RegistryProject(
                name=str(row.get("name", "")),
                is_public=str(row.get("metadata", {}).get("public", "false")) == "true",
                repository_count=row.get("repo_count"),
            )
            for row in rows
            if row.get("name")
        ]

    def list_repositories(self, project: str) -> list[str]:
        rows = self._paginate(f"/api/v2.0/projects/{project}/repositories")
        names: list[str] = []
        for row in rows:
            name = str(row.get("name", ""))
            # Harbor returns "project/repo". Split on the *first* slash so only
            # the project prefix goes: a nested repository arrives as
            # "lab-vision/zhangsan/my-env" and must stay "zhangsan/my-env".
            # Keeping the prefix would rebuild the reference as
            # "library/library/pytorch"; splitting on the last slash would lose
            # the owner segment that the platform's scheme is built on.
            names.append(name.split("/", 1)[-1] if "/" in name else name)
        return [name for name in names if name]

    def list_tags(self, project: str, repository: str) -> list[RegistryTag]:
        rows = self._paginate(
            f"/api/v2.0/projects/{project}/repositories/{_repository_path(repository)}/artifacts"
        )
        tags: list[RegistryTag] = []
        for artifact in rows:
            digest = artifact.get("digest")
            extra = artifact.get("extra") or {}
            config = extra.get("config") or {}
            for tag in artifact.get("tags") or []:
                tags.append(
                    RegistryTag(
                        project=project,
                        repository=repository,
                        tag=str(tag.get("name", "")),
                        digest=digest,
                        size_bytes=artifact.get("size"),
                        # Harbor reports this only for image manifests; a chart
                        # or an index simply has no layer count.
                        layer_count=_layer_count(config.get("mediaType"), config),
                        pushed_at=tag.get("push_time"),
                    )
                )
        return [tag for tag in tags if tag.tag]

    def get_tag(self, project: str, repository: str, tag: str) -> RegistryTag | None:
        try:
            artifact = self._request(
                "GET",
                f"/api/v2.0/projects/{project}/repositories/{_repository_path(repository)}"
                f"/artifacts/{tag}",
            )
        except RegistryError as exc:
            if exc.not_found:
                return None
            raise
        if not artifact:
            return None
        config = (artifact.get("extra") or {}).get("config") or {}
        return RegistryTag(
            project=project,
            repository=repository,
            tag=tag,
            digest=artifact.get("digest"),
            size_bytes=artifact.get("size"),
            layer_count=_layer_count(config.get("mediaType"), config),
            pushed_at=None,
        )

    def delete_tag(self, project: str, repository: str, tag: str) -> None:
        # Harbor deletes a tag through the repository's artifacts endpoint with
        # the tag as the reference. `DELETE /tags/{tag}` is not a route and
        # returns 405.
        self._request(
            "DELETE",
            f"/api/v2.0/projects/{project}/repositories/{_repository_path(repository)}"
            f"/artifacts/{tag}",
        )

    def close(self) -> None:
        self._client.close()


def _layer_count(media_type: object, config: dict[str, Any]) -> int | None:
    """Harbor only reports layers for real image manifests.

    An OCI index or a Helm chart has no layers, and reporting ``0`` for them
    would be a lie that the "is this a stub?" heuristic would act on.
    """
    if media_type and "manifest" not in str(media_type):
        return None
    layers = config.get("history") or config.get("layers")
    return len(layers) if isinstance(layers, list) else None
