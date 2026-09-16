"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    """Isolated settings: in-memory SQLite, no .env pickup."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="development",
        secret_key="test-secret",  # type: ignore[arg-type]
        database_url="sqlite+aiosqlite:///:memory:",
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
async def client(app) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
