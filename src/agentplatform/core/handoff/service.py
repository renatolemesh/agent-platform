"""Handoff (plan 05 §6): reason + summary without sensitive data; off-hours keeps the bot serving (review #8)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection

from agentplatform.core.config.schema import PlatformConfig
from agentplatform.core.orchestration.conversations import transition_ownership
from agentplatform.core.security.pii import mask


def in_human_hours(cfg: PlatformConfig, now: datetime) -> bool:
    q = cfg.sending.quiet_hours
    local = now.astimezone(ZoneInfo(q.timezone)).time()
    return not (local >= q.start or local < q.end)


def next_open(cfg: PlatformConfig, now: datetime) -> datetime:
    q = cfg.sending.quiet_hours
    local = now.astimezone(ZoneInfo(q.timezone))
    cand = local.replace(hour=q.end.hour, minute=q.end.minute, second=0, microsecond=0)
    if cand <= local:
        cand += timedelta(days=1)
    return cand.astimezone(now.tzinfo)


def build_summary(first_name: str | None, stage: str, reason: str, profile: dict, last_customer_text: str | None) -> dict:
    safe_profile = {k: v.get("value") for k, v in profile.items() if isinstance(v, dict)}
    return {"first_name": first_name, "stage": stage, "reason": reason, "profile": safe_profile,
            "last_message": mask(last_customer_text or "").text[:300]}


async def create_notice(conn: AsyncConnection, cfg: PlatformConfig, *, tenant_id: UUID, conversation_id: UUID,
                        lead_id: UUID | None, reason: str, summary: dict, now: datetime) -> UUID | None:
    """Tells the seller something while the conversation stays where it is (a question without an approved answer, someone
    who asked not to be contacted and wrote again). Never repeats within notice_dedupe_hours, and never counts as an open handoff."""
    seen = await (await conn.execute(
        "select 1 from handoffs where conversation_id=%s and reason=%s and created_at > %s limit 1",
        (conversation_id, reason, now - timedelta(hours=cfg.handoff.notice_dedupe_hours)))).fetchone()
    if seen:
        return None
    off_hours = not in_human_hours(cfg, now) and cfg.handoff.notify_off_hours == "at_open"
    notify_at = next_open(cfg, now) if off_hours else now
    row = await (await conn.execute(
        """insert into handoffs (tenant_id, conversation_id, lead_id, reason, summary, notify_at, sla_due_at, is_notice, taken_at)
           values (%s,%s,%s,%s,%s,%s,%s,true,%s) returning id""",
        (tenant_id, conversation_id, lead_id, reason, json.dumps(summary), notify_at, notify_at, now))).fetchone()
    return row["id"]


# The turn could not produce an answer. Any other handoff reason means the turn did its job: running it again would only hand off again.
_NOT_ANSWERED_BY_FAILURE = ["technical_failure", "budget_exhausted", "conversation_budget", "llm_call_cap", "guard_blocked"]


async def reopen_unanswered(conn: AsyncConnection, conversation_id: UUID) -> int:
    """The customer's messages after the last real reply go back to pending, for the assistant to answer them. A notice such as
    "I'll call the seller" did not answer anything, so it does not count as a reply. A message whose turn decided a handoff stays
    consumed, unless that handoff was a failure: "quero falar com um vendedor" would just call the seller again."""
    cur = await conn.execute(
        """update messages set turn_id = null where conversation_id = %(c)s and direction = 'in' and turn_id is not null
             and kind not in ('comment', 'shared', 'deleted')
             and provider_ts > coalesce((select max(provider_ts) from messages where conversation_id = %(c)s
                                         and direction in ('out_bot', 'out_human') and kind <> 'handoff_notice'), 'epoch')
             and not exists (select 1 from turns t where t.id = messages.turn_id and t.trace->>'handoff' is not null
                             and t.trace->>'handoff' <> all(%(failures)s))""",
        {"c": conversation_id, "failures": _NOT_ANSWERED_BY_FAILURE})
    return cur.rowcount


async def create_handoff(conn: AsyncConnection, cfg: PlatformConfig, *, tenant_id: UUID, conversation_id: UUID,
                         lead_id: UUID | None, reason: str, summary: dict, now: datetime) -> dict:
    """Returns {"handoff_id", "mode": "awaiting"|"off_hours", "notify_at"}; ownership/epochs updated accordingly."""
    off_hours = not in_human_hours(cfg, now) and cfg.handoff.off_hours_mode == "bot_keeps_serving"
    urgent = reason in ("complaint_or_abuse", "legal", "requires_sensitive_data")
    notify_at = now if (not off_hours or cfg.handoff.notify_off_hours == "immediately") else next_open(cfg, now)
    row = await (await conn.execute(
        """insert into handoffs (tenant_id, conversation_id, lead_id, reason, priority, summary, notify_at, sla_due_at)
           values (%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
        (tenant_id, conversation_id, lead_id, reason, "high" if urgent else "normal", json.dumps(summary), notify_at,
         notify_at + timedelta(minutes=cfg.handoff.sla_minutes)))).fetchone()
    if off_hours and not urgent:
        await conn.execute("update conversations set handoff_pending = true where id = %s", (conversation_id,))
        mode = "off_hours"
    else:
        await transition_ownership(conn, tenant_id, conversation_id, "AWAITING_HUMAN", cause=f"handoff:{reason}")
        mode = "awaiting"
    return {"handoff_id": row["id"], "mode": mode, "notify_at": notify_at}
