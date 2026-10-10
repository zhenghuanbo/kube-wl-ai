#!/usr/bin/env python3
"""Phase 6 smoke: the usage centre against a running backend.

Run with::

    source scripts/smoke-env.sh
    K8S_ENABLED=false .venv/bin/uvicorn app.main:app --port 8000 &
    python3 scripts/smoke_phase6.py

The interesting assertion here is an *identity*, not a number. A task submitted
through the API starts running immediately, so a couple of seconds later the
rollup must report ``gpuSeconds == gpuCount × runtimeSeconds`` exactly — for that
task's owner, read back through the scoped summary. That holds regardless of how
long the request took, which is what makes it a meaningful thing to check from
outside the process — unlike "roughly two seconds", which would be a flaky test
dressed up as a thorough one.

The identity is checked against a *per-user* total on purpose. The global rollup
totals cover every workload in the window, and a CPU-only job contributes runtime
without GPU seconds while an eight-GPU job contributes eight times as many, so
the same identity does not hold there and never did.

Everything else is the permission matrix and the wire shape: two labs so the
scope can actually be seen to exclude something, and a group administrator who
may export their own group but not recompute everybody's totals.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8000/api/v1")
ADMIN_USERNAME = os.environ.get("SMOKE_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("SMOKE_ADMIN_PASSWORD", "smoke-test-password-123")
SUFFIX = os.environ.get("SMOKE_SUFFIX", "p6")
#: Names must be unique per run or a second invocation collides; the script is
#: meant to be re-runnable, so it mints its own token.
RUN = os.environ.get("SMOKE_RUN", "") or str(int(time.time()))

PASSWORD = "smoke-usage-password-123"

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
            decoded = json.loads(payload) if payload else None
        except json.JSONDecodeError:
            decoded = payload
        return error.code, decoded


def call_raw(path: str, *, token: str) -> tuple[int, bytes, dict[str, str]]:
    """A plain GET without the JSON envelope, for the CSV download.

    Header names are lower-cased here: ``dict(response.headers)`` normalises them
    that way, so looking up ``Content-Type`` afterwards finds nothing and the
    check fails for a reason that has nothing to do with the server.
    """
    request = urllib.request.Request(f"{BASE}{path}")
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request) as response:
            return (
                response.status,
                response.read(),
                {key.lower(): value for key, value in response.headers.items()},
            )
    except urllib.error.HTTPError as error:
        return (
            error.code,
            error.read(),
            {key.lower(): value for key, value in error.headers.items()},
        )


def first_row(page: object) -> object:
    if isinstance(page, list):
        return page[0] if page else None
    if isinstance(page, dict):
        items = page.get("items")
        if isinstance(items, list):
            return items[0] if items else None
    return None


def rows_of(page: object) -> list[dict]:
    """Every row of a list response, whether it is enveloped or bare."""
    if isinstance(page, list):
        return [row for row in page if isinstance(row, dict)]
    if isinstance(page, dict):
        items = page.get("items")
        if isinstance(items, list):
            return [row for row in items if isinstance(row, dict)]
    return []


def find_by_name(page: object, name: str) -> dict | None:
    """
    The row whose ``name`` is *exactly* ``name``, or ``None``.

    `first_row` was used here first, and it is the wrong tool for looking a
    fixture back up: it returns whatever the API happened to sort first. When the
    lookup silently stopped filtering, that turned into "test a different object
    and never notice", which is worse than failing.
    """
    for row in rows_of(page):
        if row.get("name") == name:
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


def ensure_home_volume(
    admin: str, user_id: int, workspace_id: int, username: str
) -> int | None:
    """Register the user's home directory, or find the one already registered.

    The task submitted below has no disk of its own: it mounts the *owner's* storage
    volume at ``/home/{username}``, and submitting one without a volume is refused
    with ``user_volume_missing``. So this is a prerequisite of making any usage.
    """
    status, body = call(
        "POST",
        "/storages",
        token=admin,
        body={
            "name": f"p6-home-{username}",
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
    # by their group and username. Usernames here carry the run token, so this only
    # fires for an identical re-run.
    if status == 409 and error_code(body) == "storage_path_taken":
        status, page = call("GET", "/storages?kind=user_volume&pageSize=200", token=admin)
        if status == 200 and isinstance(page, dict):
            for row in page.get("items", []):
                if row.get("ownerUsername") == username:
                    return int(row["id"])
    return None


def ensure_user(admin: str, username: str, group_id: int, role: str = "user") -> int | None:
    status, body = call(
        "POST",
        "/users",
        token=admin,
        body={
            "username": username,
            "password": PASSWORD,
            # `fullName`, not `displayName`: the display name is derived.
            "fullName": username,
            "roleCodes": [role],
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


def main() -> int:
    print("\n\033[1mPhase 6 — 用量中心\033[0m")

    admin = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if admin is None:
        print("  \033[31m无法以管理员登录，冒烟环境是否已启动？\033[0m")
        return 1
    check("管理员登录", True)

    # --- fixtures ---------------------------------------------------------
    lab_a = ensure_group(admin, f"p6-lab-a-{SUFFIX}", "P6 甲实验室")
    lab_b = ensure_group(admin, f"p6-lab-b-{SUFFIX}", "P6 乙实验室")
    check("建两个用户组", lab_a is not None and lab_b is not None)
    if lab_a is None or lab_b is None:
        return 1

    status, pool = call(
        "POST",
        "/resource-groups",
        token=admin,
        body={"name": f"p6-pool-{SUFFIX}", "displayName": "P6 池", "kind": "public"},
    )
    if status == 409:
        pool = find_by_name(
            call("GET", f"/resource-groups?search=p6-pool-{SUFFIX}", token=admin)[1],
            f"p6-pool-{SUFFIX}",
        )
        status = 200
    check("建资源组", status in (200, 201), str(pool))
    if not isinstance(pool, dict):
        return 1
    pool_id = int(pool["id"])

    spec_name = f"p6-spec-{SUFFIX}"
    status, spec = call(
        "POST",
        "/resource-specs",
        token=admin,
        body={
            "name": spec_name,
            "displayName": "P6 单卡",
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
    check("建单卡资源规格", status in (200, 201), str(spec))
    if not isinstance(spec, dict):
        return 1
    # Asserted rather than assumed. An earlier version of this script picked the
    # first row of the list and silently ran against a CPU-only spec, which made
    # the GPU accounting identity below fail for a reason nothing pointed at.
    check(
        "选中的规格是单卡 A100",
        spec.get("gpuCount") == 1 and str(spec.get("gpuType", "")).lower() == "a100",
        str(spec),
    )
    spec_id = int(spec["id"])
    call("PUT", f"/resource-groups/{pool_id}/specs", token=admin, body=[spec_id])

    # Workspace names carry the run token, unlike the groups. A fresh workspace
    # per run gets a fresh GPU quota: the task submitted below is still running
    # when the script exits (there is no reconciler here), so a reused workspace
    # would make the second run fail on quota and look like a product bug.
    # Groups are the only fixture that stays fixed, because they consume nothing.
    workspace_ids: dict[str, int] = {}
    for tag, group_id in (("a", lab_a), ("b", lab_b)):
        ws_name = f"p6-ws-{tag}-{SUFFIX}-{RUN}"
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

    # Usernames carry the run token as well as the workspaces. Quota is counted
    # against the *user* across all their workspaces, so a stable name would
    # inherit last run's still-running task and the second run would be refused
    # for exceeding a limit it never approached. Groups are the only fixture that
    # stays fixed, because a group consumes no quota.
    worker_a = ensure_user(admin, f"p6-dana-{SUFFIX}-{RUN}", lab_a)
    worker_b = ensure_user(admin, f"p6-erin-{SUFFIX}-{RUN}", lab_b)
    boss_a = ensure_user(admin, f"p6-boss-{SUFFIX}-{RUN}", lab_a, role="group_admin")
    gazer = ensure_user(admin, f"p6-gazer-{SUFFIX}-{RUN}", lab_a, role="observer")
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

    token_a = login(f"p6-dana-{SUFFIX}-{RUN}", PASSWORD)
    token_b = login(f"p6-erin-{SUFFIX}-{RUN}", PASSWORD)
    token_boss = login(f"p6-boss-{SUFFIX}-{RUN}", PASSWORD)
    token_gazer = login(f"p6-gazer-{SUFFIX}-{RUN}", PASSWORD)
    check("四个账号均可登录", None not in (token_a, token_b, token_boss, token_gazer))
    if None in (token_a, token_b, token_boss, token_gazer):
        return 1

    # Dana's task below mounts her own volume, so it must exist first. She is the
    # only account here that creates a workload.
    home_a = ensure_home_volume(
        admin, worker_a, workspace_ids["a"], f"p6-dana-{SUFFIX}-{RUN}"
    )
    check("为甲组用户登记主目录存储卷", home_a is not None, str(home_a))
    if home_a is None:
        return 1

    # --- make some usage --------------------------------------------------
    # A submitted task starts running immediately, which is what gives the
    # rollup a real live interval to measure.
    status, task = call(
        "POST",
        "/tasks",
        token=token_a,
        body={
            "name": f"p6-job-{RUN}",
            "resourceGroupId": pool_id,
            "resourceSpecId": spec_id,
            "image": "registry.local/pytorch:2.4",
            "script": "sleep 1",
        },
    )
    check("甲组用户提交任务", status == 201, str(task))

    time.sleep(3)

    status, rolled = call("POST", "/usage/rollup", token=admin, body={})
    check("管理员触发用量重算", status == 200, str(rolled))
    if not isinstance(rolled, dict):
        return 1
    check("重算扫描到该任务", rolled["tasksScanned"] >= 1, str(rolled))
    check("重算写入了记录", rolled["rowsWritten"] >= 1, str(rolled))

    # Deliberately *not* asserting `gpuSeconds == runtimeSeconds` here. That
    # identity holds for one GPU on one workload, and this result covers every
    # workload in the window — a CPU-only job contributes runtime but no GPU
    # seconds, an eight-GPU job contributes eight times as many. It looked like a
    # passing check for a while only because the database happened to contain
    # nothing but single-GPU work. The identity belongs where the scope is a
    # single workload, which is the per-user summary below.
    check(
        "重算返回了计量结果",
        rolled["gpuSeconds"] > 0 and rolled["runtimeSeconds"] > 0,
        str(rolled),
    )

    today = time.strftime("%Y-%m-%d", time.gmtime())
    window = f"dayFrom={today}&dayTo={today}"

    # --- summary and the accounting identity ------------------------------
    status, summary = call("GET", f"/usage/summary?{window}", token=token_a)
    check("本人用量摘要", status == 200, str(summary))
    if not isinstance(summary, dict):
        return 1
    totals = summary["totals"]

    # The global rollup must at least cover this user, who is the only one this
    # run added. This is the check that actually has teeth: it compares two
    # independently produced numbers, so a rollup that silently dropped rows
    # fails here rather than passing on its own self-consistency.
    check(
        "重算总量覆盖本人用量",
        rolled["runtimeSeconds"] >= totals["runtimeSeconds"]
        and rolled["gpuSeconds"] >= totals["gpuSeconds"],
        f"重算 {rolled['runtimeSeconds']}/{rolled['gpuSeconds']} "
        f"本人 {totals['runtimeSeconds']}/{totals['gpuSeconds']}",
    )
    check("总量覆盖了该任务", totals["workloadCount"] >= 1, str(totals))
    check(
        "gpuSeconds == 卡数 × 运行秒",
        totals["gpuSeconds"] == totals["runtimeSeconds"],
        str(totals),
    )
    check("运行了至少 2 秒", totals["runtimeSeconds"] >= 2, str(totals))
    check(
        "卡时由秒折算",
        abs(totals["gpuHours"] - round(totals["gpuSeconds"] / 3600, 2)) < 0.011,
        str(totals),
    )
    check("已重算标记为真", summary["covered"] is True)
    check("普通用户不返回自身份额（它就是自身）", summary["selfTotals"] is None)

    # --- the scope actually excludes something ----------------------------
    status, other = call("GET", f"/usage/summary?{window}", token=token_b)
    check(
        "乙组用户看不到甲组的用量",
        isinstance(other, dict) and other["totals"]["gpuSeconds"] == 0,
        str(other),
    )

    status, boss_view = call("GET", f"/usage/summary?{window}", token=token_boss)
    check(
        "甲组管理员能看到甲组用量",
        isinstance(boss_view, dict) and boss_view["totals"]["gpuSeconds"] >= totals["gpuSeconds"],
        str(boss_view),
    )
    check(
        "管理员另附自身份额",
        isinstance(boss_view, dict) and boss_view["selfTotals"] is not None,
        str(boss_view),
    )

    # --- the series -------------------------------------------------------
    status, series = call("GET", f"/usage/series?{window}", token=admin)
    check("时间序列", status == 200 and isinstance(series, dict), str(series))
    if isinstance(series, dict):
        check("区间内每天一个点", len(series["points"]) == 1, str(series["points"]))
        check(
            "点上有日期与总量",
            "day" in series["points"][0] and "totals" in series["points"][0],
        )

    status, wide = call(
        "GET",
        f"/usage/series?dayFrom={today}&dayTo={today}&workloadKind=task",
        token=admin,
    )
    check("按负载类型过滤", status == 200, str(wide))

    status, empty = call(
        "GET",
        f"/usage/series?dayFrom={today}&dayTo={today}&gpuType=V100",
        token=admin,
    )
    check(
        "不存在的卡型号过滤为空",
        isinstance(empty, dict) and empty["points"][0]["totals"]["gpuSeconds"] == 0,
        str(empty),
    )

    # --- the breakdown ----------------------------------------------------
    for dimension in ("user", "group", "workspace", "gpu_type", "workload_kind"):
        status, body = call("GET", f"/usage/breakdown?by={dimension}&{window}", token=admin)
        ok = status == 200 and isinstance(body, dict) and len(body["rows"]) >= 1
        check(f"分组维度 {dimension}", ok, str(body)[:160])

    status, by_user = call("GET", f"/usage/breakdown?by=user&{window}", token=admin)
    if isinstance(by_user, dict):
        labels = [row["label"] for row in by_user["rows"]]
        check(
            "按用户分组带出姓名而非编号",
            all(not label.startswith("用户 #") for label in labels),
            str(labels),
        )
        check(
            "甲组用户出现在按用户分组中",
            any("p6-dana" in label for label in labels),
            str(labels),
        )
        check(
            "乙组用户不出现",
            not any("p6-erin" in label for label in labels),
            str(labels),
        )
        check("按用户分组带负载类型拆分", all("kinds" in row for row in by_user["rows"]))

    status, by_model = call("GET", f"/usage/breakdown?by=gpu_type&{window}", token=admin)
    if isinstance(by_model, dict):
        check(
            "按卡型号分组给出 A100",
            by_model["rows"][0]["label"] == "A100",
            str(by_model["rows"]),
        )
        check("卡型号没有可下钻的 id", by_model["rows"][0]["entityId"] is None)

    # --- validation -------------------------------------------------------
    status, body = call("GET", "/usage/summary?dayFrom=2026-03-10&dayTo=2026-03-01", token=admin)
    check(
        "区间倒置被拒",
        status == 400 and error_code(body) == "usage_invalid_range",
        str(body),
    )

    status, body = call("GET", "/usage/summary?dayFrom=1999-01-01&dayTo=2026-03-01", token=admin)
    check(
        "区间过大被拒",
        status == 400 and error_code(body) == "usage_range_too_large",
        str(body),
    )

    status, body = call("GET", "/usage/breakdown?by=moon_phase", token=admin)
    check(
        "未知分组维度被拒",
        status == 400 and error_code(body) == "usage_unknown_dimension",
        str(body),
    )

    status, body = call("POST", "/usage/rollup", token=admin, body={"dayFromm": today})
    check(
        "请求体拼写错误被拒",
        status == 422 and error_code(body) == "validation_failed",
        str(body),
    )

    # snake_case query keys are not aliases; the explicit range is ignored.
    status, snake = call("GET", "/usage/summary?day_from=2026-03-09&day_to=2026-03-09", token=admin)
    check(
        "snake_case 查询参数被忽略（camelCase 才是别名）",
        isinstance(snake, dict) and snake["dayFrom"] != "2026-03-09",
        str(snake)[:160],
    )

    # --- metadata ---------------------------------------------------------
    status, meta = call("GET", "/usage/meta", token=admin)
    check("元数据端点", status == 200 and isinstance(meta, dict), str(meta))
    if isinstance(meta, dict):
        check("元数据列出全部维度", len(meta["dimensions"]) == 5, str(meta["dimensions"]))
        check(
            "元数据列出负载类型",
            [k["value"] for k in meta["kinds"]] == ["instance", "task"],
            str(meta["kinds"]),
        )
        check("元数据给出区间上限", meta["maxRangeDays"] == 731)

    # --- export -----------------------------------------------------------
    status, body = call("GET", f"/usage/export?{window}", token=token_a)
    check(
        "普通用户不能导出",
        status == 403 and error_code(body) == "permission_denied",
        str(body),
    )

    status, raw, headers = call_raw(f"/usage/export?{window}", token=token_boss)
    check("组管理员可以导出", status == 200, str(status))
    check(
        "导出是 CSV",
        headers.get("content-type", "").startswith("text/csv"),
        str(headers),
    )
    check(
        "导出带下载文件名",
        "attachment" in headers.get("content-disposition", ""),
        headers.get("content-disposition", ""),
    )
    check("导出带 UTF-8 BOM（Excel 需要）", raw.startswith(b"\xef\xbb\xbf"), repr(raw[:6]))
    text = raw.decode("utf-8-sig")
    check("导出表头是中文", "GPU卡时" in text, text[:120])
    check("导出含甲组用户", f"p6-dana-{SUFFIX}-{RUN}" in text, text[:400])
    check("导出不含乙组用户", f"p6-erin-{SUFFIX}-{RUN}" not in text, text[:400])

    # --- permissions ------------------------------------------------------
    status, body = call("POST", "/usage/rollup", token=token_boss, body={})
    check(
        "组管理员不能触发全局重算",
        status == 403 and error_code(body) == "permission_denied",
        str(body),
    )

    status, body = call("GET", f"/usage/summary?{window}", token=token_gazer)
    check("观察者可以看自己的用量", status == 200, str(body))
    status, body = call("GET", f"/usage/export?{window}", token=token_gazer)
    check("观察者不能导出", status == 403, str(body))

    # The audit trail is asserted in tests/test_acceptance_phase6.py against the
    # table directly. There is no read endpoint for it yet — that arrives with
    # Phase 8 — so a smoke script has no way to see it, and inventing one here
    # would be testing a surface that does not exist.

    print()
    if failures:
        print(f"\033[31m{len(failures)} 项失败\033[0m / 共 {passed + len(failures)} 项")
        for label in failures:
            print(f"  - {label}")
        return 1
    print(f"\033[32m全部 {passed} 项通过\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
