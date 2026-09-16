"""Application settings loaded from environment variables / .env file."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration.

    Every value can be overridden through an environment variable of the same
    (upper-cased) name, or through a local ``.env`` file.
    See the project plan section 11.1 for the canonical variable list.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---------------------------------------------------------
    app_name: str = "kube-wl-ai"
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    secret_key: SecretStr = SecretStr("change-me")
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Logging -------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False

    # --- Database ------------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./app.db"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # --- Redis / Celery ------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # --- Authentication ------------------------------------------------------
    auth_providers: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["local"])

    # Local (break-glass) provider
    local_admin_username: str = "admin"
    local_admin_password: SecretStr = SecretStr("change-me")

    # JWT
    jwt_algorithm: str = "RS256"
    jwt_private_key: SecretStr = SecretStr("")
    jwt_public_key: SecretStr = SecretStr("")
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    jwt_issuer: str = "kube-wl-ai"

    # FreeIPA / LDAP
    ldap_uri: str = ""
    ldap_base_dn: str = ""
    ldap_user_dn_template: str = "uid={username},cn=users,cn=accounts,{base_dn}"
    ldap_group_base_dn: str = "cn=groups,cn=accounts,{base_dn}"
    ldap_bind_dn: str = ""
    ldap_bind_password: SecretStr = SecretStr("")
    ldap_ca_cert: str = ""
    ldap_observer_group: str = ""

    # OIDC
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_redirect_uri: str = ""

    # --- Kubernetes ----------------------------------------------------------
    k8s_in_cluster: bool = False
    kubeconfig: str = ""
    k8s_managed_label: str = "wl-managed"
    k8s_namespace_prefix: str = "wl-"

    # --- Harbor --------------------------------------------------------------
    harbor_url: str = ""
    harbor_robot_name: str = ""
    harbor_robot_secret: SecretStr = SecretStr("")

    # --- Monitoring ----------------------------------------------------------
    prometheus_url: str = ""
    grafana_url: str = ""
    grafana_datasource_uid: str = ""

    # --- SSH -----------------------------------------------------------------
    ssh_mode: Literal["nodeport"] = "nodeport"
    ssh_nodeport_range: str = "30000-32767"

    # --- Storage -------------------------------------------------------------
    storage_class_home: str = ""
    storage_class_data: str = ""
    storage_class_share: str = ""

    # --- Validators ----------------------------------------------------------
    @field_validator("cors_origins", "auth_providers", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept either a comma-separated string or a JSON list."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON list: {exc}") from exc
                if not isinstance(parsed, list):
                    raise ValueError("expected a JSON list")
                return parsed
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    # --- Helpers -------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
