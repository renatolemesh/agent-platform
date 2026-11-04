"""Posts of a channel account (ADR-018): what the console lists and what comments refer to. Filled by a periodic sync and
by events (an ad post or a live only shows up when someone comments). Comment polling is the safety net for lost webhooks:
same idempotency key as the webhook, so a comment is processed once whichever arrives first. Network calls run outside
transactions; job arguments carry IDs only."""
from __future__ import annotations

import logging
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.channels.ports import (
    AccountRef,
    MediaInfo,
    RateLimitedError,
    RetryableSendError,
    SendError,
    TokenInvalidError,
)
from agentplatform.core.config.schema import CommentsConfig
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.security.keys import decrypt_field
from agentplatform.core.security.pii import mask

log = logging.getLogger(__name__)
CATALOG_ERRORS = (TokenInvalidError, RateLimitedError, RetryableSendError, SendError)
_POLLED_KINDS = ("post", "reel", "ad")


async def ensure_media(conn: AsyncConnection, tenant_id: UUID, channel_account_id: UUID, external_media_id: str,
                       kind: str = "post", *, ad_id: str | None = None, ad_title: str | None = None,
                       original_media_id: str | None = None) -> tuple[dict, bool]:
    """Returns (row, created). A post first seen in an event still needs its details (hydrate_media job)."""
    row = await (await conn.execute(
        """insert into channel_media (tenant_id, channel_account_id, external_media_id, media_kind, ad_id, ad_title, original_media_id)
           values (%s,%s,%s,%s,%s,%s,%s) on conflict (tenant_id, external_media_id) do nothing returning *""",
        (tenant_id, channel_account_id, external_media_id, kind, ad_id, ad_title, original_media_id))).fetchone()
    if row:
        return row, True
    return await (await conn.execute("select * from channel_media where tenant_id=%s and external_media_id=%s",
                                      (tenant_id, external_media_id))).fetchone(), False


async def _upsert(conn: AsyncConnection, tenant_id: UUID, channel_account_id: UUID, m: MediaInfo) -> None:
    await conn.execute(
        """insert into channel_media (tenant_id, channel_account_id, external_media_id, media_kind, media_type, caption_masked,
                                      permalink, image_url, posted_at, comments_count, synced_at)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
           on conflict (tenant_id, external_media_id) do update set
             media_type=excluded.media_type, caption_masked=excluded.caption_masked, permalink=excluded.permalink,
             image_url=excluded.image_url, posted_at=excluded.posted_at, comments_count=excluded.comments_count, synced_at=now(), removed_at=null""",
        (tenant_id, channel_account_id, m.external_id, m.kind, m.media_type, mask(m.caption).text if m.caption else None,
         m.permalink, m.image_url, m.posted_at, m.comments_count))


async def accounts(tenant_id: UUID) -> list[tuple[str, AccountRef]]:
    """Connected accounts with a usable token, as (channel, account)."""
    async with tenant_tx(tenant_id) as conn:
        rows = await (await conn.execute(
            """select id, channel, external_account_id, external_page_id, token_enc from channel_accounts
               where tenant_id=%s and token_enc is not null and token_status = 'valid'""", (tenant_id,))).fetchall()
        return [(r["channel"], AccountRef(tenant_id, r["id"], r["external_account_id"], r["external_page_id"],
                                          await decrypt_field(conn, tenant_id, r["token_enc"], "channel_accounts.token")))
                for r in rows]


async def account_of_media(conn: AsyncConnection, tenant_id: UUID, external_media_id: str) -> tuple[str, AccountRef, dict] | None:
    r = await (await conn.execute(
        """select m.*, ca.channel, ca.external_account_id, ca.external_page_id, ca.token_enc from channel_media m
           join channel_accounts ca on ca.id = m.channel_account_id where m.tenant_id=%s and m.external_media_id=%s""",
        (tenant_id, external_media_id))).fetchone()
    if not r:
        return None
    token = await decrypt_field(conn, tenant_id, r["token_enc"], "channel_accounts.token") if r["token_enc"] else None
    return r["channel"], AccountRef(tenant_id, r["channel_account_id"], r["external_account_id"], r["external_page_id"], token), r


async def sync_account(tenant_id: UUID, adapter, account: AccountRef, cfg: CommentsConfig) -> int:
    """Refreshes the posts and active stories of one account. Raises CATALOG_ERRORS for the caller to report."""
    media = await adapter.list_media(account, cfg.catalog_max_media)
    try:
        media += await adapter.list_stories(account)
    except CATALOG_ERRORS as e:      # stories are a bonus: they never cost the posts
        log.info("stories unavailable for tenant %s: %s", tenant_id, type(e).__name__)
    async with tenant_tx(tenant_id) as conn:
        for m in media:
            await _upsert(conn, tenant_id, account.channel_account_id, m)
    return len(media)


async def poll_account(tenant_id: UUID, adapter, account: AccountRef, cfg: CommentsConfig) -> list[tuple[UUID, int]]:
    """New comments of recent posts whose count changed. Returns the (tenant, event) pairs to route."""
    from agentplatform.core.orchestration.ingress import persist_events
    async with tenant_tx(tenant_id) as conn:
        rows = await (await conn.execute(
            """select external_media_id, media_kind, comments_count from channel_media
               where channel_account_id=%s and media_kind = any(%s) and coalesce(posted_at, created_at) > now() - make_interval(days => %s)
                 and (comments_seen is null or comments_count is distinct from comments_seen)
               order by coalesce(posted_at, created_at) desc limit %s""",
            (account.channel_account_id, list(_POLLED_KINDS), cfg.poll_lookback_days, cfg.poll_max_media))).fetchall()
    created = []
    for r in rows:
        events = await adapter.list_comments(account, r["external_media_id"], r["media_kind"], cfg.poll_page_size)
        own = (account.external_account_id, account.external_page_id)
        created += await persist_events([e for e in events if e.contact_external_id not in own])
        async with tenant_tx(tenant_id) as conn:
            await conn.execute("update channel_media set comments_seen=%s, polled_at=now() where tenant_id=%s and external_media_id=%s",
                               (r["comments_count"], tenant_id, r["external_media_id"]))
    return created


async def hydrate(tenant_id: UUID, adapter, external_media_id: str) -> bool:
    async with tenant_tx(tenant_id) as conn:
        found = await account_of_media(conn, tenant_id, external_media_id)
    if not found:
        return False
    _, account, row = found
    info = await adapter.get_media(account, external_media_id)
    if info is None:
        return False
    async with tenant_tx(tenant_id) as conn:      # keeps the kind seen in the event (an ad's product type says FEED)
        await conn.execute(
            """update channel_media set media_type=%s, caption_masked=%s, permalink=%s, image_url=%s, posted_at=%s, comments_count=%s,
                      synced_at=now() where tenant_id=%s and external_media_id=%s""",
            (info.media_type, mask(info.caption).text if info.caption else None, info.permalink, info.image_url, info.posted_at,
             info.comments_count, tenant_id, external_media_id))
    return bool(row)
