"""Connect (a Chatwoot instance) as HumanInboxPort — API and webhooks only, never its database (plan 05, anti-pattern §17).

Human signals from Connect: an outgoing message authored by a User (agent), or the conversation status becoming `open`
(assigned to a human). Returning to the bot = status `pending`."""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Mapping

import httpx

from agentplatform.core.channels.ports import Alert


@dataclass(frozen=True)
class ConnectSignal:
    kind: str                  # human_message | status_open | status_pending | status_resolved
    connect_conversation_id: int
    contact_identifier: str | None


class ChatwootInbox:
    def __init__(self, base_url: str, account_id: int, api_token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.account_id = account_id
        self._c = httpx.AsyncClient(base_url=base_url.rstrip("/"), transport=transport, timeout=10,
                                    headers={"api_access_token": api_token})

    def _p(self, conv_id: int, suffix: str) -> str:
        return f"/api/v1/accounts/{self.account_id}/conversations/{conv_id}/{suffix}"

    async def add_note(self, connect_conversation_id: int, note: str) -> None:
        r = await self._c.post(self._p(connect_conversation_id, "messages"),
                               json={"content": note, "message_type": "outgoing", "private": True})
        r.raise_for_status()

    async def assign(self, connect_conversation_id: int, agent_id: int) -> None:
        (await self._c.post(self._p(connect_conversation_id, "assignments"), json={"assignee_id": agent_id})).raise_for_status()

    async def set_status(self, connect_conversation_id: int, status: str) -> None:
        (await self._c.post(self._p(connect_conversation_id, "toggle_status"), json={"status": status})).raise_for_status()

    async def notify_handoff_conversation(self, connect_conversation_id: int, agent_id: int | None, alert: Alert) -> None:
        await self.add_note(connect_conversation_id,
                            f"[Assistente] Handoff: {alert.reason}\nEstágio: {alert.stage}\nResumo: {alert.summary}")
        if agent_id:
            await self.assign(connect_conversation_id, agent_id)
        await self.set_status(connect_conversation_id, "open")

    async def return_to_bot(self, connect_conversation_id: int) -> None:
        await self.set_status(connect_conversation_id, "pending")


def verify_connect_webhook(secret: bytes, headers: Mapping[str, str], body: bytes) -> bool:
    sig = headers.get("x-chatwoot-signature") or headers.get("X-Chatwoot-Signature") or ""
    return bool(sig) and hmac.compare_digest(sig, hmac.new(secret, body, hashlib.sha256).hexdigest())


def parse_connect_webhook(body: bytes) -> list[ConnectSignal]:
    d = json.loads(body)
    ev = d.get("event")
    conv = d.get("conversation") or {}
    cid = conv.get("id") or d.get("id")
    contact = ((conv.get("meta") or {}).get("sender") or {}).get("identifier") or (d.get("sender") or {}).get("identifier")
    out: list[ConnectSignal] = []
    if ev == "message_created" and d.get("message_type") == "outgoing" and not d.get("private") \
            and (d.get("sender") or {}).get("type", "").lower() == "user":
        out.append(ConnectSignal("human_message", cid, contact))
    if ev in ("conversation_status_changed", "conversation_updated") and d.get("status") in ("open", "pending", "resolved"):
        out.append(ConnectSignal(f"status_{d['status']}", cid, contact))
    return out
