"""Handoff alert loop (periodic): send due alerts, re-alert on SLA breach / unanswered inbound while HUMAN owns,
and promote off-hours pending handoffs to AWAITING_HUMAN at opening time (reviews #8, #9)."""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.channels.ports import Alert, SellerContact
from agentplatform.core.config.schema import PlatformConfig
from agentplatform.core.handoff.service import in_human_hours, reopen_unanswered
from agentplatform.core.orchestration.conversations import transition_ownership
from agentplatform.core.security.keys import decrypt_field


async def sellers_for(conn: AsyncConnection, tenant_id: UUID, cfg: PlatformConfig) -> list[SellerContact]:
    """Who may be told, in a stable order: the sellers named by the routing rule when it names real users, then sellers, then owners."""
    return await _contacts(conn, tenant_id, cfg, ("seller", "tenant_owner"))


async def _contacts(conn: AsyncConnection, tenant_id: UUID, cfg: PlatformConfig, roles: tuple[str, ...]) -> list[SellerContact]:
    rows = await (await conn.execute(
        """select id, notify_whatsapp_enc, notify_email_enc from tenant_users where tenant_id=%s and role = any(%s) and is_active
           order by case when role = 'seller' then 0 else 1 end, created_at, id""", (tenant_id, list(roles)))).fetchall()
    named = [str(s) for rule in cfg.handoff.routing for s in rule.seller_ids]
    rows = sorted(rows, key=lambda r: named.index(str(r["id"])) if str(r["id"]) in named else len(named))      # stable: keeps the rest in order
    out = []
    for r in rows:
        out.append(SellerContact(r["id"], await decrypt_field(conn, tenant_id, r["notify_whatsapp_enc"], "tenant_users.whatsapp"),
                                 await decrypt_field(conn, tenant_id, r["notify_email_enc"], "tenant_users.email")))
    return out


async def _deliver(inbox, candidates: list[SellerContact], alert: Alert) -> tuple[str | None, SellerContact | None]:
    """First candidate who can really be reached; a log line only counts when there is nobody else to try."""
    fallback: tuple[str | None, SellerContact | None] = (None, None)
    for seller in candidates:
        via = await inbox.notify_handoff(seller, alert)
        if via == "log":
            fallback = fallback if fallback[0] else (via, seller)
        elif via:
            return via, seller
    return fallback


SIMULATOR_CHANNEL = "simulator"     # console simulator channel (lab/simulator.py)


async def run_alerts(conn: AsyncConnection, cfg: PlatformConfig, tenant_id: UUID, inbox, now: datetime,
                     console_base: str) -> dict:
    stats = {"sent": 0, "realerts": 0, "promoted": 0, "failed": 0, "escalated": 0}
    if in_human_hours(cfg, now):
        pending = await (await conn.execute(
            "select id from conversations where tenant_id=%s and handoff_pending and ownership='BOT'", (tenant_id,))).fetchall()
        for p in pending:
            await transition_ownership(conn, tenant_id, p["id"], "AWAITING_HUMAN", cause="handoff_open_hours")
            stats["promoted"] += 1
    sellers = await sellers_for(conn, tenant_id, cfg)
    due = await (await conn.execute(
        """select h.*, l.stage, ca.channel from handoffs h left join leads l on l.id = h.lead_id
           join conversations c on c.id = h.conversation_id join channel_accounts ca on ca.id = c.channel_account_id
           where h.tenant_id=%s and h.notified_at is null and h.notify_at <= %s for update of h skip locked""",
        (tenant_id, now))).fetchall()
    for h in due:
        if h["channel"] == SIMULATOR_CHANNEL:          # console simulator: never notify a real seller
            await conn.execute("update handoffs set notified_at=now(), notified_via='simulator' where id=%s", (h["id"],))
            continue
        alert = Alert("handoff", h["summary"].get("first_name"), h["stage"], h["reason"],
                      _summary_line(h["summary"]), f"{console_base}/conversations/{h['conversation_id']}", h["id"])
        first = await pick_seller(conn, tenant_id, cfg, sellers)
        via, seller = await _deliver(inbox, [first, *(s for s in sellers if s is not first)] if first else [], alert)
        if via:
            await conn.execute(
                "insert into audit_log (tenant_id, actor_kind, action, target, after) values (%s,'system','handoff.routed',%s,%s)",
                (tenant_id, str(h["id"]), __import__("json").dumps({"seller": str(seller.seller_id)})))
            await conn.execute("update handoffs set notified_at=now(), notified_via=%s where id=%s", (via, h["id"]))
            stats["sent"] += 1
        else:
            stats["failed"] += 1
    if in_human_hours(cfg, now):
        late = await (await conn.execute(
            """select h.*, l.stage from handoffs h left join leads l on l.id = h.lead_id
               join conversations c on c.id = h.conversation_id join channel_accounts ca on ca.id = c.channel_account_id
               where h.tenant_id=%s and h.taken_at is null and h.notified_at is not null and h.sla_due_at < %s
                 and h.realerts < 2 and not h.is_notice and c.ownership in ('AWAITING_HUMAN','HUMAN') and ca.channel <> %s""",
            (tenant_id, now, SIMULATOR_CHANNEL))).fetchall()
        for h in late:
            alert = Alert("realert", h["summary"].get("first_name"), h["stage"], f"SLA: {h['reason']}",
                          _summary_line(h["summary"]), f"{console_base}/conversations/{h['conversation_id']}", h["id"])
            first = await pick_seller(conn, tenant_id, cfg, sellers)
            if (await _deliver(inbox, [first, *(s for s in sellers if s is not first)] if first else [], alert))[0]:
                stats["realerts"] += 1
            await conn.execute("update handoffs set realerts = realerts + 1, sla_breached = true, "
                               "sla_due_at = %s where id=%s", (now + timedelta(minutes=cfg.handoff.sla_minutes), h["id"]))
        # unanswered inbound while a human owns the conversation (review #9)
        stuck = await (await conn.execute(
            """select c.id from conversations c join channel_accounts ca on ca.id = c.channel_account_id
               where c.tenant_id=%s and c.ownership in ('HUMAN','AWAITING_HUMAN','PAUSED') and ca.channel <> 'simulator'
               and c.last_inbound_at > coalesce(c.last_reply_at, 'epoch') and c.last_inbound_at < %s
               and not exists (select 1 from handoffs h where h.conversation_id=c.id and not h.is_notice and h.created_at > c.last_inbound_at)""",
            (tenant_id, now - timedelta(minutes=cfg.handoff.human_unanswered_realert_min)))).fetchall()
        for c in stuck:
            await conn.execute(
                """insert into handoffs (tenant_id, conversation_id, reason, summary, notify_at, sla_due_at)
                   values (%s,%s,'human_unanswered',%s,%s,%s)""",
                (tenant_id, c["id"], '{"first_name": null}', now, now + timedelta(minutes=cfg.handoff.sla_minutes)))
        stats["escalated"] = await _escalate_to_owner(conn, cfg, tenant_id, inbox, now, console_base)
    return stats


async def _escalate_to_owner(conn: AsyncConnection, cfg: PlatformConfig, tenant_id: UUID, inbox, now: datetime, console_base: str) -> int:
    """Re-alerts to the seller ran out and the customer is still waiting: the owner hears about it, once per handoff."""
    after = cfg.handoff.escalate_to_owner_after_min
    if not after:
        return 0
    rows = await (await conn.execute(
        """select h.id, h.reason, h.summary, h.conversation_id, l.stage from handoffs h left join leads l on l.id = h.lead_id
           join conversations c on c.id = h.conversation_id join channel_accounts ca on ca.id = c.channel_account_id
           where h.tenant_id=%s and h.taken_at is null and not h.is_notice and h.escalated_at is null and h.created_at < %s
             and c.ownership in ('AWAITING_HUMAN','HUMAN','PAUSED') and c.last_inbound_at > coalesce(c.last_reply_at, 'epoch')
             and ca.channel <> %s""", (tenant_id, now - timedelta(minutes=after), SIMULATOR_CHANNEL))).fetchall()
    if not rows:
        return 0
    owners = await _contacts(conn, tenant_id, cfg, ("tenant_owner",))
    n = 0
    for h in rows:
        alert = Alert("escalation", h["summary"].get("first_name"), h["stage"], f"Cliente sem resposta: {h['reason']}",
                      _summary_line(h["summary"]), f"{console_base}/conversations/{h['conversation_id']}", h["id"])
        if (await _deliver(inbox, owners, alert))[0]:
            await conn.execute("update handoffs set escalated_at=now() where id=%s", (h["id"],))
            n += 1
    return n


async def auto_return_to_bot(conn: AsyncConnection, cfg: PlatformConfig, tenant_id: UUID, now: datetime) -> list[UUID]:
    """Handoffs caused by our own failure go back to the assistant when nobody took them, up to a daily limit per conversation.
    Returns the conversations with messages to answer. Off hours they stayed with the assistant: only the pending handoff is dropped."""
    h = cfg.handoff
    if not h.auto_return_after_min:
        return []
    rows = await (await conn.execute(
        """select distinct on (c.id) c.id, c.ownership, a.id as handoff_id from handoffs a join conversations c on c.id = a.conversation_id
           where a.tenant_id=%(t)s and a.reason = any(%(reasons)s) and a.taken_at is null and not a.is_notice and a.created_at < %(before)s
             and (c.ownership = 'AWAITING_HUMAN' or (c.ownership = 'BOT' and c.handoff_pending))
             and not exists (select 1 from contact_identities ci where ci.contact_id = c.contact_id and ci.channel = 'simulator')
             and not exists (select 1 from handoffs b where b.conversation_id = c.id and b.taken_at is null and not b.is_notice
                             and b.reason <> all(%(reasons)s) and b.reason <> 'human_unanswered')
             and not exists (select 1 from messages m where m.conversation_id = c.id and m.direction = 'out_human' and m.provider_ts > a.created_at)
             and (select count(*) from ownership_events e where e.conversation_id = c.id and e.cause = 'auto_return'
                  and e.at > %(day)s) < %(limit)s
           order by c.id, a.created_at desc""",
        {"t": tenant_id, "reasons": h.auto_return_reasons, "before": now - timedelta(minutes=h.auto_return_after_min),
         "day": now - timedelta(hours=24), "limit": h.auto_return_max_per_day})).fetchall()
    out = []
    for r in rows:
        await conn.execute(
            """update handoffs set taken_at = %s, notified_at = coalesce(notified_at, %s), notified_via = coalesce(notified_via, 'auto_return')
               where conversation_id = %s and taken_at is null and not is_notice""", (now, now, r["id"]))
        if r["ownership"] == "BOT":
            await conn.execute("update conversations set handoff_pending = false where id = %s", (r["id"],))
            continue
        await transition_ownership(conn, tenant_id, r["id"], "BOT", cause="auto_return")
        await reopen_unanswered(conn, r["id"])
        out.append(r["id"])
    return out


async def return_to_bot(conn: AsyncConnection, tenant_id: UUID, conversation_id: UUID, *, cause: str, actor: UUID | None) -> None:
    """A person gave the conversation back: the customer's unanswered messages are answered and this conversation's spending cap
    starts over (the tenant's daily and total caps still apply; the ledger keeps what was spent)."""
    await transition_ownership(conn, tenant_id, conversation_id, "BOT", cause=cause, actor=actor)
    await conn.execute("update conversations set handoff_pending = false where id = %s", (conversation_id,))
    await conn.execute("update handoffs set taken_at = coalesce(taken_at, now()) where conversation_id = %s and taken_at is null and not is_notice",
                       (conversation_id,))
    await conn.execute("update budget_balances set spent_usd = 0 where tenant_id = %s and scope = %s",
                       (tenant_id, f"conversation:{conversation_id}"))
    await reopen_unanswered(conn, conversation_id)


async def pick_seller(conn, tenant_id, cfg: PlatformConfig, sellers: list[SellerContact]) -> SellerContact | None:
    """Routing strategies: single (first), round_robin (fewest routed), least_open (fewest open handoffs)."""
    if not sellers:
        return None
    strategy = cfg.handoff.routing[0].strategy if cfg.handoff.routing else "single"
    if strategy == "single" or len(sellers) == 1:
        return sellers[0]
    counts = {}
    for s in sellers:
        if strategy == "round_robin":
            q = "select count(*) as n from audit_log where tenant_id=%s and action='handoff.routed' and after->>'seller' = %s"
        else:
            q = ("select count(*) as n from handoffs h join audit_log a on a.target = h.id::text and a.action='handoff.routed' "
                 "where h.tenant_id=%s and a.after->>'seller' = %s and h.taken_at is null")
        counts[s.seller_id] = (await (await conn.execute(q, (tenant_id, str(s.seller_id)))).fetchone())["n"]
    return min(sellers, key=lambda s: counts[s.seller_id])


def _summary_line(summary: dict) -> str:
    prof = ", ".join(f"{k}={v}" for k, v in (summary.get("profile") or {}).items() if v is not None)
    return (prof + (" | " if prof else "") + (summary.get("last_message") or ""))[:300]


async def apply_seller_action(conn: AsyncConnection, tenant_id: UUID, action: str, handoff_id: UUID, actor: UUID | None) -> str:
    h = await (await conn.execute("select conversation_id from handoffs where id=%s", (handoff_id,))).fetchone()
    if h is None:
        return "unknown_handoff"
    if action == "took":
        await conn.execute("update handoffs set taken_at = coalesce(taken_at, now()), taken_by=%s where id=%s", (actor, handoff_id))
        await transition_ownership(conn, tenant_id, h["conversation_id"], "HUMAN", cause="notifier_button", actor=actor)
        return "HUMAN"
    await return_to_bot(conn, tenant_id, h["conversation_id"], cause="return_to_bot", actor=actor)
    return "BOT"
