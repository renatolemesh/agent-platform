"""Publishes approved posts (ADR-024), one step per run. Creating a container publishes nothing, so that part may be repeated. From the
moment the publish call is about to leave, the post may exist: it is never sent again, and only the container's status can say what happened."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from uuid import UUID

from agentplatform.core.channels.ports import (
    AccountRef,
    AmbiguousSendError,
    PostItem,
    RetryableSendError,
    SendError,
    TokenInvalidError,
)
from agentplatform.core.comments.catalog import ensure_media
from agentplatform.core.comments.settings import render_text
from agentplatform.core.config.registry import registry
from agentplatform.core.config.schema import PlatformConfig, PostsConfig
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.posts.drafts import caption_problems, content_hash, file_problems, hash_token, load

_MEDIA_KIND = {"feed": "post", "reel": "reel", "story": "story"}
FEATURE = "posts"
_RECENT_POSTS = 10


class _Stop(Exception):
    """The attempt is over and nothing was published."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


def publish_block(c: PostsConfig, adapter, account: dict | None) -> str | None:
    """Why nothing can be approved or published right now."""
    if not c.publishing_enabled:
        return "publishing_off"
    if account is None:
        return "no_account"
    if adapter is None or not hasattr(adapter, "publish_post"):
        return "unsupported"
    scope = getattr(adapter, "publish_scope", None)
    if scope and scope not in (account.get("scopes") or []):
        return "missing_permission"
    return None


async def due_posts(conn, tenant_id: UUID, c: PostsConfig) -> list:
    """With publishing off, only what may already have been sent goes on, to find out what happened to it."""
    rows = await (await conn.execute(
        """select id from post_drafts where tenant_id=%s and (publish_started_at is not null and status='publishing'
             or %s and (status='publishing' or status='approved' and scheduled_at <= now()))""",
        (tenant_id, c.publishing_enabled))).fetchall()
    return [("post_publish", {"tenant_id": str(tenant_id), "post_id": str(r["id"])}, {"queueing_lock": f"post:{r['id']}", "lock": f"post:{r['id']}"})
            for r in rows]


async def _audit(conn, tid: UUID, action: str, post_id: UUID, after: dict) -> None:
    await conn.execute("insert into audit_log (tenant_id, actor_kind, action, target, after) values (%s,'system',%s,%s,%s)",
                       (tid, action, str(post_id), json.dumps(after)))


async def _close(tid: UUID, post_id: UUID, status: str, code: str, detail: str = "") -> None:
    async with tenant_tx(tid) as conn:
        await conn.execute("update post_drafts set status=%s, error=%s, error_detail=%s, updated_at=now() where id=%s",
                           (status, code, detail[:300] or None, post_id))
        await conn.execute("update post_draft_files set public_token_hash=null where draft_id=%s", (post_id,))
        await _audit(conn, tid, f"posts.{status}", post_id, {"error": code})


async def finish(tid: UUID, p: dict, media_id: str | None) -> list:
    async with tenant_tx(tid) as conn:
        await conn.execute("update post_drafts set status='published', external_media_id=%s, published_at=now(), error=null, error_detail=null, updated_at=now() where id=%s",
                           (media_id, p["id"]))
        await conn.execute("update post_draft_files set public_token_hash=null where draft_id=%s", (p["id"],))
        await conn.execute("delete from post_file_chunks c using post_draft_files f where c.file_id=f.id and f.draft_id=%s", (p["id"],))
        await _audit(conn, tid, "posts.published", p["id"], {"media": media_id})
        if media_id:
            await ensure_media(conn, tid, p["channel_account_id"], media_id, _MEDIA_KIND[p["kind"]])
    return [("hydrate_media", {"tenant_id": str(tid), "media_id": media_id}, {})] if media_id else []


async def _find_published(p: dict, caption: str, adapter, account: AccountRef) -> str | None:
    """The answer with its id was lost. The list has it; if not, the 5-minute sync brings the post in anyway. A story has no text to recognise."""
    if p["kind"] == "story":
        return None
    since = p["publishing_since"] - timedelta(minutes=1)
    try:
        listed = await adapter.list_media(account, _RECENT_POSTS)
    except SendError:
        return None
    return next((m.external_id for m in listed if (m.caption or "") == caption and m.posted_at and m.posted_at >= since), None)


async def _settle(tid: UUID, p: dict, caption: str, adapter, account: AccountRef | None, c: PostsConfig, now: datetime) -> list:
    """Reads only. Whatever is short of certain, after the wait, goes to a person: never a second attempt."""
    expired = now - p["publish_started_at"] > timedelta(minutes=c.container_wait_min)
    status = None
    if account is not None and adapter is not None:
        try:
            status = await adapter.post_status(account, p["container_id"])
        except SendError:
            status = None
    if status == "PUBLISHED":
        return await finish(tid, p, await _find_published(p, caption, adapter, account))
    if status in ("ERROR", "EXPIRED"):
        await _close(tid, p["id"], "failed", "container_error", status)
    elif expired:
        await _close(tid, p["id"], "unknown", "unconfirmed", status or "")
    return []


async def _refusal(conn, p: dict, cfg: PlatformConfig, adapter, connected: dict, account: AccountRef | None, allowed_urls: list[str], now: datetime) -> str | None:
    c = cfg.operations.posts
    block = publish_block(c, adapter, connected if account is not None else None)
    if block:
        return block
    if p["approved_hash"] != content_hash(p["kind"], p["caption"], p["files"]):
        return "changed_after_approval"
    if caption_problems(cfg, p["caption"], allowed_urls):
        return "caption_refused"
    if file_problems(p["kind"], p["files"], c):
        return "files_refused"
    if p["status"] == "approved":
        if now - p["scheduled_at"] > timedelta(minutes=c.late_after_min):
            return "late"
        n = await (await conn.execute("select count(*) as n from post_drafts where status='published' and published_at > now() - interval '1 day'")).fetchone()
        if n["n"] >= c.max_per_day:
            return "daily_limit"
    return None


async def _item_url(tid: UUID, file_id: UUID, base_url: str) -> str:
    token = secrets.token_urlsafe(32)
    async with tenant_tx(tid) as conn:
        await conn.execute("update post_draft_files set public_token_hash=%s where id=%s", (hash_token(token), file_id))
    return f"{base_url.rstrip('/')}/pm/{token}"


async def _ready(adapter, account: AccountRef, containers: list[str]) -> bool:
    ready = True
    for container in containers:
        status = await adapter.post_status(account, container)
        if status in ("ERROR", "EXPIRED"):
            raise _Stop("container_error", status)
        ready = ready and status in ("FINISHED", "PUBLISHED")
    return ready


async def _build(tid: UUID, p: dict, caption: str, adapter, account: AccountRef, base_url: str) -> str | None:
    """The container to publish, once the provider has finished with it. None: not yet, the next run looks again."""
    files = p["files"]
    carousel = len(files) > 1
    for f in files:
        if not f["container_id"]:
            url = await _item_url(tid, f["id"], base_url)
            f["container_id"] = await adapter.create_post_item(account, PostItem(
                kind=p["kind"], url=url, is_video=f["is_video"], caption=None if carousel else caption, carousel_item=carousel))
            async with tenant_tx(tid) as conn:
                await conn.execute("update post_draft_files set container_id=%s where id=%s", (f["container_id"], f["id"]))
    children = [f["container_id"] for f in files]
    if not await _ready(adapter, account, children):
        return None
    if not carousel:
        return children[0]
    if not p["container_id"]:
        p["container_id"] = await adapter.create_carousel(account, children, caption)
        async with tenant_tx(tid) as conn:
            await conn.execute("update post_drafts set container_id=%s where id=%s", (p["container_id"], p["id"]))
    return p["container_id"] if await _ready(adapter, account, [p["container_id"]]) else None


async def advance(tid: UUID, post_id: UUID, cfg: PlatformConfig, adapters: dict, accounts: dict[UUID, AccountRef], base_url: str,
                  allowed_urls: list[str], now: datetime) -> list:
    """One step of one post; the minute tick calls it again for whatever is still on the way."""
    c = cfg.operations.posts
    async with tenant_tx(tid) as conn:
        p = await load(conn, post_id)
        if not p or p["status"] not in ("approved", "publishing"):
            return []
        connected = await (await conn.execute("select channel, scopes from channel_accounts where id=%s", (p["channel_account_id"],))).fetchone()
        adapter = adapters.get(connected["channel"])
        account = accounts.get(p["channel_account_id"])
        caption = render_text(p["caption"], cfg.conversation.persona.seller_display_name)
        if not p["publish_started_at"]:
            if not c.publishing_enabled or await registry.kill_switch_on(conn, tenant_id=tid, channel=connected["channel"], feature=FEATURE):
                return []
            code = await _refusal(conn, p, cfg, adapter, connected, account, allowed_urls, now)
            if code is None and p["status"] == "approved":
                moved = await (await conn.execute(
                    "update post_drafts set status='publishing', publishing_since=%s, updated_at=now() where id=%s and status='approved' returning publishing_since",
                    (now, post_id))).fetchone()
                if not moved:
                    return []
                p["publishing_since"] = moved["publishing_since"]
    if p["publish_started_at"]:
        return await _settle(tid, p, caption, adapter, account, c, now)
    if code is not None:
        await _close(tid, post_id, "failed", code)
        return []
    try:
        if now - p["publishing_since"] > timedelta(minutes=c.container_wait_min):
            raise _Stop("container_timeout")
        container = await _build(tid, p, caption, adapter, account, base_url)
        if container is None:
            return []
        return await _publish(tid, p, caption, container, adapter, account, c, now)
    except _Stop as e:
        await _close(tid, post_id, "failed", e.code, e.detail)
    except (RetryableSendError, AmbiguousSendError):
        pass                               # nothing was published: the next run tries again, until the wait is over
    except TokenInvalidError:
        await _close(tid, post_id, "failed", "token")
    except SendError as e:
        await _close(tid, post_id, "failed", "refused", str(e))
    return []


async def _publish(tid: UUID, p: dict, caption: str, container: str, adapter, account: AccountRef, c: PostsConfig, now: datetime) -> list:
    if await adapter.post_status(account, container) == "PUBLISHED":     # an earlier run got further than it could record
        return await finish(tid, p, await _find_published(p, caption, adapter, account))
    async with tenant_tx(tid) as conn:
        row = await (await conn.execute(
            """update post_drafts set container_id=%s, publish_started_at=%s, updated_at=now() where id=%s and status='publishing' and publish_started_at is null
               returning publish_started_at""", (container, now, p["id"]))).fetchone()
    if not row:
        return []
    p = {**p, "container_id": container, "publish_started_at": row["publish_started_at"]}
    try:
        media_id = await adapter.publish_post(account, container)
    except AmbiguousSendError:
        return await _settle(tid, p, caption, adapter, account, c, now)
    except RetryableSendError:             # never reached the provider, or it refused for now: not sent
        async with tenant_tx(tid) as conn:
            await conn.execute("update post_drafts set publish_started_at=null where id=%s", (p["id"],))
        return []
    return await finish(tid, p, media_id)
