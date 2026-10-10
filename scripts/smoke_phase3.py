#!/usr/bin/env python3
"""Phase 3 acceptance checks against a *running* API process.

Stdlib only, so it needs no virtualenv and can be run from anywhere:

    source scripts/smoke-env.sh
    (cd backend && uvicorn app.main:app --port 8000) &
    python scripts/smoke_phase3.py

It exercises the instance lifecycle over HTTP: pool grant enforcement, quota
accounting across instances, SSH key registration and fingerprinting, the
short-lived IDE ticket, the start/stop/reconcile/delete cycle, and cross-tenant
isolation.

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

PASSWORD = "smoke-instance-password-123"
SUFFIX = os.environ.get("SMOKE_SUFFIX", "p3")

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


def ensure_user(admin: str, username: str, group_id: int | None = None) -> int | None:
    """Create or fetch a user, returning their id."""
    # `fullName` and `groupIds` (plural). This used to send `displayName` and
    # `groupId`, both of which were silently discarded: the account was created
    # and the script moved on, so every later check that depended on the group
    # link was quietly testing a user who belonged to no group at all.
    status, body = call("POST", "/users", token=admin, body={
        "username": username,
        "password": PASSWORD,
        "fullName": username,
        "roleCodes": ["user"],
        "groupIds": [group_id] if group_id is not None else [],
    })
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

    An instance has no disk of its own — its container mounts the *owner's* storage
    volume at ``/home/{username}``, and creating one without a volume is refused
    with ``user_volume_missing``. So this is a prerequisite of the first instance,
    not an optional extra.

    Re-runs collide on the directory rather than on the name (a user's directory is
    fixed by their group and username), so ``storage_path_taken`` is looked up
    instead of treated as a failure.
    """
    status, body = call("POST", "/storages", token=admin, body={
        "name": f"p3-home-{username}",
        "displayName": f"{username} 的主目录",
        "kind": "user_volume",
        "workspaceId": workspace_id,
        "ownerUserId": user_id,
        "capacityGb": 10,
    })
    if status == 201 and isinstance(body, dict):
        return int(body["id"])
    if status == 409 and error_code(body) == "storage_path_taken":
        status, page = call("GET", "/storages?kind=user_volume&pageSize=200", token=admin)
        rows = page.get("items", []) if isinstance(page, dict) else []
        for row in rows:
            if row.get("ownerUsername") == username:
                return int(row["id"])
    return None


def main() -> int:
    print("\n\033[1mPhase 3 — 容器实例、SSH 与 IDE\033[0m")

    admin = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if admin is None:
        print("  \033[31m无法以管理员登录，冒烟环境是否已启动？\033[0m")
        return 1

    # --- fixtures ----------------------------------------------------------
    status, group = call("POST", "/groups", token=admin, body={
        "name": f"p3-lab-{SUFFIX}", "displayName": "P3 实验室", "kind": "lab",
    })
    check("建用户组", status == 201, str(group))
    if not isinstance(group, dict):
        return 1
    group_id = int(group["id"])

    # A second lab for the un-granted workspace below. It has to be a *different*
    # group: storage registration resolves a group to its single workspace
    # (`group_has_multiple_workspaces` otherwise), so putting two workspaces in one
    # group would make that lab permanently unable to own a storage volume — and
    # this script needs to register one for its user.
    status, spare_group = call("POST", "/groups", token=admin, body={
        "name": f"p3-spare-{SUFFIX}", "displayName": "P3 备用实验室", "kind": "lab",
    })
    check("建第二个用户组（未授权工作空间用）", status == 201, str(spare_group))
    spare_group_id = int(spare_group["id"]) if isinstance(spare_group, dict) else None
    if spare_group_id is None:
        return 1

    pool_name = f"p3-pool-{SUFFIX}"
    status, pool = call("POST", "/resource-groups", token=admin, body={
        "name": pool_name, "displayName": "P3 资源池", "kind": "public",
        "networkType": "internal", "maxRuntimeSeconds": 3600,
    })
    check("建资源组", status == 201, str(pool))
    if not isinstance(pool, dict):
        return 1
    pool_id = int(pool["id"])

    status, _ = call("PUT", f"/resource-groups/{pool_id}/limits", token=admin, body={
        "maxConcurrentInstances": 5, "maxGpuPerInstance": 1, "maxGpuPerUser": 1,
        "maxCpuMilliPerUser": 4000, "maxMemoryMbPerUser": 8192, "maxStorageGbPerUser": 20,
    })
    check("设置资源组公平上限", status == 200)

    status, spec = call("POST", "/resource-specs", token=admin, body={
        "name": f"p3-cpu-{SUFFIX}", "displayName": "P3 CPU", "cpuMilli": 2000,
        "memoryMb": 4096, "gpuType": "none", "gpuCount": 0,
    })
    check("建资源规格", status == 201, str(spec))
    if not isinstance(spec, dict):
        return 1
    spec_id = int(spec["id"])

    status, _ = call("PUT", f"/resource-groups/{pool_id}/specs", token=admin, body=[spec_id])
    check("规格挂到资源组", status == 200)

    workspace_name = f"p3-ws-{SUFFIX}"
    status, workspace = call("POST", "/workspaces", token=admin, body={
        "name": workspace_name, "groupId": group_id, "resourceGroupIds": [pool_id],
    })
    check("建工作空间并授权资源组", status == 201, str(workspace))
    if not isinstance(workspace, dict):
        return 1
    workspace_id = int(workspace["id"])
    namespace = str(workspace["k8sNamespace"])

    # A second workspace, deliberately *without* a grant, so "not granted" can be
    # told apart from "not a member". It lives in the spare lab above, so the first
    # lab keeps exactly one workspace and can still own a storage volume.
    status, bare_ws = call("POST", "/workspaces", token=admin, body={
        "name": f"p3-bare-{SUFFIX}", "groupId": spare_group_id,
    })
    check("建未授权工作空间", status == 201, str(bare_ws))
    bare_ws_id = int(bare_ws["id"]) if isinstance(bare_ws, dict) else None

    user_id = ensure_user(admin, f"p3-user-{SUFFIX}", group_id)
    check("建普通用户", user_id is not None)
    other_id = ensure_user(admin, f"p3-other-{SUFFIX}", group_id)
    check("建第二个普通用户", other_id is not None)
    if user_id is None or other_id is None:
        return 1

    for uid in (user_id, other_id):
        status, _ = call("POST", f"/workspaces/{workspace_id}/members", token=admin,
                         body={"userId": uid, "moveFromExisting": True})
        check(f"用户 {uid} 加入工作空间", status in (200, 201), str(status))

    token = login(f"p3-user-{SUFFIX}", PASSWORD)
    other = login(f"p3-other-{SUFFIX}", PASSWORD)
    check("普通用户登录", token is not None and other is not None)
    if token is None or other is None:
        return 1

    # The instance below has no disk of its own: it mounts the owner's storage
    # volume. Registering it is a prerequisite, so it is done (as the administrator)
    # before anything is created.
    home_id = ensure_home_volume(admin, user_id, workspace_id, f"p3-user-{SUFFIX}")
    check("为该用户登记主目录存储卷", home_id is not None, str(home_id))
    if home_id is None:
        return 1

    # --- SSH keys ----------------------------------------------------------
    key = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIL7Zk3pQ1WvJ0xH2mR4tYbNc8dEfGhIjKlMnOpQrStUv "
        "smoke@lab"
    )
    status, registered = call("POST", "/ssh-keys", token=token,
                              body={"name": "laptop", "publicKey": key})
    check("登记 SSH 公钥", status == 201, str(registered))
    check(
        "指纹为 OpenSSH 格式",
        isinstance(registered, dict) and str(registered["fingerprint"]).startswith("SHA256:"),
        str(registered),
    )

    status, duplicate = call("POST", "/ssh-keys", token=token,
                             body={"name": "again", "publicKey": key})
    check("同指纹公钥不可重复登记", status == 409 and error_code(duplicate) == "ssh_key_duplicate",
          f"{status} {duplicate}")

    status, bad = call("POST", "/ssh-keys", token=token,
                       body={"name": "bad", "publicKey": "definitely-not-a-key"})
    check("非法公钥被拒", status == 400 and error_code(bad) == "invalid_ssh_key",
          f"{status} {bad}")

    status, keys = call("GET", "/ssh-keys", token=token)
    check("列出自己的公钥", status == 200 and isinstance(keys, list) and len(keys) == 1,
          str(keys))

    # --- create ------------------------------------------------------------
    def payload(name: str, **overrides: object) -> dict:
        base: dict[str, object] = {
            "name": name, "displayName": name, "resourceGroupId": pool_id,
            "resourceSpecId": spec_id, "image": "registry.local/p3:latest",
            "ideType": "jupyter", "sshEnabled": True,
        }
        base.update(overrides)
        return base

    status, first = call("POST", "/instances", token=token, body=payload("dev-one"))
    check("创建实例", status == 201, str(first))
    if not isinstance(first, dict):
        return 1
    instance_id = int(first["id"])

    check("初始状态为 creating", first["status"] == "creating", str(first["status"]))
    check("归属人为自己", first["ownerUsername"] == f"p3-user-{SUFFIX}", str(first["ownerUsername"]))
    check("工作空间命名空间正确", first["workspaceName"] == namespace, str(first["workspaceName"]))
    check("分配了 SSH NodePort", isinstance(first["sshNodePort"], int), str(first["sshNodePort"]))
    check("SSH 命令带端口", f"-p {first['sshNodePort']}" in str(first["sshCommand"]),
          str(first["sshCommand"]))
    check("IDE 路径为 /x/{id}/", first["idePath"] == f"/x/{instance_id}/", str(first["idePath"]))
    check("已记录 Deployment 名", bool(first["k8sDeployment"]), str(first["k8sDeployment"]))

    status, same_name = call("POST", "/instances", token=token, body=payload("dev-one"))
    check("同工作空间内实例重名被拒", status == 409
          and error_code(same_name) == "instance_name_taken", f"{status} {same_name}")

    status, orphan = call("POST", "/resource-specs", token=admin, body={
        "name": f"p3-orphan-{SUFFIX}", "displayName": "未挂载规格", "cpuMilli": 1000,
        "memoryMb": 1024, "gpuType": "none", "gpuCount": 0,
    })
    check("建一个不挂到资源组的规格", status == 201, str(orphan))
    orphan_id = int(orphan["id"]) if isinstance(orphan, dict) else 0

    status, bad_spec = call("POST", "/instances", token=token,
                            body=payload("dev-bad", resourceSpecId=orphan_id))
    check("未授权给该资源组的规格被拒", status == 400
          and error_code(bad_spec) == "resource_spec_not_in_group", f"{status} {bad_spec}")

    status, missing_spec = call("POST", "/instances", token=token,
                                body=payload("dev-ghost", resourceSpecId=999999))
    check("不存在的规格被拒", status == 400
          and error_code(missing_spec) == "unknown_resource_spec", f"{status} {missing_spec}")

    status, too_long = call("POST", "/instances", token=token,
                            body=payload("dev-long", maxRuntimeSeconds=99999))
    check("超出资源组运行时长被拒", status == 400
          and error_code(too_long) == "runtime_exceeds_pool_limit", f"{status} {too_long}")

    # 「超出资源组存储上限」这一项已随实例的 storageGb 一起取消：实例不再自带磁盘，
    # 每用户存储上限现在校验的是存储卷的容量（见 smoke_phase5 的存储段）。

    # --- quota -------------------------------------------------------------
    status, second = call("POST", "/instances", token=token, body=payload("dev-two"))
    check("第二个实例创建成功（2×2000m = 4000m 上限）", status == 201, str(second))

    status, third = call("POST", "/instances", token=token, body=payload("dev-three"))
    check("第三个实例因 CPU 配额被拒（409）", status == 409, f"{status} {third}")
    check("配额错误指明资源类型",
          isinstance(third, dict)
          and str(third.get("error", {}).get("detail", {}).get("resource")) == "cpu",
          str(third))

    # --- lifecycle ---------------------------------------------------------
    status, stopped = call("POST", f"/instances/{instance_id}/stop", token=token, body={})
    check("停止实例", status == 200 and stopped["status"] == "stopped",
          f"{status} {stopped}")

    status, _still_blocked = call("POST", "/instances", token=token, body=payload("dev-four"))
    check("停止后配额仍被占用（重启不会因自己而失败）", status == 409, f"{status}")

    status, started = call("POST", f"/instances/{instance_id}/start", token=token, body={})
    check("启动实例", status == 200 and started["status"] == "creating",
          f"{status} {started}")

    status, reconciled = call("POST", f"/instances/{instance_id}/reconcile", token=token)
    check("重新下发幂等", status == 200, f"{status} {reconciled}")

    status, logs = call("GET", f"/instances/{instance_id}/logs", token=token)
    check("无容器时日志返回明确错误", status == 404
          and error_code(logs) == "instance_has_no_pod", f"{status} {logs}")

    status, ticket = call("GET", f"/instances/{instance_id}/ide-access", token=token)
    check("签发 IDE 票据", status == 200, f"{status} {ticket}")
    check(
        "票据 URL 指向该实例且带短时票据",
        isinstance(ticket, dict)
        and str(ticket["url"]).startswith(f"/x/{instance_id}/?ticket=")
        and 0 < int(ticket["expiresIn"]) <= 120,
        str(ticket),
    )

    # --- isolation ---------------------------------------------------------
    status, listed = call("GET", "/instances", token=other)
    check("同工作空间成员可见（数据范围=工作空间）",
          status == 200 and listed["meta"]["total"] >= 2, str(listed))

    status, hidden = call("GET", f"/instances/{instance_id}", token=admin)
    check("管理员可见", status == 200, f"{status} {hidden}")

    # A user in a workspace with no grant cannot create there.
    if bare_ws_id is not None:
        status, _ = call("POST", f"/workspaces/{bare_ws_id}/members", token=admin,
                         body={"userId": user_id, "moveFromExisting": True})
        check("把用户迁到未授权工作空间", status in (200, 201))
        status, refused = call("POST", "/instances", token=token,
                               body=payload("dev-nogrant", workspaceId=bare_ws_id))
        check("未授权资源组被拒（400）", status == 400
              and error_code(refused) == "resource_group_not_granted", f"{status} {refused}")
        # Move back so the remaining checks see the granted workspace.
        status, _ = call("POST", f"/workspaces/{workspace_id}/members", token=admin,
                         body={"userId": user_id, "moveFromExisting": True})
        check("迁回原工作空间", status in (200, 201))

    # --- delete ------------------------------------------------------------
    status, removed = call("DELETE", f"/instances/{instance_id}", token=token)
    check("删除实例", status == 200, f"{status} {removed}")

    status, fetched = call("GET", f"/instances/{instance_id}", token=token)
    check("已删除实例从活跃列表中消失", fetched["isActive"] is False, str(fetched))

    status, listing = call("GET", "/instances", token=token)
    check("列表默认不含已删除实例",
          all(item["id"] != instance_id for item in listing["items"]), str(listing))

    # --- report ------------------------------------------------------------
    total = passed + len(failures)
    print(f"\n{'─' * 46}")
    if failures:
        print(f"\033[31m{passed}/{total} 通过\033[0m，失败项：")
        for name in failures:
            print(f"  · {name}")
        return 1
    print(f"\033[32m全部 {total} 项通过\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
