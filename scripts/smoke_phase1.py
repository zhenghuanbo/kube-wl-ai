"""Phase 1 acceptance walk-through against a *running* server.

The pytest suite exercises the application in-process; this script talks to a
real uvicorn process over HTTP, in the same order the Vue frontend does. A green
run means the deployed contract — routing, middleware, cookies, serialisation —
actually holds, not just the internals.

Usage::

    python scripts/smoke_phase1.py [base_url]

Defaults to ``http://127.0.0.1:8000/api/v1``. Exits non-zero on any failure, so
it can gate a deployment.

It creates a group and two users, so run it against a scratch database — see
``scripts/smoke-env.sh`` for a ready-made one.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/api/v1"
# The break-glass administrator. Override for a real deployment rather than
# editing this file.
ADMIN_USERNAME = os.environ.get("SMOKE_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("SMOKE_ADMIN_PASSWORD", "smoke-test-password-123")

PASSED = 0
FAILED = 0


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    request = urllib.request.Request(f"{BASE}{path}", method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(request, data) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, (json.loads(raw) if raw else None)


def check(label: str, actual, expected) -> None:
    global PASSED, FAILED
    ok = actual == expected
    PASSED += ok
    FAILED += not ok
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}: {actual!r}" + ("" if ok else f" (期望 {expected!r})"))


def login(username: str, password: str):
    status, payload = call("POST", "/auth/login", body={"username": username, "password": password})
    return status, (payload or {}).get("accessToken"), payload


print("== 管理员会话 ==")
status, admin, _ = login(ADMIN_USERNAME, ADMIN_PASSWORD)
check("管理员登录", status, 200)

# --- 组与用户 ---------------------------------------------------------------
print("== 用户组与用户 ==")
status, group = call(
    "POST",
    "/groups",
    admin,
    {"name": "lab-b", "displayName": "机器人实验室", "kind": "lab"},
)
check("创建用户组", status, 201)
gid = group["id"]

status, alice = call(
    "POST",
    "/users",
    admin,
    {
        "username": "carol",
        "password": "carol-password-123",
        "fullName": "卡罗尔",
        "roleCodes": ["user"],
        "groupIds": [gid],
    },
)
check("创建用户", status, 201)
alice_id = alice["id"]
check("初始角色", alice["roles"], ["user"])
check("主用户组", [g["name"] for g in alice["groups"]], ["lab-b"])
check("主组标记", [g["isPrimary"] for g in alice["groups"]], [True])

status, members = call("GET", f"/groups/{gid}/members", admin)
check("成员列表", status, 200)
check("成员", [m["username"] for m in members["items"]], ["carol"])
# 通过 groupIds 加入是普通成员；只有建组时指定的 owner 会成为组管理员。
check("组内角色", members["items"][0]["roleInGroup"], "member")

status, _ = call("POST", f"/groups/{gid}/members", admin, {"userId": alice_id})
check("重复加成员被拒", status, 409)

# --- 角色授予 ---------------------------------------------------------------
print("== 角色与数据范围 ==")
status, updated = call(
    "POST",
    f"/users/{alice_id}/roles",
    admin,
    {"roleCode": "group_admin", "scope": "group", "groupId": gid},
)
check("授予组管理员", status, 200)
check(
    "角色分配含作用域",
    sorted((a["code"], a["scope"]) for a in updated["roleAssignments"]),
    [("group_admin", "group"), ("user", "global")],
)

_, token, _ = login("carol", "carol-password-123")
status, payload = call("GET", "/auth/permissions", token)
check("数据范围为组", payload["scope"]["kind"], "group")
check("可见组", payload["scope"]["groupIds"], [gid])
check("非全局管理员", payload["isGlobalAdmin"], False)

# What a group administrator may do, stated as structure rather than as a count.
#
# This used to be `len(permissions) == 45`, and the number had to be bumped in
# Phase 4, 5, 6 and 8. A magic number that every phase edits teaches the wrong
# reflex: the fastest way to green is to update the expectation, which is exactly
# the habit that lets an accidental widening through. These assertions say what
# has to stay true instead, and none of them needs touching when a permission is
# added on purpose.
group_perms = set(payload["permissions"])

_, root_token, _ = login(ADMIN_USERNAME, ADMIN_PASSWORD)
_, root_payload = call("GET", "/auth/permissions", root_token)
root_perms = set(root_payload["permissions"])

# The plain-user baseline comes from the role catalogue rather than from a
# second account. Creating a throwaway user to read a static mapping would add
# state to prove something the server already publishes.
_, user_role = call("GET", "/roles/user", root_token)
plain_perms = set(user_role["permissions"])

# Strictly between the two: a group admin can do more than a plain user...
# ...and strictly less than a global administrator. `subset` is checked with
# `<=` first so a failure names which direction broke.
check("组管理员 ⊋ 普通用户", group_perms > plain_perms, True)
check("组管理员 ⊆ 全局管理员", group_perms <= root_perms, True)
check("组管理员 ⊊ 全局管理员（不是等同）", group_perms < root_perms, True)

# The specific codes a group administrator must never hold: the global-only
# administration surface. `user:create` and `node:sync` are the two that would
# turn a lab administrator into a platform administrator.
#
# `audit:read` is deliberately **not** here. A group administrator holds it, and
# that is a design decision rather than an oversight: they may read the trail of
# their own group's members, which is what `DataScope` narrows to. Excluding it
# is the global administrator's ability to see *other* groups and unattributed
# rows, and that is what the Phase 8 tests pin down. Listing it as forbidden here
# would assert the opposite of the design.
FORBIDDEN_FOR_GROUP_ADMIN = {
    "user:create",
    "user:delete",
    "user:grant_role",
    "group:create",
    "group:delete",
    "node:sync",
    "resource_group:create",
    "resource_group:delete",
    "system:config",
}
check(
    "组管理员不含全局专属权限",
    sorted(group_perms & FORBIDDEN_FOR_GROUP_ADMIN),
    [],
)

status, visible = call("GET", "/users", token)
# carol 是 lab-b 唯一成员，看不到不在本组的 admin。
check("组管理员可见用户", sorted(u["username"] for u in visible["items"]), ["carol"])

# --- 越权 ---------------------------------------------------------------
print("== 越权防护 ==")
status, body = call(
    "POST", "/users", token, {"username": "mallory", "password": "mallory-password-1"}
)
check("组管理员不能建用户", status, 403)

status, _ = call("POST", f"/users/{alice_id}/roles", token, {"roleCode": "global_admin"})
check("不能自行提权", status, 403)

status, body = call("POST", "/groups", token, {"name": "rogue"})
check("组管理员不能建组", status, 403)
check("拒绝码", body["error"]["code"], "permission_denied")

status, body = call("GET", "/users/1", token)
check("跨租户读取返回 404", status, 404)

# --- 自身保护 ---------------------------------------------------------------
print("== 管理员自我保护 ==")
status, body = call("DELETE", "/users/1/roles/global_admin", admin)
check("不能撤销自己的全局管理员", status, 400)
check("错误码", body["error"]["code"], "cannot_revoke_own_admin")

status, _ = call("POST", "/users/1/deactivate", admin)
check("不能停用自己", status, 400)

status, body = call(
    "POST", "/users", admin, {"username": "carol", "password": "whatever-long-enough"}
)
check("用户名冲突", status, 409)
check("冲突码", body["error"]["code"], "username_taken")

# --- 停用 ---------------------------------------------------------------
print("== 账户状态 ==")
status, _ = call("PATCH", f"/users/{alice_id}/status", admin, {"isActive": False})
check("停用账户", status, 200)

status, _, body = login("carol", "carol-password-123")
check("停用后拒绝登录", status, 403)

# --- 审计 ---------------------------------------------------------------
print("== 审计 ==")
status, rows = call("GET", "/users?page=1&pageSize=1", admin)
check("列表仍可用", status, 200)

print(f"\n结果：{PASSED} 通过，{FAILED} 失败")
sys.exit(1 if FAILED else 0)
