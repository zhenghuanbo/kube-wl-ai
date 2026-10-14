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
    """Which web IDE an instance exposes, if any.

    Exposed by the platform's own reverse proxy under ``/x/{user}/{id}/lab``, not
    by a cluster ingress: that is what keeps the IDE on the platform's origin.
    """

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
    STORAGE = "storage"
    IMAGE = "image"


class WorkloadEventType(StrEnum):
    """What happened. Kept small so the timeline stays filterable."""

    CREATED = "created"
    STARTED = "started"
    STOPPED = "stopped"
    DELETED = "deleted"
    FAILED = "failed"
    STATUS_CHANGED = "status_changed"
    UPDATED = "updated"


class UsageKind(StrEnum):
    """Which kind of workload consumed the resources.

    Deliberately narrower than :class:`WorkloadKind`. That enum also covers
    workspaces, applications, storages and images, none of which hold a
    accelerator for any length of time — letting those values into a usage row
    would invite summing a column that can never legitimately be non-zero.
    """

    INSTANCE = "instance"
    TASK = "task"


class StorageKind(StrEnum):
    """What a managed volume *is*, which fixes who owns it and where it lives.

    Not taxonomy: the kind determines two things at once — what the volume must
    be bound to, and which directory under ``settings.storage_root`` it maps to.

    ``USER_VOLUME``
        One per user, at ``{root}/{group}/{username}``. Every workload the user
        starts mounts it, so it is where their code, environments and results
        actually live — which is what makes a container disposable.
    ``GROUP_SHARE``
        Belongs to a group. With a directory name it is
        ``{root}/{group}/{dir_name}``; **without one it is the group's own
        directory** ``{root}/{group}``, which exposes every member's home and is
        therefore mountable only by a group administrator.
    ``GLOBAL_SHARE``
        Belongs to nobody, at ``{root}/_global/{name}``, for data shared across
        the whole installation.
    """

    USER_VOLUME = "user_volume"
    GROUP_SHARE = "group_share"
    GLOBAL_SHARE = "global_share"


class StorageStatus(StrEnum):
    """Lifecycle of a volume.

    These mirror the PersistentVolume and PersistentVolumeClaim phases rather
    than inventing a parallel vocabulary, so an operator reading
    ``kubectl get pv,pvc`` and the UI see the same words.

    ``AVAILABLE`` is the one value the cluster does not have a claim-side word
    for: it means the directory is registered and ready, and nothing has claimed
    it yet. A global share sits here until the first workload mounts it, because
    a claim lives in a namespace and a global share belongs to none.
    """

    AVAILABLE = "available"
    PENDING = "pending"
    BOUND = "bound"
    RESIZING = "resizing"
    RELEASED = "released"
    FAILED = "failed"
    DELETING = "deleting"


#: Statuses in which a volume still occupies its capacity.
LIVE_STORAGE_STATUSES: frozenset[StorageStatus] = frozenset(
    {
        StorageStatus.AVAILABLE,
        StorageStatus.PENDING,
        StorageStatus.BOUND,
        StorageStatus.RESIZING,
        StorageStatus.FAILED,
    }
)


class ImageKind(StrEnum):
    """Where an image stands in the catalogue.

    ``PERSONAL`` images belong to one user and are pushed by them;
    ``OFFICIAL`` ones are curated by an administrator and are what the picker
    offers by default; ``SHARED`` covers a group's agreed images.
    """

    PERSONAL = "personal"
    OFFICIAL = "official"
    SHARED = "shared"


class ImageVisibility(StrEnum):
    """Who may *see* an image in the catalogue.

    Distinct from :class:`~app.models.enums.ApplicationVisibility` only in
    audience; the same four steps apply, and the same rule holds: visibility
    governs reads, not ownership.
    """

    PRIVATE = "private"
    WORKSPACE = "workspace"
    GROUP = "group"
    GLOBAL = "global"
