"""Who the assistant may talk to in the direct (ADR-019). A channel marked mixed_inbox can hold the owner's friends and group chats next to
customers. Fails closed: until the list of people who already talk to the account is complete, nobody new is served. Messages of people
out of scope are not stored at all; only a keyed hash of their id is kept, so the assistant keeps away from them."""
from __future__ import annotations

import logging
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.channels.events import CanonicalEvent
from agentplatform.core.channels.ports import AccountRef
from agentplatform.core.config.schema import CommentsConfig
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.security.keys import crypto

log = logging.getLogger(__name__)
SCOPES = ("origin_only", "origin_and_new", "all")
_INTENTIONAL = ("StoryReply", "Referral", "Postback")     # the person answered something the business published
_NO_TURN_ATTACHMENTS = ("share", "story_mention", "sticker", "unsupported")
_DEFAULT = {"scope": "origin_only", "contacts_synced_at": None, "contacts_cursor": None, "contacts_count": 0, "quick_questions": [],
            "quick_questions_published_at": None}


def contact_hash(external_user_id: str) -> bytes:
    return crypto().lookup_hash(external_user_id, kind="igsid")


async def load_scope(conn: AsyncConnection, channel_account_id: UUID) -> dict:
    row = await (await conn.execute("select * from dm_settings where channel_account_id=%s", (channel_account_id,))).fetchone()
    return row or dict(_DEFAULT)


async def flag_of(conn: AsyncConnection, channel_account_id: UUID, external_user_id: str) -> str | None:
    row = await (await conn.execute("select status from contact_flags where channel_account_id=%s and identifier_hash=%s",
                                    (channel_account_id, contact_hash(external_user_id)))).fetchone()
    return row["status"] if row else None


async def set_flag(conn: AsyncConnection, tenant_id: UUID, channel_account_id: UUID, external_user_id: str, status: str, source: str,
                   actor: UUID | None = None) -> None:
    """A seller's choice replaces anything; automatic sources never replace an existing flag."""
    conflict = ("do update set status=excluded.status, source=excluded.source, updated_by=excluded.updated_by, updated_at=now()"
                if source == "console" else "do nothing")
    await conn.execute(
        f"""insert into contact_flags (tenant_id, channel_account_id, identifier_hash, status, source, updated_by)
            values (%s,%s,%s,%s,%s,%s) on conflict (channel_account_id, identifier_hash) {conflict}""",
        (tenant_id, channel_account_id, contact_hash(external_user_id), status, source, actor))


async def has_conversation(conn: AsyncConnection, channel_account_id: UUID, channel: str, external_user_id: str) -> bool:
    return bool(await (await conn.execute(
        """select 1 from conversations c join contact_identities ci on ci.contact_id = c.contact_id
           where c.channel_account_id=%s and ci.channel=%s and ci.external_user_id=%s limit 1""",
        (channel_account_id, channel, external_user_id))).fetchone())


def is_shared_only(ev: CanonicalEvent) -> bool:
    """A shared reel, a story mention, a sticker, a heart on a story: nothing to answer."""
    if ev.event_type == "StoryReply" and not any(c.isalnum() for c in ev.text or ""):
        return all(a.type in _NO_TURN_ATTACHMENTS for a in ev.attachments)
    if (ev.text or "").strip() or ev.event_type in _INTENTIONAL:     # someone opening through an ad or link, even without words
        return False
    return ev.event_type == "StoryMention" or all(a.type in _NO_TURN_ATTACHMENTS for a in ev.attachments)


async def entry_allowed(conn: AsyncConnection, channel_account_id: UUID, ev: CanonicalEvent) -> bool:
    """First message of someone without a conversation here."""
    flag = await flag_of(conn, channel_account_id, ev.contact_external_id or "")
    if flag == "blocked":
        return False
    s = await load_scope(conn, channel_account_id)
    if s["scope"] == "all" or flag == "allowed":
        return True
    if not s["contacts_synced_at"] or flag == "known":
        return False
    if ev.event_type in _INTENTIONAL or ev.referral or ev.story_id:
        return True
    return s["scope"] == "origin_and_new"


async def comment_block(conn: AsyncConnection, channel_account_id: UUID, channel: str, external_user_id: str) -> str | None:
    """Why a comment must not open or continue a conversation automatically, as the decision to record (None = may go on)."""
    flag = await flag_of(conn, channel_account_id, external_user_id)
    if flag == "blocked":
        return "blocked_contact"
    if await has_conversation(conn, channel_account_id, channel, external_user_id):
        return None
    s = await load_scope(conn, channel_account_id)
    if s["scope"] == "all" or flag == "allowed":
        return None
    if not s["contacts_synced_at"]:
        return "contacts_pending"
    return "known_contact" if flag == "known" else None


async def restart_snapshot(conn: AsyncConnection, tenant_id: UUID, channel_account_id: UUID) -> None:
    """Read the list again from the start (it can come back short until the provider grants full access). Flags already recorded
    stay; until it finishes nobody new is served, the same as before the first list."""
    await conn.execute(
        """insert into dm_settings (channel_account_id, tenant_id) values (%s,%s)
           on conflict (channel_account_id) do update set contacts_synced_at = null, contacts_cursor = null, contacts_count = 0""",
        (channel_account_id, tenant_id))


async def snapshot_contacts(tenant_id: UUID, adapter, account: AccountRef, cfg: CommentsConfig) -> bool:
    """Stores who already talks to this account, page by page and resumable. Reads participants only, never messages. True when complete."""
    async with tenant_tx(tenant_id) as conn:
        s = await load_scope(conn, account.channel_account_id)
    if s["contacts_synced_at"]:
        return True
    cursor = s["contacts_cursor"]
    for _ in range(cfg.contacts_max_pages_per_run):
        ids, cursor = await adapter.list_conversation_contacts(account, cursor, cfg.contacts_page_size)   # network outside the transaction
        async with tenant_tx(tenant_id) as conn:
            for external_id in ids:
                await set_flag(conn, tenant_id, account.channel_account_id, external_id, "known", "snapshot")
            await conn.execute(
                """insert into dm_settings (channel_account_id, tenant_id, contacts_cursor, contacts_count, contacts_synced_at)
                   values (%(ca)s,%(t)s,%(c)s,%(n)s,case when %(c)s::text is null then now() end)
                   on conflict (channel_account_id) do update set contacts_cursor=%(c)s, contacts_count=dm_settings.contacts_count+%(n)s,
                     contacts_synced_at=case when %(c)s::text is null then now() end""",
                {"ca": account.channel_account_id, "t": tenant_id, "c": cursor, "n": len(ids)})
        if cursor is None:
            return True
    return False
