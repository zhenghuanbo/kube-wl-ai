"""Schemas for compute quota profiles.

A profile is a ceiling an administrator attaches to a user or to a workspace, so
these bodies are the whole of what an administrator can decide. Two rules run
through them:

* ``-1`` (:data:`~app.models.quota_profile.UNLIMITED`) switches a dimension off.
  It is accepted anywhere a ceiling is, and it is the only way to say "do not
  check this" — ``0`` keeps meaning "none allowed".
* A dimension belonging to the *other* kind is **refused**, not ignored. A
  workspace profile has no use for "how many instances may one person hold", and
  accepting the field quietly would look exactly like it had taken effect.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from app.models.enums import QuotaProfileKind
from app.models.quota_profile import UNLIMITED
from app.schemas.base import ApiModel

#: Bounds. High enough never to be the policy, low enough that a slipped digit
#: (``1000000`` cores) is caught at the edge rather than in the database.
_COUNT_MAX = 10_000
_CPU_MAX = 4096
_MEMORY_MAX = 65536
_GPU_MAX = 1024

#: Which dimensions belong to which kind, used by the applicability rule below and
#: by the service (an update does not carry ``kind``, so it checks against the row).
USER_ONLY_DIMENSIONS = ("max_instances", "max_applications", "max_tasks")
WORKSPACE_ONLY_DIMENSIONS = ("max_pods", "max_deployments", "max_services", "max_jobs")


def misplaced_dimensions(kind: QuotaProfileKind, values: dict[str, object]) -> list[str]:
    """Dimensions present in ``values`` that do not apply to ``kind``."""
    wrong = WORKSPACE_ONLY_DIMENSIONS if kind is QuotaProfileKind.USER else USER_ONLY_DIMENSIONS
    return sorted(name for name in wrong if values.get(name) is not None)


def applicability_error(kind: QuotaProfileKind, offending: list[str]) -> str:
    return (
        f"{kind.value} 配额模板不支持这些维度：{', '.join(offending)}"
        "（用户模板用实例数/应用数/自定义业务数，"
        "工作空间模板用 Pod/部署/服务/任务对象数）"
    )


class _DimensionFields(ApiModel):
    """The compute ceilings plus the kind-specific counts, shared by create/update.

    Every field is optional so that omitting one means "leave it to the configured
    default" — that is what makes ``{"kind": "user", "name": "small"}`` a complete,
    sensible request.
    """

    cpu_cores: int | None = Field(default=None, ge=UNLIMITED, le=_CPU_MAX)
    memory_gb: int | None = Field(default=None, ge=UNLIMITED, le=_MEMORY_MAX)
    gpu: int | None = Field(default=None, ge=UNLIMITED, le=_GPU_MAX)

    max_instances: int | None = Field(default=None, ge=UNLIMITED, le=_COUNT_MAX)
    max_applications: int | None = Field(default=None, ge=UNLIMITED, le=_COUNT_MAX)
    max_tasks: int | None = Field(default=None, ge=UNLIMITED, le=_COUNT_MAX)
    max_pods: int | None = Field(default=None, ge=UNLIMITED, le=_COUNT_MAX)
    max_deployments: int | None = Field(default=None, ge=UNLIMITED, le=_COUNT_MAX)
    max_services: int | None = Field(default=None, ge=UNLIMITED, le=_COUNT_MAX)
    max_jobs: int | None = Field(default=None, ge=UNLIMITED, le=_COUNT_MAX)

    @model_validator(mode="after")
    def _dimensions_match_kind(self) -> Self:
        kind = getattr(self, "kind", None)
        if kind is None:
            # An update does not carry `kind`; the service checks it against the
            # stored row instead of guessing here.
            return self
        offending = misplaced_dimensions(
            kind, {name: getattr(self, name) for name in self.model_fields_set}
        )
        if offending:
            raise ValueError(applicability_error(kind, offending))
        return self


class QuotaProfileRead(ApiModel):
    id: int
    kind: QuotaProfileKind
    name: str
    display_name: str
    description: str | None = None

    cpu_cores: int
    memory_gb: int
    gpu: int

    max_instances: int | None = None
    max_applications: int | None = None
    max_tasks: int | None = None

    max_pods: int | None = None
    max_deployments: int | None = None
    max_services: int | None = None
    max_jobs: int | None = None

    is_default: bool
    is_active: bool


class QuotaProfileCreate(_DimensionFields):
    kind: QuotaProfileKind
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    display_name: str = Field(default="", max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    is_default: bool = False


class QuotaProfileUpdate(_DimensionFields):
    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    is_default: bool | None = None
    is_active: bool | None = None


class QuotaProfileAssign(ApiModel):
    """Attach a profile to a user or a workspace.

    ``None`` means "follow the default again", which is why the field is nullable
    rather than the endpoint having a separate "unassign" verb.
    """

    profile_id: int | None = None
