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
