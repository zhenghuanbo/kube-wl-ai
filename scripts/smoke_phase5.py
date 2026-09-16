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
import urllib.request

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8000/api/v1")
ADMIN_USERNAME = os.environ.get("SMOKE_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("SMOKE_ADMIN_PASSWORD", "smoke-test-password-123")
SUFFIX = os.environ.get("SMOKE_SUFFIX", "p5")
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
        lab = page["items"][0]  # type: ignore[index]
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
        pool = page["items"][0]  # type: ignore[index]
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
        spec = page["items"][0]  # type: ignore[index]
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
        workspace = page["items"][0]  # type: ignore[index]
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
        body={"name": f"scratch-{SUFFIX}", "kind": "personal_data", "capacityGb": 10},
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
    check("可读取详情", status == 200 and detail["name"] == f"scratch-{SUFFIX}")

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
        body={"name": f"zero-{SUFFIX}", "kind": "personal_data", "capacityGb": 0},
    )
    check("容量 0 被拒（422）", status == 422, f"{status} {body}")

    status, body = call(
        "POST",
        "/storages",
        token=token,
        body={"name": f"home-{SUFFIX}", "kind": "personal_home", "capacityGb": 5},
    )
    check("创建个人主目录", status == 201, str(body))
    home_id = int(body["id"]) if isinstance(body, dict) else 0
    status, body = call(
        "POST",
        "/storages",
        token=token,
        body={"name": f"home2-{SUFFIX}", "kind": "personal_home", "capacityGb": 5},
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
            "name": f"fakedataset-{SUFFIX}",
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
            "name": f"dataset-{SUFFIX}",
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
            "name": f"nosource-{SUFFIX}",
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
            "name": f"mounted-{SUFFIX}",
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
            "name": f"gone-{SUFFIX}",
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
        "POST", "/storages", token=token, body={"name": f"grow-{SUFFIX}", "capacityGb": 10}
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

    status, usage = call("GET", "/storages/usage", token=token)
    check(
        "用量汇总只统计在用存储",
        status == 200 and usage.get("count") == 2,  # type: ignore[union-attr]
        str(usage),
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
