"""Process-level settings (secrets and endpoints). Business behaviour lives in PlatformConfig, never here."""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["dev", "staging", "prod"] = "dev"

    llm_provider: Literal["openrouter", "openai"] = "openrouter"
    openrouter_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    # Process-wide hard caps for every LLM call made with the configured key (ModelGateway enforces them)
    llm_total_cap_usd: Decimal = Decimal("0.50")
    llm_daily_cap_usd: Decimal = Decimal("0.50")

    database_url: str = "postgresql://app_rw:app_rw@127.0.0.1:55432/agentplatform"          # app role, RLS enforced
    migration_database_url: str = "postgresql://agentplatform:agentplatform@127.0.0.1:55432/agentplatform"  # owner
    redis_url: str = "redis://:agentplatform@127.0.0.1:56379/0"

    kek_b64: SecretStr | None = None
    hash_pepper_b64: SecretStr | None = None

    meta_app_secret: SecretStr | None = None
    meta_verify_token: SecretStr | None = None
    meta_graph_version: str = "v26.0"
    meta_app_id: str | None = None

    whatsapp_token: SecretStr | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_app_secret: SecretStr | None = None

    smtp_url: SecretStr | None = None
    alert_email_from: str | None = None

    console_session_secret: SecretStr | None = None
    connect_webhook_secret: SecretStr | None = None   # HMAC secret configured in the Connect (Chatwoot) webhook
    spool_dir: str = "./var/spool"
    webhook_persist_timeout_s: float = 5         # database slower than this: spool and answer, the provider must not wait
    public_base_url: str = "http://127.0.0.1:8100"

    # Console hardening (empty = 2FA optional; prod suggestion: "platform_admin,tenant_owner")
    console_totp_required_roles: str = ""
    console_totp_issuer: str = "Agent Platform"
    console_login_max_failures: int = 5
    console_login_lock_minutes: int = 15
    console_import_max_bytes: int = 2_000_000
    # Facebook Login (reconnection of the Instagram page token)
    meta_oauth_scopes: str = ("instagram_basic,instagram_manage_messages,instagram_manage_comments,pages_show_list,"
                              "pages_read_engagement,pages_manage_metadata,business_management")
    meta_oauth_config_id: str | None = None      # Facebook Login for Business configuration (replaces the scope list)
    # Page fields of POST /{page}/subscribed_apps: the subscription is what turns on delivery of the Instagram fields chosen
    # in the App Dashboard (Instagram fields are not accepted here; Page "messages" needs pages_messaging)
    meta_page_subscribed_fields: str = "feed"
    # Instagram fields that must be on in the App Dashboard (webhooks): an operator alert says which are missing
    meta_app_required_fields: str = "messages,messaging_postbacks,comments,message_edit,messaging_referral,standby,messaging_handover"

    display_timezone: str = "America/Sao_Paulo"         # console shows times in this zone (storage is UTC)
    # Display currency for costs in the console (budgets and caps stay in USD, as billed by the provider)
    fx_source: Literal["ptax", "fixed"] = "ptax"        # ptax = Banco Central sell rate, refreshed every 6 h
    fx_fallback_usd_brl: Decimal = Decimal("5.00")      # used until PTAX answers, or always with fx_source=fixed

    # Observability: bearer token for GET /metrics (empty = endpoint disabled); OTLP endpoint enables tracing
    metrics_token: SecretStr | None = None
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "agentplatform"
    # Platform operator alerts (budget, stuck queue, no webhooks, invalid token...): e-mail and/or WhatsApp number
    ops_alert_email: str | None = None
    ops_alert_whatsapp: str | None = None
    # Outside watchdog: pinged every minute while the whole loop works; the outside service alerts when the pings stop
    heartbeat_url: SecretStr | None = None
    # Apps that send automatic messages in the account (echoes from them are not a person taking over), comma-separated in env
    automation_app_ids: Annotated[list[str], NoDecode] = []
    # Public legal pages required by the provider's review
    legal_controller_name: str | None = None
    legal_contact_email: str | None = None

    @field_validator("automation_app_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        return [x.strip() for x in v.split(",") if x.strip()] if isinstance(v, str) else v

    @model_validator(mode="after")
    def _production_requirements(self) -> "Settings":
        """Refuses to start, rather than running without something nobody would notice is missing."""
        if self.app_env != "prod":
            return self
        problems = []
        if self.llm_provider != "openrouter":
            problems.append("LLM_PROVIDER must be openrouter (zero data retention; the direct key is for synthetic data only)")
        if not (self.ops_alert_email or self.ops_alert_whatsapp):
            problems.append("OPS_ALERT_EMAIL or OPS_ALERT_WHATSAPP is required (otherwise operator alerts only reach a log)")
        missing = [name.upper() for name in ("console_session_secret", "kek_b64", "hash_pepper_b64", "meta_app_secret")
                   if not (getattr(self, name) and getattr(self, name).get_secret_value().strip())]
        if missing:      # the code is public: its development fallbacks are known to anyone, and an empty secret file would quietly use them
            problems.append(f"required in production: {', '.join(missing)}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def llm_api_key(self) -> SecretStr | None:
        return self.openai_api_key if self.llm_provider == "openai" else self.openrouter_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
