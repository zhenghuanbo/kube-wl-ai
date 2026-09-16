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
import sys
import urllib.error
import time
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
        _, page = call("GET", f"/groups?search=p5-lab-{SUFFIX}", token=admin)
        lab = first_row(page)
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
        _, page = call("GET", f"/resource-groups?search=p5-pool-{SUFFIX}", token=admin)
        pool = first_row(page)
        status = 200
    check("建资源组", status in (200, 201), str(pool))
    if not isinstance(pool, dict):
        return 1
    pool_id = int(pool["id"])

    status, spec = call(
        "POST",
        "/resource-specs",
        token=admin,
        body={
            "name": f"p5-spec-{SUFFIX}",
            "displayName": "P5 规格",
            "cpuMilli": 1000,
            "memoryMb": 1024,
            "gpuType": "none",
            "gpuCount": 0,
        },
    )
    if status == 409:
        _, page = call("GET", f"/resource-specs?search=p5-spec-{SUFFIX}", token=admin)
        spec = first_row(page)
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
        _, page = call("GET", f"/workspaces?search=p5-ws-{SUFFIX}", token=admin)
        workspace = first_row(page)
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

    # --- creating a personal volume ---------------------------------------
    print("\n\033[1m创建个人存储\033[0m")
    status, storage = call(
        "POST",
        "/storages",
        token=token,
        body={"name": sname("scratch"), "kind": "personal_data", "capacityGb": 10},
    )
    check("创建个人存储", status == 201, str(storage))
    if not isinstance(storage, dict):
        return 1
    storage_id = int(storage["id"])
    pvc_name = storage.get("pvcName")

    check("状态为 bound", storage.get("status") == "bound", str(storage.get("status")))
    check("拥有者是本人", storage.get("ownerUsername") == f"p5-worker-{SUFFIX}")
    check("PVC 名与实例无关（wl-vol- 前缀）", str(pvc_name).startswith("wl-vol-"), str(pvc_name))
    check("默认挂载点", storage.get("defaultMountPath") == "/mnt/data")
    check("可写", storage.get("isReadOnly") is False)

    status, detail = call("GET", f"/storages/{storage_id}", token=token)
    check("可读取详情", status == 200 and detail["name"] == sname("scratch"))

    # --- negative cases ---------------------------------------------------
    print("\n\033[1m约束\033[0m")
    status, body = call(
        "POST", "/storages", token=token, body={"name": "Bad Name", "capacityGb": 5}
    )
    check("非法名称被拒", status == 400 and error_code(body) == "invalid_storage_name", str(body))

    status, body = call(
        "POST",
        "/storages",
        token=token,
        body={"name": sname("zero"), "kind": "personal_data", "capacityGb": 0},
    )
    check("容量 0 被拒（422）", status == 422, f"{status} {body}")

    status, body = call(
        "POST",
        "/storages",
        token=token,
        body={"name": sname("home"), "kind": "personal_home", "capacityGb": 5},
    )
    if status == 409 and error_code(body) == "storage_home_exists":
        # "One home per person" is a rule about the owner, not the name, so a
        # re-run legitimately collides. Reuse what is already there.
        _, page = call("GET", "/storages?kind=personal_home&ownedOnly=true", token=token)
        body = first_row(page)
        status = 200
    check("创建个人主目录", status in (200, 201), str(body))
    home_id = int(body["id"]) if isinstance(body, dict) else 0

    status, body = call(
        "POST",
        "/storages",
        token=token,
        body={"name": sname("home2"), "kind": "personal_home", "capacityGb": 5},
    )
    check(
        "第二个个人主目录被拒（409）",
        status == 409 and error_code(body) == "storage_home_exists",
        f"{status} {body}",
    )

    status, body = call(
        "POST",
        "/storages",
        token=token,
        body={
            "name": sname("fakedataset"),
            "kind": "dataset",
            "capacityGb": 10,
            "source": "https://example.edu/x",
        },
    )
    check("普通用户不能发布数据集（403）", status == 403, f"{status} {body}")

    # --- datasets are curated ---------------------------------------------
    print("\n\033[1m数据集（管理员）\033[0m")
    status, dataset = call(
        "POST",
        "/storages",
        token=admin,
        body={
            "name": sname("dataset"),
            "kind": "dataset",
            "capacityGb": 100,
            "source": "https://example.edu/imagenet",
            "workspaceId": workspace_id,
        },
    )
    check("管理员可发布数据集", status == 201, str(dataset))
    dataset_id = int(dataset["id"]) if isinstance(dataset, dict) else 0
    check("数据集强制只读", isinstance(dataset, dict) and dataset.get("isReadOnly") is True)
    check(
        "挂载点为 /mnt/dataset",
        isinstance(dataset, dict) and dataset.get("defaultMountPath") == "/mnt/dataset",
    )

    status, body = call(
        "POST",
        "/storages",
        token=admin,
        body={
            "name": sname("nosource"),
            "kind": "dataset",
            "capacityGb": 10,
            # A workspace must be named or the request fails earlier, on
            # `no_workspace`, and never reaches the check under test.
            "workspaceId": workspace_id,
        },
    )
    check(
        "数据集必须填写来源",
        status == 400 and error_code(body) == "dataset_source_required",
        f"{status} {body}",
    )

    status, body = call(
        "PATCH", f"/storages/{dataset_id}", token=admin, body={"isReadOnly": False}
    )
    check(
        "数据集不能改为可写",
        status == 400 and error_code(body) == "dataset_always_read_only",
        f"{status} {body}",
    )

    # --- visibility --------------------------------------------------------
    print("\n\033[1m可见性\033[0m")
    peer = login(f"p5-peer-{SUFFIX}", PASSWORD)
    status, _ = call("GET", f"/storages/{storage_id}", token=peer)
    check("同事看不到个人存储（404）", status == 404, str(status))

    status, body = call("GET", "/storages", token=peer)
    check(
        "同事的列表中不含他人个人存储",
        status == 200
        and all(row["id"] != storage_id for row in body["items"]),  # type: ignore[index]
        str(status),
    )

    status, body = call("GET", f"/storages/{storage_id}/events", token=peer)
    check("同事读不到时间线（404）", status == 404, str(status))

    status, body = call("GET", f"/storages/{dataset_id}", token=peer)
    check("同组同事可以看到数据集", status == 200, str(status))

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
            "storageIds": [storage_id],
        },
    )
    check("带存储创建实例", status == 201, str(instance))
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
            "name": f"explicit-home-{SUFFIX}",
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
        "个人主目录不能手动挂载",
        status == 400 and error_code(body) == "storage_home_implicit",
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
    status, body = call("DELETE", f"/storages/{storage_id}", token=token)
    check(
        "被实例占用时不能删除（409）",
        status == 409 and error_code(body) == "storage_in_use",
        f"{status} {body}",
    )

    call("DELETE", f"/instances/{instance_id}", token=token)
    status, body = call("DELETE", f"/storages/{storage_id}", token=token)
    check("实例删除后可删除存储", status == 200, f"{status} {body}")
    check(
        "删除后状态为 released",
        isinstance(body, dict) and body.get("status") == "released",
        str(body),
    )

    status, body = call("GET", "/storages", token=token)
    check(
        "已删除存储默认不出现在列表",
        all(row["id"] != storage_id for row in body["items"]),  # type: ignore[index]
    )
    status, body = call("GET", "/storages?includeInactive=true", token=token)
    check(
        "带 includeInactive 时仍可查到记录",
        any(row["id"] == storage_id for row in body["items"]),  # type: ignore[index]
    )

    status, grown = call(
        "POST", "/storages", token=token, body={"name": sname("grow"), "capacityGb": 10}
    )
    grow_id = int(grown["id"])  # type: ignore[index]
    status, body = call("POST", f"/storages/{grow_id}/resize", token=token, body={"capacityGb": 50})
    check("可以扩容", status == 200 and body["capacityGb"] == 50, f"{status} {body}")

    status, body = call("POST", f"/storages/{grow_id}/resize", token=token, body={"capacityGb": 5})
    check(
        "不能缩容",
        status == 400 and error_code(body) == "storage_shrink_unsupported",
        f"{status} {body}",
    )

    # --- timeline ----------------------------------------------------------
    print("\n\033[1m时间线\033[0m")
    status, events = call("GET", f"/storages/{grow_id}/events", token=token)
    kinds = [row["eventType"] for row in events] if isinstance(events, list) else []
    check("时间线记录了创建", "created" in kinds, str(kinds))
    check("时间线记录了扩容", "updated" in kinds, str(kinds))
    check(
        "时间线记录了操作人",
        bool(events) and events[0].get("actorUsername") == f"p5-worker-{SUFFIX}",  # type: ignore[index]
    )

    # Delta rather than absolute: an absolute count is only meaningful on a
    # virgin database, and an assertion that only holds once gets deleted rather
    # than re-run. What matters is that creating charges and releasing refunds.
    _, before = call("GET", "/storages/usage", token=token)
    _, created = call(
        "POST", "/storages", token=token, body={"name": sname("metered"), "capacityGb": 30}
    )
    _, after_create = call("GET", "/storages/usage", token=token)
    check(
        "创建存储后用量增加对应容量",
        after_create.get("capacityGb") == before.get("capacityGb") + 30,  # type: ignore[union-attr]
        f"{before} -> {after_create}",
    )

    call("DELETE", f"/storages/{created['id']}", token=token)  # type: ignore[index]
    _, after_delete = call("GET", "/storages/usage", token=token)
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
        status == 200
        and isinstance(body, dict)
        and body.get("displayName") == "冒烟镜像",
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
