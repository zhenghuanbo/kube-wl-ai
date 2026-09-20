"""Application settings loaded from environment variables / .env file."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
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

    # Login rate limiting -----------------------------------------------------
    # Counted from the audit trail rather than from a separate counter store, so
    # the reason somebody is locked out is readable in the same table as
    # everything else that happened. See `app.auth.rate_limit`.
    login_rate_limit_enabled: bool = True
    #: Sliding window. Long enough that a password-guessing run is cut off,
    #: short enough that a person who mistyped a few times is not stranded.
    login_rate_limit_window_seconds: int = 900
    #: Per source address. Generous, because a whole lab can sit behind one NAT
    #: address and a tight per-IP limit would lock out a lecture theatre.
    login_rate_limit_attempts_per_ip: int = 30
    #: Per account. Tighter, because this is the limit that actually stops
    #: guessing against one name. Kept above the per-IP figure for a single
    #: user only by being a separate window, not a lower number.
    login_rate_limit_attempts_per_username: int = 10

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
    #: Master switch for talking to a real cluster.
    #:
    #: Off by default, and that default is load-bearing. ``kubeconfig`` merely
    #: *locates* credentials, so treating its presence as consent means that a
    #: developer machine with an ambient ``~/.kube/config`` silently provisions
    #: namespaces on whatever cluster that file points at — which is how a test
    #: run ends up mutating production. Production sets this (or
    #: ``K8S_IN_CLUSTER``) explicitly.
    k8s_enabled: bool = False
    k8s_managed_label: str = "wl-managed"
    k8s_namespace_prefix: str = "wl-"

    # --- Harbor --------------------------------------------------------------
    harbor_url: str = ""
    harbor_robot_name: str = ""
    harbor_robot_secret: SecretStr = SecretStr("")
    #: Disable only for a registry with a private CA during bring-up.
    harbor_verify_tls: bool = True

    # --- Monitoring ----------------------------------------------------------
    prometheus_url: str = ""
    #: Internal address, used by the backend to health-check Grafana. Optional —
    #: nothing on the request path needs it.
    grafana_url: str = ""
    #: Browser-facing address, and the switch that turns the monitoring panels
    #: on. Empty (the default) means "this deployment has no Grafana": the page
    #: still shows the platform's own inventory and says the time-series panels
    #: are unavailable, rather than offering a link that 404s. In the reference
    #: deployment this is the nginx-proxied ``/grafana``, so the iframe is
    #: same-origin and needs no third-party cookie exemption.
    grafana_public_url: str = ""
    grafana_datasource_uid: str = ""
    #: Grafana's `orgId`, echoed into the minted token.
    grafana_org_id: int = 1
    grafana_jwt_audience: str = "grafana"
    #: Lifetime of a minted monitoring token. Deliberately short: the URL carries
    #: the scope, so a leaked one is usable for minutes rather than days, and the
    #: frontend re-mints on demand. Grafana's own `token_ttl` must be >= this.
    grafana_jwt_ttl_seconds: int = 300

    @property
    def grafana_enabled(self) -> bool:
        return bool(self.grafana_public_url)

    # --- SSH -----------------------------------------------------------------
    ssh_mode: Literal["nodeport"] = "nodeport"
    ssh_nodeport_range: str = "30000-32767"

    # --- Storage -------------------------------------------------------------
    storage_class_home: str = ""
    storage_class_data: str = ""
    storage_class_share: str = ""

    # --- Instances -----------------------------------------------------------
    #: Host the per-instance IDE ingress rules are published under. Empty means
    #: "match any host", which is what a single-ingress-controller lab wants.
    instance_ingress_host: str = ""
    #: Address users are told to SSH to. Defaults to the node's own hostname
    #: when empty, which is only right for single-node clusters; production must
    #: set it to the cluster's external address.
    instance_ssh_host: str = ""
    #: Default home directory size for an instance whose pool sets no storage
    #: allowance of its own.
    instance_default_storage_gb: int = 10
    #: How long the short-lived ticket that protects an IDE URL stays valid.
    #: Long enough for a websocket to be established, short enough that a
    #: leaked URL is useless.
    ide_ticket_seconds: int = 120

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

    @model_validator(mode="after")
    def _guard_production_secrets(self) -> Settings:
        """Refuse obviously unsafe configuration in production.

        Failing fast at startup is far better than silently signing sessions with
        a placeholder key or leaving the break-glass password at its shipped
        default.
        """
        if not self.is_production:
            return self

        problems: list[str] = []

        secret = self.secret_key.get_secret_value()
        if len(secret) < 32 or secret in {"change-me", "replace-with-a-long-random-string"}:
            problems.append("SECRET_KEY must be a random string of at least 32 characters")

        if "local" in self.auth_providers:
            password = self.local_admin_password.get_secret_value()
            if password in {"", "change-me", "replace-me"}:
                problems.append(
                    "LOCAL_ADMIN_PASSWORD is still the default; set a strong password "
                    "or remove 'local' from AUTH_PROVIDERS"
                )

        if not self.cors_origins:
            problems.append("CORS_ORIGINS must list the allowed web origins in production")

        if "ldap" in self.auth_providers:
            # Without a service account the attribute read falls back to an
            # anonymous bind, which usually *succeeds* and then returns nothing:
            # every directory user logs in with no email, no full name and no
            # groups, holding only the default role. Nothing raises, so the only
            # symptom is people mysteriously missing their lab access. Refuse to
            # start instead of degrading quietly.
            if not self.ldap_uri or not self.ldap_base_dn:
                problems.append(
                    "LDAP_URI and LDAP_BASE_DN are required when 'ldap' is in AUTH_PROVIDERS"
                )
            if not self.ldap_bind_dn or not self.ldap_bind_password.get_secret_value():
                problems.append(
                    "LDAP_BIND_DN and LDAP_BIND_PASSWORD are required when 'ldap' is in "
                    "AUTH_PROVIDERS; without them group membership cannot be read"
                )

        if problems:
            raise ValueError("invalid production configuration: " + "; ".join(problems))

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
