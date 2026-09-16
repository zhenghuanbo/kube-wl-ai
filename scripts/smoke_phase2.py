#!/usr/bin/env python3
"""Phase 2 acceptance checks against a *running* API process.

Stdlib only, so it needs no virtualenv and can be run from anywhere:

    source scripts/smoke-env.sh
    (cd backend && uvicorn app.main:app --port 8000) &
    python scripts/smoke_phase2.py

It exercises the workspace tenancy boundary end to end over HTTP: pool and spec
catalogue, workspace provisioning, the one-user-one-workspace rule, resource
grants, quota aggregation and cross-tenant isolation.

The server runs without a cluster, so the Kubernetes gateway falls back to the
in-memory fake. That is deliberate — these checks are about what the platform
*would* apply, and they must pass on a laptop with no kubeconfig.
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

PASSWORD = "smoke-user-password-123"

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
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        request.add_header("Content-Type", "application/json")
        request.data = data

    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read().decode()
            return response.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as error:
        payload = error.read().decode()
        try:
            return error.code, json.loads(payload) if payload else None
        except json.JSONDecodeError:
            return error.code, payload


def error_code(payload: object) -> str:
    if isinstance(payload, dict):
        envelope = payload.get("error")
        if isinstance(envelope, dict):
            return str(envelope.get("code", ""))
    return ""


def login(username: str, password: str) -> str | None:
    status, payload = call("POST", "/auth/login", body={"username": username, "password": password})
    if status != 200 or not isinstance(payload, dict):
        return None
    return payload.get("accessToken")


def main() -> int:  # noqa: C901 - a linear acceptance script reads better flat
    print(f"Phase 2 冒烟验收 → {BASE}\n")

    admin = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if admin is None:
        print("无法以管理员身份登录，请先 source scripts/smoke-env.sh 并启动服务")
        return 1

    # --- fixtures ----------------------------------------------------------
    print("准备数据")
    status, group = call(
        "POST",
        "/groups",
        token=admin,
        body={"name": "smoke-lab", "displayName": "冒烟实验室", "kind": "lab"},
    )
    if status == 409:
        _, listing = call("GET", "/groups?search=smoke-lab", token=admin)
        group = next(g for g in listing["items"] if g["name"] == "smoke-lab")  # type: ignore[index]
        status = 200
    check("创建用户组", status == 201, str(group))
    group_id = group["id"]  # type: ignore[index]

    status, user = call(
        "POST",
        "/users",
        token=admin,
        body={
            "username": "smoke-user",
            "password": PASSWORD,
            # `fullName`, not `displayName`: the display name is derived from it.
            "fullName": "冒烟用户",
            # `roleCodes`, not `role`. The old spelling was discarded and the
            # schema default (["user"]) happened to match, so the check passed
            # for the wrong reason.
            "roleCodes": ["user"],
            "groupIds": [group_id],
        },
    )
    if status == 409:
        _, listing = call("GET", "/users?search=smoke-user", token=admin)
        user = next(u for u in listing["items"] if u["username"] == "smoke-user")  # type: ignore[index]
    user_id = user["id"]  # type: ignore[index]
    check("创建普通用户", user_id > 0)

    # --- resource catalogue ------------------------------------------------
    print("\n资源目录")
    status, pool = call(
        "POST",
        "/resource-groups",
        token=admin,
        body={
            "name": "smoke-pool",
            "displayName": "冒烟资源池",
            "kind": "public",
            "networkType": "internal",
            "maxRuntimeSeconds": 86400,
        },
    )
    check("创建资源池", status == 201, error_code(pool))
    pool_id = pool["id"]  # type: ignore[index]

    if status == 201:
        status, limits = call("GET", f"/resource-groups/{pool_id}", token=admin)
        check("资源池自带默认上限", limits["limits"] is not None)  # type: ignore[index]

        status, _ = call(
            "PUT",
            f"/resource-groups/{pool_id}/limits",
            token=admin,
            body={
                "maxConcurrentInstances": 3,
                "maxGpuPerInstance": 1,
                "maxGpuPerUser": 4,
                "maxCpuMilliPerUser": 6000,
                "maxMemoryMbPerUser": 12288,
                "maxStorageGbPerUser": 50,
            },
        )
        check("调整资源池上限", status == 200, str(_))

    status, spec = call(
        "POST",
        "/resource-specs",
        token=admin,
        body={
            "name": "smoke-cpu",
            "displayName": "冒烟 CPU 规格",
            "cpuMilli": 4000,
            "memoryMb": 8192,
            "gpuType": "none",
            "gpuCount": 0,
        },
    )
    check("创建资源规格", status == 201, error_code(spec))
    spec_id = spec["id"]  # type: ignore[index]

    status, gpu_spec = call(
        "POST",
        "/resource-specs",
        token=admin,
        body={
            "name": "smoke-a100",
            "displayName": "冒烟 A100 规格",
            "cpuMilli": 8000,
            "memoryMb": 32768,
            "gpuType": "a100",
            "gpuCount": 2,
        },
    )
    check("创建 GPU 规格", status == 201, error_code(gpu_spec))

    status, _ = call(
        "PUT",
        f"/resource-groups/{pool_id}/specs",
        token=admin,
        body=[spec_id, gpu_spec["id"]],  # type: ignore[index]
    )
    check("绑定规格到资源池", status == 200, str(_))

    status, invalid = call(
        "POST",
        "/resource-specs",
        token=admin,
        body={
            "name": "smoke-broken",
            "cpuMilli": 1000,
            "memoryMb": 1024,
            "gpuType": "none",
            "gpuCount": 1,
        },
    )
    check(
        "GPU 数量与型号必须一致",
        status == 400 and error_code(invalid) == "gpu_type_required",
        f"{status} {error_code(invalid)}",
    )

    # --- workspace ---------------------------------------------------------
    print("\n工作空间")
    status, workspace = call(
        "POST",
        "/workspaces",
        token=admin,
        body={
            "name": "smoke-ws",
            "displayName": "冒烟工作空间",
            "groupId": group_id,
            "resourceGroupIds": [pool_id],
        },
    )
    check("创建工作空间", status == 201, error_code(workspace))
    workspace_id = workspace["id"]  # type: ignore[index]
    check(
        "命名空间按 wl-{组}-{名} 生成",
        workspace["k8sNamespace"] == "wl-smoke-lab-smoke-ws",  # type: ignore[index]
        str(workspace.get("k8sNamespace")),  # type: ignore[union-attr]
    )
    check("工作空间状态为 active", workspace["status"] == "active")  # type: ignore[index]
    check(
        "响应字段为 camelCase（含 k8sNamespace）",
        "k8sNamespace" in workspace and "k8s_namespace" not in workspace,  # type: ignore[operator]
    )

    status, duplicate = call(
        "POST",
        "/workspaces",
        token=admin,
        body={"name": "smoke-ws", "groupId": group_id},
    )
    check(
        "同组内工作空间重名被拒",
        status == 409 and error_code(duplicate) == "workspace_name_taken",
        f"{status} {error_code(duplicate)}",
    )

    # --- membership --------------------------------------------------------
    print("\n成员与一人一空间")
    status, _ = call(
        "POST",
        f"/workspaces/{workspace_id}/members",
        token=admin,
        body={"userId": user_id},
    )
    check("添加成员", status == 201, error_code(_))

    status, second = call(
        "POST",
        "/workspaces",
        token=admin,
        body={"name": "smoke-ws-2", "groupId": group_id},
    )
    second_id = second["id"]  # type: ignore[index]

    status, refused = call(
        "POST",
        f"/workspaces/{second_id}/members",
        token=admin,
        body={"userId": user_id},
    )
    check(
        "迁移已有工作空间的用户需显式确认",
        status == 409 and error_code(refused) == "user_already_has_workspace",
        f"{status} {error_code(refused)}",
    )

    # --- user view ---------------------------------------------------------
    print("\n普通用户视角")
    member_token = login("smoke-user", PASSWORD)
    check("普通用户可以登录", member_token is not None)

    if member_token:
        status, mine = call("GET", "/workspaces/mine", token=member_token)
        check(
            "普通用户看到自己的唯一工作空间",
            status == 200 and isinstance(mine, dict) and mine.get("name") == "smoke-ws",
            str(mine),
        )

        status, listing = call("GET", "/workspaces", token=member_token)
        check(
            "普通用户列表仅含自己的工作空间",
            status == 200 and len(listing["items"]) == 1,  # type: ignore[index]
            str(listing),
        )

        status, other = call("GET", f"/workspaces/{second_id}", token=member_token)
        check("越权读取他人工作空间返回 404", status == 404, str(status))

        status, _ = call("GET", "/nodes", token=member_token)
        check("普通用户不可读取节点清单", status == 403, str(status))

        status, _ = call("POST", "/workspaces", token=member_token, body={"name": "nope"})
        check("普通用户不可创建工作空间", status == 403, str(status))

        status, quotas = call(
            "GET", f"/quotas?scopeType=workspace&scopeId={workspace_id}", token=member_token
        )
        check("普通用户可读自己工作空间的配额", status == 200, error_code(quotas))

    # --- quota aggregation -------------------------------------------------
    print("\n配额")
    status, quotas = call(
        "GET", f"/quotas?scopeType=workspace&scopeId={workspace_id}", token=admin
    )
    by_resource = (
        {row["resource"]: row["limitValue"] for row in quotas}  # type: ignore[index]
        if isinstance(quotas, list)
        else {}
    )
    check("配额按资源组上限汇总", by_resource.get("cpu") == 6000, str(by_resource))
    check("内存配额同步汇总", by_resource.get("memory") == 12288, str(by_resource))
    check("实例数配额同步汇总", by_resource.get("instance") == 3, str(by_resource))
    check(
        "GPU 按型号取最大规格而非求和",
        by_resource.get("gpu:a100") == 2,
        str(by_resource),
    )

    # --- deletion ----------------------------------------------------------
    print("\n停用与删除")
    status, grants = call("GET", f"/workspaces/{workspace_id}/resources", token=admin)
    check("可列出资源池授权", status == 200 and len(grants) == 1, str(grants))  # type: ignore[index]

    status, _ = call("DELETE", f"/resource-groups/{pool_id}", token=admin)
    check(
        "被使用的资源池不可删除",
        status == 409 and error_code(_) == "resource_group_in_use",
        f"{status} {error_code(_)}",
    )

    status, _ = call("POST", f"/workspaces/{workspace_id}/reconcile", token=admin)
    check("重新下发幂等成功", status == 200, error_code(_))

    status, _ = call("DELETE", f"/workspaces/{workspace_id}", token=admin)
    check("删除工作空间", status == 200, error_code(_))

    status, listing = call("GET", "/workspaces?search=smoke-ws", token=admin)
    names = [item["name"] for item in listing["items"]]  # type: ignore[index]
    check("已删除的工作空间不再出现在列表中", "smoke-ws" not in names, str(names))

    status, listing = call(
        "GET", "/workspaces?search=smoke-ws&isActive=false", token=admin
    )
    check(
        "已删除的工作空间仍保留记录（审计与用量）",
        listing["meta"]["total"] >= 1,  # type: ignore[index]
        str(listing),
    )

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
