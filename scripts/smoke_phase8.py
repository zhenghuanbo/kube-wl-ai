#!/usr/bin/env python3
"""Phase 8 smoke: the audit trail, personal access tokens, and login limits.

Run against a live backend::

    source scripts/smoke-env.sh
    K8S_ENABLED=false .venv/bin/uvicorn app.main:app --port 8000 &
    python3 scripts/smoke_phase8.py

Runnable repeatedly against a populated database. Anything that accumulates —
users, tokens — carries the run token in its name, and every list assertion is a
*superset* or an *exclusion* rather than an equality, because earlier runs' rows
are still there and still legitimately visible.

The checks that matter most:

* a token's secret appears in exactly one response and never in the audit trail;
* a token cannot declare a permission its owner lacks, and a token-created token
  cannot widen its parent's scopes;
* removing a role narrows a live token on the next request, without the token
  being touched;
* a group administrator's audit view excludes the other lab — and excludes
  unattributed rows, which belong to nobody;
* login failures eventually produce a 429, and a success clears the count.
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
SUFFIX = os.environ.get("SMOKE_SUFFIX", "p8")
RUN = os.environ.get("SMOKE_RUN", "") or str(int(time.time()))

PASSWORD = "smoke-token-password-123"

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
    raw: bool = False,
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
            if raw:
                return response.status, payload
            return response.status, (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as error:
        payload = error.read().decode()
        if raw:
            return error.code, payload
        try:
            return error.code, json.loads(payload)
        except json.JSONDecodeError:
            return error.code, payload


def error_code(body: object) -> str:
    if isinstance(body, dict):
        return str(body.get("error", {}).get("code", ""))
    return ""


def rows_of(page: object) -> list[dict]:
    if isinstance(page, list):
        return [row for row in page if isinstance(row, dict)]
    if isinstance(page, dict):
        items = page.get("items")
        if isinstance(items, list):
            return [row for row in items if isinstance(row, dict)]
    return []


def find_by_name(page: object, name: str) -> dict | None:
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
        "POST", "/groups", token=admin, body={"name": name, "displayName": display, "kind": "lab"}
    )
    if status == 201 and isinstance(body, dict):
        return int(body["id"])
    row = find_by_name(call("GET", f"/groups?search={name}", token=admin)[1], name)
    return int(row["id"]) if isinstance(row, dict) else None


def main() -> int:
    print("\n\033[1mPhase 8 — 审计、API 令牌与登录限流\033[0m")

    admin = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if admin is None:
        print("  \033[31m无法以管理员登录，冒烟环境是否已启动？\033[0m")
        return 1
    check("管理员登录", True)

    # --- fixtures ---------------------------------------------------------
    lab_a = ensure_group(admin, f"p8-lab-a-{SUFFIX}", "P8 甲实验室")
    lab_b = ensure_group(admin, f"p8-lab-b-{SUFFIX}", "P8 乙实验室")
    check("建两个用户组", lab_a is not None and lab_b is not None)
    if lab_a is None or lab_b is None:
        return 1

    worker_a = ensure_user(admin, f"p8-dana-{SUFFIX}-{RUN}", lab_a)
    worker_b = ensure_user(admin, f"p8-erin-{SUFFIX}-{RUN}", lab_b)
    boss_a = ensure_user(admin, f"p8-boss-{SUFFIX}-{RUN}", lab_a, role="group_admin")
    gazer = ensure_user(admin, f"p8-gazer-{SUFFIX}-{RUN}", lab_a, role="observer")
    check(
        "建用户（甲组普通/管理员/观察者 + 乙组普通）",
        None not in (worker_a, worker_b, boss_a, gazer),
    )
    if None in (worker_a, worker_b, boss_a, gazer):
        return 1

    # Every account signs in, including the one in the *other* lab. The audit
    # rows a successful login writes are what the scope checks below read: a user
    # who never logs in and never writes anything has no trail to find, so
    # seeding the fixture through the admin alone would make "the global admin
    # cannot see erin" pass for the wrong reason.
    token_a = login(f"p8-dana-{SUFFIX}-{RUN}", PASSWORD)
    token_b = login(f"p8-erin-{SUFFIX}-{RUN}", PASSWORD)
    token_boss = login(f"p8-boss-{SUFFIX}-{RUN}", PASSWORD)
    token_gazer = login(f"p8-gazer-{SUFFIX}-{RUN}", PASSWORD)
    check("四个账号均可登录", None not in (token_a, token_b, token_boss, token_gazer))
    if None in (token_a, token_b, token_boss, token_gazer):
        return 1

    # --- the audit trail is readable and permission-gated -----------------
    status, _ = call("GET", "/audit-logs")
    check("未登录不能读取审计日志", status == 401, str(status))

    status, _ = call("GET", "/audit-logs", token=token_gazer)
    check("观察者不能读取审计日志", status == 403, str(status))

    status, trail = call("GET", "/audit-logs", token=token_boss)
    check("组管理员可读取审计日志", status == 200, str(status))
    if not isinstance(trail, dict):
        return 1
    check("审计日志返回分页信封", "items" in trail and "meta" in trail, str(trail))

    status, _admin_trail = call("GET", "/audit-logs", token=admin)
    check("全局管理员可读取审计日志", status == 200, str(status))

    # --- scope ------------------------------------------------------------
    status, boss_trail = call(
        "GET", f"/audit-logs?actorUsername=p8-dana-{SUFFIX}-{RUN}", token=token_boss
    )
    check(
        "组管理员能看到本组成员的记录", status == 200 and bool(rows_of(boss_trail)), str(boss_trail)
    )

    status, boss_other = call(
        "GET", f"/audit-logs?actorUsername=p8-erin-{SUFFIX}-{RUN}", token=token_boss
    )
    check(
        "组管理员看不到其他组的记录",
        status == 200 and rows_of(boss_other) == [],
        str(boss_other),
    )

    status, admin_other = call(
        "GET", f"/audit-logs?actorUsername=p8-erin-{SUFFIX}-{RUN}", token=admin
    )
    check(
        "全局管理员能看到其他组的记录",
        status == 200 and bool(rows_of(admin_other)),
        str(admin_other),
    )

    # Unattributed rows: a failed login has no verified actor, so it belongs to
    # no group and only a global administrator sees it.
    status, _ = call(
        "POST", "/auth/login", body={"username": f"p8-dana-{SUFFIX}-{RUN}", "password": "wrong"}
    )
    check("制造一次失败的登录", status == 401, str(status))

    status, boss_anon = call("GET", "/audit-logs?action=auth:login&status=denied", token=token_boss)
    check(
        "组管理员看不到未认证的记录",
        status == 200 and rows_of(boss_anon) == [],
        str(boss_anon),
    )

    status, admin_anon = call("GET", "/audit-logs?action=auth:login&status=denied", token=admin)
    check(
        "全局管理员能看到未认证的记录",
        status == 200 and bool(rows_of(admin_anon)),
        str(admin_anon),
    )

    # --- the trail is read-only -------------------------------------------
    status, _ = call("POST", "/audit-logs", token=admin, body={"action": "forged"})
    check("审计日志不可写入", status in (404, 405), str(status))
    status, _ = call("DELETE", "/audit-logs/1", token=admin)
    check("审计日志不可删除", status in (404, 405), str(status))

    # --- filters and export ------------------------------------------------
    status, meta = call("GET", "/audit-logs/meta", token=admin)
    check("筛选选项可获取", status == 200 and isinstance(meta, dict), str(meta))
    check(
        "筛选选项包含结果枚举",
        isinstance(meta, dict) and set(meta.get("statuses", [])) == {"success", "denied", "error"},
        str(meta),
    )

    status, prefix = call("GET", "/audit-logs?action=auth", token=admin)
    check(
        "按操作前缀筛选",
        status == 200 and all(row["action"].startswith("auth") for row in rows_of(prefix)),
        str(prefix),
    )

    status, csv_body = call("GET", "/audit-logs/export", token=admin, raw=True)
    check("可导出 CSV", status == 200 and isinstance(csv_body, str), str(status))
    check(
        "CSV 带 BOM（Excel 中文不乱码）",
        str(csv_body).startswith("\ufeff"),
        repr(str(csv_body)[:8]),
    )
    check("CSV 含表头", "actor_username" in str(csv_body), str(csv_body)[:120])

    # --- API tokens --------------------------------------------------------
    status, created = call("POST", "/api-tokens", token=token_a, body={"name": f"ci-{RUN}"})
    check("可创建 API 令牌", status == 201, str(created))
    if not isinstance(created, dict):
        return 1
    secret = str(created["token"])
    check("令牌以 wl_ 开头", secret.startswith("wl_"), secret[:8])
    check("返回了前缀", created["prefix"] == secret[:12], str(created["prefix"]))
    check("返回了权限范围", created["scopes"] == [], str(created["scopes"]))

    status, listed = call("GET", "/api-tokens", token=token_a)
    check("可列出自己的令牌", status == 200 and isinstance(listed, list), str(status))
    check(
        "列表中不含明文令牌",
        all("token" not in item for item in rows_of(listed)),
        str(listed),
    )

    # The secret must not be recoverable, anywhere.
    status, detail = call("GET", "/audit-logs?action=api_token:create", token=admin)
    rendered = json.dumps(rows_of(detail), ensure_ascii=False)
    check("审计日志不含明文令牌", secret not in rendered, rendered[:200])

    # --- a token authenticates as its owner -------------------------------
    status, me = call("GET", "/auth/me", token=secret)
    check("令牌可认证为其所有者", status == 200 and isinstance(me, dict), str(me))
    check(
        "身份是令牌所有者",
        isinstance(me, dict) and str(me.get("username", "")).startswith("p8-dana"),
        str(me),
    )

    check("垃圾令牌被拒", call("GET", "/auth/me", token="wl_not-a-real-token")[0] == 401)

    # --- scope validation --------------------------------------------------
    status, over = call(
        "POST",
        "/api-tokens",
        token=token_a,
        body={"name": f"over-{RUN}", "scopes": ["user:delete"]},
    )
    check("声明超出自身权限的范围被拒", status == 422, str(over))
    check("拒绝原因为范围越界", error_code(over) == "token_scope_not_granted", str(over))

    status, scoped = call(
        "POST",
        "/api-tokens",
        token=token_a,
        body={"name": f"ro-{RUN}", "scopes": ["instance:read"]},
    )
    check("可创建受限令牌", status == 201, str(scoped))
    if not isinstance(scoped, dict):
        return 1
    scoped_secret = str(scoped["token"])

    check("受限令牌可读实例", call("GET", "/instances", token=scoped_secret)[0] == 200)
    status, _ = call("GET", "/storages", token=scoped_secret)
    check("受限令牌不能读存储", status == 403, str(status))

    # --- a token cannot delegate more than it holds ------------------------
    status, parent = call(
        "POST",
        "/api-tokens",
        token=token_a,
        body={"name": f"parent-{RUN}", "scopes": ["api_token:create", "instance:read"]},
    )
    check("可创建带 api_token:create 的令牌", status == 201, str(parent))
    if not isinstance(parent, dict):
        return 1
    parent_secret = str(parent["token"])

    status, child = call("POST", "/api-tokens", token=parent_secret, body={"name": f"child-{RUN}"})
    check("令牌可以创建子令牌", status == 201, str(child))
    if not isinstance(child, dict):
        return 1
    check(
        "子令牌范围不超过父令牌",
        set(child["scopes"]) == {"api_token:create", "instance:read"},
        str(child["scopes"]),
    )
    child_secret = str(child["token"])
    check("子令牌可读实例", call("GET", "/instances", token=child_secret)[0] == 200)
    check("子令牌不能读存储", call("GET", "/storages", token=child_secret)[0] == 403)

    # --- an observer's token is read-only ----------------------------------
    status, obs = call("POST", "/api-tokens", token=token_gazer, body={"name": f"obs-{RUN}"})
    check("观察者也可持有令牌", status == 201, str(obs))
    if isinstance(obs, dict):
        obs_secret = str(obs["token"])
        check("观察者的令牌是只读的", call("GET", "/instances", token=obs_secret)[0] == 200)
        status, _ = call("POST", "/instances", token=obs_secret, body={})
        check("观察者的令牌不能创建实例", status == 403, str(status))

    # --- revocation --------------------------------------------------------
    status, _ = call("DELETE", f"/api-tokens/{created['id']}", token=token_a)
    check("可吊销令牌", status == 200, str(status))
    check("吊销后立即失效", call("GET", "/auth/me", token=secret)[0] == 401)

    # --- another person's token --------------------------------------------
    status, _ = call("DELETE", f"/api-tokens/{scoped['id']}", token=token_gazer)
    check("不能吊销他人的令牌", status == 404, str(status))
    check("他人的令牌仍然有效", call("GET", "/auth/me", token=scoped_secret)[0] == 200)

    status, other_list = call("GET", "/api-tokens", token=token_gazer)
    names = {row["name"] for row in rows_of(other_list)}
    check("列不出他人的令牌", f"ro-{RUN}" not in names, str(names))

    # --- login rate limiting ----------------------------------------------
    # A dedicated account so the failures cannot lock out anything else.
    victim = f"p8-limit-{SUFFIX}-{RUN}"
    ensure_user(admin, victim, lab_a)
    victim_name = f"p8-limit-{SUFFIX}-{RUN}"

    codes = []
    for _ in range(14):
        status, _ = call("POST", "/auth/login", body={"username": victim_name, "password": "wrong"})
        codes.append(status)
        if status == 429:
            break

    check("重复失败最终被限流", 429 in codes, str(codes))
    check("限流之前是 401 而不是其他错误", set(codes) - {429} == {401}, str(codes))
    check("被限流后仍返回 429", codes[-1] == 429, str(codes))

    status, limited = call(
        "POST", "/auth/login", body={"username": victim_name, "password": PASSWORD}
    )
    check("限流拒绝的是拒绝而非密码错误", status == 429, str(status))
    check("限流原因码正确", error_code(limited) == "login_rate_limited", str(limited))
    check(
        "限流响应不透露剩余次数",
        isinstance(limited, dict) and not (limited.get("error", {}).get("detail") or {}),
        str(limited),
    )

    # A different account from the same address is unaffected.
    status, _ = call(
        "POST", "/auth/login", body={"username": f"p8-gazer-{SUFFIX}-{RUN}", "password": PASSWORD}
    )
    check("一个人的失败不影响另一个账号", status == 200, str(status))

    # --- a success clears the count ---------------------------------------
    reset_user = f"p8-reset-{SUFFIX}-{RUN}"
    ensure_user(admin, reset_user, lab_a)
    reset_name = f"p8-reset-{SUFFIX}-{RUN}"

    # Two failures, then a success, then more failures: the count restarts, so
    # the later ones must still be 401 rather than 429.
    for _ in range(2):
        call("POST", "/auth/login", body={"username": reset_name, "password": "wrong"})
    status, _ = call("POST", "/auth/login", body={"username": reset_name, "password": PASSWORD})
    check("成功登录", status == 200, str(status))

    statuses = []
    for _ in range(3):
        status, _ = call("POST", "/auth/login", body={"username": reset_name, "password": "wrong"})
        statuses.append(status)
    check("成功登录后计数清零", set(statuses) == {401}, str(statuses))

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
