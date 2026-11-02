"""A person reading conversations, daily during the pilot (plan 14 F2-PILOT-01). Guards only count what they caught; violations that got
past them are known only from this review (plan 01 section 7). Conversations with a signal come first, the rest in an order stable for the day."""
from __future__ import annotations

from datetime import date
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.config.schema import ReviewConfig
from agentplatform.core.security.pii import mask

VERDICTS = ("ok", "problem")


async def day_sample(conn: AsyncConnection, day: date, timezone: str, cfg: ReviewConfig) -> dict:
    p = {"day": day, "tz": timezone}
    rows = await (await conn.execute(
        """with answered as (
             select m.conversation_id, count(*) as bot_messages from messages m
             where m.direction = 'out_bot' and (m.provider_ts at time zone %(tz)s)::date = %(day)s group by 1)
           select * from (
             select c.id, c.ownership, c.entrypoint, ct.first_name, ci.username, ci.channel, ci.external_user_id, a.bot_messages,
                    exists (select 1 from guard_results g join turns t on t.id = g.turn_id where t.conversation_id = c.id
                            and g.verdict in ('block', 'handoff') and (g.created_at at time zone %(tz)s)::date = %(day)s) as guard_flag,
                    exists (select 1 from handoffs h where h.conversation_id = c.id and not h.is_notice
                            and (h.created_at at time zone %(tz)s)::date = %(day)s) as handoff_flag,
                    (select r.verdict from conversation_reviews r where r.conversation_id = c.id
                     and (r.created_at at time zone %(tz)s)::date >= %(day)s order by r.created_at desc limit 1) as verdict
             from answered a join conversations c on c.id = a.conversation_id join contacts ct on ct.id = c.contact_id
             join lateral (select username, channel, external_user_id from contact_identities i where i.contact_id = c.contact_id limit 1) ci on true
             where not exists (select 1 from contact_identities x where x.contact_id = c.contact_id
                               and (x.channel = 'simulator' or x.external_user_id like 'LAB-%%'))) s
           order by (guard_flag or handoff_flag) desc, md5(id::text || %(day)s::text)""", p)).fetchall()
    return {"total": len(rows), "reviewed": sum(1 for r in rows if r["verdict"]), "problems": sum(1 for r in rows if r["verdict"] == "problem"),
            "rows": rows[:cfg.daily_sample]}


async def findings(conn: AsyncConnection, cfg: ReviewConfig) -> dict:
    rows = await (await conn.execute(
        """select r.conversation_id, r.category, r.note_masked, r.created_at, ct.first_name, ci.username, ci.external_user_id
           from conversation_reviews r join conversations c on c.id = r.conversation_id join contacts ct on ct.id = c.contact_id
           left join lateral (select username, external_user_id from contact_identities i where i.contact_id = c.contact_id limit 1) ci on true
           where r.verdict = 'problem' and r.created_at > now() - make_interval(days => %s) order by r.created_at desc""",
        (cfg.findings_window_days,))).fetchall()
    return {"rows": rows, "violations": sum(1 for r in rows if r["category"] in cfg.violation_categories)}


async def for_conversation(conn: AsyncConnection, conversation_id: UUID) -> list[dict]:
    return await (await conn.execute(
        "select verdict, category, note_masked, created_at from conversation_reviews where conversation_id=%s order by created_at desc limit 10",
        (conversation_id,))).fetchall()


async def record(conn: AsyncConnection, tenant_id: UUID, conversation_id: UUID, reviewer: UUID, verdict: str, category: str, note: str,
                 cfg: ReviewConfig) -> None:
    if verdict not in VERDICTS:
        raise ValueError("verdict")
    problem = verdict == "problem"
    if problem and category not in cfg.categories:
        category = "other" if "other" in cfg.categories else next(iter(cfg.categories))
    text = mask(note.strip()[:cfg.note_max_chars]).text if note.strip() else None
    await conn.execute(
        "insert into conversation_reviews (tenant_id, conversation_id, verdict, category, note_masked, reviewer) values (%s,%s,%s,%s,%s,%s)",
        (tenant_id, conversation_id, verdict, category if problem else None, text, reviewer))
