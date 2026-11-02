"""Webchat channel (phase 5): the site widget POSTs customer messages and polls bot messages. No provider window;
delivery happens when the widget polls (messages table, direction out_bot)."""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from typing import Mapping

from agentplatform.core.channels.events import CanonicalEvent
from agentplatform.core.channels.ports import AccountRef, ChannelCapabilities, OutboundMessage, SendResult

CAPS = ChannelCapabilities(max_text_bytes=4000, quick_replies_max=6, quick_reply_title_max=40, typing_indicator=False,
                           service_window=None, supports_templates=False, text_per_s=50, media_per_s=10)


class WebchatAdapter:
    channel = "webchat"

    def __init__(self, signing_secret: bytes):
        self._secret = signing_secret

    def capabilities(self) -> ChannelCapabilities:
        return CAPS

    def session_token(self, account: str, session_id: str) -> str:
        return hmac.new(self._secret, f"{account}:{session_id}".encode(), hashlib.sha256).hexdigest()

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        d = json.loads(body)
        tok = headers.get("x-webchat-token", "")
        return hmac.compare_digest(tok, self.session_token(d.get("account", ""), d.get("session_id", "")))

    def parse(self, body: bytes) -> list[CanonicalEvent]:
        d = json.loads(body)
        mid = d.get("client_message_id") or uuid.uuid4().hex
        return [CanonicalEvent(event_type="MessageReceived", channel="webchat", account_external_id=d["account"],
                               contact_external_id=d["session_id"], idempotency_key=f"web:{mid}", mid=mid,
                               provider_ts=datetime.now(timezone.utc), text=str(d.get("text", ""))[:2000],
                               referral={"utm": d.get("utm")} if d.get("utm") else None)]

    async def send(self, account: AccountRef, msg: OutboundMessage) -> SendResult:
        return SendResult(f"web.{uuid.uuid4().hex}")      # stored by the dispatcher; the widget polls

    async def find_sent_message(self, account: AccountRef, recipient_external_id: str, text: str,
                                sent_after=None) -> str | None:
        return None
