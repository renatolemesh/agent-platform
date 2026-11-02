"""Canonical events (plan 05 §2). Adapters translate provider payloads into these; nothing downstream sees raw payloads."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EventType = Literal[
    "MessageReceived", "StoryReply", "StoryMention", "Referral", "Postback", "CommentReceived",
    "MessageEcho", "MessageEdited", "MessageDeleted", "Reaction", "Read", "ConversationStatusChanged",
]


class Attachment(BaseModel):
    type: Literal["audio", "image", "video", "file", "story_mention", "share", "sticker", "unsupported"]
    url: str | None = None


class CanonicalEvent(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)     # providers send some ids as numbers

    event_type: EventType
    channel: str
    account_external_id: str                 # IG professional account id (or page id) that received the event
    contact_external_id: str | None = None   # IGSID of the customer (for echoes: the recipient)
    idempotency_key: str
    provider_ts: datetime
    mid: str | None = None
    text: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    # comments
    comment_id: str | None = None
    media_id: str | None = None
    media_product_type: str | None = None
    parent_comment_id: str | None = None
    author_username: str | None = None
    ad_id: str | None = None                 # comment on an ad post
    ad_title: str | None = None
    original_media_id: str | None = None
    # entry points / attribution
    referral: dict | None = None
    postback_payload: str | None = None
    story_id: str | None = None
    reply_to_mid: str | None = None
    is_self: bool = False                    # self messaging (test with a single account)
    standby: bool = False                    # delivered while another app owns the thread: we may read, never answer
    sender_app_id: str | None = None         # app that sent an echo, when the provider says

    @property
    def is_customer_message(self) -> bool:
        return self.event_type in ("MessageReceived", "StoryReply", "StoryMention", "Referral", "Postback")
