"""Comment automation (plan 05 §7, ADR-018): self/dedup/spam/sensitive filters, then what the post was set to do in the console
(account default or per-post choice), by cost order (keyword -> any -> intent). Public reply variants, private reply with an
ATOMIC per-person-per-media claim (review #11). Every comment is recorded for the seller, including those nothing was done about.
Comments older than the moment a post became active are never answered unless the seller asks for it.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.agent.guards import normalize
from agentplatform.core.channels.events import CanonicalEvent
from agentplatform.core.comments.catalog import ensure_media
from agentplatform.core.comments.settings import SENDS_PRIVATE, SENDS_PUBLIC, Effective, effective_for, render_text
from agentplatform.core.config.schema import PlatformConfig
from agentplatform.core.db.pool import advisory_xact_lock
from agentplatform.core.handoff.service import build_summary, create_handoff
from agentplatform.core.orchestration.conversations import (
    conv_lock_key,
    get_or_create_conversation,
    transition_ownership,
)
from agentplatform.core.orchestration.dm_scope import comment_block
from agentplatform.core.outbox.store import PRIORITY, enqueue
from agentplatform.core.packages.contract import PackageManifest
from agentplatform.core.security.keys import encrypt_field
from agentplatform.core.security.pii import mask

_KIND_BY_PRODUCT = {"REELS": "reel", "STORY": "story", "LIVE": "live", "AD": "ad"}
_HUMAN_STATES = ("HUMAN", "AWAITING_HUMAN", "PAUSED")


@dataclass
class CommentDecision:
    # acted | registered | ignored_self | duplicate | spam | human_queue | inactive | paused | before_activation | no_rule |
    # dedup_window | already_in_contact | with_human | opted_out | existing_customer | disabled |
    # known_contact | blocked_contact | contacts_pending
    decision: str
    rule_id: str | None = None
    public_outbox_id: UUID | None = None
    private_outbox_id: UUID | None = None
    conversation_id: UUID | None = None
    hide_outbox_id: UUID | None = None
    media_created: bool = False


def classify(cfg: PlatformConfig, normalized: str) -> str | None:
    """spam | sensitive | None"""
    if any(re.search(p, normalized) for p in cfg.comments.spam_patterns):
        return "spam"
    if cfg.comments.sensitive_topics_to_human and any(re.search(p, normalized) for p in cfg.comments.sensitive_patterns):
        return "sensitive"
    return None


def trigger_matches(eff: Effective, cfg: PlatformConfig, normalized: str, intent: float | None) -> bool:
    keywords = any(re.search(rf"\b{re.escape(normalize(k))}\b", normalized) for k in eff.keywords)
    if eff.trigger_kind == "any":
        return not any(re.search(p, normalized) for p in cfg.comments.any_ignore_patterns)
    if eff.trigger_kind == "intent" and intent is not None:
        return intent >= cfg.comments.intent_threshold
    return keywords           # keyword rule, or intent with no answer from the model (budget, outage)


def _too_old(eff: Effective, ts: datetime) -> bool:
    if eff.backfill_since and ts >= eff.backfill_since:
        return False
    return bool(eff.active_since) and ts < eff.active_since


async def plan_intent(conn: AsyncConnection, cfg: PlatformConfig, channel_account_id: UUID, ev: CanonicalEvent, now: datetime) -> bool:
    """True when this comment needs the (paid) intent model: passed the free filters on a post set to use it."""
    if not ev.media_id or await (await conn.execute("select 1 from comment_actions where tenant_id=(select tenant_id from channel_accounts where id=%s) and comment_id=%s",
                                                    (channel_account_id, ev.comment_id))).fetchone():
        return False
    if classify(cfg, normalize(ev.text or "")):
        return False
    media = await (await conn.execute("select * from channel_media where channel_account_id=%s and external_media_id=%s",
                                      (channel_account_id, ev.media_id))).fetchone()
    if media is None:
        return False
    eff = await effective_for(conn, channel_account_id, media, now)
    return eff.mode != "off" and eff.trigger_kind == "intent" and not _too_old(eff, ev.provider_ts)


async def process_comment(conn: AsyncConnection, cfg: PlatformConfig, manifest: PackageManifest, *, tenant_id: UUID,
                          channel_account_id: UUID, account_external_id: str, ev: CanonicalEvent, now: datetime,
                          rng: random.Random | None = None, intent: float | None = None, paused: bool = False,
                          mixed_inbox: bool = False) -> CommentDecision:
    rng = rng or random.Random()
    text = ev.text or ""
    n = normalize(text)
    question = any(re.search(p, n) for p in cfg.comments.attention_patterns)
    masked = mask(text).text if text else None

    async def record(decision: str, source: str | None = None, *, attention: bool = False, conversation_id: UUID | None = None,
                     **kw) -> None:
        await conn.execute(
            """insert into comment_actions (tenant_id, comment_id, media_id, author_external_id, rule_id, decision, public_variant,
                                            private_variant, public_reply_outbox_id, private_reply_outbox_id, text_masked,
                                            author_username, comment_ts, conversation_id, attention)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (tenant_id, comment_id) do nothing""",
            (tenant_id, ev.comment_id, ev.media_id or "", ev.contact_external_id or "", source, decision, kw.get("pv"), kw.get("prv"),
             kw.get("po"), kw.get("pro"), masked, ev.author_username, ev.provider_ts, conversation_id, attention))

    if not cfg.comments.enabled:
        return CommentDecision("disabled")
    if ev.contact_external_id in (None, account_external_id):
        await record("ignored_self")
        return CommentDecision("ignored_self")
    if await (await conn.execute("select 1 from comment_actions where tenant_id=%s and comment_id=%s",
                                 (tenant_id, ev.comment_id))).fetchone():
        return CommentDecision("duplicate")

    media, created = await ensure_media(conn, tenant_id, channel_account_id, ev.media_id or "",
                                        "ad" if ev.ad_id else _KIND_BY_PRODUCT.get(ev.media_product_type or "", "post"),
                                        ad_id=ev.ad_id, ad_title=ev.ad_title, original_media_id=ev.original_media_id)
    eff = await effective_for(conn, channel_account_id, media, now)

    async def open_conversation(entrypoint: str = "comment") -> dict:
        conv = await get_or_create_conversation(conn, tenant_id, channel_account_id, ev.channel, ev.contact_external_id,
                                                manifest.initial_stage, username=ev.author_username, entrypoint=entrypoint)
        await advisory_xact_lock(conn, conv_lock_key(conv["id"]))     # parallel comments of one person are handled in order
        return await (await conn.execute("select * from conversations where id=%s", (conv["id"],))).fetchone()

    async def register(conv: dict) -> dict:
        """The comment goes into the history (read by the agent as origin, never as a turn input) and the lead gets its source."""
        await conn.execute(
            """insert into messages (tenant_id, conversation_id, direction, external_mid, kind, text_masked, text_enc, provider_ts)
               values (%s,%s,'in',%s,'comment',%s,%s,%s) on conflict do nothing""",
            (tenant_id, conv["id"], f"comment:{ev.comment_id}", masked, await encrypt_field(conn, tenant_id, text, "messages.text"),
             ev.provider_ts))
        lead = await (await conn.execute("select id, stage, profile, opted_out_at from leads where contact_id=%s",
                                         (conv["contact_id"],))).fetchone()
        first = not await (await conn.execute("select 1 from lead_sources where lead_id=%s limit 1", (lead["id"],))).fetchone()
        await conn.execute(
            """insert into lead_sources (tenant_id, lead_id, channel, entrypoint, media_id, ad_id, comment_id, is_first_touch)
               values (%s,%s,%s,'comment',%s,%s,%s,%s)""",
            (tenant_id, lead["id"], ev.channel, ev.media_id, ev.ad_id, ev.comment_id, first))
        missing = {k: {"value": v, "source_message_id": None, "confidence": 0.5, "at": now.isoformat(), "source": "post"}
                   for k, v in eff.context_slots.items() if v and not (lead["profile"] or {}).get(k)}
        if missing:      # low confidence: whatever the customer says about it overrides the post
            await conn.execute("update leads set profile = profile || %s where id=%s", (json.dumps(missing), lead["id"]))
        return lead

    kind = classify(cfg, n)
    if kind == "spam":
        conv_id = hide_id = None
        if eff.hide_spam and not paused:
            conv = await open_conversation("comment_spam")
            conv_id = conv["id"]
            if conv["entrypoint"] == "comment_spam" and conv["ownership"] == "BOT":     # this record exists to hide a comment, not to talk
                conv = await transition_ownership(conn, tenant_id, conv_id, "PAUSED", cause="spam_comment")
            hide_id = await enqueue(conn, tenant_id=tenant_id, conversation_id=conv_id, effect_key=f"cmt:{ev.comment_id}:hide",
                                    kind="hide_comment", text=None, recipient=None, comment_id=ev.comment_id,
                                    send_epoch=conv["send_epoch"], priority=PRIORITY["comment"], max_staleness_s=7 * 86400)
        await record("spam", conversation_id=conv_id)
        return CommentDecision("spam", conversation_id=conv_id, hide_outbox_id=hide_id, media_created=created)
    if kind == "sensitive":
        conv = await open_conversation()
        lead = await register(conv)
        if conv["ownership"] not in ("HUMAN", "AWAITING_HUMAN"):
            summary = build_summary(None, lead["stage"], "complaint_or_abuse", lead["profile"] or {}, masked)
            await create_handoff(conn, cfg, tenant_id=tenant_id, conversation_id=conv["id"], lead_id=lead["id"],
                                 reason="complaint_or_abuse", summary=summary, now=now)
        await record("human_queue", attention=True, conversation_id=conv["id"])
        return CommentDecision("human_queue", conversation_id=conv["id"], media_created=created)

    if eff.mode == "off":
        await record("inactive", attention=question)
        return CommentDecision("inactive", media_created=created)
    if paused:
        await record("paused", eff.source, attention=True)
        return CommentDecision("paused", eff.source, media_created=created)
    if _too_old(eff, ev.provider_ts):
        await record("before_activation", eff.source, attention=question)
        return CommentDecision("before_activation", eff.source, media_created=created)
    if not trigger_matches(eff, cfg, n, intent):
        await record("no_rule", eff.source, attention=question)
        return CommentDecision("no_rule", eff.source, media_created=created)

    if mixed_inbox:           # the owner's friends and old contacts are the owner's: the seller sees the comment and decides
        block = await comment_block(conn, channel_account_id, ev.channel, ev.contact_external_id)
        if block:
            await record(block, eff.source, attention=block != "blocked_contact")
            return CommentDecision(block, eff.source, media_created=created)

    conv = await open_conversation()
    lead = await register(conv)

    async def stop(decision: str, attention: bool) -> CommentDecision:
        await record(decision, eff.source, attention=attention, conversation_id=conv["id"])
        return CommentDecision(decision, eff.source, conversation_id=conv["id"], media_created=created)

    if eff.mode == "register":
        return await stop("registered", True)
    if lead["opted_out_at"]:
        return await stop("opted_out", False)
    if lead["stage"] == manifest.won_stage:
        return await stop("existing_customer", True)         # a customer gets no sales opener; the seller sees the comment
    if conv["ownership"] in _HUMAN_STATES:
        return await stop("with_human", True)                # never talks over the seller; the seller sees the comment
    if cfg.comments.once_per_person and await _answered_before(conn, tenant_id, conv["id"], ev.contact_external_id, ev.comment_id):
        return await stop("already_answered", question)      # one reply and one direct per person, however many comments
    if conv["ownership"] == "CLOSED":
        conv = await transition_ownership(conn, tenant_id, conv["id"], "BOT", cause="reopened_by_comment")

    seller = cfg.conversation.persona.seller_display_name
    private_id = prv = None
    wants_private = eff.mode in SENDS_PRIVATE and bool(eff.private_variants)
    contacted = wants_private and await _in_contact(conn, conv["id"], now, cfg.comments.person_contact_window_days)
    if wants_private and not contacted:
        window = timedelta(days=cfg.comments.private_reply_dedup_window_days)
        claim = await (await conn.execute(
            """insert into private_reply_claims (tenant_id, media_id, author_external_id, claimed_at, comment_id)
               values (%s,%s,%s,now(),%s)
               on conflict (tenant_id, media_id, author_external_id) do update
                 set claimed_at = now(), comment_id = excluded.comment_id
                 where private_reply_claims.claimed_at < now() - %s
               returning comment_id""",
            (tenant_id, ev.media_id or "", ev.contact_external_id, ev.comment_id, window))).fetchone()
        if claim is not None:
            prv = rng.choice(sorted(eff.private_variants))
            private_id = await enqueue(
                conn, tenant_id=tenant_id, conversation_id=conv["id"], effect_key=f"cmt:{ev.comment_id}:private",
                kind="private_reply", text=render_text(eff.private_variants[prv], seller), recipient=None, comment_id=ev.comment_id,
                send_epoch=conv["send_epoch"], priority=PRIORITY["comment"], max_staleness_s=7 * 86400,
                extra={"comment_ts": ev.provider_ts.isoformat(), "variant": prv})

    public_id = pv = None
    if eff.mode in SENDS_PUBLIC and ev.parent_comment_id is None and ev.media_product_type != "LIVE":
        already = wants_private and private_id is None        # no new direct went out: the reply cannot say it did
        options = eff.repeat_variants if already else eff.public_variants
        last = await (await conn.execute(
            "select public_variant from comment_actions where tenant_id=%s and media_id=%s and public_variant is not null "
            "order by created_at desc limit 1", (tenant_id, ev.media_id or ""))).fetchone()
        options = [v for v in options if not last or render_text(v, seller) != last["public_variant"]] or options
        if options:
            pv = render_text(rng.choice(options), seller)
            public_id = await enqueue(conn, tenant_id=tenant_id, conversation_id=conv["id"], effect_key=f"cmt:{ev.comment_id}:public",
                                      kind="public_comment_reply", text=pv, recipient=None, comment_id=ev.comment_id,
                                      send_epoch=conv["send_epoch"], priority=PRIORITY["comment"], max_staleness_s=7 * 86400)
    if not (public_id or private_id):
        return await stop("already_in_contact" if contacted else "dedup_window", question)
    await record("acted", eff.source, conversation_id=conv["id"], pv=pv, prv=prv, po=public_id, pro=private_id)
    return CommentDecision("acted", eff.source, public_id, private_id, conv["id"], media_created=created)


async def _answered_before(conn: AsyncConnection, tenant_id: UUID, conversation_id: UUID, author: str | None, comment_id: str | None) -> bool:
    """This person already got an automatic reply or direct from a comment, or any message from the bot or the seller.
    Comments of one person are handled in order (conversation lock), so a reply still in the outbox is already recorded."""
    return bool(await (await conn.execute(
        """select 1 from comment_actions where tenant_id=%s and author_external_id=%s and decision='acted' and comment_id <> %s
           union all select 1 from messages where conversation_id=%s and direction in ('out_bot','out_human')
           union all select 1 from outbox where conversation_id=%s and kind in ('private_reply','public_comment_reply')
                                            and status in ('pending','sending') limit 1""",
        (tenant_id, author or "", comment_id or "", conversation_id, conversation_id))).fetchone())


async def _in_contact(conn: AsyncConnection, conversation_id: UUID, now: datetime, window_days: int) -> bool:
    """Already talking to this person (from any post): the conversation goes on, without a new opener."""
    if window_days <= 0:
        return False
    since = now - timedelta(days=window_days)
    return bool(await (await conn.execute(
        """select 1 from messages where conversation_id=%s and direction in ('out_bot','out_human') and kind <> 'comment_reply'
             and provider_ts > %s
           union all select 1 from outbox where conversation_id=%s and kind='private_reply' and status in ('pending','sending') limit 1""",
        (conversation_id, since, conversation_id))).fetchone())
