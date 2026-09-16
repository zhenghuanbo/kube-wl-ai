"""Operational CLI: ``python -m app.cli <command>``.

Only the commands an operator needs to bring a deployment up or recover it.
Everything else has an API endpoint.

The bootstrap command exists because the platform's break-glass local account
cannot be created through the API: with no users there is nobody allowed to call
``POST /users``. Every other account is either JIT-provisioned by the directory
or created by an administrator through the UI.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.auth.passwords import hash_password
from app.auth.permissions import sync_rbac
from app.core.config import get_settings
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.models.enums import UserSource
from app.models.rbac import Role, UserRole
from app.models.user import User

MIN_PASSWORD_LENGTH = 12


async def _bootstrap_admin(username: str, password: str, *, update_password: bool) -> int:
    session_factory = get_session_factory()
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.username == username))

        if user is None:
            user = User(
                username=username,
                full_name="平台管理员",
                source=UserSource.LOCAL,
                password_hash=hash_password(password),
                is_active=True,
                is_superuser=True,
            )
            session.add(user)
            await session.flush()
            action = f"已创建本地管理员 {username!r}"
        else:
            if user.source is not UserSource.LOCAL:
                print(
                    f"错误：{username!r} 是 {user.source.value} 账户，不能设置本地密码。",
                    file=sys.stderr,
                )
                return 1
            if update_password:
                user.password_hash = hash_password(password)
                action = f"已重置 {username!r} 的密码"
            else:
                action = f"账户 {username!r} 已存在，密码保持不变"
            user.is_superuser = True
            user.is_active = True

        role = await session.scalar(select(Role).where(Role.code == "global_admin"))
        if role is None:
            print("错误：RBAC 目录未初始化，请先执行 alembic upgrade head。", file=sys.stderr)
            return 1

        already_granted = await session.scalar(
            select(UserRole).where(
                UserRole.user_id == user.id,
                UserRole.role_id == role.id,
                UserRole.group_id.is_(None),
            )
        )
        if already_granted is None:
            session.add(UserRole(user_id=user.id, role_id=role.id, group_id=None))

        await session.commit()
        print(f"{action}，并已授予 global_admin 角色。")
    return 0


async def _sync_rbac() -> int:
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await sync_rbac(session)
        await session.commit()
        print(
            f"RBAC 同步完成：权限 {result.permissions_created} 新增 / "
            f"{result.permissions_updated} 更新，角色 {result.roles_created} 新增 / "
            f"{result.roles_updated} 更新，授权 {result.grants_added} 新增 / "
            f"{result.grants_removed} 移除。"
        )
    return 0


def _resolve_password(args: argparse.Namespace) -> str:
    """Prefer the configured secret, then a prompt — never a shell argument."""
    # argparse attributes are untyped; narrow once here rather than casting at
    # every use.
    supplied: str | None = args.password
    if supplied:
        return supplied

    configured: str = get_settings().local_admin_password.get_secret_value()
    if configured and configured != "change-me":
        return configured

    first = getpass.getpass("请输入管理员密码：")
    second = getpass.getpass("请再次输入：")
    if first != second:
        raise SystemExit("两次输入的密码不一致。")
    if len(first) < MIN_PASSWORD_LENGTH:
        raise SystemExit(f"密码至少需要 {MIN_PASSWORD_LENGTH} 位。")
    return first


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="app.cli", description="kube-wl-ai 运维命令")
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap = sub.add_parser("bootstrap-admin", help="创建或修复本地应急管理员账户")
    bootstrap.add_argument(
        "--username",
        default=None,
        help="默认取配置中的 LOCAL_ADMIN_USERNAME",
    )
    bootstrap.add_argument(
        "--password",
        default=None,
        help="不推荐：会留在 shell 历史中。默认读取配置或交互式输入。",
    )
    bootstrap.add_argument(
        "--reset-password",
        action="store_true",
        help="账户已存在时同时重置密码",
    )

    sub.add_parser("sync-rbac", help="把权限与角色目录同步到数据库（幂等）")
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    init_engine(settings)
    try:
        if args.command == "bootstrap-admin":
            username = args.username or settings.local_admin_username
            return await _bootstrap_admin(
                username, _resolve_password(args), update_password=args.reset_password
            )
        if args.command == "sync-rbac":
            return await _sync_rbac()
        raise SystemExit(f"未知命令：{args.command}")
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
