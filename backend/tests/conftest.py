"""Shared pytest fixtures.

Tests run against a real file-backed SQLite database (created per test under the
workspace scratch directory) rather than a mock. That means the whole stack — the
engine, the session factory the audit middleware uses, and dependency overrides —
is exercised exactly as it is in production.

The FastAPI app's settings and session dependencies are overridden so tests never
touch the developer's database.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth.permissions import sync_rbac
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import dispose_engine, get_session, init_engine
from app.main import create_app
from tests import factories


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def settings(db_url: str) -> Settings:
    """Isolated settings: a scratch SQLite file, no .env pickup, local auth only."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="development",
        debug=False,
        secret_key="test-secret-key-for-signing-with-enough-entropy",  # type: ignore[arg-type]
        database_url=db_url,
        auth_providers=["local"],
        local_admin_username="admin",
        local_admin_password="test-admin-password",  # type: ignore[arg-type]
        log_level="WARNING",
        ldap_uri="",
        k8s_in_cluster=False,
    )


@pytest.fixture
async def engine(settings: Settings) -> AsyncGenerator[AsyncEngine]:
    """A fresh schema per test."""
    # Import so every table is registered on Base.metadata.
    import app.models  # noqa: F401

    eng = create_async_engine(settings.database_url, connect_args={"check_same_thread": False})
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    async with session_factory() as db_session:
        yield db_session


@pytest.fixture
async def seeded(session: AsyncSession) -> AsyncSession:
    """Session with the RBAC catalogue seeded (roles/permissions/grants)."""
    await sync_rbac(session)
    await session.commit()
    return session


@pytest.fixture
async def admin(seeded: AsyncSession):
    """A global administrator with a local password."""
    user = await factories.create_user(
        seeded,
        username="admin",
        password="test-admin-password",
        role="global_admin",
        is_superuser=True,
    )
    # The HTTP layer runs on its own connection, so the row must be committed
    # before the login request can see it.
    await seeded.commit()
    return user


@pytest.fixture
def app(settings: Settings, engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]):
    """The FastAPI app wired to the test database.

    ``init_engine`` matters: the audit middleware opens its own session through
    the module-level factory, so that has to point at the scratch database too.
    """
    init_engine(settings)
    application = create_app(settings)

    application.dependency_overrides[get_settings] = lambda: settings

    async def _override_get_session() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as db_session:
            try:
                yield db_session
                await db_session.commit()
            except Exception:
                await db_session.rollback()
                raise

    application.dependency_overrides[get_session] = _override_get_session
    return application


@pytest.fixture
async def client(app) -> AsyncGenerator[AsyncClient]:
    """HTTP client with the cookie jar enabled, like a browser."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await dispose_engine()


@pytest.fixture
async def admin_client(client: AsyncClient, admin) -> AsyncClient:
    """A client already authenticated as the global administrator."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "test-admin-password"},
    )
    assert response.status_code == 200, response.text
    token = response.json()["accessToken"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.fixture
def audit_rows(session_factory: async_sessionmaker[AsyncSession]):
    """Read audit entries written during a request.

    The audit middleware commits on its own session, so tests must open a fresh
    one to observe what it wrote.
    """
    from sqlalchemy import select

    from app.models.audit import AuditLog

    async def _rows(*, action: str | None = None) -> list[AuditLog]:
        async with session_factory() as db_session:
            stmt = select(AuditLog).order_by(AuditLog.id)
            if action is not None:
                stmt = stmt.where(AuditLog.action == action)
            return list((await db_session.scalars(stmt)).all())

    return _rows


@pytest.fixture
def reload_from_db(session_factory: async_sessionmaker[AsyncSession]):
    """Re-read any entity in a brand new session (bypasses identity maps)."""

    async def _load(model: type, pk: int):
        async with session_factory() as db_session:
            return await db_session.get(model, pk)

    return _load
