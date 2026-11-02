"""Webhook persistence (fast-ack) and inbound event routing (plan 07 §1).

persist_events(): verify -> normalize -> insert inbound_events (idempotent) -> 200. Nothing else synchronous.
route_event(): runs in the `ingress` worker: contact/conversation, message, stop cadences, reopen CLOSED, then
returns what to schedule next (debounce close_batch, echo check, comment automation).
"""
from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from uuid import UUID

from agentplatform.core.agent.guards import extract_numbers, normalize, privacy_gate
from agentplatform.core.channels.events import CanonicalEvent
from agentplatform.core.channels.ports import ChannelAdapter
from agentplatform.core.comments.catalog import ensure_media
from agentplatform.core.config.schema import ComplianceConfig
from agentplatform.core.db.pool import advisory_xact_lock, platform_tx, tenant_tx
from agentplatform.core.lgpd.checkpoints import delete_checkpoints
from agentplatform.core.orchestration import dm_scope
from agentplatform.core.orchestration.conversations import (
    conv_lock_key,
    get_or_create_conversation,
    stop_followups,
    store_inbound_message,
    transition_ownership,
)
from agentplatform.core.security.keys import decrypt_field, encrypt_field
from agentplatform.core.security.pii import mask
from agentplatform.settings import get_settings

log = logging.getLogger(__name__)
_EVENT_TEXT = "inbound_events.text"
_DELETED_TEXT = "[mensagem apagada]"


@dataclass
class Routed:
    action: str                      # debounce | echo | comment | none
    tenant_id: UUID
    conversation_id: UUID | None = None
    event_id: int | None = None
    has_pending_media: bool = False
    media_to_hydrate: str | None = None   # story first seen in a reply: fetch its details off the critical path
    username_missing: bool = False


async def resolve_account(channel: str, external_id: str) -> dict | None:
    async with platform_tx() as conn:
        return await (await conn.execute(
            """select id, tenant_id from channel_accounts where channel = %s
               and (external_account_id = %s or external_page_id = %s)""", (channel, external_id, external_id))).fetchone()


async def persist_events(events: list[CanonicalEvent]) -> list[tuple[UUID, int]]:
    """Returns [(tenant_id, inbound_event_id)] for newly inserted (non-duplicate) events."""
    created = []
    for ev in events:
        acc = await resolve_account(ev.channel, ev.account_external_id)
        if acc is None:
            log.warning("event for unknown account %s ignored", ev.account_external_id)
            continue
        canonical = ev.model_dump(mode="json")
        async with tenant_tx(acc["tenant_id"]) as conn:
            text = canonical.pop("text", None)
            if text:                 # the log of events is not where a customer's words live: encrypted, and only until processed
                canonical["text_masked"] = mask(text).text
                blob = await encrypt_field(conn, acc["tenant_id"], text, _EVENT_TEXT)
                canonical["text_enc"] = base64.b64encode(blob).decode("ascii")
            row = await (await conn.execute(
                """insert into inbound_events (tenant_id, channel_account_id, idempotency_key, event_type, provider_ts, payload_canonical)
                   values (%s,%s,%s,%s,%s,%s) on conflict (channel_account_id, idempotency_key) do nothing returning id""",
                (acc["tenant_id"], acc["id"], ev.idempotency_key, ev.event_type, ev.provider_ts, json.dumps(canonical)))).fetchone()
        if row:
            created.append((acc["tenant_id"], row["id"]))
    return created


async def decode_event(conn, tenant_id: UUID, payload: dict) -> CanonicalEvent:
    p = dict(payload)
    enc = p.pop("text_enc", None)
    p.pop("text_masked", None)
    if enc:
        p["text"] = await decrypt_field(conn, tenant_id, base64.b64decode(enc), _EVENT_TEXT)
    return CanonicalEvent.model_validate(p)


async def load_event(conn, tenant_id: UUID, event_id: int) -> CanonicalEvent:
    row = await (await conn.execute("select payload_canonical from inbound_events where id=%s", (event_id,))).fetchone()
    return await decode_event(conn, tenant_id, row["payload_canonical"])


async def scrub_event(conn, event_id: int) -> None:
    """Processed: the message (encrypted, under its own retention) is the record. What identifies the event stays, its contents go."""
    await conn.execute(
        """update inbound_events set payload_raw = null,
                  payload_canonical = payload_canonical - 'text' - 'text_enc' - 'text_masked' - 'attachments' - 'author_username'
           where id = %s""", (event_id,))


async def _done(conn, event_id: int) -> None:
    await conn.execute("update inbound_events set status='done' where id=%s", (event_id,))
    await scrub_event(conn, event_id)


async def _apply_deletion(conn, conversation_id: UUID, channel_account_id: UUID, mid: str | None) -> None:
    """The sender deleted it: the provider's rules say it goes, in the message, in the event that brought it, in the checkpoints of
    the turn that read it and in the summary that may quote it. A pending message is simply never answered."""
    if not mid:
        return
    msg = await (await conn.execute(
        """update messages set text_enc = null, text_masked = %s, media = null, kind = 'deleted'
           where conversation_id = %s and external_mid = %s returning tenant_id, turn_id""",
        (_DELETED_TEXT, conversation_id, mid))).fetchone()
    await conn.execute(
        "update inbound_events set payload_raw = null, payload_canonical = '{}'::jsonb where channel_account_id = %s and idempotency_key = %s",
        (channel_account_id, f"msg:{mid}"))
    if msg:
        await conn.execute("update conversations set summary = null where id = %s", (conversation_id,))
        if msg["turn_id"]:
            await delete_checkpoints(conn, f"{msg['tenant_id']}:{conversation_id}:{msg['turn_id']}")


def is_correction(old: str | None, new: str, similarity: float) -> bool:
    """Other numbers ("300 mil" to "400 mil") or other words change what the customer asked. A typo fixed does not."""
    a, b = normalize(old or ""), normalize(new)
    return extract_numbers(a) != extract_numbers(b) or SequenceMatcher(None, a, b).ratio() < similarity


async def _apply_edit(conn, tenant_id: UUID, conversation_id: UUID, mid: str | None, text: str | None, similarity: float) -> bool:
    """A pending message is answered with what the customer meant in the end. One already answered goes back to pending when the edit
    is a correction: the assistant must not go on believing the old version. True when the message needs a turn."""
    if not (mid and text):
        return False
    masked = mask(text).text
    old = await (await conn.execute(
        "select text_masked, turn_id from messages where conversation_id = %s and external_mid = %s and direction = 'in'",
        (conversation_id, mid))).fetchone()
    if not old:
        return False
    answer_again = old["turn_id"] is None or is_correction(old["text_masked"], masked, similarity)
    await conn.execute(
        """update messages set text_masked = %s, text_enc = %s, turn_id = case when %s then null else turn_id end
           where conversation_id = %s and external_mid = %s and direction = 'in'""",
        (masked, await encrypt_field(conn, tenant_id, text, "messages.text"), answer_again, conversation_id, mid))
    return answer_again


async def _forget(conn, tenant_id: UUID, event_id: int) -> Routed:
    """Out of the assistant's scope: the event is marked handled and its contents dropped."""
    await conn.execute("update inbound_events set status='ignored', payload_raw=null, payload_canonical='{}'::jsonb where id=%s", (event_id,))
    return Routed("none", tenant_id)


ENTRYPOINT = {"StoryReply": "story_reply", "StoryMention": "story_mention", "Referral": "ad_referral",
              "Postback": "icebreaker", "MessageReceived": "dm"}


async def route_event(tenant_id: UUID, event_id: int, adapter: ChannelAdapter | Mapping[str, ChannelAdapter],
                      initial_stage: str, compliance: ComplianceConfig | None = None, edit_similarity: float = 0.85,
                      reopen_closed: bool = True) -> Routed:
    """`adapter` may be the runtime's adapters by channel; the event's own channel decides which capabilities apply.
    `compliance` turns on the direct scope for channels with a mixed inbox (ADR-019); the ingress job always passes it."""
    async with tenant_tx(tenant_id) as conn:
        row = await (await conn.execute(
            "select * from inbound_events where id = %s for update skip locked", (event_id,))).fetchone()
        if row is None or row["status"] not in ("pending", "failed"):
            return Routed("none", tenant_id)
        ev = await decode_event(conn, tenant_id, row["payload_canonical"])
        if isinstance(adapter, Mapping):
            adapter = adapter[ev.channel]
        caps = adapter.capabilities()

        if ev.event_type == "CommentReceived":
            await conn.execute("update inbound_events set status='done' where id=%s", (event_id,))
            return Routed("comment", tenant_id, event_id=event_id)

        if ev.contact_external_id is None:
            await conn.execute("update inbound_events set status='ignored' where id=%s", (event_id,))
            return Routed("none", tenant_id)

        gated = compliance is not None and caps.mixed_inbox
        leave_to_seller = False
        asks_to_stop = gated and ev.is_customer_message and privacy_gate([ev.text or ""], compliance).opt_out
        if gated and not asks_to_stop and await dm_scope.flag_of(conn, row["channel_account_id"], ev.contact_external_id) == "blocked":
            return await _forget(conn, tenant_id, event_id)      # "never serve": nothing more of this person is kept, not even the owner's replies
        if gated and not await dm_scope.has_conversation(conn, row["channel_account_id"], ev.channel, ev.contact_external_id):
            if ev.event_type == "MessageEcho":      # the owner talking to someone: from now on the assistant stays away from this person
                if ev.sender_app_id not in get_settings().automation_app_ids:     # the account's own automation is not the owner talking
                    await dm_scope.set_flag(conn, tenant_id, row["channel_account_id"], ev.contact_external_id, "known", "seller_message")
                return await _forget(conn, tenant_id, event_id)
            if not ev.is_customer_message or dm_scope.is_shared_only(ev):
                return await _forget(conn, tenant_id, event_id)
            if not await dm_scope.entry_allowed(conn, row["channel_account_id"], ev):
                # Out of scope: nothing of it is kept. A request to stop still counts (privacy gate), without any reply.
                if not asks_to_stop:
                    return await _forget(conn, tenant_id, event_id)
                leave_to_seller = True
        conv = await get_or_create_conversation(conn, tenant_id, row["channel_account_id"], ev.channel,
                                                ev.contact_external_id, initial_stage,
                                                entrypoint=ENTRYPOINT.get(ev.event_type))
        await conn.execute("update inbound_events set conversation_id=%s where id=%s", (conv["id"], event_id))

        if ev.event_type == "MessageEcho":
            await conn.execute("update inbound_events set status='done' where id=%s", (event_id,))
            return Routed("echo", tenant_id, conv["id"], event_id)

        if ev.event_type == "MessageDeleted":
            await advisory_xact_lock(conn, conv_lock_key(conv["id"]))
            await _apply_deletion(conn, conv["id"], row["channel_account_id"], ev.mid)
            await _done(conn, event_id)
            return Routed("none", tenant_id, conv["id"], event_id)

        if ev.event_type == "MessageEdited":
            await advisory_xact_lock(conn, conv_lock_key(conv["id"]))
            needs_turn = await _apply_edit(conn, tenant_id, conv["id"], ev.mid, ev.text, edit_similarity)
            await _done(conn, event_id)
            return Routed("debounce" if needs_turn else "none", tenant_id, conv["id"], event_id)

        if ev.event_type == "ConversationStatusChanged" and (ev.referral or {}).get("handover"):
            ours = get_settings().meta_app_id or ""
            new_owner = ev.referral.get("new_owner_app_id")
            if new_owner and new_owner != ours and conv["ownership"] == "BOT":
                await transition_ownership(conn, tenant_id, conv["id"], "HUMAN", cause="conversation_routing",
                                           evidence={"new_owner_app_id": new_owner})
            elif new_owner == ours and conv["ownership"] in ("HUMAN", "AWAITING_HUMAN"):
                await transition_ownership(conn, tenant_id, conv["id"], "BOT", cause="conversation_routing_return")
            await _done(conn, event_id)
            return Routed("none", tenant_id, conv["id"], event_id)

        if ev.event_type in ("Reaction", "Read", "ConversationStatusChanged"):
            st = (ev.referral or {}).get("status")
            if ev.mid and st in ("sent", "delivered", "read", "failed"):      # delivery receipt of a message we sent
                from agentplatform.core.campaigns.engine import apply_delivery_status
                await apply_delivery_status(conn, tenant_id, ev.mid, st, (ev.referral or {}).get("errors"))
            await _done(conn, event_id)
            return Routed("none", tenant_id, conv["id"], event_id)

        # customer message: text, story reply/mention, ad referral, postback/ice breaker
        await advisory_xact_lock(conn, conv_lock_key(conv["id"]))
        media = None
        pending_media = False
        if ev.attachments:
            media = {"attachments": [a.model_dump() for a in ev.attachments], "transcript": None}
            pending_media = any(a.type in ("audio", "image", "file") and a.url for a in ev.attachments)
            media["pending"] = pending_media
        text = ev.text or (f"[clique: {ev.postback_payload}]" if ev.event_type == "Postback" and not ev.text else None)
        media_kinds = {a.type for a in ev.attachments}
        kind = next((k for k in ("audio", "image", "file", "video") if k in media_kinds), None) \
            or {"Postback": "postback", "Referral": "referral"}.get(ev.event_type, "text")
        shared_only = gated and dm_scope.is_shared_only(ev)
        if shared_only:
            kind = "shared"                  # history only: never a turn input
        await store_inbound_message(conn, tenant_id, conv["id"], mid=ev.mid, kind=kind, text=text, media=media,
                                    provider_ts=ev.provider_ts, inbound_event_id=event_id,
                                    service_window=caps.service_window)
        if leave_to_seller:
            await transition_ownership(conn, tenant_id, conv["id"], "HUMAN", cause="dm_scope")
        elif ev.standby and conv["ownership"] == "BOT":        # another app owns this thread: we would be refused, the seller must know
            await transition_ownership(conn, tenant_id, conv["id"], "HUMAN", cause="standby")
        if shared_only:
            await _done(conn, event_id)
            return Routed("none", tenant_id, conv["id"], event_id)
        # stop cadences right away (review #10)
        await stop_followups(conn, conv["id"], "inbound_message")
        # attribution
        lead = await (await conn.execute("select id from leads where contact_id=%s", (conv["contact_id"],))).fetchone()
        first = not await (await conn.execute("select 1 from lead_sources where lead_id=%s limit 1", (lead["id"],))).fetchone()
        if first or ev.referral or ev.story_id:
            ref = ev.referral or {}
            await conn.execute(
                """insert into lead_sources (tenant_id, lead_id, channel, entrypoint, media_id, ad_id, campaign, utm, is_first_touch)
                   values (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (tenant_id, lead["id"], ev.channel, ENTRYPOINT.get(ev.event_type, "dm"), ev.story_id, ref.get("ad_id"), ref.get("ref"),
                 json.dumps(ref) if ref else None, first))
        hydrate = None
        if ev.story_id:
            _, created = await ensure_media(conn, tenant_id, row["channel_account_id"], ev.story_id, "story")
            hydrate = ev.story_id if created else None
        ident = await (await conn.execute(
            "select username, username_checked_at from contact_identities where contact_id=%s and channel=%s",
            (conv["contact_id"], ev.channel))).fetchone()
        if conv["ownership"] == "CLOSED":
            if not reopen_closed:      # recorded, and the conversation stays closed until a person opens it
                await _done(conn, event_id)
                return Routed("none", tenant_id, conv["id"], event_id)
            await transition_ownership(conn, tenant_id, conv["id"], "BOT", cause="reopened_by_inbound")
        await _done(conn, event_id)
        return Routed("debounce", tenant_id, conv["id"], event_id, has_pending_media=pending_media, media_to_hydrate=hydrate,
                      username_missing=bool(ident and not ident["username"] and not ident["username_checked_at"]))
