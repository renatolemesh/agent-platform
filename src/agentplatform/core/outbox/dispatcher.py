"""Dispatcher: the last-mile check before every bubble (plan 07 §5). Duplicates weigh more than losses:
an ambiguous send is never blindly resent (reconcile by echo, then the Conversations API, else failed + handoff).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Awaitable, Callable
from uuid import UUID
from zoneinfo import ZoneInfo

from agentplatform.core.channels.ports import (
    AccountRef,
    AmbiguousSendError,
    ChannelAdapter,
    OutboundMessage,
    RateLimitedError,
    RetryableSendError,
    SendError,
    TokenInvalidError,
)
from agentplatform.core.config.registry import registry
from agentplatform.core.db.pool import advisory_xact_lock, tenant_tx
from agentplatform.core.ops.tracing import span
from agentplatform.core.orchestration.conversations import conv_lock_key, utcnow
from agentplatform.core.outbox.ratelimit import LocalRateLimiter, RateLimiter
from agentplatform.core.outbox.store import record_sent_message
from agentplatform.core.security.crypto import content_hash
from agentplatform.core.security.keys import decrypt_field

log = logging.getLogger(__name__)

BR_HOLIDAYS_FIXED = {(1, 1), (4, 21), (5, 1), (9, 7), (10, 12), (11, 2), (11, 15), (11, 20), (12, 25)}


def _easter(year: int) -> date:
    """Gregorian Easter (Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 19 * m) // 433
    month = (h + m - 7 * n + 90) // 25
    return date(year, month, (h + m - 7 * n + 33 * month + 19) % 32)


def is_national_holiday(day: date) -> bool:
    return (day.month, day.day) in BR_HOLIDAYS_FIXED or day == _easter(day.year) - timedelta(days=2)      # Good Friday moves every year


# Capability kill switches (scope 'feature'): which capability produced an outbox item, from its effect key.
FEATURES = ("agent", "comments", "followups", "campaigns")
_FEATURE_BY_PREFIX = {"camp:": "campaigns", "cmt:": "comments", "fu:": "followups"}


# Refusals that are about the account: nothing is wrong with this conversation, so the seller gets no handoff per conversation.
_ACCOUNT_STOP_PREFIXES = ("kill_switch:", "token_invalid")
_COMMENT_ACTIONS = ("hide_comment", "public_comment_reply")


def feature_of(effect_key: str) -> str:
    return next((f for p, f in _FEATURE_BY_PREFIX.items() if effect_key.startswith(p)), "agent")


@dataclass
class DispatchPolicy:
    timezone: str = "America/Sao_Paulo"
    quiet_start: dtime = dtime(22, 0)
    quiet_end: dtime = dtime(8, 0)
    holidays_national: bool = True
    holidays_local: tuple[str, ...] = ()     # "MM-DD" every year, "YYYY-MM-DD" once
    deferred_reply_quiet_after: timedelta = timedelta(hours=1)
    private_reply_window: timedelta = timedelta(days=7)
    live: bool = True                    # False in production until go_live: nothing reaches a real customer
    bubble_delay_s: float = 0            # between two messages of the same reply

    def is_holiday(self, day: date) -> bool:
        if self.holidays_national and is_national_holiday(day):
            return True
        return day.isoformat() in self.holidays_local or day.isoformat()[5:] in self.holidays_local

    def is_quiet(self, now: datetime) -> bool:
        local = now.astimezone(ZoneInfo(self.timezone))
        t = local.time()
        return t >= self.quiet_start or t < self.quiet_end or self.is_holiday(local.date())

    def next_allowed(self, now: datetime) -> datetime:
        local = now.astimezone(ZoneInfo(self.timezone))
        cand = local.replace(hour=self.quiet_end.hour, minute=self.quiet_end.minute, second=0, microsecond=0)
        if cand <= local:
            cand += timedelta(days=1)
        while self.is_holiday(cand.date()):
            cand += timedelta(days=1)
        return cand.astimezone(now.tzinfo)


@dataclass
class DispatchOutcome:
    sent: list[UUID] = field(default_factory=list)
    cancelled: dict[UUID, str] = field(default_factory=dict)
    rescheduled: list[UUID] = field(default_factory=list)
    unknown: list[UUID] = field(default_factory=list)
    failed: list[UUID] = field(default_factory=list)
    retry_in: float | None = None        # rate limited: seconds until the rescheduled item is due
    notify: list[str] = field(default_factory=list)   # reasons to tell the seller, handled after the transaction


OnFailure = Callable[[UUID, UUID, str], Awaitable[None]]   # (tenant_id, conversation_id, reason) -> e.g. handoff


class Dispatcher:
    def __init__(self, adapters: dict[str, ChannelAdapter], limiter: RateLimiter | None = None,
                 policy_for: Callable[[UUID], Awaitable[DispatchPolicy]] | None = None,
                 on_failure: OnFailure | None = None, now: Callable[[], datetime] = utcnow):
        self.adapters = adapters
        self.limiter = limiter or LocalRateLimiter()
        self.policy_for = policy_for
        self.on_failure = on_failure
        self.now = now

    async def _policy(self, tenant_id: UUID) -> DispatchPolicy:
        return await self.policy_for(tenant_id) if self.policy_for else DispatchPolicy()

    async def dispatch_conversation(self, tenant_id: UUID, conversation_id: UUID) -> DispatchOutcome:
        out = DispatchOutcome()
        policy = await self._policy(tenant_id)
        sent = False
        while True:
            if sent and policy.bubble_delay_s and await self._more_due(tenant_id, conversation_id):
                await asyncio.sleep(policy.bubble_delay_s)      # outside any transaction; every check runs again for the next bubble
            decision = await self._next_decision(tenant_id, conversation_id, policy, out)
            if self.on_failure:
                while out.notify:
                    await self.on_failure(tenant_id, conversation_id, out.notify.pop(0))
            if decision is None:
                return out
            if decision is _CONTINUE:
                continue
            if isinstance(decision, _RetryAfter):          # short rate-limit wait: sleep outside the transaction
                await asyncio.sleep(decision.seconds)
                continue
            item, account, adapter, msg = decision
            await self._send(tenant_id, conversation_id, item, account, adapter, msg, out)
            sent = True

    async def _more_due(self, tenant_id: UUID, conversation_id: UUID) -> bool:
        async with tenant_tx(tenant_id) as conn:
            row = await (await conn.execute(
                "select 1 from outbox where conversation_id=%s and status='pending' and not_before <= now() limit 1", (conversation_id,))).fetchone()
        return bool(row)

    async def _next_decision(self, tenant_id, conversation_id, policy, out):
        """One transaction: pick the next due item, run checks 1-10, mark it `sending` (check 11) under the lock."""
        async with tenant_tx(tenant_id) as conn:
            await advisory_xact_lock(conn, conv_lock_key(conversation_id))
            item = await (await conn.execute(
                """select * from outbox where conversation_id = %s and status = 'pending' and not_before <= now()
                   order by priority, created_at, effect_key limit 1 for update skip locked""",
                (conversation_id,))).fetchone()
            if item is None:
                return None
            conv = await (await conn.execute(
                """select c.*, ca.channel, ca.external_account_id, ca.external_page_id, ca.token_enc, ca.token_status,
                          ca.id as ca_id, l.opted_out_at
                   from conversations c join channel_accounts ca on ca.id = c.channel_account_id
                   left join leads l on l.contact_id = c.contact_id where c.id = %s""", (conversation_id,))).fetchone()
            now = self.now()
            reason = await self._check(conn, item, conv, policy, now)
            if reason == "reschedule_quiet":
                await conn.execute("update outbox set not_before = %s where id = %s", (policy.next_allowed(now), item["id"]))
                out.rescheduled.append(item["id"])
                return _CONTINUE
            if reason:
                await conn.execute("update outbox set status='cancelled', cancel_reason=%s where id=%s", (reason, item["id"]))
                out.cancelled[item["id"]] = reason
                if reason.startswith(_ACCOUNT_STOP_PREFIXES):
                    await _reopen_turn_inputs(conn, item)
                elif reason == "window_closed" and item["kind"] == "dm_text" and not item["is_proactive"]:
                    out.notify.append("window_closed")      # too late for the assistant; the seller may still reach out
                return _CONTINUE
            adapter = self.adapters[conv["channel"]]
            bucket = {"private_reply": "ig_private_reply"}.get(item["kind"], "ig_text")
            wait = await self.limiter.try_acquire(str(conv["ca_id"]), bucket)
            if 0 < wait <= 2:
                return _RetryAfter(wait)
            if wait > 0:
                await conn.execute("update outbox set not_before = now() + make_interval(secs => %s) where id = %s",
                                   (wait, item["id"]))
                await _keep_bubbles_in_order(conn, item)
                out.rescheduled.append(item["id"])
                out.retry_in = wait
                return None
            await conn.execute(
                "update outbox set status='sending', attempt_registered_at=now(), attempts=attempts+1 where id=%s",
                (item["id"],))
            token = await decrypt_field(conn, tenant_id, conv["token_enc"], "channel_accounts.token") if conv["token_enc"] else None
            account = AccountRef(tenant_id, conv["ca_id"], conv["external_account_id"], conv["external_page_id"], token)
            p = item["payload"]
            msg = OutboundMessage(kind=item["kind"] if item["kind"] != "handoff_expectation" else "dm_text",
                                  recipient_external_id=p.get("recipient"), text=p.get("text"),
                                  comment_id=p.get("comment_id"), quick_replies=p.get("quick_replies"),
                                  template=p.get("template"))
            return item, account, adapter, msg

    async def _check(self, conn, item, conv, policy: DispatchPolicy, now: datetime) -> str | None:
        ks = (await registry.kill_switch_on(conn, tenant_id=item["tenant_id"], channel=conv["channel"], feature="outbox")
              or await registry.kill_switch_on(conn, tenant_id=item["tenant_id"], feature=feature_of(item["effect_key"]),
                                               campaign=item["payload"].get("campaign_id")))
        if ks:                                                                          # 1
            return f"kill_switch:{ks}"
        if not policy.live and conv["channel"] != "simulator":                          # the console simulator is how it gets tested
            return "kill_switch:not_live"
        moderation = item["kind"] == "hide_comment"                                     # not a message to the customer
        if item["send_epoch"] != conv["send_epoch"] and not moderation:                 # 2
            return "superseded"
        if conv["ownership"] != "BOT" and item["kind"] != "handoff_expectation" and not moderation:  # 3
            if not (item["kind"] == "dm_text" and item["payload"].get("is_optout_confirmation")):
                return f"ownership:{conv['ownership']}"
        if conv["opted_out_at"] and not item["payload"].get("is_optout_confirmation") \
                and item["kind"] != "public_comment_reply" and not moderation:         # 4
            return "opt_out"
        if item["kind"] == "template":                                                  # 5 (templates: outside window ok)
            consent = await (await conn.execute(
                """select status from consents where contact_id = %s and purpose = 'marketing' order by at desc limit 1""",
                (conv["contact_id"],))).fetchone()
            if item["payload"].get("campaign_id") and (consent is None or consent["status"] != "granted"):
                return "no_marketing_consent"
        has_window = self.adapters[conv["channel"]].capabilities().service_window is not None
        if item["kind"] in ("dm_text", "handoff_expectation") and has_window:           # 5
            if conv["window_expires_at"] is None or conv["window_expires_at"] <= now:
                return "window_closed"
        if item["kind"] == "private_reply":
            cts = item["payload"].get("comment_ts")
            if cts and datetime.fromisoformat(cts) + policy.private_reply_window <= now:
                return "window_closed"
        # late private replies (queue delay, or old comments the seller chose to answer) respect the quiet hours
        waited_since = min(item["created_at"], datetime.fromisoformat(item["payload"].get("comment_ts") or item["created_at"].isoformat()))
        deferred = item["kind"] == "private_reply" and now - waited_since > policy.deferred_reply_quiet_after
        if (item["is_proactive"] or deferred) and policy.is_quiet(now):                 # 6
            return "reschedule_quiet"
        if item["is_proactive"]:                                                         # 7
            newer = await (await conn.execute(
                "select 1 from messages where conversation_id=%s and direction='in' and provider_ts > %s limit 1",
                (item["conversation_id"], item["created_at"]))).fetchone()
            if newer:
                return "customer_spoke"
        age = (now - item["created_at"]).total_seconds()                                 # 8
        if age > item["max_staleness_s"] and item["source_inbound_at"] is not None:
            newer = await (await conn.execute(
                "select 1 from messages where conversation_id=%s and direction='in' and provider_ts > %s limit 1",
                (item["conversation_id"], item["source_inbound_at"]))).fetchone()
            if newer:
                return "stale"
        if content_hash(item["payload"].get("text") or "") != item["content_hash"]:     # 9
            log.error("outbox content hash mismatch for %s", item["id"])
            return "tampered"
        if conv["token_status"] == "invalid":
            return "token_invalid"
        return None

    async def _send(self, tenant_id, conversation_id, item, account, adapter, msg, out: DispatchOutcome) -> None:
        scope = None
        try:
            with span("channel.send", channel=item["payload"].get("channel"), kind=item["kind"], tenant_id=tenant_id):
                res = await adapter.send(account, msg)
            status, mid, err = "sent", res.external_mid, None
        except AmbiguousSendError as e:
            status, mid, err = "unknown", None, str(e)
        except RateLimitedError as e:
            status, mid, err = "pending_retry", None, str(e)
        except RetryableSendError as e:
            status, mid, err = "pending_retry", None, str(e)
        except TokenInvalidError as e:
            status, mid, err = "failed", None, f"token_invalid: {e}"
            scope = "token"
        except SendError as e:
            status, mid, err = "failed", None, str(e)
            scope = e.scope
        except Exception as e:             # the request may have gone out: treated like a timeout, never resent blindly
            log.exception("unexpected error sending outbox item %s", item["id"])
            status, mid, err = "unknown", None, f"ambiguous: {type(e).__name__}"
        tell_seller = False
        async with tenant_tx(tenant_id) as conn:
            # `status='sending'` everywhere: an echo may already have confirmed this item while the call was in flight
            if status == "sent":
                await conn.execute("update outbox set status='sent', external_mid=%s, sent_at=now() where id=%s and status='sending'",
                                   (mid, item["id"]))
                await record_sent_message(conn, item, mid)
                out.sent.append(item["id"])
            elif status == "unknown":
                await conn.execute("update outbox set status='unknown', last_error=%s where id=%s and status='sending'", (err, item["id"]))
                out.unknown.append(item["id"])
            elif status == "pending_retry":
                await conn.execute(
                    "update outbox set status='pending', last_error=%s, not_before = now() + make_interval(secs => least(300, 2 ^ attempts)) "
                    "where id=%s and status='sending'", (err, item["id"]))
                await _keep_bubbles_in_order(conn, item)
                out.rescheduled.append(item["id"])
            else:
                await conn.execute("update outbox set status='failed', last_error=%s where id=%s and status='sending'", (err, item["id"]))
                out.failed.append(item["id"])
                if scope == "token":
                    await conn.execute("update channel_accounts set token_status='invalid' where id=%s", (account.channel_account_id,))
                if scope == "account":
                    await registry.set_channel_stop(conn, tenant_id, adapter.channel, "channel_error")
                    log.error("channel %s stopped for tenant %s: %s", adapter.channel, tenant_id, err)
                if scope in ("token", "account"):      # answered again as soon as the account can send
                    await _reopen_turn_inputs(conn, item)
                tell_seller = scope not in ("token", "account", "recipient", "object") and item["kind"] not in _COMMENT_ACTIONS
        if scope == "account":
            registry.invalidate_kill_switches()
        if tell_seller and self.on_failure:
            await self.on_failure(tenant_id, conversation_id, err or "send_failed")

    async def reconcile_unknown(self, tenant_id: UUID, outbox_id: UUID) -> str:
        """Called after echo_reconcile_window_s: echo already confirmed? else Conversations API; else failed + handoff."""
        async with tenant_tx(tenant_id) as conn:
            item = await (await conn.execute("select * from outbox where id=%s", (outbox_id,))).fetchone()
            if item is None or item["status"] != "unknown":
                return item["status"] if item else "missing"
            conv = await (await conn.execute(
                """select ca.channel, ca.external_account_id, ca.external_page_id, ca.token_enc, ca.id as ca_id
                   from conversations c join channel_accounts ca on ca.id=c.channel_account_id where c.id=%s""",
                (item["conversation_id"],))).fetchone()
            token = await decrypt_field(conn, tenant_id, conv["token_enc"], "channel_accounts.token") if conv["token_enc"] else None
        adapter = self.adapters[conv["channel"]]
        account = AccountRef(tenant_id, conv["ca_id"], conv["external_account_id"], conv["external_page_id"], token)
        mid = await adapter.find_sent_message(account, item["payload"].get("recipient") or "", item["payload"].get("text") or "",
                                              sent_after=item["attempt_registered_at"])
        async with tenant_tx(tenant_id) as conn:
            if mid:
                done = await (await conn.execute(
                    "update outbox set status='sent', external_mid=%s, sent_at=now() where id=%s and status='unknown' returning id",
                    (mid, outbox_id))).fetchone()
                if done:
                    await record_sent_message(conn, item, mid)
                return "sent"
            await conn.execute("update outbox set status='failed', last_error='ambiguous_unconfirmed' where id=%s and status='unknown'",
                               (outbox_id,))
        if self.on_failure:
            await self.on_failure(tenant_id, item["conversation_id"], "ambiguous_send_unconfirmed")
        return "failed"


async def _keep_bubbles_in_order(conn, item) -> None:
    """A delayed bubble holds back the rest of its reply: otherwise the later part of the message arrives first."""
    if item["turn_id"] is None:
        return
    await conn.execute(
        """update outbox o set not_before = greatest(o.not_before, d.not_before) from outbox d
           where d.id = %s and o.turn_id = d.turn_id and o.id <> d.id and o.status = 'pending'""", (item["id"],))


async def _reopen_turn_inputs(conn, item) -> None:
    """The account could not send this reply, through no fault of the conversation: the customer's messages go back to pending and
    are answered when it can send again. Not when part of the reply already went out (the customer would read it twice)."""
    if item["turn_id"] is None or item["kind"] != "dm_text" or item["is_proactive"]:
        return
    went_out = await (await conn.execute(
        "select 1 from outbox where turn_id=%s and status in ('sent','sending','unknown') limit 1", (item["turn_id"],))).fetchone()
    if not went_out:
        await conn.execute("update messages set turn_id = null where turn_id = %s and direction = 'in'", (item["turn_id"],))


class _Continue:
    pass


@dataclass
class _RetryAfter:
    seconds: float


_CONTINUE = _Continue()
