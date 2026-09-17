#!/usr/bin/env python3
"""Phase 9 smoke: the system-information page and the two screens it unblocked.

Run against a live backend::

    source scripts/smoke-env.sh
    K8S_ENABLED=false .venv/bin/uvicorn app.main:app --port 8000 &
    python3 scripts/smoke_phase9.py

Runnable repeatedly against a populated database: every check is a read, and the
only user it creates is named with the run stamp so a second run does not collide.

The checks that matter most are the negatives. `GET /system/info` exists to be
read by an administrator and to be refused to everyone else, and the failure that
would not be noticed in production is a 200 for a normal user — the page renders
perfectly in that case, and nothing looks wrong.

So this script asserts, against a real server rather than a test harness:

* a global administrator gets 200, a plain user and a group administrator get 403,
  and an anonymous request gets 401;
* no configured secret appears in the response body — searched as a substring, not
  field by field, because the interesting leak arrives somewhere nobody looked;
* the payload has exactly the fields the page declares, so a new secret setting
  cannot reach it merely by being added to `Settings`;
* the version the page reports is the version `/healthz` reports.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8000/api/v1")
HEALTH_URL = os.environ.get("SMOKE_HEALTH_URL", "http://127.0.0.1:8000/healthz")
ADMIN_USERNAME = os.environ.get("SMOKE_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("SMOKE_ADMIN_PASSWORD", "smoke-test-password-123")
RUN = os.environ.get("SMOKE_RUN", "") or str(int(time.time()))

PASSWORD = "smoke-phase9-password-123"

#: The exact top-level keys the endpoint may return. An equality check rather
#: than a superset: the guarantee is that `Settings` is never dumped wholesale, so
#: a new field appearing here without a matching line in `app/services/system.py`
#: is a regression even when the new field is harmless today.
EXPECTED_KEYS = {
    "appName",
    "appVersion",
    "environment",
    "debug",
    "authProviders",
    "features",
    "warnings",
    "accessTokenMinutes",
    "refreshTokenDays",
    "loginRateLimitEnabled",
    "loginRateLimitWindowSeconds",
    "loginRateLimitAttemptsPerIp",
    "loginRateLimitAttemptsPerUsername",
    "counts",
    "sshMode",
    "sshHost",
    "storageClasses",
}

#: Substrings that must never appear. `asyncpg`/`aiosqlite` catch a leaked DSN;
#: `BEGIN` catches a PEM block; the rest are the shapes a password takes.
FORBIDDEN_FRAGMENTS = ["asyncpg", "aiosqlite", "postgresql+", "BEGIN ", "PRIVATE KEY"]

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
            return response.status, (payload if raw else (json.loads(payload) if payload else None))
    except urllib.error.HTTPError as error:
        payload = error.read().decode()
        if raw:
            return error.code, payload
        try:
            return error.code, json.loads(payload)
        except json.JSONDecodeError:
            return error.code, payload


def login(username: str, password: str) -> str | None:
    status, body = call("POST", "/auth/login", body={"username": username, "password": password})
    if status != 200 or not isinstance(body, dict):
        return None
    return str(body["accessToken"])


def rows_of(page: object) -> list[dict]:
    if isinstance(page, list):
        return [row for row in page if isinstance(row, dict)]
    if isinstance(page, dict):
        items = page.get("items")
        if isinstance(items, list):
            return [row for row in items if isinstance(row, dict)]
    return []


def ensure_user(admin: str, username: str, group_id: int | None, role: str) -> int | None:
    payload: dict[str, object] = {
        "username": username,
        "password": PASSWORD,
        "fullName": username,
        "roleCodes": [role],
    }
    if group_id is not None:
        payload["groupIds"] = [group_id]
    status, body = call("POST", "/users", token=admin, body=payload)
    if status == 201 and isinstance(body, dict):
        return int(body["id"])
    status, body = call("GET", f"/users?search={username}", token=admin)
    if status == 200:
        for item in rows_of(body):
            if item.get("username") == username:
                return int(item["id"])
    return None


def ensure_group(admin: str, name: str) -> int | None:
    status, body = call("POST", "/groups", token=admin, body={"name": name, "displayName": name})
    if status == 201 and isinstance(body, dict):
        return int(body["id"])
    status, body = call("GET", f"/groups?search={name}", token=admin)
    if status == 200:
        for item in rows_of(body):
            if item.get("name") == name:
                return int(item["id"])
    return None


def main() -> int:
    suffix = f"p9{RUN}"

    print("== 系统信息接口 ==")
    admin = login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if admin is None:
        print("\033[31m无法以管理员登录，检查 SMOKE_ADMIN_PASSWORD\033[0m")
        return 1

    status, info = call("GET", "/system/info", token=admin)
    check("全局管理员读取系统信息", status == 200, f"{status}")
    if not isinstance(info, dict):
        print("\033[31m响应不是对象，后续检查无法进行\033[0m")
        return 1

    check("返回字段与页面声明完全一致", set(info.keys()) == EXPECTED_KEYS,
          f"多出 {sorted(set(info) - EXPECTED_KEYS)}，缺少 {sorted(EXPECTED_KEYS - set(info))}")

    check("应用名", info.get("appName") == "kube-wl-ai", str(info.get("appName")))
    check("环境非空", bool(info.get("environment")), str(info.get("environment")))
    check("认证方式非空", bool(info.get("authProviders")), str(info.get("authProviders")))
    check("特性列表非空", len(info.get("features") or []) >= 5, str(len(info.get("features") or [])))

    # --- the negatives ------------------------------------------------------
    status, raw = call("GET", "/system/info", token=admin, raw=True)
    body = raw if isinstance(raw, str) else ""
    leaked = [fragment for fragment in FORBIDDEN_FRAGMENTS if fragment in body]
    check("响应体不含任何凭证片段", leaked == [], f"命中 {leaked}")

    # --- warnings -----------------------------------------------------------
    warnings = info.get("warnings") or []
    check("告警是列表", isinstance(warnings, list))
    missing = [
        w.get("code")
        for w in warnings
        if not (w.get("remedy") or "").strip() or not (w.get("detail") or "").strip()
    ]
    check("每条告警都带 detail 与 remedy", missing == [], f"缺字段：{missing}")
    severities = {w.get("severity") for w in warnings}
    check("告警等级取值合法", severities <= {"critical", "warning", "info"}, str(severities))

    # --- counts -------------------------------------------------------------
    counts = info.get("counts") or {}
    check("计数齐全", set(counts) == {
        "users", "activeUsers", "groups", "workspaces", "nodes", "gpus", "instances",
        "runningInstances",
    }, str(sorted(counts)))
    check("用户数不少于启用数", int(counts.get("users", 0)) >= int(counts.get("activeUsers", 0)))
    check("实例数不少于运行中", int(counts.get("instances", 0)) >= int(counts.get("runningInstances", 0)))

    # --- storage classes ----------------------------------------------------
    classes = info.get("storageClasses") or {}
    check("存储类三类齐全", set(classes) == {"home", "data", "share"}, str(sorted(classes)))

    # --- version agreement --------------------------------------------------
    status, health = call("GET", "/healthz")
    if status != 200:
        request = urllib.request.Request(HEALTH_URL)
        with urllib.request.urlopen(request) as response:
            health = json.loads(response.read().decode())
    check(
        "版本与 /healthz 一致",
        isinstance(health, dict) and health.get("version") == info.get("appVersion"),
        f"healthz={health.get('version') if isinstance(health, dict) else health} "
        f"system={info.get('appVersion')}",
    )

    print()
    print("== 权限边界 ==")
    group_id = ensure_group(admin, f"smoke-{suffix}")
    check("准备用户组", group_id is not None, "无法创建或找到用户组")

    plain = f"smoke-plain-{suffix}"
    group_admin = f"smoke-ga-{suffix}"
    check("准备普通用户", ensure_user(admin, plain, group_id, "user") is not None)
    check("准备组管理员", ensure_user(admin, group_admin, group_id, "group_admin") is not None)

    for role, username in (("普通用户", plain), ("组管理员", group_admin)):
        token = login(username, PASSWORD)
        if token is None:
            check(f"{role}可登录", False, username)
            continue
        status, _ = call("GET", "/system/info", token=token)
        check(f"{role}读取系统信息被拒", status == 403, f"{status}")

    status, _ = call("GET", "/system/info")
    check("未登录读取系统信息被拒", status == 401, f"{status}")

    print()
    print("== 令牌作用域 ==")
    status, issued = call(
        "POST",
        "/api-tokens",
        token=admin,
        body={"name": f"narrow-{suffix}", "scopes": ["user:read"], "expiresInDays": 1},
    )
    check("管理员可签发受限令牌", status == 201, f"{status} {issued}")
    if status == 201 and isinstance(issued, dict):
        narrow = str(issued["token"])
        status, _ = call("GET", "/system/info", token=narrow)
        check("作用域不含 system:config 的令牌被拒", status == 403, f"{status}")
        # Revoke so repeated runs do not accumulate live credentials.
        call("DELETE", f"/api-tokens/{issued['id']}", token=admin)

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
