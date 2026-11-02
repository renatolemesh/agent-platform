"""Console simulator (agent calibration): chat with the real agent through the real pipeline, on a dedicated
`simulator` channel account, with the active config or any draft/canary version. Spends real LLM budget (it is the
real model), but:
- nothing reaches a customer (the simulator channel only records sends) and no seller is notified (handoff alerts
  skip this channel; the simulation runtime uses a log-only inbox);
- simulated leads stay out of the inbox and the business dashboards (migration 0006 views);
- a draft is tested without activating it: the runtime is scoped to this request (runtime.scoped_runtime).
- a table (package dataset) draft is used here before it is activated (datasets.PREFER_DRAFTS).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from uuid import UUID

from agentplatform.core.agent.prompts import config_prompt_versions
from agentplatform.core.agent.prompts import preload as preload_prompts
from agentplatform.core.channels.events import CanonicalEvent
from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
from agentplatform.core.config.registry import ConfigError, resolve_with_package
from agentplatform.core.config.schema import PlatformConfig
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier
from agentplatform.core.knowledge.datasets import PREFER_DRAFTS
from agentplatform.core.ops.turn_explain import explain_turns
from agentplatform.core.orchestration.ingress import persist_events
from agentplatform.runtime import Runtime, TenantRuntime, get_runtime, reset_scoped_runtime, scoped_runtime

CHANNEL = "simulator"
SESSION_PREFIX = "sim-"
MAX_REPLAY_MESSAGES = 12
# The real debounce window waits for more customer messages; in the simulator each message is one batch.
SIM_MAX_DELAY_S = 0.2       # do not wait for scheduled background jobs (e.g. the rolling summary, 5 s) inside a request
SIM_DEBOUNCE = {"debounce_window_s": 0.1, "debounce_max_wait_s": 0.5, "echo_grace_s": 0.1}


def new_session() -> str:
    return f"{SESSION_PREFIX}{uuid.uuid4().hex[:8]}"


def valid_session(s: str | None) -> bool:
    return bool(s) and s.startswith(SESSION_PREFIX) and len(s) <= 40 and s[len(SESSION_PREFIX):].isalnum()


async def ensure_account(tenant_id: UUID) -> str:
    """The tenant's simulator channel account (created on first use)."""
    ext = f"sim-{tenant_id.hex[:12]}"
    async with tenant_tx(tenant_id) as conn:
        row = await (await conn.execute(
            "select external_account_id from channel_accounts where tenant_id=%s and channel=%s", (tenant_id, CHANNEL))).fetchone()
        if row:
            return row["external_account_id"]
        await conn.execute(
            "insert into channel_accounts (tenant_id, channel, external_account_id, external_page_id) values (%s,%s,%s,%s)",
            (tenant_id, CHANNEL, ext, ext))
    return ext


async def versions(tenant_id: UUID, env: str) -> list[dict]:
    async with tenant_tx(tenant_id) as conn:
        return await (await conn.execute(
            """select id, version, status, created_at from config_versions where tenant_id=%s and env=%s
               and status in ('active','canary','draft') order by version desc limit 20""", (tenant_id, env))).fetchall()


class _SimRuntime(Runtime):
    """Fixed config version for every conversation (no canary resolution)."""

    async def runner(self, tenant_id: UUID, checkpointer=None, conversation_id: UUID | None = None):
        from agentplatform.core.agent.turn import TurnDeps, TurnRunner
        tr = await self.tenant(tenant_id)
        return TurnRunner(TurnDeps(config=tr.config, package=tr.package, gateway=self.gateway, knowledge=tr.knowledge,
                                   config_version_id=tr.config_version_id), checkpointer=checkpointer)


async def _runtime(tenant_id: UUID, version_id: UUID | None) -> tuple[_SimRuntime, FakeChannelAdapter]:
    base = await get_runtime().tenant(tenant_id)
    cfg, vid = base.config, base.config_version_id
    if version_id is not None and version_id != base.config_version_id:
        async with tenant_tx(tenant_id) as conn:
            row = await (await conn.execute(
                "select id, document from config_versions where id=%s and tenant_id=%s", (version_id, tenant_id))).fetchone()
        if not row:
            raise ConfigError("versão de configuração não encontrada")
        cfg = resolve_with_package(PlatformConfig.model_validate(row["document"]), base.package.manifest.compliance)
        vid = row["id"]
        async with tenant_tx(tenant_id) as conn:
            await preload_prompts(conn, config_prompt_versions(cfg))
    cfg = cfg.model_copy(update={"concurrency": cfg.concurrency.model_copy(update=SIM_DEBOUNCE),
                                 "conversation": cfg.conversation.model_copy(update={"bubble_delay_ms": 0})})
    adapter = FakeChannelAdapter(channel=CHANNEL)
    rt = _SimRuntime(adapters={CHANNEL: adapter}, gateway=get_runtime().gateway, inbox=FallbackInbox([LogNotifier()]))
    from agentplatform.core.knowledge.service import KnowledgeService
    knowledge = KnowledgeService(rt.gateway, cfg.models.embedding, cfg.rag, base.package.manifest.name) if rt.gateway else None
    rt._tenants[str(tenant_id)] = (float("inf"), TenantRuntime(tenant_id, cfg, vid, base.package, knowledge))
    rt.use_checkpointer = False
    return rt, adapter


async def send(tenant_id: UUID, session: str, texts: list[str], version_id: UUID | None) -> list[str]:
    """Each text is one customer message (one batch -> one turn). Returns the agent's replies."""
    from agentplatform.jobs.pump import pump
    account = await ensure_account(tenant_id)
    rt, adapter = await _runtime(tenant_id, version_id)
    token = scoped_runtime(rt)
    drafts = PREFER_DRAFTS.set(True)          # tables (package datasets) are tested here before they are activated
    try:
        for text in texts:
            ev = CanonicalEvent(event_type="MessageReceived", channel=CHANNEL, account_external_id=account,
                                contact_external_id=session, idempotency_key=f"sim:{uuid.uuid4().hex}", mid=uuid.uuid4().hex,
                                provider_ts=datetime.now(timezone.utc), text=text)
            created = await persist_events([ev])
            await pump([("ingress", {"tenant_id": str(t), "event_id": e}, {}) for t, e in created], honor_delays=True,
                       max_delay=SIM_MAX_DELAY_S)
    finally:
        PREFER_DRAFTS.reset(drafts)
        reset_scoped_runtime(token)
    return [r.msg.text for r in adapter.sent if r.msg.text]


async def customer_messages(tenant_id: UUID, session: str) -> list[str]:
    async with tenant_tx(tenant_id) as conn:
        rows = await (await conn.execute(
            """select m.text_masked from messages m join conversations c on c.id = m.conversation_id
               join contact_identities ci on ci.contact_id = c.contact_id and ci.channel = %s
               where ci.external_user_id = %s and m.direction = 'in' order by m.provider_ts""", (CHANNEL, session))).fetchall()
    return [r["text_masked"] for r in rows if r["text_masked"]][:MAX_REPLAY_MESSAGES]


async def snapshot(tenant_id: UUID, session: str) -> dict:
    """Everything the calibration screen shows for a session: transcript, lead state and the last turn explained."""
    async with tenant_tx(tenant_id) as conn:
        conv = await (await conn.execute(
            """select c.id, c.ownership, l.stage, l.profile,
                      (select coalesce(sum(cost_usd), 0) from turns t where t.conversation_id = c.id) as cost_usd
               from conversations c join contact_identities ci on ci.contact_id = c.contact_id and ci.channel = %s
               join leads l on l.contact_id = c.contact_id where ci.external_user_id = %s""", (CHANNEL, session))).fetchone()
        if not conv:
            return {"conv": None, "msgs": [], "turn": None, "guards": [], "chunks": [], "handoff": None}
        msgs = await (await conn.execute(
            "select direction, text_masked, provider_ts from messages where conversation_id=%s order by provider_ts",
            (conv["id"],))).fetchall()
        turn = await (await conn.execute(
            """select t.id, t.status, t.stage_before, t.stage_after, t.llm_calls, t.cost_usd, t.trace, t.started_at,
                      v.version as config_version
               from turns t left join config_versions v on v.id = t.config_version_id
               where t.conversation_id=%s order by t.started_at desc limit 1""", (conv["id"],))).fetchone()
        guards, chunks, explained = [], [], None
        if turn:
            guards = await (await conn.execute(
                "select guard, rule_id, verdict, detail from guard_results where turn_id=%s and verdict <> 'pass' order by id",
                (turn["id"],))).fetchall()
            ids = [UUID(i) for i in ((turn["trace"] or {}).get("retrieval_ids") or [])]
            if ids:
                chunks = await (await conn.execute(
                    """select d.file_name, k.page, left(k.text, 220) as excerpt from kb_chunks k
                       join kb_documents d on d.id = k.document_id where k.id = any(%s)""", (ids,))).fetchall()
            explained = (await explain_turns(conn, [turn]))[turn["id"]]
        handoff = await (await conn.execute(
            "select reason, created_at from handoffs where conversation_id=%s and not is_notice order by created_at desc limit 1",
            (conv["id"],))).fetchone()
    return {"conv": conv, "msgs": msgs, "turn": turn, "guards": guards, "chunks": chunks, "handoff": handoff, "explained": explained}


async def recent_sessions(tenant_id: UUID, limit: int = 8) -> list[dict]:
    async with tenant_tx(tenant_id) as conn:
        return await (await conn.execute(
            """select ci.external_user_id as session, l.stage, c.last_inbound_at,
                      (select count(*) from messages m where m.conversation_id = c.id and m.direction = 'in') as turns
               from conversations c join contact_identities ci on ci.contact_id = c.contact_id and ci.channel = %s
               join leads l on l.contact_id = c.contact_id where ci.external_user_id like 'sim-%%'
               order by c.last_inbound_at desc nulls last limit %s""",
            (CHANNEL, limit))).fetchall()
