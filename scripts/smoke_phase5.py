#!/usr/bin/env python3
"""Phase 5 smoke: storage volumes against a running backend.

Run with::

    source scripts/smoke-env.sh
    K8S_ENABLED=false .venv/bin/uvicorn app.main:app --port 8000 &
    python3 scripts/smoke_phase5.py

Checks only what a live HTTP round-trip can prove — status codes, the wire
shape, and the permission boundary. The cluster-side assertions live in the
pytest suite, where ``FakeK8sGateway`` makes them observable.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8000/api/v1")
ADMIN_USERNAME = os.environ.get("SMOKE_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("SMOKE_ADMIN_PASSWORD", "smoke-test-password-123")
SUFFIX = os.environ.get("SMOKE_SUFFIX", "p5")
#: Storage names are unique per (workspace, name) with no upsert, so a re-run
#: against an existing database would collide. A per-run token keeps the script
#: re-runnable, which matters because a smoke script that only works once gets
#: deleted rather than re-run.
RUN = os.environ.get("SMOKE_RUN", "") or str(int(time.time()))


def sname(base: str) -> str:
    return f"{base}-{RUN}"


PASSWORD = "smoke-storage-password-123"

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
    method: str, path: str, *, token: str | None = None, body: object | None = None
) -> tuple[int, object]:
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


def first_row(page: object) -> object:
    """Pull the first item out of a listing.

    Collection endpoints are not uniform: some return a page envelope and some
    return a bare array, and a smoke script that assumes the wrong one fails in a
    way that looks like a server error.
    """
    if isinstance(page, dict) and isinstance(page.get("items"), list):
        return page["items"][0] if page["items"] else None
    if isinstance(page, list):
        return page[0] if page else None


def find_by_name(page: object, name: str) -> dict | None:
    """The row whose ``name`` is exactly ``name``, or ``None``.

    Looking a fixture back up with ``first_row`` is how this script once ran
    against a CPU-only spec while its checks claimed to be testing storage:
    when the server-side filter silently stopped applying, "the first row" became
    "an unrelated object", and nothing failed.
    """
    rows = page.get("items", []) if isinstance(page, dict) else page
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("name") == name:
            return row
    return None


def find_user_volume(token: str, username: str) -> dict | None:
    """The live personal volume of ``username``, whatever it happens to be called.

    A user's directory is fixed by the group and the username alone, so it is the
    one fixture a re-run cannot rename its way out of colliding with. Finding the
    row that already owns the directory is the only honest way to re-run.
    """
    _, page = call("GET", "/storages?kind=user_volume&pageSize=200", token=token)
    rows = page.get("items", []) if isinstance(page, dict) else []
    for row in rows:
        if isinstance(row, dict) and row.get("ownerUsername") == username:
            return row
    return None


def find_group_level_volume(token: str, group_id: int) -> dict | None:
    """The live volume that *is* the group's own directory, by ``dir_name IS NULL``.

    Same reasoning as :func:`find_user_volume`: the group's directory has one path
    however many times it is registered.
    """
    _, page = call("GET", "/storages?kind=group_share&pageSize=200", token=token)
    rows = page.get("items", []) if isinstance(page, dict) else []
    for row in rows:
        if isinstance(row, dict) and row.get("isGroupLevel") and row.get("groupId") == group_id:
            return row
    return None


def login(username: str, password: str) -> str | None:
    status, body = call("POST", "/auth/login", body={"username": username, "password": password})
    if status != 200 or not isinstance(body, dict):
        return None
    return str(body["accessToken"])


def error_code(body: object) -> str:
    if isinstance(body, dict):
        envelope = body.get("error")
        if isinstance(envelope, dict):
            return str(envelope.get("code", ""))
    return ""


def ensure_user(admin: str, username: str, group_id: int) -> int | None:
    status, body = call(
        "POST",
        "/users",
        token=admin,
        body={
            "username": username,
            "password": PASSWORD,
            # `fullName`, not `displayName`: the display name is derived.
            "fullName": username,
            "roleCodes": ["user"],
            "groupIds": [group_id],
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


def main() -> int:
    print("\n\033[1mPhase 5 — 持久化存储\033[0m")

    admin = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if admin is None:
        print("  \033[31m无法以管理员登录，冒烟环境是否已启动？\033[0m")
        return 1
    check("管理员登录", True)

    # --- fixtures ---------------------------------------------------------
    status, lab = call(
        "POST",
        "/groups",
        token=admin,
        body={"name": f"p5-lab-{SUFFIX}", "displayName": "P5 实验室", "kind": "lab"},
    )
    if status == 409:
        lab = find_by_name(
            call("GET", f"/groups?search=p5-lab-{SUFFIX}", token=admin)[1], f"p5-lab-{SUFFIX}"
        )
        status = 200
    check("建用户组", status in (200, 201), str(lab))
    if not isinstance(lab, dict):
        return 1
    lab_id = int(lab["id"])

    status, pool = call(
        "POST",
        "/resource-groups",
        token=admin,
        body={"name": f"p5-pool-{SUFFIX}", "displayName": "P5 池", "kind": "public"},
    )
    if status == 409:
        pool = find_by_name(
            call("GET", f"/resource-groups?search=p5-pool-{SUFFIX}", token=admin)[1],
            f"p5-pool-{SUFFIX}",
        )
        status = 200
    check("建资源组", status in (200, 201), str(pool))
    if not isinstance(pool, dict):
        return 1
    pool_id = int(pool["id"])

    spec_name = f"p5-spec-{SUFFIX}"
    status, spec = call(
        "POST",
        "/resource-specs",
        token=admin,
        body={
            "name": spec_name,
            "displayName": "P5 规格",
            "cpuMilli": 1000,
            "memoryMb": 1024,
            "gpuType": "none",
            "gpuCount": 0,
        },
    )
    if status == 409:
        spec = find_by_name(
            call("GET", f"/resource-specs?search={spec_name}", token=admin)[1], spec_name
        )
        status = 200
    check("建资源规格", status in (200, 201), str(spec))
    if not isinstance(spec, dict):
        return 1
    spec_id = int(spec["id"])
    call("PUT", f"/resource-groups/{pool_id}/specs", token=admin, body=[spec_id])

    status, workspace = call(
        "POST",
        "/workspaces",
        token=admin,
        body={
            "name": f"p5-ws-{SUFFIX}",
            "groupId": lab_id,
            "resourceGroupIds": [pool_id],
        },
    )
    if status == 409:
        workspace = find_by_name(
            call("GET", f"/workspaces?search=p5-ws-{SUFFIX}", token=admin)[1], f"p5-ws-{SUFFIX}"
        )
        status = 200
    check("建工作空间并授权资源组", status in (200, 201), str(workspace))
    if not isinstance(workspace, dict):
        return 1
    workspace_id = int(workspace["id"])

    worker_id = ensure_user(admin, f"p5-worker-{SUFFIX}", lab_id)
    check("建普通用户并加入用户组", worker_id is not None)
    if worker_id is None:
        return 1
    peer_id = ensure_user(admin, f"p5-peer-{SUFFIX}", lab_id)
    check("建第二个用户（对照）", peer_id is not None)

    for user_id in (worker_id, peer_id):
        call(
            "POST",
            f"/workspaces/{workspace_id}/members",
            token=admin,
            body={"userId": user_id, "moveFromExisting": True},
        )

    token = login(f"p5-worker-{SUFFIX}", PASSWORD)
    check("普通用户登录", token is not None)
    if token is None:
        return 1

    # --- the catalogue is reachable ---------------------------------------
    print("\n\033[1m列表与权限\033[0m")
    status, body = call("GET", "/storages", token=token)
    check("存储列表可读", status == 200, str(body))

    # Placing a directory is an administrator's act, so the interesting negative
    # case moved: an ordinary user is no longer refused *by kind*, they are refused
    # outright. One code guards every mutation.
    status, body = call(
        "POST", "/storages", token=token, body={"name": sname("mine"), "kind": "global_share"}
    )
    check(
        "普通用户不能登记存储（403）——放置目录是管理员的事",
        status == 403 and error_code(body) == "permission_denied",
        f"{status} {body}",
    )

    # --- one directory, one volume ----------------------------------------
    print("\n\033[1m管理员登记存储（一个目录一个卷）\033[0m")
    status, home = call(
        "POST",
        "/storages",
        token=admin,
        body={
            "name": sname("home"),
            "kind": "user_volume",
            "ownerUserId": worker_id,
            "capacityGb": 10,
        },
    )
    if status == 409 and error_code(home) == "storage_path_taken":
        # A user's directory is fixed by their username, not by the name on the
        # request, so a re-run legitimately collides with the volume a previous run
        # placed. Reuse it instead of failing on a name only this run invented —
        # and read the *detail* back, because the listing model omits the cluster
        # coordinates (`pvcName`, `k8sNamespace`) that the checks below assert on.
        reused = find_user_volume(admin, f"p5-worker-{SUFFIX}")
        if reused is not None:
            _, home = call("GET", f"/storages/{reused['id']}", token=admin)
            status = 200
    check("为普通用户登记个人存储卷", status in (200, 201), str(home))
    if not isinstance(home, dict):
        return 1
    home_id = int(home["id"])
    check(
        "目录是 {root}/{组}/{用户名}",
        str(home.get("hostPath", "")).endswith(f"/p5-lab-{SUFFIX}/p5-worker-{SUFFIX}"),
        str(home.get("hostPath")),
    )
    check(
        "默认挂载点为 /home/{用户名}", home.get("defaultMountPath") == f"/home/p5-worker-{SUFFIX}"
    )
    check("状态为 bound", home.get("status") == "bound", str(home.get("status")))
    check("拥有者是本人", home.get("ownerUsername") == f"p5-worker-{SUFFIX}")
    check(
        "PVC 名与实例无关（wl-vol- 前缀）",
        str(home.get("pvcName")).startswith("wl-vol-"),
        str(home.get("pvcName")),
    )
    check("可写", home.get("isReadOnly") is False)
    check(
        "个人卷的访问模式最窄",
        home.get("accessMode") == "ReadWriteOnce",
        str(home.get("accessMode")),
    )
    check("个人卷不是组本级卷", home.get("isGroupLevel") is False)

    status, share = call(
        "POST",
        "/storages",
        token=admin,
        body={
            "name": sname("scratch"),
            "kind": "group_share",
            "groupId": lab_id,
            "dirName": sname("scratch"),
            "capacityGb": 20,
        },
    )
    check("为用户组登记共享目录", status == 201, str(share))
    if not isinstance(share, dict):
        return 1
    share_id = int(share["id"])
    check(
        "组共享是组目录下的子目录",
        str(share.get("hostPath", "")).endswith(f"/p5-lab-{SUFFIX}/{sname('scratch')}"),
        str(share.get("hostPath")),
    )
    check("组共享挂载点为 /mnt/group_shared", share.get("defaultMountPath") == "/mnt/group_shared")
    check(
        "组共享默认读写多路",
        share.get("accessMode") == "ReadWriteMany",
        str(share.get("accessMode")),
    )
    check("组共享不是组本级卷", share.get("isGroupLevel") is False and share.get("dirName"))

    status, root_volume = call(
        "POST",
        "/storages",
        token=admin,
        body={"name": sname("whole-group"), "kind": "group_share", "groupId": lab_id},
    )
    if status == 409 and error_code(root_volume) == "storage_path_taken":
        # The group's own directory is one directory however many times it is
        # registered, so a re-run collides and the live row is the one to use.
        reused = find_group_level_volume(admin, lab_id)
        if reused is not None:
            _, root_volume = call("GET", f"/storages/{reused['id']}", token=admin)
            status = 200
    check("组本级卷就是组目录本身", status in (200, 201), str(root_volume))
    if not isinstance(root_volume, dict):
        return 1
    root_volume_id = int(root_volume["id"])
    check(
        "组本级卷的目录是 {root}/{组}",
        str(root_volume.get("hostPath", "")).endswith(f"/p5-lab-{SUFFIX}"),
        str(root_volume.get("hostPath")),
    )
    check(
        "组本级卷以 dir_name IS NULL 标识",
        root_volume.get("dirName") is None and root_volume.get("isGroupLevel") is True,
        str(root_volume),
    )

    status, wide = call(
        "POST",
        "/storages",
        token=admin,
        body={"name": sname("public-data"), "kind": "global_share", "capacityGb": 50},
    )
    check("登记全局共享目录", status == 201, str(wide))
    if not isinstance(wide, dict):
        return 1
    wide_id = int(wide["id"])
    check(
        "全局共享的目录在 _global 段下",
        str(wide.get("hostPath", "")).endswith(f"/_global/{sname('public-data')}"),
        str(wide.get("hostPath")),
    )
    check(
        "全局共享不属于任何工作空间",
        wide.get("workspaceId") is None and wide.get("groupId") is None,
        str(wide),
    )
    # A claim lives in a namespace and a global share belongs to none: the volume
    # object exists, the claim does not — so the resting state is `available`
    # rather than an optimistic `pending` that would never resolve.
    check(
        "全局共享有卷无声明，状态为 available",
        wide.get("pvcName") is None and wide.get("status") == "available",
        str(wide),
    )
    check(
        "全局共享挂载点为 /mnt/global_shared", wide.get("defaultMountPath") == "/mnt/global_shared"
    )

    # --- negative cases ---------------------------------------------------
    print("\n\033[1m约束\033[0m")
    status, body = call(
        "POST", "/storages", token=admin, body={"name": "Bad Name", "capacityGb": 5}
    )
    check("非法名称被拒", status == 400 and error_code(body) == "invalid_storage_name", str(body))

    status, body = call(
        "POST",
        "/storages",
        token=admin,
        body={"name": sname("zero"), "kind": "global_share", "capacityGb": 0},
    )
    check("容量 0 被拒（422）", status == 422, f"{status} {body}")

    status, body = call(
        "POST",
        "/storages",
        token=admin,
        body={"name": sname("home2"), "kind": "user_volume", "ownerUserId": worker_id},
    )
    check(
        "同一个人不能有第二个存储卷（一个目录一个卷）",
        status == 409 and error_code(body) == "storage_path_taken",
        f"{status} {body}",
    )

    status, body = call(
        "POST", "/storages", token=admin, body={"name": sname("noowner"), "kind": "user_volume"}
    )
    check(
        "用户卷必须指定所属用户",
        status == 400 and error_code(body) == "storage_requires_owner",
        f"{status} {body}",
    )

    status, body = call(
        "POST", "/storages", token=admin, body={"name": sname("nogroup"), "kind": "group_share"}
    )
    check(
        "组共享卷必须指定用户组",
        status == 400 and error_code(body) == "storage_requires_group",
        f"{status} {body}",
    )

    status, body = call(
        "POST",
        "/storages",
        token=admin,
        body={
            "name": sname("escape"),
            "kind": "group_share",
            "groupId": lab_id,
            "dirName": "../escape",
        },
    )
    check(
        "目录名不能逃出组目录",
        status == 400 and error_code(body) == "invalid_path_component",
        f"{status} {body}",
    )

    status, body = call(
        "POST",
        "/storages",
        token=admin,
        body={"name": sname("oldkind"), "kind": "dataset", "capacityGb": 5},
    )
    check("旧模型的数据集类型已被移除（422）", status == 422, f"{status} {body}")

    # --- visibility --------------------------------------------------------
    print("\n\033[1m可见性\033[0m")
    peer = login(f"p5-peer-{SUFFIX}", PASSWORD)
    status, _ = call("GET", f"/storages/{home_id}", token=peer)
    check("同事看不到他人的个人存储卷（404）", status == 404, str(status))

    status, body = call("GET", "/storages?pageSize=200", token=peer)
    check(
        "同事的列表中不含他人个人存储卷",
        status == 200 and all(row["id"] != home_id for row in body["items"]),  # type: ignore[index]
        str(status),
    )

    status, body = call("GET", f"/storages/{home_id}/events", token=peer)
    check("同事读不到时间线（404）", status == 404, str(status))

    status, _ = call("GET", f"/storages/{share_id}", token=peer)
    check("同组同事可以看到命名组共享目录", status == 200, str(status))

    status, _ = call("GET", f"/storages/{wide_id}", token=peer)
    check("全局共享对所有人可见", status == 200, str(status))

    status, _ = call("GET", f"/storages/{root_volume_id}", token=token)
    check(
        "普通成员看不到组本级目录（404）——它装着全组所有人的主目录",
        status == 404,
        str(status),
    )

    status, body = call("GET", f"/storages/attachable?workspaceId={workspace_id}", token=token)
    offered = [row["id"] for row in body] if isinstance(body, list) else []
    check(
        "可挂载列表只提供共享，不提供个人卷，也不提供组本级目录",
        status == 200
        and share_id in offered
        and home_id not in offered
        and root_volume_id not in offered,
        f"{status} {offered}",
    )

    # --- attaching to an instance -----------------------------------------
    print("\n\033[1m挂载到实例\033[0m")
    status, instance = call(
        "POST",
        "/instances",
        token=token,
        body={
            "name": sname("mounted"),
            "resourceGroupId": pool_id,
            "resourceSpecId": spec_id,
            "image": "registry.local/base:1",
            "ideType": "none",
            "sshEnabled": False,
            "storageGb": 5,
            "storageIds": [share_id],
        },
    )
    check("带共享存储创建实例", status == 201, str(instance))
    instance_id = int(instance["id"]) if isinstance(instance, dict) else 0
    check(
        "附加存储不重复计入配额",
        isinstance(instance, dict) and instance.get("storageGb") == 5,
        str(instance.get("storageGb") if isinstance(instance, dict) else None),
    )

    status, body = call(
        "POST",
        "/instances",
        token=token,
        body={
            "name": sname("explicit-home"),
            "resourceGroupId": pool_id,
            "resourceSpecId": spec_id,
            "image": "registry.local/base:1",
            "ideType": "none",
            "sshEnabled": False,
            "storageGb": 5,
            "storageIds": [home_id],
        },
    )
    check(
        "个人存储卷不能手动挂载（它会被自动挂载）",
        status == 400 and error_code(body) == "storage_user_volume_implicit",
        f"{status} {body}",
    )

    status, body = call(
        "POST",
        "/instances",
        token=token,
        body={
            "name": sname("peek"),
            "resourceGroupId": pool_id,
            "resourceSpecId": spec_id,
            "image": "registry.local/base:1",
            "ideType": "none",
            "sshEnabled": False,
            "storageGb": 5,
            "storageIds": [root_volume_id],
        },
    )
    check(
        "普通成员挂载组本级目录被拒（404，先判可见性）",
        status == 404 and error_code(body) == "storage_not_found",
        f"{status} {body}",
    )

    status, body = call(
        "POST",
        "/instances",
        token=token,
        body={
            "name": sname("gone"),
            "resourceGroupId": pool_id,
            "resourceSpecId": spec_id,
            "image": "registry.local/base:1",
            "ideType": "none",
            "sshEnabled": False,
            "storageGb": 5,
            "storageIds": [999999],
        },
    )
    check(
        "不存在的存储被拒（404）",
        status == 404 and error_code(body) == "storage_not_found",
        f"{status} {body}",
    )

    # --- deletion ----------------------------------------------------------
    print("\n\033[1m删除与扩容\033[0m")
    status, body = call("DELETE", f"/storages/{share_id}", token=token)
    check(
        "普通用户不能删除存储卷（403）——退役目录是管理员的事",
        status == 403 and error_code(body) == "permission_denied",
        f"{status} {body}",
    )

    status, body = call("DELETE", f"/storages/{share_id}", token=admin)
    check(
        "被实例占用时不能删除（409）",
        status == 409 and error_code(body) == "storage_in_use",
        f"{status} {body}",
    )

    call("DELETE", f"/instances/{instance_id}", token=token)
    status, body = call("DELETE", f"/storages/{share_id}", token=admin)
    check("实例删除后可删除存储", status == 200, f"{status} {body}")
    check(
        "删除后状态为 released",
        isinstance(body, dict) and body.get("status") == "released",
        str(body),
    )
    # The cluster objects go, the directory stays: removing somebody's home is not
    # what "delete a volume" can mean when the volume *is* a directory.
    check(
        "删除的是集群对象，目录中的数据保留",
        isinstance(body, dict) and "保留" in str(body.get("statusMessage")),
        str(body),
    )

    status, body = call("GET", "/storages?pageSize=200", token=admin)
    check(
        "已删除存储默认不出现在列表",
        all(row["id"] != share_id for row in body["items"]),  # type: ignore[index]
    )
    status, body = call("GET", "/storages?includeInactive=true&pageSize=200", token=admin)
    check(
        "带 includeInactive 时仍可查到记录",
        any(row["id"] == share_id for row in body["items"]),  # type: ignore[index]
    )

    status, grown = call(
        "POST",
        "/storages",
        token=admin,
        body={
            "name": sname("grow"),
            "kind": "group_share",
            "groupId": lab_id,
            "dirName": sname("grow"),
            "capacityGb": 10,
        },
    )
    check("登记待扩容的目录", status == 201, str(grown))
    grow_id = int(grown["id"])  # type: ignore[index]
    status, body = call("POST", f"/storages/{grow_id}/resize", token=admin, body={"capacityGb": 50})
    check("可以扩容", status == 200 and body["capacityGb"] == 50, f"{status} {body}")

    status, body = call("POST", f"/storages/{grow_id}/resize", token=admin, body={"capacityGb": 5})
    check(
        "不能缩容",
        status == 400 and error_code(body) == "storage_shrink_unsupported",
        f"{status} {body}",
    )

    status, body = call("POST", f"/storages/{grow_id}/resize", token=token, body={"capacityGb": 80})
    check(
        "普通用户不能扩容（403）",
        status == 403 and error_code(body) == "permission_denied",
        f"{status} {body}",
    )

    # --- timeline ----------------------------------------------------------
    print("\n\033[1m时间线\033[0m")
    status, events = call("GET", f"/storages/{grow_id}/events", token=admin)
    kinds = [row["eventType"] for row in events] if isinstance(events, list) else []
    check("时间线记录了创建", "created" in kinds, str(kinds))
    check("时间线记录了扩容", "updated" in kinds, str(kinds))
    check(
        "时间线记录了操作人",
        bool(events) and events[0].get("actorUsername") == ADMIN_USERNAME,  # type: ignore[index]
    )

    # --- usage -------------------------------------------------------------
    print("\n\033[1m用量\033[0m")
    status, worker_usage = call("GET", "/storages/usage", token=token)
    check(
        "普通用户只为自己的卷计费（组共享不属于他）",
        isinstance(worker_usage, dict)
        and worker_usage.get("count") == 1
        and worker_usage.get("capacityGb") == 10,
        str(worker_usage),
    )

    # Delta rather than absolute: an absolute count is only meaningful on a virgin
    # database, and an assertion that only holds once gets deleted rather than
    # re-run. The administrator's total is the installation total, which is what a
    # create-and-retire pair must move by exactly the recorded capacity.
    _, before = call("GET", "/storages/usage", token=admin)
    status, metered = call(
        "POST",
        "/storages",
        token=admin,
        body={"name": sname("metered"), "kind": "global_share", "capacityGb": 30},
    )
    check("登记计量用目录", status == 201, str(metered))
    _, after_create = call("GET", "/storages/usage", token=admin)
    check(
        "创建存储后用量增加对应容量",
        after_create.get("capacityGb") == before.get("capacityGb") + 30,  # type: ignore[union-attr]
        f"{before} -> {after_create}",
    )

    call("DELETE", f"/storages/{metered['id']}", token=admin)  # type: ignore[index]
    _, after_delete = call("GET", "/storages/usage", token=admin)
    check(
        "删除存储后用量退回",
        after_delete.get("capacityGb") == before.get("capacityGb"),  # type: ignore[union-attr]
        f"{before} -> {after_delete}",
    )

    # --- the image catalogue -----------------------------------------------
    print("\n\033[1m镜像目录\033[0m")
    status, body = call("GET", "/images", token=token)
    check("镜像列表可读", status == 200, str(body))

    status, body = call("GET", "/images/registry", token=token)
    check("镜像仓库健康状态可读", status == 200 and "reachable" in body, str(body))

    status, body = call(
        "POST",
        "/images",
        token=token,
        body={"reference": "registry.local/library/base-notebook:latest"},
    )
    if status == 409:
        # The reference key is global and a retired row still occupies it, so a
        # re-run legitimately collides. Look past the default listing — a
        # retired row is exactly what a previous run of this script leaves — and
        # revive it.
        _, page = call(
            "GET", "/images?search=base-notebook&ownedOnly=true&includeInactive=true", token=token
        )
        body = first_row(page)
        if isinstance(body, dict):
            call("PATCH", f"/images/{body['id']}", token=token, body={"isActive": True})
            body["isActive"] = True
        status = 200
    check("登记镜像（仓库中存在）", status in (200, 201), str(body))
    image_id = int(body["id"]) if isinstance(body, dict) else 0
    check(
        "参考地址被拆解为 registry/project/repository/tag",
        isinstance(body, dict)
        and body.get("reference") == "registry.local/library/base-notebook:latest"
        and body.get("tag") == "latest",
        str(body),
    )

    status, body = call(
        "POST", "/images", token=token, body={"reference": "registry.local/nope:9.9"}
    )
    check(
        "打错的镜像在入库前被拒（这正是镜像目录存在的理由）",
        status == 400 and error_code(body) == "image_not_found_upstream",
        f"{status} {body}",
    )

    status, body = call(
        "POST",
        "/images",
        token=token,
        body={"reference": "registry.local/library/base-notebook:latest"},
    )
    check("同一 reference 不能重复登记", status == 409, f"{status} {body}")

    status, body = call(
        "POST",
        "/images",
        token=token,
        body={"reference": "registry.local/library/base-notebook :latest"},
    )
    check("镜像地址含空格被拒（422，而不是 500）", status == 422, f"{status} {body}")

    status, body = call(
        "POST",
        "/images",
        token=token,
        body={"reference": "registry.local/library/base-notebook:latest", "visibility": "global"},
    )
    check("普通用户不能全局共享镜像（403）", status == 403, f"{status} {body}")

    status, body = call(
        "POST",
        "/images",
        token=token,
        body={"reference": "registry.local/library/base-notebook:latest", "kind": "official"},
    )
    check("普通用户不能发布官方镜像（403）", status == 403, f"{status} {body}")

    status, body = call(
        "POST",
        "/images",
        token=token,
        body={"reference": "registry.local/library/base-notebook:latest", "visibility": "group"},
    )
    check("普通用户不能把镜像共享到用户组（403）", status == 403, f"{status} {body}")

    # A global administrator holds `image:share` but belongs to no group, so
    # there is nothing to infer — guessing would publish it somewhere unasked.
    status, body = call(
        "POST",
        "/images",
        token=admin,
        body={"reference": "registry.local/library/base-notebook:latest", "visibility": "group"},
    )
    check(
        "共享到用户组但未指定组被拒（管理员无从推断）",
        status == 400 and error_code(body) == "image_group_required",
        f"{status} {body}",
    )

    peer_token = login(f"p5-peer-{SUFFIX}", PASSWORD)
    status, _ = call("GET", f"/images/{image_id}", token=peer_token)
    check("同事看不到他人的私有镜像（404）", status == 404, str(status))

    status, body = call("POST", "/images/sync", token=token, body={})
    check("普通用户不能触发同步（403）", status == 403, f"{status} {body}")

    status, body = call("POST", "/images/sync", token=admin, body={})
    check("管理员可以同步镜像目录", status == 200, str(body))
    check(
        "同步把仓库中的镜像纳入目录",
        isinstance(body, dict) and body.get("created", 0) + body.get("unchanged", 0) >= 1,
        str(body),
    )

    status, body = call(
        "PATCH",
        f"/images/{image_id}",
        token=token,
        body={"displayName": "冒烟镜像", "cudaVersion": "12.1"},
    )
    check(
        "可以修改自己的镜像条目",
        status == 200 and isinstance(body, dict) and body.get("displayName") == "冒烟镜像",
        f"{status} {body}",
    )

    status, events = call("GET", f"/images/{image_id}/events", token=token)
    kinds = [row["eventType"] for row in events] if isinstance(events, list) else []
    check("镜像时间线记录了登记", "created" in kinds, str(kinds))

    status, body = call("DELETE", f"/images/{image_id}", token=token)
    check(
        "可以移除自己的镜像条目",
        status == 200 and isinstance(body, dict) and body.get("isActive") is False,
        f"{status} {body}",
    )

    status, body = call("GET", "/images", token=token)
    check(
        "已移除的镜像默认不出现在列表",
        all(row["id"] != image_id for row in body["items"]),  # type: ignore[index]
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
