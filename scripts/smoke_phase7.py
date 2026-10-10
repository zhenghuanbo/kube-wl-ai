#!/usr/bin/env python3
"""Phase 7 smoke: the monitoring centre and the Grafana handoff.

Run with::

    source scripts/smoke-env.sh
    K8S_ENABLED=false .venv/bin/uvicorn app.main:app --port 8000 &
    python3 scripts/smoke_phase7.py

This script is designed to run in **both** Grafana modes, and the backend decides
which one it is:

* `GRAFANA_PUBLIC_URL` unset → `enabled: false`, no dashboards, no embeds, and
  the mint endpoint refuses. That is the default deployment, and it must be a
  working state rather than a broken one.
* `GRAFANA_PUBLIC_URL` set → dashboards filtered per role, embed URLs locked to
  the caller's own namespaces, and a fresh URL mintable on demand.

Run it once in each mode. `SMOKE_EXPECT_GRAFANA=0` or `=1` asserts which mode the
server is in, so a run that silently takes the wrong branch fails instead of
reporting a comfortable pass:

    ./scripts/smoke-env.sh
    K8S_ENABLED=false .venv/bin/uvicorn app.main:app --port 8000     # disabled
    SMOKE_EXPECT_GRAFANA=0 python3 scripts/smoke_phase7.py

    GRAFANA_PUBLIC_URL=/grafana K8S_ENABLED=false \
        .venv/bin/uvicorn app.main:app --port 8001                    # enabled
    SMOKE_BASE=http://127.0.0.1:8001/api/v1 SMOKE_EXPECT_GRAFANA=1 \
        python3 scripts/smoke_phase7.py

The assertion with the most weight is the one on the minted URL: a normal user's
URL must name their own namespace, must not name the other lab's, and must carry
a token whose claims say the same thing. Everything else on this page reads data
the platform already scopes; that URL is the one artefact that leaves the process
carrying an authorisation decision.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8000/api/v1")
#: Origin without the API prefix. `/.well-known/jwks.json` is served from the
#: root on purpose — Grafana fetches that exact path and will not look under
#: /api/v1 — so it cannot be reached through `BASE`.
ROOT = BASE.rsplit("/api/v1", 1)[0]
EXPECT_GRAFANA = os.environ.get("SMOKE_EXPECT_GRAFANA", "").strip()
ADMIN_USERNAME = os.environ.get("SMOKE_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("SMOKE_ADMIN_PASSWORD", "smoke-test-password-123")
SUFFIX = os.environ.get("SMOKE_SUFFIX", "p7")
RUN = os.environ.get("SMOKE_RUN", "") or str(int(time.time()))

PASSWORD = "smoke-monitor-password-123"

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
            return error.code, json.loads(payload)
        except json.JSONDecodeError:
            return error.code, payload


def call_root(method: str, path: str) -> tuple[int, object]:
    """Like `call`, but against the origin root rather than the API prefix."""
    request = urllib.request.Request(f"{ROOT}{path}", method=method)
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read().decode()
            return response.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as error:
        payload = error.read().decode()
        try:
            return error.code, json.loads(payload)
        except json.JSONDecodeError:
            return error.code, payload


def error_code(body: object) -> str:
    if isinstance(body, dict):
        return str(body.get("error", {}).get("code", ""))
    return ""


def ensure_home_volume(
    admin: str, user_id: int, workspace_id: int, username: str
) -> int | None:
    """Register the user's home directory, or find the one already registered.

    The instances watched below have no disk of their own: each mounts its *owner's*
    storage volume at ``/home/{username}``, and creating one without a volume is
    refused with ``user_volume_missing``. So this is a prerequisite of making
    anything to monitor.
    """
    status, body = call(
        "POST",
        "/storages",
        token=admin,
        body={
            "name": f"p7-home-{username}",
            "displayName": f"{username} 的主目录",
            "kind": "user_volume",
            "workspaceId": workspace_id,
            "ownerUserId": user_id,
            "capacityGb": 10,
        },
    )
    if status == 201 and isinstance(body, dict):
        return int(body["id"])
    # A re-run collides on the directory, not the name. Usernames here carry the run
    # token, so this only fires for an identical re-run.
    if status == 409 and error_code(body) == "storage_path_taken":
        status, page = call("GET", "/storages?kind=user_volume&pageSize=200", token=admin)
        if status == 200:
            for row in rows_of(page):
                if row.get("ownerUsername") == username:
                    return int(row["id"])
    return None


def rows_of(page: object) -> list[dict]:
    if isinstance(page, list):
        return [row for row in page if isinstance(row, dict)]
    if isinstance(page, dict):
        items = page.get("items")
        if isinstance(items, list):
            return [row for row in items if isinstance(row, dict)]
    return []


def find_by_name(page: object, name: str) -> dict | None:
    """The row whose ``name`` is exactly ``name``. Never "the first row"."""
    for row in rows_of(page):
        if row.get("name") == name:
            return row
    return None


def login(username: str, password: str) -> str | None:
    status, body = call("POST", "/auth/login", body={"username": username, "password": password})
    if status != 200 or not isinstance(body, dict):
        return None
    return str(body["accessToken"])


def ensure_user(admin: str, username: str, group_id: int, *, role: str = "user") -> int | None:
    status, body = call(
        "POST",
        "/users",
        token=admin,
        body={
            "username": username,
            "password": PASSWORD,
            "fullName": username,
            "roleCodes": [role],
            "groupIds": [group_id],
        },
    )
    if status == 201 and isinstance(body, dict):
        return int(body["id"])
    status, body = call("GET", f"/users?search={username}", token=admin)
    if status == 200:
        for item in rows_of(body):
            if item.get("username") == username:
                return int(item["id"])
    return None


def ensure_group(admin: str, name: str, display: str) -> int | None:
    status, body = call(
        "POST",
        "/groups",
        token=admin,
        body={"name": name, "displayName": display, "kind": "lab"},
    )
    if status == 201 and isinstance(body, dict):
        return int(body["id"])
    row = find_by_name(call("GET", f"/groups?search={name}", token=admin)[1], name)
    return int(row["id"]) if isinstance(row, dict) else None


def token_claims(url: str) -> dict:
    """Decode (without verifying) the token Grafana would be handed."""
    query = parse_qs(urlparse(url).query)
    token = query.get("auth_token", [""])[0]
    if not token:
        return {}
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def query_of(url: str) -> dict[str, str]:
    return {key: value[0] for key, value in parse_qs(urlparse(url).query).items()}


def main() -> int:
    print("\n\033[1mPhase 7 — 监控中心与 Grafana 交接\033[0m")

    admin = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if admin is None:
        print("  \033[31m无法以管理员登录，冒烟环境是否已启动？\033[0m")
        return 1
    check("管理员登录", True)

    # --- fixtures ---------------------------------------------------------
    lab_a = ensure_group(admin, f"p7-lab-a-{SUFFIX}", "P7 甲实验室")
    lab_b = ensure_group(admin, f"p7-lab-b-{SUFFIX}", "P7 乙实验室")
    check("建两个用户组", lab_a is not None and lab_b is not None)
    if lab_a is None or lab_b is None:
        return 1

    pool_name = f"p7-pool-{SUFFIX}"
    status, pool = call(
        "POST",
        "/resource-groups",
        token=admin,
        body={"name": pool_name, "displayName": "P7 池", "kind": "public"},
    )
    if status == 409:
        pool = find_by_name(
            call("GET", f"/resource-groups?search={pool_name}", token=admin)[1], pool_name
        )
        status = 200
    check("建资源组", status in (200, 201), str(pool))
    if not isinstance(pool, dict):
        return 1
    pool_id = int(pool["id"])

    spec_name = f"p7-spec-{SUFFIX}"
    status, spec = call(
        "POST",
        "/resource-specs",
        token=admin,
        body={
            "name": spec_name,
            "displayName": "P7 单卡",
            "cpuMilli": 2000,
            "memoryMb": 4096,
            "gpuType": "A100",
            "gpuCount": 1,
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

    # Workspace and user names carry the run token: an instance holds GPU quota
    # until a reconciler stops it, and there is no reconciler in the smoke
    # environment. A stable name would make the second run fail on quota.
    workspace_ids: dict[str, int] = {}
    namespaces: dict[str, str] = {}
    for tag, group_id in (("a", lab_a), ("b", lab_b)):
        ws_name = f"p7-ws-{tag}-{SUFFIX}-{RUN}"
        status, workspace = call(
            "POST",
            "/workspaces",
            token=admin,
            body={"name": ws_name, "groupId": group_id, "resourceGroupIds": [pool_id]},
        )
        if status == 409:
            workspace = find_by_name(
                call("GET", f"/workspaces?search={ws_name}", token=admin)[1], ws_name
            )
            status = 200
        check(f"建工作空间 {tag}", status in (200, 201), str(workspace))
        if not isinstance(workspace, dict):
            return 1
        workspace_ids[tag] = int(workspace["id"])
        namespaces[tag] = str(workspace["k8sNamespace"])

    worker_a = ensure_user(admin, f"p7-dana-{SUFFIX}-{RUN}", lab_a)
    worker_b = ensure_user(admin, f"p7-erin-{SUFFIX}-{RUN}", lab_b)
    boss_a = ensure_user(admin, f"p7-boss-{SUFFIX}-{RUN}", lab_a, role="group_admin")
    gazer = ensure_user(admin, f"p7-gazer-{SUFFIX}-{RUN}", lab_a, role="observer")
    check(
        "建用户（甲组普通/管理员/观察者 + 乙组普通）",
        None not in (worker_a, worker_b, boss_a, gazer),
    )
    if None in (worker_a, worker_b, boss_a, gazer):
        return 1

    for user_id, tag in ((worker_a, "a"), (boss_a, "a"), (gazer, "a"), (worker_b, "b")):
        call(
            "POST",
            f"/workspaces/{workspace_ids[tag]}/members",
            token=admin,
            body={"userId": user_id, "moveFromExisting": True},
        )

    token_a = login(f"p7-dana-{SUFFIX}-{RUN}", PASSWORD)
    token_b = login(f"p7-erin-{SUFFIX}-{RUN}", PASSWORD)
    token_boss = login(f"p7-boss-{SUFFIX}-{RUN}", PASSWORD)
    token_gazer = login(f"p7-gazer-{SUFFIX}-{RUN}", PASSWORD)
    check("四个账号均可登录", None not in (token_a, token_b, token_boss, token_gazer))
    if None in (token_a, token_b, token_boss, token_gazer):
        return 1

    # The two instances below mount their owners' volumes, so both must exist first.
    # Dana (lab A) and Erin (lab B) are the only accounts that create a workload.
    for worker, tag, username in (
        (worker_a, "a", f"p7-dana-{SUFFIX}-{RUN}"),
        (worker_b, "b", f"p7-erin-{SUFFIX}-{RUN}"),
    ):
        home = ensure_home_volume(admin, worker, workspace_ids[tag], username)
        check(f"为用户 {username} 登记主目录存储卷", home is not None, str(home))
        if home is None:
            return 1

    # --- live containers to be watched ------------------------------------
    instance_names: dict[str, str] = {}
    for tag, token in (("a", token_a), ("b", token_b)):
        name = f"p7-dev-{tag}-{RUN}"
        status, instance = call(
            "POST",
            "/instances",
            token=token,
            body={
                "name": name,
                "displayName": f"P7 {tag} 开发环境",
                "resourceGroupId": pool_id,
                "resourceSpecId": spec_id,
                "image": "registry.local/pytorch:2.4",
                "sshEnabled": False,
            },
        )
        check(f"创建实例 {tag}", status == 201, str(instance))
        if not isinstance(instance, dict):
            return 1
        instance_names[tag] = str(instance["name"])

    # --- permission --------------------------------------------------------
    status, _anon = call("GET", "/monitor/overview")
    check("未登录不能访问监控总览", status == 401, str(status))

    status, overview = call("GET", "/monitor/overview", token=token_gazer)
    check("观察者可访问监控总览", status == 200, str(status))
    if not isinstance(overview, dict):
        return 1

    # --- scope -------------------------------------------------------------
    check(
        "总览返回是否启用 Grafana 的标记", isinstance(overview.get("enabled"), bool), str(overview)
    )

    status, as_user = call("GET", "/monitor/overview", token=token_a)
    check("普通用户可访问监控总览", status == 200, str(status))
    if not isinstance(as_user, dict):
        return 1

    names = {row["name"] for row in as_user["containers"]}
    check("普通用户只看到自己的容器", names == {instance_names["a"]}, str(names))
    check(
        "普通用户的命名空间被限定为本组",
        as_user["scope"]["namespaces"] == [namespaces["a"]],
        str(as_user["scope"]),
    )
    # A list, not necessarily non-empty. The smoke environment has no
    # reconciler, so an instance stays `creating` with no pod assigned and
    # `pod_name` is NULL. That is a real state — and the one worth checking, since
    # an empty pod list must fail *closed*: `dashboard_variables` sends
    # `var-pod=none` for it, which matches no pod, rather than omitting the
    # parameter and letting the panel fall back to the whole lab's namespace.
    check(
        "普通用户的 Pod 范围是一个列表",
        isinstance(as_user["scope"]["pods"], list),
        str(as_user["scope"]),
    )
    check(
        "普通用户不是无限制范围",
        as_user["scope"]["unrestricted"] is False,
        str(as_user["scope"]),
    )

    status, as_admin = call("GET", "/monitor/overview", token=admin)
    check("全局管理员可访问监控总览", status == 200, str(status))
    if not isinstance(as_admin, dict):
        return 1
    check(
        "全局管理员为无限制范围", as_admin["scope"]["unrestricted"] is True, str(as_admin["scope"])
    )
    # A superset, not an equality: this database is shared with the other phase
    # smokes, so an administrator legitimately sees their leftover instances too.
    # Asserting equality would make this script pass only on a virgin database —
    # which is the opposite of re-runnable.
    check(
        "全局管理员看到两个组的容器",
        set(instance_names.values()) <= {row["name"] for row in as_admin["containers"]},
        str(as_admin["containers"]),
    )

    status, as_boss = call("GET", "/monitor/overview", token=token_boss)
    check("组管理员可访问监控总览", status == 200, str(status))
    if not isinstance(as_boss, dict):
        return 1
    # Superset again — lab A accumulates a workspace per run on a shared
    # database — so the assertion that carries weight is the *exclusion*: the
    # other lab's namespace must never appear, however many of this lab's do.
    check(
        "组管理员的范围含本组命名空间",
        namespaces["a"] in as_boss["scope"]["namespaces"],
        str(as_boss["scope"]),
    )
    check(
        "组管理员的范围不含其他组命名空间",
        namespaces["b"] not in as_boss["scope"]["namespaces"],
        str(as_boss["scope"]),
    )

    # --- the container list carries the join keys --------------------------
    container = as_user["containers"][0]
    check("容器行带命名空间", container["namespace"] == namespaces["a"], str(container))
    # The key is always present; the value is null until a pod is scheduled.
    # Asserting the presence of the field is what matters here — a rename or a
    # drop would break the Grafana join silently — and the pod-lock behaviour
    # itself is covered in `tests/test_acceptance_phase7.py`, where a pod name
    # can be set deterministically.
    check("容器行带 Pod 字段", "podName" in container, str(container))
    check("容器行带负责人", container["ownerUsername"].startswith("p7-dana"), str(container))
    check(
        "容器行带加速卡",
        container["gpuCount"] == 1 and container["gpuType"] == "A100",
        str(container),
    )

    # --- inventory ---------------------------------------------------------
    check("总览包含平台自身的清单", isinstance(as_admin.get("inventory"), dict), str(as_admin))
    check(
        "清单按状态统计实例",
        any(row["status"] == "creating" for row in as_admin["inventory"]["instances"]),
        str(as_admin["inventory"]["instances"]),
    )

    # --- the Grafana branch, whichever one this deployment is in -----------
    enabled = bool(as_admin["enabled"])

    # The mode the operator believes they are testing, asserted rather than
    # assumed: "the disabled branch passed" is a very reassuring and completely
    # useless result when Grafana was in fact configured.
    if EXPECT_GRAFANA in ("0", "1"):
        check(
            f"服务端 Grafana 状态与 SMOKE_EXPECT_GRAFANA={EXPECT_GRAFANA} 一致",
            enabled == (EXPECT_GRAFANA == "1"),
            f"enabled={enabled}",
        )

    if enabled:
        print("  \033[2m（Grafana 已配置：校验面板编目与签名地址）\033[0m")
    else:
        print("  \033[2m（Grafana 未配置：校验降级行为）\033[0m")

    user_keys = [item["key"] for item in as_user["dashboards"]]
    admin_keys = [item["key"] for item in as_admin["dashboards"]]
    boss_keys = [item["key"] for item in as_boss["dashboards"]]

    if enabled:
        check("普通用户只看到自己的面板", user_keys == ["my-containers"], str(user_keys))
        check(
            "组管理员看到本组面板但不含集群面板",
            set(boss_keys) == {"lab-overview", "my-containers"},
            str(boss_keys),
        )
        check(
            "全局管理员看到全部面板",
            set(admin_keys) == {"lab-overview", "my-containers", "gpu-fleet", "node-health"},
            str(admin_keys),
        )
        check(
            "未配置时不会有禁用原因",
            as_user["disabledReason"] is None,
            str(as_user["disabledReason"]),
        )

        embed = as_user["embeds"]["my-containers"]
        query = query_of(embed["url"])
        claims = token_claims(embed["url"])

        check(
            "普通用户的面板地址可用",
            embed["url"].startswith("http") or embed["url"].startswith("/"),
            embed["url"],
        )
        check("面板地址按 uid 寻址", "/d/wl-my-containers" in embed["url"], embed["url"])
        check("命名空间变量被锁定", query.get("var-namespace") == namespaces["a"], str(query))
        check("容器变量被锁定", bool(query.get("var-pod")), str(query))
        check(
            "地址中不含乙组命名空间",
            namespaces["b"] not in embed["url"],
            embed["url"],
        )
        check("令牌类型为 grafana", claims.get("typ") == "grafana", str(claims))
        check("令牌带命名空间声明", claims.get("wl_namespaces") == [namespaces["a"]], str(claims))
        check("令牌声明非无限制", claims.get("wl_unrestricted") is False, str(claims))
        check(
            "令牌为短期凭证",
            0 < claims.get("exp", 0) - claims.get("iat", 0) <= 600,
            str(claims),
        )

        admin_embed = as_admin["embeds"]["gpu-fleet"]
        admin_query = query_of(admin_embed["url"])
        check(
            "管理员面板不带命名空间过滤而非通配",
            "var-namespace" not in admin_query and "$__all" not in admin_embed["url"],
            admin_embed["url"],
        )
        check(
            "管理员令牌声明无限制",
            token_claims(admin_embed["url"]).get("wl_unrestricted") is True,
            str(token_claims(admin_embed["url"])),
        )

        status, minted = call("POST", "/monitor/dashboards/my-containers/embed", token=token_a)
        check("可重新签发面板地址", status == 200 and isinstance(minted, dict), str(minted))
        if isinstance(minted, dict):
            check(
                "重新签发的地址仍然被锁定",
                query_of(minted["url"]).get("var-namespace") == namespaces["a"],
                str(minted),
            )
            check("返回了有效期秒数", minted["expiresIn"] > 0, str(minted))
        else:
            check("重新签发的地址仍然被锁定", False, "no body")
            check("返回了有效期秒数", False, "no body")

        status, forbidden = call("POST", "/monitor/dashboards/gpu-fleet/embed", token=token_a)
        check("无权打开的面板拒绝签发", status == 404, f"{status} {forbidden}")
        check(
            "拒绝理由不暴露面板是否存在",
            error_code(forbidden) == "dashboard_not_found",
            str(forbidden),
        )
    else:
        check("未配置 Grafana 时不列出面板", user_keys == [], str(user_keys))
        check("未配置 Grafana 时不列出面板（管理员）", admin_keys == [], str(admin_keys))
        check("未配置 Grafana 时不列出面板（组管理员）", boss_keys == [], str(boss_keys))
        check(
            "未配置时给出原因",
            bool(as_user.get("disabledReason")),
            str(as_user.get("disabledReason")),
        )
        check("未配置时不签发任何地址", as_user["embeds"] == {}, str(as_user["embeds"]))
        check("未配置时管理员也没有地址", as_admin["embeds"] == {}, str(as_admin["embeds"]))
        check("未配置时组管理员也没有地址", as_boss["embeds"] == {}, str(as_boss["embeds"]))

        status, minted = call("POST", "/monitor/dashboards/my-containers/embed", token=token_a)
        check("未配置时拒绝签发地址", status == 404, f"{status} {minted}")
        check(
            "拒绝原因为未启用监控",
            error_code(minted) == "monitoring_disabled",
            str(minted),
        )

        status, forbidden = call("POST", "/monitor/dashboards/no-such/embed", token=token_a)
        check("未知面板同样拒绝", status == 404, f"{status} {forbidden}")
        check(
            "未知面板不区分于无权面板",
            error_code(forbidden) == "dashboard_not_found",
            str(forbidden),
        )

    # --- the JWKS Grafana fetches ------------------------------------------
    status, jwks = call_root("GET", "/.well-known/jwks.json")
    check("JWKS 端点可访问", status == 200 and isinstance(jwks, dict), f"{status} {jwks}")
    check("JWKS 含 keys 字段", isinstance(jwks, dict) and "keys" in jwks, str(jwks))

    print()
    if failures:
        print(
            f"\033[31m{len(failures)} 项失败\033[0m / 共 {passed + len(failures)} 项 - {failures[0]}"
        )
        return 1
    print(f"\033[32m全部 {passed} 项通过\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
