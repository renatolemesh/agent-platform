"""WhatsApp Cloud API channel (phase 4). Free text only inside the 24h service window; outside it, only approved
templates of the right category. Coexistence echoes (smb_message_echoes) are a human signal. Status webhooks carry
the real `pricing` object (cost per message is a parameter, never a constant)."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Mapping

import httpx

from agentplatform.core.channels.events import Attachment, CanonicalEvent
from agentplatform.core.channels.ports import (
    AccountRef,
    AmbiguousSendError,
    ChannelCapabilities,
    OutboundMessage,
    RateLimitedError,
    RetryableSendError,
    SendError,
    SendResult,
    TokenInvalidError,
)

CAPS = ChannelCapabilities(
    max_text_bytes=4096, quick_replies_max=3, quick_reply_title_max=20, typing_indicator=False,
    service_window=timedelta(hours=24), supports_templates=True, text_per_s=80, media_per_s=20,
)


def _ts(v) -> datetime:
    return datetime.fromtimestamp(int(v), tz=timezone.utc)


class WhatsAppCloudAdapter:
    channel = "whatsapp"

    def __init__(self, app_secret: str | None, graph_version: str = "v26.0",
                 transport: httpx.AsyncBaseTransport | None = None):
        self._secret = app_secret.encode() if app_secret else None
        self._c = httpx.AsyncClient(base_url=f"https://graph.facebook.com/{graph_version}", transport=transport, timeout=10)

    def capabilities(self) -> ChannelCapabilities:
        return CAPS

    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        sig = headers.get("x-hub-signature-256") or headers.get("X-Hub-Signature-256") or ""
        if not self._secret or not sig.startswith("sha256="):
            return False
        return hmac.compare_digest(sig[7:], hmac.new(self._secret, body, hashlib.sha256).hexdigest())

    def parse(self, body: bytes) -> list[CanonicalEvent]:
        data = json.loads(body)
        out: list[CanonicalEvent] = []
        for entry in data.get("entry", []):
            for ch in entry.get("changes", []):
                v = ch.get("value") or {}
                phone_id = str((v.get("metadata") or {}).get("phone_number_id", ""))
                field = ch.get("field")
                for m in v.get("messages", []) or []:
                    out.append(self._message(phone_id, m, echo=False))
                for m in v.get("message_echoes", []) or []:          # coexistence: sent from the WhatsApp Business app
                    out.append(self._message(phone_id, m, echo=True))
                for st in v.get("statuses", []) or []:
                    out.append(CanonicalEvent(
                        event_type="Read" if st.get("status") == "read" else "ConversationStatusChanged",
                        channel="whatsapp", account_external_id=phone_id, contact_external_id=st.get("recipient_id"),
                        idempotency_key=f"wa-status:{st.get('id')}:{st.get('status')}", provider_ts=_ts(st.get("timestamp", 0)),
                        mid=st.get("id"), referral={"status": st.get("status"), "pricing": st.get("pricing"),
                                                    "errors": st.get("errors")}))
                if field == "smb_message_echoes" and not v.get("message_echoes"):
                    for m in v.get("messages", []) or []:
                        out.append(self._message(phone_id, m, echo=True))
        return out

    def _message(self, phone_id: str, m: dict, echo: bool) -> CanonicalEvent:
        mtype = m.get("type")
        text, payload, atts = None, None, []
        if mtype == "text":
            text = (m.get("text") or {}).get("body")
        elif mtype == "button":
            text, payload = m["button"].get("text"), m["button"].get("payload")
        elif mtype == "interactive":
            rep = (m.get("interactive") or {}).get("button_reply") or (m.get("interactive") or {}).get("list_reply") or {}
            text, payload = rep.get("title"), rep.get("id")
        elif mtype in ("audio", "image", "document", "video"):
            mid_ = (m.get(mtype) or {}).get("id")      # media id: downloaded with the token (core/media/processor.py)
            atts = [Attachment(type="file" if mtype == "document" else mtype, url=f"whatsapp-media://{mid_}" if mid_ else None)]
            text = (m.get(mtype) or {}).get("caption")
        contact = m.get("to") if echo else m.get("from")
        return CanonicalEvent(
            event_type="MessageEcho" if echo else ("Postback" if payload else ("Referral" if m.get("referral") and not text else "MessageReceived")),
            channel="whatsapp", account_external_id=phone_id, contact_external_id=contact,
            idempotency_key=f"wa:{'echo' if echo else 'msg'}:{m.get('id')}", provider_ts=_ts(m.get("timestamp", 0)),
            mid=m.get("id"), text=text, attachments=atts, postback_payload=payload, referral=m.get("referral"))

    async def send(self, account: AccountRef, msg: OutboundMessage) -> SendResult:
        if not account.token:
            raise TokenInvalidError("no token")
        body: dict = {"messaging_product": "whatsapp", "to": msg.recipient_external_id}
        if msg.kind == "template":
            t = msg.template or {}
            body.update(type="template", template={"name": t["name"], "language": {"code": t.get("language", "pt_BR")},
                                                    "components": t.get("components", [])})
        else:
            body.update(type="text", text={"body": msg.text, "preview_url": False})
        try:
            r = await self._c.post(f"/{account.external_account_id}/messages", json=body,
                                   headers={"Authorization": f"Bearer {account.token}"})
        except httpx.ConnectError as e:
            raise RetryableSendError(str(e)) from e
        except (httpx.TimeoutException, httpx.ReadError, httpx.RemoteProtocolError) as e:
            raise AmbiguousSendError(type(e).__name__) from e
        if r.status_code >= 500:
            raise AmbiguousSendError(f"HTTP {r.status_code}")
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            code = (data.get("error") or {}).get("code")
            if code == 190:
                raise TokenInvalidError("190")
            if code in (4, 80007, 130429, 131048, 131056):
                raise RateLimitedError(str(code))
            if code == 131047:          # re-engagement: outside the 24h window -> template required
                raise SendError("window_closed_template_required")
            raise SendError(f"HTTP {r.status_code} code={code}")
        return SendResult((data.get("messages") or [{}])[0].get("id"))

    async def find_sent_message(self, account: AccountRef, recipient_external_id: str, text: str,
                                sent_after: datetime | None = None) -> str | None:
        return None    # WhatsApp has no history read API: ambiguous sends are confirmed by status webhooks or fail safe
