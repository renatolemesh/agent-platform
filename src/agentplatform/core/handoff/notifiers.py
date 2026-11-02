"""Seller notifiers (ADR-010): WhatsApp Cloud API utility template with signed quick-reply buttons, e-mail fallback,
and a log notifier for dev. Minimal personal data: first name, stage, reason, masked summary, console link (login required).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import smtplib
from email.message import EmailMessage
from urllib.parse import urlparse
from uuid import UUID

import httpx

from agentplatform.core.channels.ports import Alert, SellerContact

log = logging.getLogger(__name__)


def sign_action(secret: bytes, action: str, handoff_id: UUID) -> str:
    mac = hmac.new(secret, f"{action}:{handoff_id}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{action}:{handoff_id}:{mac}"


def verify_action(secret: bytes, payload: str) -> tuple[str, UUID] | None:
    try:
        action, hid, mac = payload.split(":", 2)
        expected = hmac.new(secret, f"{action}:{hid}".encode(), hashlib.sha256).hexdigest()[:24]
        if action in ("took", "return") and hmac.compare_digest(mac, expected):
            return action, UUID(hid)
    except ValueError:
        pass
    return None


class WhatsAppCloudNotifier:
    name = "whatsapp_cloud"

    def __init__(self, token: str, phone_number_id: str, action_secret: bytes, template: str = "handoff_alert_v1",
                 graph_version: str = "v26.0", transport: httpx.AsyncBaseTransport | None = None):
        self.phone_number_id, self.template, self.secret = phone_number_id, template, action_secret
        self._c = httpx.AsyncClient(base_url=f"https://graph.facebook.com/{graph_version}", transport=transport, timeout=10,
                                    headers={"Authorization": f"Bearer {token}"})

    async def send_alert(self, seller: SellerContact, alert: Alert) -> bool:
        if not seller.whatsapp:
            return False
        body = {
            "messaging_product": "whatsapp", "to": seller.whatsapp, "type": "template",
            "template": {"name": self.template, "language": {"code": "pt_BR"}, "components": [
                {"type": "body", "parameters": [{"type": "text", "text": v[:300]} for v in (
                    alert.first_name or "cliente", alert.first_name or "Um cliente", alert.stage or "-", alert.reason,
                    alert.summary or "-", alert.console_url)]},
                {"type": "button", "sub_type": "quick_reply", "index": "0",
                 "parameters": [{"type": "payload", "payload": sign_action(self.secret, "took", alert.handoff_id)}]},
                {"type": "button", "sub_type": "quick_reply", "index": "1",
                 "parameters": [{"type": "payload", "payload": sign_action(self.secret, "return", alert.handoff_id)}]},
            ]}}
        try:
            r = await self._c.post(f"/{self.phone_number_id}/messages", json=body)
            if r.status_code >= 400:
                log.warning("whatsapp notifier HTTP %s", r.status_code)
                return False
            return True
        except httpx.HTTPError as e:
            log.warning("whatsapp notifier error %s", type(e).__name__)
            return False


    async def send_ops(self, to: str, title: str, detail: str, template: str = "ops_alert_v1") -> bool:
        """Platform-operator alert (utility template with two body parameters, no buttons)."""
        body = {"messaging_product": "whatsapp", "to": to, "type": "template",
                "template": {"name": template, "language": {"code": "pt_BR"}, "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": title[:300]}, {"type": "text", "text": detail[:900]}]}]}}
        try:
            r = await self._c.post(f"/{self.phone_number_id}/messages", json=body)
            return r.status_code < 400
        except httpx.HTTPError as e:
            log.warning("whatsapp ops alert error %s", type(e).__name__)
            return False


def parse_whatsapp_button(body: bytes) -> list[str]:
    """Extract quick-reply payloads from a WhatsApp webhook (type=button)."""
    out = []
    data = json.loads(body)
    for entry in data.get("entry", []):
        for ch in entry.get("changes", []):
            for msg in (ch.get("value") or {}).get("messages", []) or []:
                if msg.get("type") == "button" and msg.get("button", {}).get("payload"):
                    out.append(msg["button"]["payload"])
                elif msg.get("type") == "interactive":
                    rep = (msg.get("interactive") or {}).get("button_reply") or {}
                    if rep.get("id"):
                        out.append(rep["id"])
    return out


class EmailNotifier:
    name = "email"

    def __init__(self, smtp_url: str, sender: str, action_secret: bytes, public_base_url: str):
        self.url, self.sender, self.secret, self.base = urlparse(smtp_url), sender, action_secret, public_base_url.rstrip("/")

    async def send_alert(self, seller: SellerContact, alert: Alert) -> bool:
        if not seller.email:
            return False
        msg = EmailMessage()
        msg["Subject"] = f"Novo atendimento: {alert.first_name or 'cliente'} ({alert.reason})"
        msg["From"], msg["To"] = self.sender, seller.email
        took = f"{self.base}/actions/{sign_action(self.secret, 'took', alert.handoff_id)}"
        ret = f"{self.base}/actions/{sign_action(self.secret, 'return', alert.handoff_id)}"
        msg.set_content(f"Estágio: {alert.stage}\nMotivo: {alert.reason}\nResumo: {alert.summary}\n\n"
                        f"Abrir no console (login necessário): {alert.console_url}\nAssumi: {took}\nDevolver ao bot: {ret}\n")
        return await self._deliver(msg)

    async def send_text(self, to: str, subject: str, body: str) -> bool:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, self.sender, to
        msg.set_content(body)
        return await self._deliver(msg)

    async def _deliver(self, msg: EmailMessage) -> bool:
        def _send() -> None:
            with smtplib.SMTP(self.url.hostname, self.url.port or 587, timeout=10) as s:
                s.starttls()
                if self.url.username:
                    s.login(self.url.username, self.url.password or "")
                s.send_message(msg)
        try:
            await asyncio.to_thread(_send)
            return True
        except Exception as e:
            log.warning("email notifier error %s", type(e).__name__)
            return False


class LogNotifier:
    """Dev/test notifier: records alerts in memory (no network)."""
    name = "log"

    def __init__(self):
        self.sent: list[tuple[SellerContact, Alert]] = []

    async def send_alert(self, seller: SellerContact, alert: Alert) -> bool:
        self.sent.append((seller, alert))
        log.info("ALERT %s for seller %s: %s", alert.kind, seller.seller_id, alert.reason)
        return True


class FallbackInbox:
    """HumanInboxPort for the MVP: Instagram app as inbox + notifier chain (WhatsApp -> e-mail)."""

    def __init__(self, notifiers: list):
        self.notifiers = notifiers

    async def notify_handoff(self, seller: SellerContact, alert: Alert) -> str | None:
        """Name of the notifier that delivered, or None. Exceptions count as "not delivered": the chain goes on, and the alert
        loop tries again at the next tick."""
        for n in self.notifiers:
            try:
                if await n.send_alert(seller, alert):
                    return n.name
            except Exception as e:
                log.warning("notifier %s error %s", n.name, type(e).__name__)
        return None
