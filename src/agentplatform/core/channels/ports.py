"""Channel and human-inbox ports (ADR-001). New channels/inboxes are new adapters; the agent never changes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Protocol
from uuid import UUID

from agentplatform.core.channels.events import CanonicalEvent


@dataclass(frozen=True)
class ChannelCapabilities:
    max_text_bytes: int
    quick_replies_max: int
    quick_reply_title_max: int
    typing_indicator: bool
    service_window: timedelta | None          # None = no window (e.g. webchat session)
    supports_templates: bool
    text_per_s: int
    media_per_s: int
    private_replies_per_h: int | None = None
    mixed_inbox: bool = False                 # the account may hold personal conversations: the assistant needs a scope (ADR-019)


@dataclass(frozen=True)
class AccountRef:
    tenant_id: UUID
    channel_account_id: UUID
    external_account_id: str
    external_page_id: str | None
    token: str | None


@dataclass(frozen=True)
class OutboundMessage:
    kind: str                                 # dm_text | private_reply | public_comment_reply | hide_comment | typing | template
    recipient_external_id: str | None         # IGSID; never chosen by the LLM
    text: str | None = None
    comment_id: str | None = None
    quick_replies: list[str] | None = None
    template: dict | None = None


@dataclass(frozen=True)
class SendResult:
    external_mid: str | None


@dataclass(frozen=True)
class MediaInfo:
    """A post, reel, story or ad as the provider describes it (channels that have a catalog implement MediaCatalog)."""
    external_id: str
    kind: str                                 # post | reel | story | ad | live
    media_type: str | None = None
    caption: str | None = None
    permalink: str | None = None
    image_url: str | None = None
    posted_at: datetime | None = None
    comments_count: int | None = None


class SendError(Exception):
    """Base. retryable=False means do not retry (permanent). `scope` says what the refusal is about: account (nothing can be
    sent from this account), recipient (this person cannot receive), object (the comment is gone), window (outside the
    channel's window), thread_owner (another app owns the conversation); None when the provider's answer does not say."""
    retryable = False

    def __init__(self, message: str = "", *, scope: str | None = None):
        super().__init__(message)
        self.scope = scope


class RetryableSendError(SendError):
    retryable = True


class AmbiguousSendError(SendError):
    """Timeout/connection drop after the request may have reached the provider: must reconcile, never blind-resend."""


class RateLimitedError(RetryableSendError):
    pass


class TokenInvalidError(SendError):
    pass


class PartialParse(Exception):
    """Some items of a delivery could not be read. `events` are the good ones: they go on; the delivery is set aside."""

    def __init__(self, events: list[CanonicalEvent], failures: int):
        super().__init__(f"{failures} unreadable item(s)")
        self.events, self.failures = events, failures


class ChannelAdapter(Protocol):
    channel: str

    def capabilities(self) -> ChannelCapabilities: ...
    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> bool: ...
    def parse(self, body: bytes) -> list[CanonicalEvent]: ...
    async def send(self, account: AccountRef, msg: OutboundMessage) -> SendResult: ...
    async def find_sent_message(self, account: AccountRef, recipient_external_id: str, text: str,
                                sent_after: datetime | None = None) -> str | None: ...


class MediaCatalog(Protocol):
    """Optional adapter capability: read the account's posts and comments (console panel, lost-webhook safety net)."""

    async def list_media(self, account: AccountRef, limit: int) -> list[MediaInfo]: ...
    async def list_stories(self, account: AccountRef) -> list[MediaInfo]: ...
    async def get_media(self, account: AccountRef, media_id: str) -> MediaInfo | None: ...
    async def list_comments(self, account: AccountRef, media_id: str, kind: str, limit: int) -> list[CanonicalEvent]: ...
    async def get_username(self, account: AccountRef, external_user_id: str) -> str | None: ...
    async def list_conversation_contacts(self, account: AccountRef, after: str | None, limit: int) -> tuple[list[str], str | None]: ...


@dataclass(frozen=True)
class Alert:
    kind: str                 # handoff | realert | ops
    first_name: str | None
    stage: str | None
    reason: str
    summary: str
    console_url: str
    handoff_id: UUID | None


@dataclass(frozen=True)
class SellerContact:
    seller_id: UUID
    whatsapp: str | None
    email: str | None


class Notifier(Protocol):
    name: str

    async def send_alert(self, seller: SellerContact, alert: Alert) -> bool: ...


class HumanInboxPort(Protocol):
    async def notify_handoff(self, seller: SellerContact, alert: Alert) -> str: ...
