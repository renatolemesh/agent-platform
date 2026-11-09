"""Process runtime: builds per-tenant dependencies (config, package, gateway, knowledge, adapters, dispatcher)."""
from __future__ import annotations

import contextvars
import logging
import time
from dataclasses import dataclass
from uuid import UUID

from agentplatform.core.agent.prompts import config_prompt_versions
from agentplatform.core.agent.prompts import preload as preload_prompts
from agentplatform.core.agent.turn import TurnDeps, TurnRunner
from agentplatform.core.channels.instagram.adapter import InstagramGraphAdapter
from agentplatform.core.channels.ports import ChannelAdapter
from agentplatform.core.config.registry import ConfigError, load_yaml, registry, resolve_with_package
from agentplatform.core.config.schema import PlatformConfig
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.handoff.notifiers import (
    EmailNotifier,
    FallbackInbox,
    LogNotifier,
    WhatsAppCloudNotifier,
    WhatsAppGatewayNotifier,
)
from agentplatform.core.knowledge.service import KnowledgeService
from agentplatform.core.models.gateway import ModelGateway, get_gateway
from agentplatform.core.outbox.dispatcher import Dispatcher, DispatchPolicy
from agentplatform.core.packages.contract import SegmentPackage, load_package
from agentplatform.paths import ROOT
from agentplatform.settings import get_settings

log = logging.getLogger(__name__)


@dataclass
class TenantRuntime:
    tenant_id: UUID
    config: PlatformConfig
    config_version_id: UUID | None
    package: SegmentPackage
    knowledge: KnowledgeService | None

    @property
    def allowed_urls(self) -> list[str]:
        return [*self.package.manifest.allowed_urls, *self.config.conversation.allowed_urls]

    def policy(self) -> DispatchPolicy:
        q = self.config.sending.quiet_hours
        return DispatchPolicy(timezone=q.timezone, quiet_start=q.start, quiet_end=q.end,
                              holidays_national=self.config.sending.holidays_national,
                              holidays_local=tuple(self.config.sending.holidays_local),
                              live=self.config.environment != "prod" or self.config.operations.feature_flags.get("go_live", False),
                              bubble_delay_s=self.config.conversation.bubble_delay_ms / 1000)


class Runtime:
    def __init__(self, adapters: dict[str, ChannelAdapter] | None = None, gateway: ModelGateway | None = None,
                 inbox=None):
        s = get_settings()
        self.settings = s
        self.adapters = adapters or self._build_adapters()
        self._gateway = gateway
        self._tenants: dict[str, tuple[float, TenantRuntime]] = {}
        self.inbox = inbox or self._build_inbox()
        self.ops = self._build_ops_notifier()
        self.dispatcher = Dispatcher(self.adapters, limiter=self._build_limiter(), policy_for=self._policy_for,
                                     on_failure=self._on_send_failure)

    @property
    def gateway(self) -> ModelGateway | None:
        if self._gateway is None:
            try:
                self._gateway = get_gateway()
            except Exception as e:
                log.warning("no model gateway: %s", e)
        return self._gateway

    def _build_adapters(self) -> dict:
        s = self.settings
        out: dict = {"instagram": InstagramGraphAdapter(s.meta_app_secret.get_secret_value() if s.meta_app_secret else None,
                                                        s.meta_graph_version)}
        if s.whatsapp_app_secret:                  # phase 4 channel (same Meta app secret family)
            from agentplatform.core.channels.whatsapp.adapter import WhatsAppCloudAdapter
            out["whatsapp"] = WhatsAppCloudAdapter(s.whatsapp_app_secret.get_secret_value(), s.meta_graph_version)
        from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
        out["simulator"] = FakeChannelAdapter(channel="simulator")   # console simulator: sends go nowhere, only recorded
        if s.console_session_secret:               # phase 5 webchat (session tokens signed with a server secret)
            from agentplatform.core.channels.webchat.adapter import WebchatAdapter
            out["webchat"] = WebchatAdapter(("webchat:" + s.console_session_secret.get_secret_value()).encode())
        return out

    def _build_limiter(self):
        """Shared token buckets in Redis (all workers); falls back to a conservative local limiter if Redis is down."""
        try:
            import redis.asyncio as aioredis

            from agentplatform.core.outbox.ratelimit import RedisRateLimiter
            return RedisRateLimiter(aioredis.from_url(self.settings.redis_url, socket_timeout=1))
        except Exception:
            return None

    def _build_ops_notifier(self):
        from agentplatform.core.ops.observability import OpsNotifier
        s = self.settings
        secret = (s.console_session_secret.get_secret_value() if s.console_session_secret else "dev").encode()
        email = (EmailNotifier(s.smtp_url.get_secret_value(), s.alert_email_from, secret, s.public_base_url)
                 if s.smtp_url and s.alert_email_from else None)
        return OpsNotifier(email, s.ops_alert_email, self._whatsapp_alerts(secret), s.ops_alert_whatsapp)

    def _whatsapp_alerts(self, secret: bytes):
        """The official Cloud API, or a text gateway (uazapi, Evolution API, Evolution Go); None when not configured."""
        s = self.settings
        if s.whatsapp_alert_provider == "cloud":
            return (WhatsAppCloudNotifier(s.whatsapp_token.get_secret_value(), s.whatsapp_phone_number_id, secret,
                                          graph_version=s.meta_graph_version)
                    if s.whatsapp_token and s.whatsapp_phone_number_id else None)
        if s.whatsapp_alert_url and s.whatsapp_alert_token:
            return WhatsAppGatewayNotifier(s.whatsapp_alert_provider, s.whatsapp_alert_url, s.whatsapp_alert_token.get_secret_value(),
                                           secret, s.public_base_url, instance=s.whatsapp_alert_instance)
        return None

    def _build_inbox(self):
        s = self.settings
        secret = (s.console_session_secret.get_secret_value() if s.console_session_secret else "dev").encode()
        chain = []
        wa = self._whatsapp_alerts(secret)
        if wa:
            chain.append(wa)
        if s.smtp_url and s.alert_email_from:
            chain.append(EmailNotifier(s.smtp_url.get_secret_value(), s.alert_email_from, secret, s.public_base_url))
        if s.app_env != "prod":        # a log line is not a seller being told: in production it must not count as delivered
            chain.append(LogNotifier())
        return FallbackInbox(chain)

    async def tenant(self, tenant_id: UUID) -> TenantRuntime:
        hit = self._tenants.get(str(tenant_id))
        if hit and time.monotonic() - hit[0] < 30:
            return hit[1]
        async with tenant_tx(tenant_id) as conn:
            t = await (await conn.execute("select slug, segment_package from tenants where id=%s", (tenant_id,))).fetchone()
            try:
                active = await registry.active(conn, tenant_id, self.settings.app_env)
                cfg, version_id = active.config, active.version_id
            except ConfigError:
                path = ROOT / "config" / "tenants" / f"{t['slug']}.yaml"
                if self.settings.app_env == "prod" or not path.exists():     # production never runs on a file nobody approved
                    raise
                cfg, version_id = load_yaml(path), None
            await preload_prompts(conn, config_prompt_versions(cfg))     # console-edited prompt versions (prompt lab)
        pkg = load_package(cfg.segment_package.name)
        cfg = resolve_with_package(cfg, pkg.manifest.compliance)      # idempotent for already-resolved docs
        knowledge = KnowledgeService(self.gateway, cfg.models.embedding, cfg.rag, pkg.manifest.name) if self.gateway else None
        tr = TenantRuntime(tenant_id, cfg, version_id, pkg, knowledge)
        self._tenants[str(tenant_id)] = (time.monotonic(), tr)
        return tr

    async def tenant_for_change(self, tenant_id: UUID) -> TenantRuntime:
        """What a person is about to change, or looks at to change it. Never a cached copy: a form opened right after an approval would carry the old
        values, and saving it would silently undo that approval."""
        self._tenants.pop(str(tenant_id), None)
        registry.forget(tenant_id)
        return await self.tenant_for_setup(tenant_id)

    async def tenant_for_setup(self, tenant_id: UUID) -> TenantRuntime:
        """Tables and material are prepared before the first activation, because the gate that allows it reads them. The newest proposed
        version gives the limits and the models. Never used for a turn, so nothing reaches a customer from here."""
        try:
            return await self.tenant(tenant_id)
        except ConfigError:
            async with tenant_tx(tenant_id) as conn:
                row = await (await conn.execute(
                    "select document from config_versions where tenant_id=%s and env=%s and status='draft' order by version desc limit 1",
                    (tenant_id, self.settings.app_env))).fetchone()
            if not row:
                raise
            cfg = PlatformConfig.model_validate(row["document"])
            pkg = load_package(cfg.segment_package.name)
            return TenantRuntime(tenant_id, resolve_with_package(cfg, pkg.manifest.compliance), None, pkg, None)

    async def get_checkpointer(self):
        """Encrypted AsyncPostgresSaver, created once per process (workers). Tests inject None."""
        if getattr(self, "_checkpointer", None) is None and getattr(self, "use_checkpointer", True):
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            from agentplatform.core.agent.checkpoint import AesGcmCipher, checkpoint_key
            pool = AsyncConnectionPool(self.settings.database_url, min_size=1, max_size=5, open=False,
                                       kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0})
            await pool.open()
            self._checkpointer = AsyncPostgresSaver(pool, serde=EncryptedSerializer(AesGcmCipher(checkpoint_key())))
        return getattr(self, "_checkpointer", None)

    async def runner(self, tenant_id: UUID, checkpointer=None, conversation_id: UUID | None = None) -> TurnRunner:
        """Turn runner with the config this conversation must use: the active version, or the canary version for the
        stable slice of conversations below canary_percent (registry.for_conversation). The turn records the version."""
        tr = await self.tenant(tenant_id)
        cfg, version_id, knowledge = tr.config, tr.config_version_id, tr.knowledge
        if conversation_id is not None and version_id is not None:
            async with tenant_tx(tenant_id) as conn:
                chosen = await registry.for_conversation(conn, tenant_id, self.settings.app_env, conversation_id)
                await preload_prompts(conn, config_prompt_versions(chosen.config))
            if chosen.version_id != version_id:
                cfg = resolve_with_package(chosen.config, tr.package.manifest.compliance)
                version_id = chosen.version_id
                knowledge = (KnowledgeService(self.gateway, cfg.models.embedding, cfg.rag, tr.package.manifest.name)
                             if self.gateway else None)
        return TurnRunner(TurnDeps(config=cfg, package=tr.package, gateway=self.gateway, knowledge=knowledge,
                                   config_version_id=version_id, dispatch_now=self.dispatcher.dispatch_conversation), checkpointer=checkpointer)

    async def _policy_for(self, tenant_id: UUID) -> DispatchPolicy:
        return (await self.tenant(tenant_id)).policy()

    async def _on_send_failure(self, tenant_id: UUID, conversation_id: UUID, reason: str) -> None:
        from agentplatform.core.handoff.service import create_handoff
        from agentplatform.core.orchestration.conversations import utcnow
        tr = await self.tenant(tenant_id)
        full_reason = f"send_failure:{reason}"[:60]
        async with tenant_tx(tenant_id) as conn:
            told = await (await conn.execute(      # several bubbles of one reply failing are one problem for the seller
                "select 1 from handoffs where conversation_id=%s and reason=%s and taken_at is null limit 1",
                (conversation_id, full_reason))).fetchone()
            if not told:
                await create_handoff(conn, tr.config, tenant_id=tenant_id, conversation_id=conversation_id, lead_id=None,
                                     reason=full_reason, summary={"first_name": None, "reason": reason}, now=utcnow())


_runtime: Runtime | None = None
# Scoped override (console simulator): jobs run in-process for one request see this runtime; everything else in the
# process (webhooks, other requests) keeps the global one.
_scoped: contextvars.ContextVar[Runtime | None] = contextvars.ContextVar("scoped_runtime", default=None)


def get_runtime() -> Runtime:
    global _runtime
    scoped = _scoped.get()
    if scoped is not None:
        return scoped
    if _runtime is None:
        _runtime = Runtime()
    return _runtime


def scoped_runtime(rt: Runtime) -> contextvars.Token:
    return _scoped.set(rt)


def reset_scoped_runtime(token: contextvars.Token) -> None:
    _scoped.reset(token)


def set_runtime(rt: Runtime) -> None:
    global _runtime
    _runtime = rt
