"""In-memory channel adapter for tests, sandbox and the lab (no network). Scriptable failures and echo timing."""
from __future__ import annotations

import asyncio
import itertools
import uuid
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Awaitable, Callable, Mapping

from agentplatform.core.channels.events import CanonicalEvent
from agentplatform.core.channels.ports import (
    AccountRef,
    AmbiguousSendError,
    ChannelCapabilities,
    MediaInfo,
    OutboundMessage,
    PostItem,
    SendResult,
)

FAKE_CAPS = ChannelCapabilities(
    max_text_bytes=1000, quick_replies_max=13, quick_reply_title_max=20, typing_indicator=True,
    service_window=timedelta(hours=24), supports_templates=False, text_per_s=100, media_per_s=10,
    private_replies_per_h=750,
)


@dataclass
class SentRecord:
    account: str
    msg: OutboundMessage
    mid: str


@dataclass
class FakeChannelAdapter:
    channel: str = "instagram"
    sent: list[SentRecord] = field(default_factory=list)
    # behaviours
    fail_next: list[Exception] = field(default_factory=list)
    ambiguous_but_delivered: int = 0          # next N sends time out AFTER being delivered
    on_echo: Callable[[SentRecord], Awaitable[None]] | None = None   # deliver the echo (e.g. before returning the mid)
    echo_before_return: bool = False
    media: list[MediaInfo] = field(default_factory=list)          # catalog returned to the panel and the poller
    comments: dict[str, list[CanonicalEvent]] = field(default_factory=dict)
    usernames: dict[str, str] = field(default_factory=dict)
    mixed_inbox: bool = False
    contact_pages: list = field(default_factory=list)   # pages of ids; an Exception in the list is raised
    publish_scope: str | None = None
    delete_scope: str | None = None
    post_items: list[PostItem] = field(default_factory=list)
    carousels: list[tuple[list[str], str]] = field(default_factory=list)
    post_statuses: dict[str, str] = field(default_factory=dict)         # container -> status; FINISHED unless said otherwise
    published_posts: list[str] = field(default_factory=list)            # containers actually made public
    post_failures: dict[str, list[Exception]] = field(default_factory=dict)   # create | status | publish: raised in order
    publish_then_fail: list[Exception] = field(default_factory=list)    # the post goes out and the answer is lost
    comments_set: list[tuple[str, bool]] = field(default_factory=list)
    deleted_media: list[str] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def capabilities(self) -> ChannelCapabilities:
        return replace(FAKE_CAPS, mixed_inbox=self.mixed_inbox)

    async def list_conversation_contacts(self, account: AccountRef, after: str | None, limit: int) -> tuple[list[str], str | None]:
        i = int(after or 0)
        page = self.contact_pages[i] if i < len(self.contact_pages) else []
        if isinstance(page, Exception):
            raise page
        return page, str(i + 1) if i + 1 < len(self.contact_pages) else None

    def _post_failure(self, step: str) -> None:
        pending = self.post_failures.get(step)
        if pending:
            raise pending.pop(0)

    async def create_post_item(self, account: AccountRef, item: PostItem) -> str:
        self._post_failure("create")
        self.post_items.append(item)
        return f"container-{next(self._ids)}"

    async def create_carousel(self, account: AccountRef, children: list[str], caption: str) -> str:
        self._post_failure("create")
        self.carousels.append((children, caption))
        return f"carousel-{next(self._ids)}"

    async def post_status(self, account: AccountRef, container_id: str) -> str:
        self._post_failure("status")
        return "PUBLISHED" if container_id in self.published_posts else self.post_statuses.get(container_id, "FINISHED")

    async def publish_post(self, account: AccountRef, container_id: str) -> str:
        self._post_failure("publish")
        self.published_posts.append(container_id)
        if self.publish_then_fail:
            raise self.publish_then_fail.pop(0)
        return f"media-{next(self._ids)}"

    async def set_comments_enabled(self, account: AccountRef, media_id: str, enabled: bool) -> None:
        self._post_failure("comments")
        self.comments_set.append((media_id, enabled))

    async def delete_media(self, account: AccountRef, media_id: str) -> None:
        self._post_failure("delete")
        self.deleted_media.append(media_id)

    async def list_media(self, account: AccountRef, limit: int) -> list[MediaInfo]:
        return [m for m in self.media if m.kind != "story"][:limit]

    async def list_stories(self, account: AccountRef) -> list[MediaInfo]:
        return [m for m in self.media if m.kind == "story"]

    async def get_media(self, account: AccountRef, media_id: str) -> MediaInfo | None:
        return next((m for m in self.media if m.external_id == media_id), None)

    async def list_comments(self, account: AccountRef, media_id: str, kind: str, limit: int) -> list[CanonicalEvent]:
        return self.comments.get(media_id, [])[:limit]

    async def get_username(self, account: AccountRef, external_user_id: str) -> str | None:
        return self.usernames.get(external_user_id)

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        return headers.get("x-fake-signature") == "ok"

    def parse(self, body: bytes) -> list[CanonicalEvent]:
        import json
        return [CanonicalEvent.model_validate(e) for e in json.loads(body)]

    async def send(self, account: AccountRef, msg: OutboundMessage) -> SendResult:
        if self.fail_next:
            raise self.fail_next.pop(0)
        if msg.kind == "typing":
            return SendResult(None)
        rec = SentRecord(account.external_account_id, msg, f"mid.fake.{next(self._ids)}.{uuid.uuid4().hex[:8]}")
        self.sent.append(rec)
        if self.on_echo and self.echo_before_return:
            await self.on_echo(rec)
        if self.ambiguous_but_delivered > 0:
            self.ambiguous_but_delivered -= 1
            raise AmbiguousSendError("simulated timeout after delivery")
        if self.on_echo and not self.echo_before_return:
            asyncio.get_running_loop().create_task(self.on_echo(rec))
        return SendResult(rec.mid)

    async def find_sent_message(self, account: AccountRef, recipient_external_id: str, text: str,
                                sent_after=None) -> str | None:
        for r in reversed(self.sent):
            if r.msg.recipient_external_id == recipient_external_id and (r.msg.text or "").strip() == text.strip():
                return r.mid
        return None

    def texts(self) -> list[str]:
        return [r.msg.text or "" for r in self.sent]
