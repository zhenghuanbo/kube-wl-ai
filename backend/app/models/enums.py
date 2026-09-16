"""Enumerations shared by the ORM models.

All are ``StrEnum`` subclasses persisted as ``VARCHAR`` + ``CHECK`` (see
``app.db.base.str_enum``), so the stored values are the lower-case strings used
throughout the API and the frontend.
"""

from __future__ import annotations

from enum import StrEnum


class UserSource(StrEnum):
    """Where the identity came from."""

    LOCAL = "local"
    IPA = "ipa"
    OIDC = "oidc"


class GroupKind(StrEnum):
    """A lab group owns workspaces; the public group is the shared fallback."""

    LAB = "lab"
    PUBLIC = "public"


class GroupRole(StrEnum):
    """A user's standing *inside* one group."""

    MEMBER = "member"
    GROUP_ADMIN = "group_admin"


class RoleCode(StrEnum):
    """The four business roles (plan §5.3)."""

    OBSERVER = "observer"
    USER = "user"
    GROUP_ADMIN = "group_admin"
    GLOBAL_ADMIN = "global_admin"


class RoleScope(StrEnum):
    """Where a role assignment applies.

    ``GROUP`` scoped assignments carry a ``group_id`` — this is what makes
    ``group_admin`` inherently limited to one group.
    """

    GLOBAL = "global"
    GROUP = "group"


class WorkspaceStatus(StrEnum):
    """Lifecycle of a workspace.

    A workspace is only ``ACTIVE`` once its Kubernetes namespace, quota and
    network policy all exist; ``ERROR`` means provisioning half-finished and the
    reconcile loop will retry.
    """

    PENDING = "pending"
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETING = "deleting"
    ERROR = "error"


class WorkspaceRole(StrEnum):
    """A user's standing inside a workspace.

    Distinct from :class:`GroupRole`: administering a lab does not automatically
    give you the right to delete someone's workspace, and a workspace owner is
    not thereby a group admin.
    """

    OWNER = "owner"
    MEMBER = "member"


class ResourceGroupKind(StrEnum):
    """Who may use a resource group.

    ``PUBLIC`` groups are offered to every workspace; ``PRIVATE`` groups must be
    granted explicitly (plan §5.2) — that grant is the *only* binding between a
    tenant and a pool of hardware.
    """

    PUBLIC = "public"
    PRIVATE = "private"


class NetworkType(StrEnum):
    """Egress posture of a workspace namespace.

    Every workspace starts from default-deny; this widens it deliberately.
    """

    ISOLATED = "isolated"
    INTERNAL = "internal"
    INTERNET = "internet"


class NodeStatus(StrEnum):
    """Mirrors the Kubernetes node condition, cached for scheduling decisions."""

    READY = "ready"
    NOT_READY = "not_ready"
    CORDONED = "cordoned"
    UNKNOWN = "unknown"


class GpuDeviceStatus(StrEnum):
    """Whether a physical accelerator is usable right now."""

    AVAILABLE = "available"
    ALLOCATED = "allocated"
    UNHEALTHY = "unhealthy"


class QuotaScope(StrEnum):
    """What a quota row is attached to."""

    USER = "user"
    GROUP = "group"
    WORKSPACE = "workspace"


class QuotaResource(StrEnum):
    """The dimension a quota limits.

    Deliberately an open set implemented as a string column: ``gpu`` is
    per-model in practice (see :data:`app.models.resource.GPU_MODELS`), so the
    resource key is built at runtime rather than enumerated here.
    """

    CPU = "cpu"
    MEMORY = "memory"
    GPU = "gpu"
    STORAGE = "storage"
    INSTANCE = "instance"
    TASK = "task"


class InstanceStatus(StrEnum):
    """Lifecycle of a container instance.

    Mirrors what the UI shows, not the Kubernetes phase: ``PENDING`` is "accepted
    but nothing applied yet", ``CREATING`` is "objects applied, waiting for the
    pod", and ``RUNNING`` is "the pod is up". Collapsing those would leave a user
    staring at a spinner with no idea which half is stuck.
    """

    PENDING = "pending"
    CREATING = "creating"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    DELETING = "deleting"
    ERROR = "error"


class IdeType(StrEnum):
    """Which web IDE an instance exposes through the ingress, if any."""

    NONE = "none"
    JUPYTER = "jupyter"
    VSCODE = "vscode"
    WEBSSH = "webssh"


class ApplicationVisibility(StrEnum):
    """Who may see and launch an application.

    Widening is deliberate and explicit: a private definition is the author's
    scratch space, ``workspace`` shares it with colleagues in the same
    namespace, ``group`` with the whole lab, and ``global`` with the university.
    """

    PRIVATE = "private"
    WORKSPACE = "workspace"
    GROUP = "group"
    GLOBAL = "global"


class ApplicationCategory(StrEnum):
    """A coarse grouping for the application catalogue.

    Deliberately short. A long taxonomy decays into "other" the moment nobody
    curates it, and the search box is what people actually use.
    """

    DEVELOPMENT = "development"
    DATA_SCIENCE = "data_science"
    VISUALIZATION = "visualization"
    DATABASE = "database"
    WEB = "web"
    OTHER = "other"


class TaskStatus(StrEnum):
    """Lifecycle of a one-shot task (a Kubernetes Job).

    ``TIMEOUT`` is kept apart from ``FAILED``: hitting your own runtime limit is
    a different problem from your code returning non-zero, and the fix is
    different too.
    """

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    ERROR = "error"


#: Statuses after which a task will never change again.
TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.TIMEOUT,
        TaskStatus.CANCELLED,
        TaskStatus.ERROR,
    }
)


class WorkloadKind(StrEnum):
    """What a :class:`~app.models.workload_event.WorkloadEvent` is about.

    A single string column rather than four nullable foreign keys: an event
    belongs to exactly one kind, and four columns would make every query filter
    on three NULLs.
    """

    WORKSPACE = "workspace"
    INSTANCE = "instance"
    APPLICATION = "application"
    TASK = "task"


class WorkloadEventType(StrEnum):
    """What happened. Kept small so the timeline stays filterable."""

    CREATED = "created"
    STARTED = "started"
    STOPPED = "stopped"
    DELETED = "deleted"
    FAILED = "failed"
    STATUS_CHANGED = "status_changed"
