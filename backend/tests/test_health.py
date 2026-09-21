"""Smoke tests for the Phase 0 application skeleton."""

from __future__ import annotations

from httpx import AsyncClient

from app.core.config import Settings


async def test_healthz(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["app_env"] == "development"
    assert "version" in body


async def test_openapi_is_served(client: AsyncClient) -> None:
    response = await client.get("/api/v1/openapi.json")
    assert response.status_code == 200

    schema = response.json()
    assert schema["info"]["title"] == "kube-wl-ai API"
    assert "/healthz" in schema["paths"]


async def test_unknown_route_returns_error_envelope(client: AsyncClient) -> None:
    response = await client.get("/definitely-not-a-route")
    assert response.status_code == 404

    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "not_found"
    assert "message" in body["error"]
    assert "detail" in body["error"]


def test_settings_parses_comma_separated_lists(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "http://a.test, http://b.test")
    monkeypatch.setenv("AUTH_PROVIDERS", "local,ldap")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.cors_origins == ["http://a.test", "http://b.test"]
    assert settings.auth_providers == ["local", "ldap"]


def test_settings_accepts_json_lists(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", '["http://json.test"]')
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.cors_origins == ["http://json.test"]


# --- readiness covers the schema, not just connectivity ------------------------
def test_the_expected_revision_is_the_code_head() -> None:
    """The check must read the real migration head, or it would bless anything."""
    from app.db.schema import expected_revision

    expected_revision.cache_clear()
    head = expected_revision()

    assert head is not None
    assert len(head) == 12


async def test_an_up_to_date_database_is_ok(client: AsyncClient) -> None:
    """A database `create_all`-ed from the models has no `alembic_version` table.

    That is "unversioned", not "ok" — see the next test — so this one stamps the
    table to prove the happy path reports `ok` rather than merely not-erroring.
    """
    from sqlalchemy import text

    from app.db.schema import check_schema, expected_revision
    from app.db.session import get_engine

    head = expected_revision()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32))")
        )
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": head})
        assert await check_schema(conn) == "ok"

    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"]["schema"] == "ok"


async def test_a_lagging_database_fails_readiness_and_names_the_fix(
    client: AsyncClient,
) -> None:
    """The failure this exists for: a schema behind the models answers `SELECT 1`.

    Without the revision check `/readyz` returned 200 and the first query touching
    a new column died inside the ORM's eager loading with `no such column`,
    surfacing as a 500 with a 200-line traceback instead of "run alembic upgrade
    head".
    """
    from sqlalchemy import text

    from app.db.session import get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32))")
        )
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(text("INSERT INTO alembic_version VALUES ('3f0dbd1b4617')"))

    response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == "ok"
    assert "alembic upgrade head" in body["checks"]["schema"]
    assert "3f0dbd1b4617" in body["checks"]["schema"]


async def test_an_unversioned_database_is_reported_as_such(client: AsyncClient) -> None:
    """An unstamped database has run no migration — worth saying plainly."""
    from sqlalchemy import text

    from app.db.session import get_engine

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert "unversioned" in response.json()["checks"]["schema"]


async def test_an_unknown_head_does_not_fail_readiness(client: AsyncClient, monkeypatch) -> None:
    """A build that cannot see its migration scripts must not be judged unhealthy.

    That is the "unknown", not "mismatch" case: an image shipping without the
    scripts, or a deployment that migrates out of band, cannot know what the
    database should be on — so it reports what it sees instead of inventing a
    failure.
    """
    from app.db import schema

    monkeypatch.setattr(schema, "expected_revision", lambda: None)

    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["checks"]["schema"].startswith("unknown")
