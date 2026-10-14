"""Instance and SSH-key endpoints.

Instances are the platform's most permission-dense resource: a caller must hold
the right permission code, be within the workspace's data scope, and stay inside
the workspace's quota. The first two are handled here and by ``policy``; the
third is enforced in the service, because it needs a consistent read of current
usage and must not be split across layers.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.auth.policy import PermissionCheck
from app.core.config import Settings
from app.core.deps import AppSettings, CurrentUser, DbSession, require
from app.k8s import get_gateway
from app.models.enums import IdeType, InstanceStatus
from app.schemas.base import ApiMessage
from app.schemas.common import Page, PageMeta
from app.schemas.instance import (
    IdeAccess,
    InstanceAction,
    InstanceCreate,
    InstanceDetail,
    InstanceSummary,
    InstanceUpdate,
    SshKeyCreate,
    SshKeyRead,
)
from app.services import instances as instance_service

router = APIRouter(tags=["instances"])

CanRead = Depends(require("instance:read"))
CanCreate = Depends(require("instance:create"))
CanUpdate = Depends(require("instance:update"))
CanDelete = Depends(require("instance:delete"))
CanStart = Depends(require("instance:start"))
CanStop = Depends(require("instance:stop"))
CanExec = Depends(require("instance:exec"))


def _pages(total: int, page_size: int) -> int:
    return (total + page_size - 1) // page_size if page_size else 0


def _detail(instance: object, settings: Settings) -> InstanceDetail:
    summary = instance_service.to_summary(instance)  # type: ignore[arg-type]
    return InstanceDetail(
        **summary.model_dump(),
        k8s_deployment=instance.k8s_deployment,  # type: ignore[attr-defined]
        k8s_ssh_service=instance.k8s_ssh_service,  # type: ignore[attr-defined]
        k8s_ide_service=instance.k8s_ide_service,  # type: ignore[attr-defined]
        k8s_ingress=instance.k8s_ingress,  # type: ignore[attr-defined]
        k8s_pvc=instance.k8s_pvc,  # type: ignore[attr-defined]
        pod_name=instance.pod_name,  # type: ignore[attr-defined]
        ssh_command=instance_service.ssh_command(instance, settings),  # type: ignore[arg-type]
        ide_path=instance_service.ide_path(instance),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# SSH keys (self-service)
# ---------------------------------------------------------------------------
#: Keys are managed under the caller's own identity, like `/users/me`. There is
#: no `ssh_key:*` permission code because there is nothing to authorise: a user
#: may only ever touch their own keys, and that is enforced by ownership rather
#: than by a grant an administrator could widen.
@router.get("/ssh-keys", response_model=list[SshKeyRead])
async def list_ssh_keys(session: DbSession, actor: CurrentUser) -> list[SshKeyRead]:
    keys = await instance_service.list_ssh_keys(session, actor.id)
    return [SshKeyRead.model_validate(key, from_attributes=True) for key in keys]


@router.post("/ssh-keys", response_model=SshKeyRead, status_code=status.HTTP_201_CREATED)
async def add_ssh_key(session: DbSession, actor: CurrentUser, payload: SshKeyCreate) -> SshKeyRead:
    """Register a public key.

    Only the public half ever reaches the platform; it is mounted into instances
    as ``authorized_keys``. The fingerprint is computed from the key material,
    so the same key cannot be registered twice under different names.
    """
    key = await instance_service.add_ssh_key(
        session, actor, name=payload.name, public_key=payload.public_key
    )
    return SshKeyRead.model_validate(key, from_attributes=True)


@router.delete("/ssh-keys/{key_id}", response_model=ApiMessage)
async def delete_ssh_key(session: DbSession, actor: CurrentUser, key_id: int) -> ApiMessage:
    await instance_service.delete_ssh_key(session, actor, key_id)
    return ApiMessage(message="公钥已删除。正在运行的实例将在下次重新下发后更新。")


# ---------------------------------------------------------------------------
# Instances
# ---------------------------------------------------------------------------
@router.get("/instances", response_model=Page[InstanceSummary])
async def list_instances(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanRead],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, alias="pageSize")] = 20,
    search: Annotated[str | None, Query(max_length=150)] = None,
    workspace_id: Annotated[int | None, Query(alias="workspaceId")] = None,
    owner_user_id: Annotated[int | None, Query(alias="ownerUserId")] = None,
    status_filter: Annotated[InstanceStatus | None, Query(alias="status")] = None,
    ide_type: Annotated[IdeType | None, Query(alias="ideType")] = None,
) -> Page[InstanceSummary]:
    """A user sees their own instances; an administrator sees their scope's."""
    result = await instance_service.list_instances(
        session,
        actor,
        workspace_id=workspace_id,
        owner_user_id=owner_user_id,
        status=status_filter,
        ide_type=ide_type,
        search=search,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return Page[InstanceSummary](
        items=[instance_service.to_summary(item) for item in result.items],
        meta=PageMeta(
            page=page,
            page_size=page_size,
            total=result.total,
            pages=_pages(result.total, page_size),
        ),
    )


@router.post("/instances", response_model=InstanceDetail, status_code=status.HTTP_201_CREATED)
async def create_instance(
    session: DbSession,
    actor: CurrentUser,
    settings: AppSettings,
    _: Annotated[PermissionCheck, CanCreate],
    payload: InstanceCreate,
) -> InstanceDetail:
    """Create an instance and apply its Kubernetes objects.

    Validation (ownership, pool grant, quota) happens before anything reaches
    the cluster, so a rejected request never leaves a partial workload behind.
    """
    instance = await instance_service.create_instance(session, actor, payload, get_gateway())
    return _detail(instance, settings)


@router.get("/instances/{instance_id}", response_model=InstanceDetail)
async def get_instance(
    session: DbSession,
    actor: CurrentUser,
    settings: AppSettings,
    _: Annotated[PermissionCheck, CanRead],
    instance_id: int,
) -> InstanceDetail:
    instance = await instance_service.get_instance(session, actor, instance_id)
    return _detail(instance, settings)


@router.patch("/instances/{instance_id}", response_model=InstanceDetail)
async def update_instance(
    session: DbSession,
    actor: CurrentUser,
    settings: AppSettings,
    _: Annotated[PermissionCheck, CanUpdate],
    instance_id: int,
    payload: InstanceUpdate,
) -> InstanceDetail:
    """Rename or re-describe. The resource shape is immutable by design."""
    instance = await instance_service.get_instance(session, actor, instance_id)
    instance = await instance_service.update_instance(session, instance, payload)
    return _detail(instance, settings)


@router.delete("/instances/{instance_id}", response_model=ApiMessage)
async def delete_instance(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanDelete],
    instance_id: int,
) -> ApiMessage:
    """Delete the workload. The home volume is deleted with it."""
    instance = await instance_service.get_instance(session, actor, instance_id)
    await instance_service.delete_instance(session, instance, get_gateway())
    return ApiMessage(message=f"实例 {instance.name} 已删除")


@router.post("/instances/{instance_id}/start", response_model=InstanceDetail)
async def start_instance(
    session: DbSession,
    actor: CurrentUser,
    settings: AppSettings,
    _: Annotated[PermissionCheck, CanStart],
    instance_id: int,
    payload: InstanceAction | None = None,
) -> InstanceDetail:
    """Scale the deployment back to one replica.

    The home volume is untouched, so work resumes where it stopped.
    """
    instance = await instance_service.get_instance(session, actor, instance_id)
    instance = await instance_service.start_instance(session, instance, get_gateway())
    return _detail(instance, settings)


@router.post("/instances/{instance_id}/stop", response_model=InstanceDetail)
async def stop_instance(
    session: DbSession,
    actor: CurrentUser,
    settings: AppSettings,
    _: Annotated[PermissionCheck, CanStop],
    instance_id: int,
    payload: InstanceAction | None = None,
) -> InstanceDetail:
    """Scale the deployment to zero.

    Quota stays reserved: restarting must not fail for lack of capacity the
    instance already held.
    """
    instance = await instance_service.get_instance(session, actor, instance_id)
    instance = await instance_service.stop_instance(session, instance, get_gateway())
    return _detail(instance, settings)


@router.post("/instances/{instance_id}/reconcile", response_model=InstanceDetail)
async def reconcile_instance(
    session: DbSession,
    actor: CurrentUser,
    settings: AppSettings,
    _: Annotated[PermissionCheck, CanUpdate],
    instance_id: int,
) -> InstanceDetail:
    """Re-apply the objects and refresh the observed status."""
    instance = await instance_service.get_instance(session, actor, instance_id)
    workspace = instance.workspace
    pool = instance.resource_group
    if pool is None:
        from app.core.errors import BadRequestError

        raise BadRequestError("实例所属资源组已删除，无法重新下发", code="resource_group_missing")
    # A stopped instance is rebuilt with zero replicas: repairing its objects
    # must not silently start it again.
    await instance_service.apply_cluster_state(
        session,
        instance,
        workspace,
        pool,
        get_gateway(),
        replicas=0 if instance.status is InstanceStatus.STOPPED else 1,
    )
    instance = await instance_service.reconcile_instance(session, instance, get_gateway())
    return _detail(instance, settings)


@router.get("/instances/{instance_id}/logs")
async def instance_logs(
    session: DbSession,
    actor: CurrentUser,
    _: Annotated[PermissionCheck, CanRead],
    instance_id: int,
    tail_lines: Annotated[int, Query(ge=1, le=5000, alias="tailLines")] = 500,
    previous: bool = False,
) -> dict[str, str]:
    """Container logs.

    ``previous=true`` reads the terminated container's output, which is the only
    way to see why a container that is now crash-looping actually died.
    """
    from app.core.errors import NotFoundError
    from app.k8s import run_gateway

    instance = await instance_service.get_instance(session, actor, instance_id)
    if not instance.pod_name:
        raise NotFoundError("实例尚未产生容器，暂时没有日志", code="instance_has_no_pod")
    output = await run_gateway(
        get_gateway().read_pod_logs,
        instance_service.instance_workspace_ns(instance),
        instance.pod_name,
        tail_lines=tail_lines,
        previous=previous,
    )
    return {"logs": output}


@router.get("/instances/{instance_id}/ide-access", response_model=IdeAccess)
async def ide_access(
    session: DbSession,
    actor: CurrentUser,
    settings: AppSettings,
    _: Annotated[PermissionCheck, CanExec],
    instance_id: int,
) -> IdeAccess:
    """Where to open the instance's web IDE.

    The URL is a path on the platform's own origin: the frontend proxies ``/x``
    to the backend, which forwards to the instance's ClusterIP. Nothing here has
    to be reachable from the browser directly.

    **It carries no credential.** This used to mint a short-lived ticket and
    append it as ``?ticket=``, and the plan was for the ingress to verify it —
    but no verification was ever written (``decode_ide_ticket`` is called only
    from tests), so the ticket was decoration. It is not minted here any more,
    because a credential nothing checks is worse than none: it looks like
    protection, it leaks into browser history and logs, and it invites the
    assumption that the route behind it is guarded. It is not, yet — see the note
    on :func:`app.api.x.proxy.ide_proxy` for what that means and where the check
    belongs when it is added.
    """
    from app.core.errors import BadRequestError

    instance = await instance_service.get_instance(session, actor, instance_id)
    if instance.ide_type is IdeType.NONE:
        raise BadRequestError("该实例未启用 Web IDE", code="instance_has_no_ide")

    path = instance_service.ide_path(instance)
    if path is None:  # pragma: no cover - `ide_type` was just checked
        raise BadRequestError("该实例未启用 Web IDE", code="instance_has_no_ide")
    return IdeAccess(url=path)
