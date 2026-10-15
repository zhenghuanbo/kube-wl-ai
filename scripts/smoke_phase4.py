#!/usr/bin/env python3
"""Phase 4 acceptance checks against a *running* API process.

Stdlib only, so it needs no virtualenv and can be run from anywhere:

    source scripts/smoke-env.sh
    (cd backend && uvicorn app.main:app --port 8000) &
    python scripts/smoke_phase4.py

It exercises the application catalogue (visibility, editing rights, launch) and
custom tasks (submission, the shell argv, quota sharing with instances, the
lifecycle transitions, cancel, delete and the timeline) over HTTP.

Why a separate script when there is already a pytest suite: the suite runs in
one process with a shared session, and that has hidden real defects twice now —
a ``lazy="joined"`` relationship that only load-on-query populates, and a
base64 padding assumption. Both looked fine in-process. This script talks to a
real uvicorn worker with its own session per request, which is where that class
of bug shows up.

The server runs with ``K8S_ENABLED`` unset, so the Kubernetes gateway is the
in-memory fake. These checks are about what the platform *would* apply, and they
must pass on a machine with a kubeconfig pointing at a live cluster — which is
precisely why the real gateway now requires an explicit opt-in.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000") + "/api/v1"
ADMIN_USERNAME = os.environ.get("SMOKE_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("SMOKE_ADMIN_PASSWORD", "smoke-test-password-123")

PASSWORD = "smoke-task-password-123"
SUFFIX = os.environ.get("SMOKE_SUFFIX", "p4")

passed = 0
failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  \033[32m✓\033[0m {label}")
    else:
        failures.append(label)
        print(f"  \033[31m✗\033[0m {label}" + (f" — {detail}" if detail else ""))


def call(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: object | None = None,
) -> tuple[int, object]:
    """Return ``(status, decoded_body)``; never raises on an HTTP error status."""
    request = urllib.request.Request(f"{BASE}{path}", method=method)
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        request.data = json.dumps(body).encode()
        request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read().decode()
            return response.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as error:
        payload = error.read().decode()
        try:
            decoded = json.loads(payload) if payload else None
        except json.JSONDecodeError:
            decoded = payload
        return error.code, decoded


def login(username: str, password: str) -> str | None:
    status, body = call(
        "POST", "/auth/login", body={"username": username, "password": password}
    )
    if status != 200 or not isinstance(body, dict):
        return None
    return str(body["accessToken"])


def error_code(body: object) -> str:
    if isinstance(body, dict):
        envelope = body.get("error")
        if isinstance(envelope, dict):
            return str(envelope.get("code", ""))
    return ""


def error_detail(body: object, key: str) -> object:
    if isinstance(body, dict):
        envelope = body.get("error")
        if isinstance(envelope, dict):
            detail = envelope.get("detail")
            if isinstance(detail, dict):
                return detail.get(key)
    return None


def ensure_user(admin: str, username: str, group_id: int | None = None) -> int | None:
    status, body = call(
        "POST",
        "/users",
        token=admin,
        body={
            "username": username,
            "password": PASSWORD,
            # `fullName`, not `displayName`: `User.display_name` is a derived
            # property (`full_name or username`), so there is no display-name
            # column to write. This script used to send `displayName`, which was
            # silently discarded — the account was still created, so the check
            # passed while verifying something that never happened.
            "fullName": username,
            "roleCodes": ["user"],
            # `groupIds`, plural: an account may be linked to several directory
            # groups. The singular spelling used to be silently ignored rather
            # than rejected, which is how this script first "passed" without ever
            # creating a group link at all.
            "groupIds": [group_id] if group_id is not None else [],
        },
    )
    if status == 201 and isinstance(body, dict):
        return int(body["id"])
    status, body = call("GET", f"/users?search={username}", token=admin)
    if status == 200 and isinstance(body, dict):
        for item in body["items"]:
            if item["username"] == username:
                return int(item["id"])
    return None


def ensure_home_volume(
    admin: str, user_id: int, workspace_id: int, username: str
) -> int | None:
    """Register the user's home directory, or find the one already registered.

    Neither an instance nor a task has a disk of its own: both mount the *owner's*
    storage volume at ``/home/{username}``, and creating one without a volume is
    refused with ``user_volume_missing``. Launching an application goes through
    instance creation, so this is a prerequisite of everything below.
    """
    status, body = call(
        "POST",
        "/storages",
        token=admin,
        body={
            "name": f"p4-home-{username}",
            "displayName": f"{username} 的主目录",
            "kind": "user_volume",
            "workspaceId": workspace_id,
            "ownerUserId": user_id,
            "capacityGb": 10,
        },
    )
    if status == 201 and isinstance(body, dict):
        return int(body["id"])
    # A re-run collides on the directory, not the name: a user's directory is fixed
    # by their group and username.
    if status == 409 and error_code(body) == "storage_path_taken":
        status, page = call("GET", "/storages?kind=user_volume&pageSize=200", token=admin)
        rows = page.get("items", []) if isinstance(page, dict) else []
        for row in rows:
            if row.get("ownerUsername") == username:
                return int(row["id"])
    return None


def main() -> int:
    print("\n\033[1mPhase 4 — 应用程序与自定义任务\033[0m")

    admin = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if admin is None:
        print("  \033[31m无法以管理员登录，冒烟环境是否已启动？\033[0m")
        return 1

    # --- fixtures ----------------------------------------------------------
    status, lab = call(
        "POST",
        "/groups",
        token=admin,
        body={
            "name": f"p4-lab-{SUFFIX}",
            "displayName": "P4 实验室",
            "kind": "lab",
        },
    )
    check("建用户组", status == 201, str(lab))
    if not isinstance(lab, dict):
        return 1
    group_id = int(lab["id"])

    status, other = call(
        "POST",
        "/groups",
        token=admin,
        body={
            "name": f"p4-other-{SUFFIX}",
            "displayName": "P4 另一个实验室",
            "kind": "lab",
        },
    )
    check("建第二个用户组", status == 201, str(other))
    other_group_id = int(other["id"]) if isinstance(other, dict) else 0

    status, pool = call(
        "POST",
        "/resource-groups",
        token=admin,
        body={
            "name": f"p4-pool-{SUFFIX}",
            "displayName": "P4 资源池",
            "kind": "public",
            "networkType": "internal",
            "maxRuntimeSeconds": 3600,
        },
    )
    check("建资源组", status == 201, str(pool))
    if not isinstance(pool, dict):
        return 1
    pool_id = int(pool["id"])

    status, limits = call(
        "PUT",
        f"/resource-groups/{pool_id}/limits",
        token=admin,
        body={
            "maxGpuPerUser": 1,
            "maxCpuCoresPerUser": 4,
            "maxMemoryGbPerUser": 8,
            "maxStorageGbPerUser": 20,
        },
    )
    check("设置资源组公平上限", status == 200, str(limits))
    check(
        "上限按核与 GB 记录（不再是 milli/MB）",
        isinstance(limits, dict)
        and limits.get("maxCpuCoresPerUser") == 4
        and limits.get("maxMemoryGbPerUser") == 8,
        str(limits),
    )

    status, spec = call(
        "POST",
        "/resource-specs",
        token=admin,
        body={
            "name": f"p4-spec-{SUFFIX}",
            "displayName": "P4 规格",
            "cpuMilli": 2000,
            "memoryMb": 4096,
            "gpuType": "none",
            "gpuCount": 0,
        },
    )
    check("建资源规格", status == 201, str(spec))
    if not isinstance(spec, dict):
        return 1
    spec_id = int(spec["id"])

    status, orphan = call(
        "POST",
        "/resource-specs",
        token=admin,
        body={
            "name": f"p4-orphan-{SUFFIX}",
            "displayName": "未挂载规格",
            "cpuMilli": 1000,
            "memoryMb": 1024,
            "gpuType": "none",
            "gpuCount": 0,
        },
    )
    check("建未挂载规格", status == 201, str(orphan))
    orphan_id = int(orphan["id"]) if isinstance(orphan, dict) else 0

    status, _ = call(
        "PUT", f"/resource-groups/{pool_id}/specs", token=admin, body=[spec_id]
    )
    check("规格挂到资源组", status == 200)

    status, workspace = call(
        "POST",
        "/workspaces",
        token=admin,
        body={
            "name": f"p4-ws-{SUFFIX}",
            "groupId": group_id,
            "resourceGroupIds": [pool_id],
        },
    )
    check("建工作空间并授权资源组", status == 201, str(workspace))
    if not isinstance(workspace, dict):
        return 1
    workspace_id = int(workspace["id"])

    worker_id = ensure_user(admin, f"p4-worker-{SUFFIX}", group_id)
    check("建普通用户并加入用户组", worker_id is not None)
    if worker_id is None:
        return 1
    status, _ = call(
        "POST",
        f"/workspaces/{workspace_id}/members",
        token=admin,
        body={"userId": worker_id, "moveFromExisting": True},
    )
    check("用户加入工作空间", status in (200, 201), str(status))

    # Everything this script launches — via an application or directly — mounts the
    # worker's own volume, so it has to exist before the first launch.
    home_id = ensure_home_volume(admin, worker_id, workspace_id, f"p4-worker-{SUFFIX}")
    check("为该用户登记主目录存储卷", home_id is not None, str(home_id))
    if home_id is None:
        return 1

    outsider_id = ensure_user(admin, f"p4-outsider-{SUFFIX}", other_group_id)
    check("建另一个实验室的用户", outsider_id is not None)

    token = login(f"p4-worker-{SUFFIX}", PASSWORD)
    check("普通用户登录", token is not None)
    if token is None:
        return 1

    # --- applications ------------------------------------------------------
    status, app = call(
        "POST",
        "/applications",
        token=token,
        body={
            "name": f"pytorch-{SUFFIX}",
            "displayName": "PyTorch 2.4",
            "summary": "开箱即用的 PyTorch 环境",
            "readme": "# PyTorch\n\n直接开始训练。",
            "category": "data_science",
            "image": "registry.local/pytorch:2.4",
            "ideType": "jupyter",
            "resourceGroupId": pool_id,
            "resourceSpecId": spec_id,
            "defaultCpuMilli": 2000,
            "defaultMemoryMb": 4096,
            "visibility": "workspace",
        },
    )
    check("创建应用程序", status == 201, str(app))
    if not isinstance(app, dict):
        return 1
    app_id = int(app["id"])
    check(
        "介绍信息被保存",
        app.get("readme", "").startswith("# PyTorch"),
        str(app.get("readme")),
    )
    check("启动次数初始为 0", app.get("launchCount") == 0, str(app.get("launchCount")))

    status, dup = call(
        "POST",
        "/applications",
        token=token,
        body={
            "name": f"pytorch-{SUFFIX}",
            "image": "x",
            "resourceGroupId": pool_id,
            "resourceSpecId": spec_id,
        },
    )
    check(
        "同组内应用重名被拒（409）",
        status == 409 and error_code(dup) == "application_name_taken",
        f"{status} {dup}",
    )

    status, bad_spec = call(
        "POST",
        "/applications",
        token=token,
        body={
            "name": f"broken-{SUFFIX}",
            "image": "x",
            "resourceGroupId": pool_id,
            "resourceSpecId": orphan_id,
        },
    )
    check(
        "未挂载到资源组的规格不能作为默认值",
        status == 400 and error_code(bad_spec) == "resource_spec_not_in_group",
        f"{status} {bad_spec}",
    )

    status, half = call(
        "POST",
        "/applications",
        token=token,
        body={
            "name": f"half-{SUFFIX}",
            "image": "x",
            "resourceGroupId": pool_id,
        },
    )
    check(
        "只填一半的资源默认值被拒",
        status == 400 and error_code(half) == "incomplete_resource_defaults",
        f"{status} {half}",
    )

    # An application visible only inside the workspace must not be listed by
    # another lab — and asking for it must be a 404, not a 403.
    outsider = login(f"p4-outsider-{SUFFIX}", PASSWORD)
    status, listing = call("GET", "/applications", token=outsider)
    check(
        "另一个实验室看不到本工作空间的应用",
        status == 200 and isinstance(listing, dict) and not listing["items"],
        str(listing),
    )
    status, hidden = call("GET", f"/applications/{app_id}", token=outsider)
    check(
        "越权访问返回 404 而非 403（不泄露存在性）",
        status == 404 and error_code(hidden) == "application_not_found",
        f"{status} {hidden}",
    )

    status, _ = call(
        "PATCH", f"/applications/{app_id}", token=token, body={"visibility": "group"}
    )
    check("作者可以把可见范围放宽到全组", status == 200)

    # Another member of the same workspace may read it but not rewrite it.
    peer_id = ensure_user(admin, f"p4-peer-{SUFFIX}", group_id)
    status, _ = call(
        "POST",
        f"/workspaces/{workspace_id}/members",
        token=admin,
        body={"userId": peer_id, "moveFromExisting": True},
    )
    peer = login(f"p4-peer-{SUFFIX}", PASSWORD)
    status, _ = call("GET", f"/applications/{app_id}", token=peer)
    check("同工作空间成员可以查看", status == 200, str(status))
    status, denied = call(
        "PATCH", f"/applications/{app_id}", token=peer, body={"displayName": "改过了"}
    )
    check("同工作空间成员不能修改他人的应用", status == 404, f"{status} {denied}")

    # --- launching ---------------------------------------------------------
    status, launched = call(
        "POST",
        f"/applications/{app_id}/launch",
        token=token,
        body={"name": f"from-app-{SUFFIX}"},
    )
    check("从应用启动实例", status == 201, str(launched))
    if isinstance(launched, dict):
        check(
            "实例继承了应用的镜像",
            launched.get("image") == "registry.local/pytorch:2.4",
        )
        check("实例继承了应用的 IDE", launched.get("ideType") == "jupyter")
        check("实例落在他自己的工作空间", launched.get("workspaceId") == workspace_id)
        check(
            "实例拿到了 SSH NodePort", bool(launched.get("sshNodePort")), str(launched)
        )
        check("实例记录了 Deployment 名", bool(launched.get("k8sDeployment")))
        app_instance_id = int(launched["id"])
    else:
        app_instance_id = 0

    status, refreshed = call("GET", f"/applications/{app_id}", token=token)
    check(
        "启动成功后计数 +1",
        isinstance(refreshed, dict) and refreshed.get("launchCount") == 1,
        str(refreshed),
    )

    status, _ = call(
        "POST",
        f"/applications/{app_id}/launch",
        token=token,
        body={"name": f"from-app-{SUFFIX}"},
    )
    status, refreshed = call("GET", f"/applications/{app_id}", token=token)
    check(
        "启动失败时计数不变",
        isinstance(refreshed, dict) and refreshed.get("launchCount") == 1,
        str(refreshed),
    )

    # Starting in somebody else's workspace must not be possible just because
    # the application itself is visible.
    status, stolen = call(
        "POST",
        f"/applications/{app_id}/launch",
        token=outsider,
        body={"workspaceId": workspace_id},
    )
    check(
        "他人不能借可见的应用把实例开进本工作空间",
        status in (403, 404),
        f"{status} {stolen}",
    )

    # --- tasks -------------------------------------------------------------
    status, task = call(
        "POST",
        "/tasks",
        token=token,
        body={
            "name": f"train-{SUFFIX}",
            "displayName": "训练任务",
            "resourceGroupId": pool_id,
            "resourceSpecId": spec_id,
            "image": "registry.local/pytorch:2.4",
            "script": "python train.py --epochs 3",
        },
    )
    check("提交自定义任务", status == 201, str(task))
    if not isinstance(task, dict):
        return 1
    task_id = int(task["id"])
    check(
        "初始状态为 running", task.get("status") == "running", str(task.get("status"))
    )
    check(
        "Job 名带 wl-job- 前缀",
        str(task.get("k8sJobName", "")).startswith("wl-job-"),
        str(task.get("k8sJobName")),
    )
    check("已记录开始时间", bool(task.get("startedAt")), str(task.get("startedAt")))

    status, dup = call(
        "POST",
        "/tasks",
        token=token,
        body={
            "name": f"train-{SUFFIX}",
            "resourceGroupId": pool_id,
            "resourceSpecId": spec_id,
            "image": "x",
            "script": "true",
        },
    )
    check(
        "同工作空间内任务重名被拒（409）",
        status == 409 and error_code(dup) == "task_name_taken",
        f"{status} {dup}",
    )

    status, too_long = call(
        "POST",
        "/tasks",
        token=token,
        body={
            "name": f"long-{SUFFIX}",
            "resourceGroupId": pool_id,
            "resourceSpecId": spec_id,
            "image": "x",
            "script": "true",
            "maxRuntimeSeconds": 7200,
        },
    )
    check(
        "超出资源组运行时长被拒",
        status == 400 and error_code(too_long) == "runtime_exceeds_pool_limit",
        f"{status} {too_long}",
    )

    status, bad_script = call(
        "POST",
        "/tasks",
        token=token,
        body={
            "name": f"huge-{SUFFIX}",
            "resourceGroupId": pool_id,
            "resourceSpecId": spec_id,
            "image": "x",
            "script": "x" * (64 * 1024 + 1),
        },
    )
    check(
        "超大脚本被拒",
        status == 400 and error_code(bad_script) == "script_too_large",
        f"{status} {bad_script}",
    )

    # A task and an instance share the same workspace quota. One instance
    # (2000m) plus one task (2000m) already fills the 4000m ceiling.
    status, second_task = call(
        "POST",
        "/tasks",
        token=token,
        body={
            "name": f"train2-{SUFFIX}",
            "resourceGroupId": pool_id,
            "resourceSpecId": spec_id,
            "image": "x",
            "script": "true",
        },
    )
    check(
        "第二个任务因 CPU 配额被拒（409）",
        status == 409 and error_code(second_task) == "quota_exceeded",
        f"{status} {second_task}",
    )
    check(
        "配额错误指明是 CPU",
        error_detail(second_task, "resource") == "cpu",
        str(second_task),
    )

    # --- lifecycle ---------------------------------------------------------
    status, logs = call("GET", f"/tasks/{task_id}/logs", token=token)
    check(
        "无容器时日志返回明确错误",
        status == 400 and error_code(logs) == "task_no_pod",
        f"{status} {logs}",
    )

    status, cancelled = call("POST", f"/tasks/{task_id}/cancel", token=token)
    check(
        "取消任务",
        status == 200
        and isinstance(cancelled, dict)
        and cancelled.get("status") == "cancelled",
        str(cancelled),
    )
    check(
        "取消后记录保留（可查询）",
        call("GET", f"/tasks/{task_id}", token=token)[0] == 200,
    )

    status, again = call("POST", f"/tasks/{task_id}/cancel", token=token)
    check(
        "已结束的任务不能再次取消",
        status == 400 and error_code(again) == "task_already_finished",
        f"{status} {again}",
    )

    # Releasing the task's quota must let the instance be stopped and recreated.
    status, freed = call(
        "POST",
        "/tasks",
        token=token,
        body={
            "name": f"after-{SUFFIX}",
            "resourceGroupId": pool_id,
            "resourceSpecId": spec_id,
            "image": "x",
            "script": "true",
        },
    )
    check("取消后配额被释放（终态任务不再占用）", status == 201, str(freed))
    after_task_id = int(freed["id"]) if isinstance(freed, dict) else 0

    status, events = call("GET", f"/tasks/{task_id}/events", token=token)
    kinds = [e.get("eventType") for e in events] if isinstance(events, list) else []
    check("任务时间线记录了创建", "created" in kinds, str(kinds))
    check("任务时间线记录了取消", "stopped" in kinds, str(kinds))
    check(
        "时间线记录了操作人",
        any(e.get("actorUsername") == f"p4-worker-{SUFFIX}" for e in events)
        if isinstance(events, list)
        else False,
        str(events),
    )

    status, events = call("GET", f"/applications/{app_id}/events", token=token)
    kinds = [e.get("eventType") for e in events] if isinstance(events, list) else []
    check("应用时间线记录了创建", "created" in kinds, str(kinds))

    status, _ = call("DELETE", f"/tasks/{after_task_id}", token=token)
    check("删除任务", status == 204, str(status))
    status, listing = call("GET", "/tasks", token=token)
    names = [t["name"] for t in listing["items"]] if isinstance(listing, dict) else []
    check("已删除任务从活跃列表中消失", f"after-{SUFFIX}" not in names, str(names))
    status, listing = call("GET", "/tasks?includeInactive=true", token=token)
    names = [t["name"] for t in listing["items"]] if isinstance(listing, dict) else []
    check("已删除任务仍在历史中", f"after-{SUFFIX}" in names, str(names))

    # --- visibility --------------------------------------------------------
    status, listing = call("GET", "/tasks", token=outsider)
    check(
        "另一个实验室看不到本工作空间的任务",
        status == 200 and isinstance(listing, dict) and not listing["items"],
        str(listing),
    )
    status, hidden = call("GET", f"/tasks/{task_id}", token=outsider)
    check("越权查看任务返回 404", status == 404, f"{status} {hidden}")

    status, _ = call("DELETE", f"/applications/{app_id}", token=token)
    status, retired = call("GET", f"/applications/{app_id}", token=token)
    check(
        "下架后作者仍可看到记录",
        status == 200
        and isinstance(retired, dict)
        and retired.get("isActive") is False,
        str(retired),
    )
    status, refused = call(
        "POST", f"/applications/{app_id}/launch", token=token, body={}
    )
    check(
        "下架的应用不能启动",
        status == 400 and error_code(refused) == "application_inactive",
        f"{status} {refused}",
    )

    status, _ = call("DELETE", f"/instances/{app_instance_id}", token=token)

    # ------------------------------------------------------------------
    # Unknown request fields are refused, not ignored.
    #
    # This is the check that would have caught two real bugs: `groupId` sent
    # where the API wants `groupIds`, and query parameters that silently
    # dropped a filter. Both returned 200 and did nothing.
    # ------------------------------------------------------------------
    print("\n\033[1m未知字段必须被拒绝\033[0m")

    status, body = call(
        "POST",
        "/tasks",
        token=token,
        body={
            "name": f"typo-{SUFFIX}",
            "resourceGroupId": pool_id,
            "resourceSpecId": spec_id,
            "image": "registry.local/pytorch:2.4",
            "script": "echo hi",
            # `maxRuntimeSecond` — a typo a frontend author would plausibly make.
            "maxRuntimeSecond": 600,
        },
    )
    check(
        "任务体里的拼错字段被拒绝（422）",
        status == 422 and error_code(body) == "validation_failed",
        f"{status} {body}",
    )
    check(
        "错误信息点名了出错的字段",
        "maxRuntimeSecond" in json.dumps(body, ensure_ascii=False),
        json.dumps(body, ensure_ascii=False)[:200],
    )

    status, body = call(
        "POST",
        f"/applications/{app_id}/launch",
        token=token,
        body={"name": f"typo-launch-{SUFFIX}", "stroageGb": 10},
    )
    check(
        "启动体里的拼错字段被拒绝（422）",
        status == 422 and error_code(body) == "validation_failed",
        f"{status} {body}",
    )

    print("\n" + "─" * 46)
    if failures:
        print(f"\033[31m{passed}/{passed + len(failures)} 通过\033[0m，失败项：")
        for name in failures:
            print(f"  · {name}")
        return 1
    print(f"\033[32m全部 {passed} 项通过\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
