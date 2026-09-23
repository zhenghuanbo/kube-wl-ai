"""Container registry integration.

Callers obtain a client via :func:`get_registry` rather than constructing one, so
that tests and cluster-less deployments can substitute the fake — the same shape
as :mod:`app.k8s`.

The opt-in rule mirrors the Kubernetes gateway's, for the same reason: a machine
with an ambient Harbor URL in its environment should not be able to delete tags
on whatever registry that URL names.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.registries.fake import FakeRegistryClient
from app.registries.registry import (
    RegistryClient,
    RegistryError,
    RegistryProject,
    RegistryStatus,
    RegistryTag,
)

logger = get_logger(__name__)

__all__ = [
    "FakeRegistryClient",
    "RegistryClient",
    "RegistryError",
    "RegistryProject",
    "RegistryStatus",
    "RegistryTag",
    "get_registry",
    "parse_reference",
    "set_registry",
]

_client: RegistryClient | None = None


def get_registry(settings: Settings | None = None) -> RegistryClient:
    """Return the process-wide registry client, building it on first use."""
    global _client
    if _client is not None:
        return _client

    resolved = settings or get_settings()
    if not resolved.harbor_url:
        logger.warning(
            "harbor_disabled",
            hint="set HARBOR_URL (and robot credentials) to use a real registry",
        )
        _client = FakeRegistryClient()
        return _client

    try:
        from app.registries.harbor import RealHarborClient

        _client = RealHarborClient(
            resolved.harbor_url,
            username=resolved.harbor_robot_name,
            password=resolved.harbor_robot_secret.get_secret_value(),
            verify=resolved.harbor_verify_tls,
        )
    except Exception as exc:
        # Unreachable registry is a supported way to run; log it loudly so a
        # production misconfiguration is not mistaken for an intentional one.
        logger.warning("harbor_unavailable", error=str(exc))
        _client = FakeRegistryClient()
    return _client


def set_registry(client: RegistryClient | None) -> None:
    """Override the client — used by tests."""
    global _client
    _client = client


def parse_reference(reference: str, default_host: str = "") -> dict[str, Any]:
    """Split an image reference into its parts.

    ``harbor.x.edu/library/pytorch:2.4-cuda12`` becomes host, project,
    repository and tag. The registry's own hosting rules are honoured where they
    are unambiguous:

    * a first component containing ``.`` or ``:``, or equal to ``localhost``, is
      a host — otherwise ``ubuntu:22.04`` would be read as host ``ubuntu``;
    * a reference with no slash has no project, so it defaults to ``library``,
      which is where Harbor puts it;
    * a reference with no colon defaults to ``latest``;
    * a ``@sha256:`` digest wins over the tag, because a reference carrying both
      is pulled by digest and recording the tag would misdescribe it.

    The project is the **first** path segment after the host and the repository
    is **everything after it**, so repositories nest:
    ``harbor.x.edu/lab-vision/zhangsan/my-env:v1`` is project ``lab-vision`` and
    repository ``zhangsan/my-env``. That split is what the platform's image
    scheme is built on — one project per group, with the owner's username as the
    repository's first segment. Splitting at the *last* slash instead read the
    project as ``lab-vision/zhangsan``, which no registry would ever have a
    project by; worse, the sync path builds its rows from what Harbor reports
    (``project=lab-vision, repository=zhangsan/my-env``), so the same image got
    two different catalogue rows depending on which code path wrote it.

    Rejects rather than guesses: a malformed reference stored in the catalogue
    surfaces much later as an ``ImagePullBackOff``, which is precisely the
    problem this catalogue exists to prevent.
    """
    value = reference.strip()
    if not value:
        raise RegistryError("镜像地址不能为空")

    digest: str | None = None
    if "@" in value:
        value, _, digest = value.partition("@")
        digest = digest or None

    tag = "latest"
    # A colon after the last slash is a tag; before it, it is a host port.
    last_segment = value.rsplit("/", 1)[-1]
    if ":" in last_segment:
        value, _, tag = value.rpartition(":")
        tag = tag or "latest"

    if not value:
        raise RegistryError(f"镜像地址不合法：{reference}")

    parts = value.split("/")
    host = default_host
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        host = parts.pop(0)

    if not parts or not all(parts):
        # `all` rather than `parts[-1]`: an empty segment anywhere (``a//b``,
        # ``a/b/``) makes the joined repository ambiguous, and a reference the
        # platform cannot round-trip back to a name is worse than a rejection.
        raise RegistryError(f"镜像地址不合法：{reference}")

    if len(parts) == 1:
        # No project in the reference at all: Harbor files it under `library`.
        project = "library"
        repository = parts[0]
    else:
        project = parts[0]
        repository = "/".join(parts[1:])

    if not host:
        raise RegistryError(f"镜像地址缺少仓库主机名：{reference}（请写成 host/project/name:tag）")

    return {
        "host": host,
        "project": project,
        "repository": repository,
        "tag": tag,
        "digest": digest,
    }
