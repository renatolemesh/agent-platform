"""Campaigns (plan 09 §4, phase 4): segment -> targets -> throttled template sends (priority 3, never ahead of live
conversations) -> auto-pause on opt-out/block spikes. Only contacts with a valid marketing basis and not suppressed."""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.outbox.store import PRIORITY, enqueue


@dataclass
class CampaignSpec:
    name: str
    stages: list[str]
    template_name: str
    variants: dict[str, float]            # template name -> weight (A/B)
    daily_limit: int = 250                 # WhatsApp portfolio tier (unique users outside the window / 24h)
    max_optout_rate: float = 0.03
    max_block_rate: float = 0.02
    min_sample: int = 20                   # attempts before the auto-pause rates are evaluated


async def create_campaign(conn: AsyncConnection, tenant_id: UUID, spec: CampaignSpec) -> UUID:
    row = await (await conn.execute(
        """insert into campaigns (tenant_id, name, segment, template, schedule, limits, status)
           values (%s,%s,%s,%s,'{}',%s,'scheduled') returning id""",
        (tenant_id, spec.name, json.dumps({"stages": spec.stages}),
         json.dumps({"name": spec.template_name, "variants": spec.variants}),
         json.dumps({"daily_limit": spec.daily_limit, "max_optout_rate": spec.max_optout_rate,
                     "max_block_rate": spec.max_block_rate, "min_sample": spec.min_sample})))).fetchone()
    targets = await (await conn.execute(
        """select distinct l.contact_id from leads l
           join contact_identities ci on ci.contact_id = l.contact_id and ci.channel = 'whatsapp'
           join consents k on k.contact_id = l.contact_id and k.purpose = 'marketing' and k.status = 'granted'
           where l.tenant_id = %s and l.stage = any(%s) and l.opted_out_at is null
             and not exists (select 1 from consents r where r.contact_id = l.contact_id and r.purpose='marketing'
                             and r.status='revoked' and r.at > k.at)""",
        (tenant_id, spec.stages))).fetchall()
    rng = random.Random(str(row["id"]))
    names, weights = list(spec.variants), list(spec.variants.values())
    for t in targets:
        await conn.execute(
            "insert into campaign_targets (campaign_id, tenant_id, contact_id, variant, status) values (%s,%s,%s,%s,'pending')",
            (row["id"], tenant_id, t["contact_id"], rng.choices(names, weights)[0]))
    return row["id"]


# Target lifecycle: pending -> queued -> sent -> delivered -> read; or failed / opted_out / skipped.
_RANK = {"pending": 0, "queued": 1, "sent": 2, "delivered": 3, "read": 4}
# WhatsApp: the user stopped marketing messages from this business -> treat as marketing opt-out
MARKETING_OPTOUT_CODES = {"131050"}


async def _revoke_marketing(conn: AsyncConnection, tenant_id: UUID, contact_id: UUID, evidence: dict) -> None:
    await conn.execute(
        """insert into consents (tenant_id, contact_id, purpose, legal_basis, status, evidence, channel)
           values (%s,%s,'marketing','consent','revoked',%s,'whatsapp')""", (tenant_id, contact_id, json.dumps(evidence)))


async def apply_delivery_status(conn: AsyncConnection, tenant_id: UUID, external_mid: str, status: str,
                                errors: list[dict] | None) -> str | None:
    """Status webhook (sent/delivered/read/failed) for a message we sent. Records asynchronous delivery failures on the
    outbox row and advances the campaign target (never backwards). Returns the new target status, if any."""
    codes = {str(e.get("code")) for e in errors or []}
    if status == "failed":
        await conn.execute("update outbox set last_error=%s where tenant_id=%s and external_mid=%s",
                           (f"delivery_failed:{','.join(sorted(codes)) or 'unknown'}", tenant_id, external_mid))
    t = await (await conn.execute(
        """select t.campaign_id, t.contact_id, t.status from campaign_targets t join outbox o on o.id = t.outbox_id
           where o.tenant_id=%s and o.external_mid=%s""", (tenant_id, external_mid))).fetchone()
    if not t:
        return None
    if status == "failed":
        new = "opted_out" if codes & MARKETING_OPTOUT_CODES else "failed"
        if new == "opted_out":
            await _revoke_marketing(conn, tenant_id, t["contact_id"], {"source": "delivery_status", "codes": sorted(codes)})
    elif status in _RANK and _RANK.get(t["status"], 99) < _RANK[status]:
        new = status
    else:
        return t["status"]
    await conn.execute("update campaign_targets set status=%s where campaign_id=%s and contact_id=%s",
                       (new, t["campaign_id"], t["contact_id"]))
    return new


async def _sync_targets(conn: AsyncConnection, campaign_id: UUID) -> None:
    """Outbox outcome -> target status (send-time result), and customer opt-outs after the campaign started."""
    await conn.execute(
        """update campaign_targets t set status = case
               when o.status = 'sent' then 'sent'
               when o.status = 'cancelled' then 'skipped'
               when o.last_error like any (%s) then 'opted_out'
               else 'failed' end
           from outbox o where o.id = t.outbox_id and t.campaign_id = %s and t.status = 'queued'
             and o.status in ('sent', 'failed', 'cancelled')""",
        ([f"%{c}%" for c in MARKETING_OPTOUT_CODES], campaign_id))
    await conn.execute(
        """update campaign_targets t set status = 'opted_out'
           from leads l, campaigns c where c.id = t.campaign_id and l.contact_id = t.contact_id and t.campaign_id = %s
             and t.status in ('sent', 'delivered', 'read') and l.opted_out_at > c.created_at""", (campaign_id,))


async def campaign_metrics(conn: AsyncConnection, campaign_id: UUID) -> list[dict]:
    """Per variant: targets, waiting, sent (incl. delivered/read), delivered, read, failed, opted_out, replied."""
    return await (await conn.execute(
        """select t.variant,
                  count(*) as targets,
                  count(*) filter (where t.status in ('pending','queued')) as waiting,
                  count(*) filter (where t.status in ('sent','delivered','read')) as sent,
                  count(*) filter (where t.status in ('delivered','read')) as delivered,
                  count(*) filter (where t.status = 'read') as read,
                  count(*) filter (where t.status = 'failed') as failed,
                  count(*) filter (where t.status = 'opted_out') as opted_out,
                  count(*) filter (where exists (
                      select 1 from messages m where m.conversation_id = o.conversation_id
                        and m.direction = 'in' and m.created_at > o.sent_at)) as replied
           from campaign_targets t left join outbox o on o.id = t.outbox_id
           where t.campaign_id = %s group by t.variant order by t.variant""", (campaign_id,))).fetchall()


async def tick(conn: AsyncConnection, tenant_id: UUID, campaign_id: UUID, now: datetime, *, is_quiet: bool) -> dict:
    camp = await (await conn.execute("select * from campaigns where id=%s for update", (campaign_id,))).fetchone()
    if camp["status"] not in ("scheduled", "running"):
        return {"status": camp["status"]}
    limits = camp["limits"]
    await _sync_targets(conn, campaign_id)
    stats = await (await conn.execute(
        """select count(*) filter (where status in ('sent','delivered','read','failed','opted_out')) as attempted,
                  count(*) filter (where status in ('sent','delivered','read')) as sent,
                  count(*) filter (where status='failed') as failed,
                  count(*) filter (where status='opted_out') as opted_out
           from campaign_targets where campaign_id=%s""", (campaign_id,))).fetchone()
    attempted = max(stats["attempted"], 1)
    if stats["attempted"] >= limits.get("min_sample", 20) and (
            stats["opted_out"] / attempted > limits["max_optout_rate"] or stats["failed"] / attempted > limits["max_block_rate"]):
        await conn.execute("update campaigns set status='paused_auto' where id=%s", (campaign_id,))
        return {"status": "paused_auto", **stats}
    if is_quiet:
        return {"status": "waiting_quiet_hours"}
    today = (await (await conn.execute(
        """select count(*) as n from outbox where tenant_id=%s and kind='template' and created_at > %s - interval '1 day'""",
        (tenant_id, now))).fetchone())["n"]
    budget = max(0, limits["daily_limit"] - today)
    rows = await (await conn.execute(
        """select t.contact_id, t.variant, ci.external_user_id as wa_id from campaign_targets t
           join contact_identities ci on ci.contact_id = t.contact_id and ci.channel='whatsapp'
           where t.campaign_id=%s and t.status='pending' limit %s for update of t skip locked""",
        (campaign_id, budget))).fetchall()
    queued = 0
    for r in rows:
        conv = await (await conn.execute(
            """insert into conversations (tenant_id, contact_id, channel_account_id, entrypoint)
               select %s, %s, ca.id, 'campaign' from channel_accounts ca where ca.tenant_id=%s and ca.channel='whatsapp' limit 1
               on conflict (channel_account_id, contact_id) do update set entrypoint = conversations.entrypoint returning id, send_epoch""",
            (tenant_id, r["contact_id"], tenant_id))).fetchone()
        if conv is None:
            break      # no WhatsApp account configured for the tenant
        oid = await enqueue(conn, tenant_id=tenant_id, conversation_id=conv["id"], effect_key=f"camp:{campaign_id}:{r['contact_id']}",
                            kind="template", text=f"[template:{r['variant']}]", recipient=r["wa_id"], send_epoch=conv["send_epoch"],
                            priority=PRIORITY["campaign"], max_staleness_s=86400, is_proactive=True,
                            extra={"template": {"name": r["variant"], "language": "pt_BR"}, "campaign_id": str(campaign_id)})
        await conn.execute("update campaign_targets set status='queued', outbox_id=%s where campaign_id=%s and contact_id=%s",
                           (oid, campaign_id, r["contact_id"]))
        queued += 1
    await conn.execute("update campaigns set status='running' where id=%s and status='scheduled'", (campaign_id,))
    return {"status": "running", "queued": queued, "budget_left": budget - queued, **stats}
