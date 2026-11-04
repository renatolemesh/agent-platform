"""Follow-up engine (plan 09): scheduled state machines, not LLM decisions. Only inside the channel window and
outside quiet hours; skipped (never rescheduled past the window) on Instagram; stopped by inbound/opt-out/handoff/human.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.config.schema import Cadence, PlatformConfig
from agentplatform.core.outbox.dispatcher import DispatchPolicy
from agentplatform.core.outbox.store import PRIORITY, enqueue


def parse_delay(s: str) -> timedelta:
    m = re.fullmatch(r"(\d+)\s*([smhd])", s.strip())
    if not m:
        raise ValueError(f"bad delay {s!r}")
    n, unit = int(m.group(1)), m.group(2)
    return {"s": timedelta(seconds=n), "m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


def trigger_stages(trigger: dict) -> list[str]:
    return trigger.get("stages") or ([trigger["stage"]] if trigger.get("stage") else [])


def _live(cfg: PlatformConfig, event: str) -> list[Cadence]:
    return [c for c in cfg.followups.cadences if cfg.followups.enabled and c.enabled and c.trigger.get("event") == event]


async def on_stage_entered(conn: AsyncConnection, cfg: PlatformConfig, tenant_id: UUID, conversation_id: UUID,
                           stage: str, now: datetime) -> int:
    n = 0
    for cad in _live(cfg, "stage_entered"):
        if stage in trigger_stages(cad.trigger):
            n += await _schedule(conn, tenant_id, conversation_id, cad, now)
    return n


async def on_bot_message(conn: AsyncConnection, cfg: PlatformConfig, tenant_id: UUID, conversation_id: UUID, now: datetime) -> int:
    """The silence counts from the assistant's last message, whatever the turn did to the stage."""
    cadences = _live(cfg, "bot_message_unanswered")
    if not cadences:
        return 0
    lead = await (await conn.execute(
        "select l.stage from conversations c join leads l on l.contact_id = c.contact_id where c.id = %s", (conversation_id,))).fetchone()
    n = 0
    for cad in cadences:
        if lead and lead["stage"] in trigger_stages(cad.trigger):
            n += await _schedule(conn, tenant_id, conversation_id, cad, now, restart=True)
    return n


# A reply stops the run and the row stays, so the silence after the next message needs it back. Once a reminder
# reached the outbox the row keeps its outbox_id and is never armed again: attempts are per conversation.
_ONCE = "do nothing"
_RESTART = """do update set status = 'scheduled', due_at = excluded.due_at, stop_reason = null
              where followup_runs.outbox_id is null"""


async def _schedule(conn, tenant_id, conversation_id, cad: Cadence, base: datetime, *, restart: bool = False) -> int:
    due = base
    n = 0
    for i, step in enumerate(cad.steps[: cad.max_attempts]):
        due = due + parse_delay(step.delay)
        r = await conn.execute(
            """insert into followup_runs (tenant_id, conversation_id, cadence_id, step, status, due_at)
               values (%s,%s,%s,%s,'scheduled',%s) on conflict (conversation_id, cadence_id, step) """ + (_RESTART if restart else _ONCE),
            (tenant_id, conversation_id, cad.id, i, due))
        n += r.rowcount
    return n


def _jitter(run_id: UUID, cfg: PlatformConfig) -> int:
    low, high = cfg.sending.proactive_jitter_s
    return low + int.from_bytes(hashlib.sha256(str(run_id).encode()).digest()[:4], "big") % (max(high - low, 0) + 1)


@dataclass
class TickResult:
    queued: int = 0
    skipped: int = 0
    rescheduled: int = 0


async def tick(conn: AsyncConnection, cfg: PlatformConfig, tenant_id: UUID, templates: dict[str, str], now: datetime,
               policy: DispatchPolicy | None = None, limit: int = 100) -> TickResult:
    policy = policy or DispatchPolicy(timezone=cfg.sending.quiet_hours.timezone, quiet_start=cfg.sending.quiet_hours.start,
                                      quiet_end=cfg.sending.quiet_hours.end, holidays_national=cfg.sending.holidays_national,
                                      holidays_local=tuple(cfg.sending.holidays_local))
    res = TickResult()
    cadences = {c.id: c for c in cfg.followups.cadences}
    rows = await (await conn.execute(
        """select f.*, c.window_expires_at, c.ownership, c.send_epoch, ci.external_user_id as recipient, l.opted_out_at
           from followup_runs f join conversations c on c.id = f.conversation_id
           join contact_identities ci on ci.contact_id = c.contact_id join leads l on l.contact_id = c.contact_id
           where f.status = 'scheduled' and f.due_at <= %s order by f.due_at limit %s for update of f skip locked""",
        (now, limit))).fetchall()
    for r in rows:
        cad = cadences.get(r["cadence_id"])
        reason = None
        if cad is None or r["step"] >= len(cad.steps):
            reason = "cadence_removed"
        elif not (cfg.followups.enabled and cad.enabled):
            reason = "cadence_disabled"
        elif r["opted_out_at"]:
            reason = "opt_out"
        elif r["ownership"] != "BOT":
            reason = f"ownership:{r['ownership']}"
        elif r["window_expires_at"] is None or r["window_expires_at"] <= now:
            reason = "window_closed"
        if reason:
            await conn.execute("update followup_runs set status='skipped', stop_reason=%s where id=%s", (reason, r["id"]))
            res.skipped += 1
            continue
        if policy.is_quiet(now):
            nxt = policy.next_allowed(now)
            if nxt >= r["window_expires_at"]:
                await conn.execute("update followup_runs set status='skipped', stop_reason='window_closed_before_open_hours' where id=%s",
                                   (r["id"],))
                res.skipped += 1
            else:
                await conn.execute("update followup_runs set due_at=%s where id=%s", (nxt, r["id"]))
                res.rescheduled += 1
            continue
        sent = await (await conn.execute(      # the person, across every channel and flow
            """select count(*) filter (where o.sent_at > %(now)s - interval '1 day') as day, count(*) as week
               from outbox o join conversations c on c.id = o.conversation_id
               where c.contact_id = (select contact_id from conversations where id = %(c)s) and o.is_proactive and o.status='sent'
                 and o.sent_at > %(now)s - interval '7 days'""", {"now": now, "c": r["conversation_id"]})).fetchone()
        if sent["day"] >= cfg.sending.frequency_cap_per_contact_day or sent["week"] >= cfg.sending.frequency_cap_per_contact_week:
            await conn.execute("update followup_runs set status='skipped', stop_reason='frequency_cap' where id=%s", (r["id"],))
            res.skipped += 1
            continue
        step = cad.steps[r["step"]]
        words = {**templates, **cfg.followups.texts}
        text = words.get(step.template_key or "") if step.content_kind == "template" else words.get("no_reply_nudge")
        if not text:
            await conn.execute("update followup_runs set status='skipped', stop_reason='no_template' where id=%s", (r["id"],))
            res.skipped += 1
            continue
        oid = await enqueue(conn, tenant_id=tenant_id, conversation_id=r["conversation_id"], effect_key=f"fu:{r['id']}",
                            kind="dm_text", text=text, recipient=r["recipient"], send_epoch=r["send_epoch"],
                            priority=PRIORITY["followup"], max_staleness_s=cfg.concurrency.max_staleness_proactive_s,
                            is_proactive=True, not_before=now + timedelta(seconds=_jitter(r["id"], cfg)))
        await conn.execute("update followup_runs set status='queued', outbox_id=%s where id=%s", (oid, r["id"]))
        res.queued += 1
    return res
