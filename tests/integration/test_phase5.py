"""Phase 5: Connect (Chatwoot) inbox via API only, Conversation Routing signal, webchat channel, seller routing."""
import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from agentplatform.core.channels.connect.chatwoot import ChatwootInbox, parse_connect_webhook
from agentplatform.core.channels.events import CanonicalEvent
from agentplatform.core.channels.ports import Alert, SellerContact
from agentplatform.core.channels.webchat.adapter import WebchatAdapter
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.handoff.alerts import pick_seller
from agentplatform.core.orchestration.ingress import persist_events, route_event
from agentplatform.core.outbox.dispatcher import Dispatcher
from tests.integration.helpers import make_conversation, put


async def test_chatwoot_uses_api_only():
    calls = []
    def h(req):
        calls.append((req.method, req.url.path, json.loads(req.content or b"{}"), req.headers.get("api_access_token")))
        return httpx.Response(200, json={})
    inbox = ChatwootInbox("https://connect.local", 7, "tok", transport=httpx.MockTransport(h))
    await inbox.notify_handoff_conversation(42, 3, Alert("handoff", "Ana", "qualificando", "hot_lead", "resumo", "u", None))
    assert [c[1] for c in calls] == ["/api/v1/accounts/7/conversations/42/messages", "/api/v1/accounts/7/conversations/42/assignments",
                                     "/api/v1/accounts/7/conversations/42/toggle_status"]
    assert calls[0][2]["private"] is True and calls[2][2]["status"] == "open" and calls[0][3] == "tok"


def test_connect_webhook_signals():
    agent_msg = json.dumps({"event": "message_created", "message_type": "outgoing", "private": False,
                            "sender": {"type": "user"}, "conversation": {"id": 42, "meta": {"sender": {"identifier": "IG1"}}}}).encode()
    bot_note = json.dumps({"event": "message_created", "message_type": "outgoing", "private": True,
                           "sender": {"type": "user"}, "conversation": {"id": 42}}).encode()
    status = json.dumps({"event": "conversation_status_changed", "status": "pending", "id": 42}).encode()
    assert [s.kind for s in parse_connect_webhook(agent_msg)] == ["human_message"]
    assert parse_connect_webhook(bot_note) == []                   # private notes are not customer-facing
    assert [s.kind for s in parse_connect_webhook(status)] == ["status_pending"]


@pytest.mark.db
async def test_conversation_routing_moves_to_human(tenant, monkeypatch):
    from agentplatform.settings import get_settings
    monkeypatch.setattr(get_settings(), "meta_app_id", "OUR_APP")
    conv, ig = await make_conversation(tenant)
    async with tenant_tx(tenant) as conn:
        acc = (await (await conn.execute("select external_account_id from channel_accounts where tenant_id=%s", (tenant,))).fetchone())["external_account_id"]
    ev = CanonicalEvent(event_type="ConversationStatusChanged", channel="instagram", account_external_id=acc,
                        contact_external_id=ig, idempotency_key=f"handover:{uuid.uuid4()}", provider_ts=datetime.now(timezone.utc),
                        referral={"handover": "messaging_handover", "new_owner_app_id": "263902037430900"})
    created = await persist_events([ev])
    from agentplatform.core.channels.instagram.adapter import InstagramGraphAdapter
    await route_event(tenant, created[0][1], InstagramGraphAdapter(None), "novo")
    async with tenant_tx(tenant) as conn:
        c = await (await conn.execute("select ownership from conversations where id=%s", (conv["id"],))).fetchone()
    assert c["ownership"] == "HUMAN"


@pytest.mark.db
async def test_webchat_has_no_window_and_signs_sessions(tenant):
    w = WebchatAdapter(b"k")
    tok = w.session_token("site1", "sess1")
    body = json.dumps({"account": "site1", "session_id": "sess1", "text": "oi"}).encode()
    assert w.verify_signature({"x-webchat-token": tok}, body)
    assert not w.verify_signature({"x-webchat-token": "x"}, body)
    async with platform_tx() as conn:
        await conn.execute("insert into channel_accounts (tenant_id, channel, external_account_id) values (%s,'webchat',%s)",
                           (tenant, f"site-{tenant.hex[:6]}"))
        ca = await (await conn.execute("select id from channel_accounts where tenant_id=%s and channel='webchat'", (tenant,))).fetchone()
    from agentplatform.core.orchestration.conversations import get_or_create_conversation
    async with tenant_tx(tenant) as conn:
        conv = await get_or_create_conversation(conn, tenant, ca["id"], "webchat", "sess1", "novo")
    oid = await put(tenant, conv, "sess1", "Olá pelo site!")
    out = await Dispatcher({"webchat": w}).dispatch_conversation(tenant, conv["id"])
    assert out.sent == [oid]                                      # no service window on webchat


@pytest.mark.db
async def test_round_robin_routing(tenant):
    from pathlib import Path

    import yaml

    from agentplatform.core.config.schema import PlatformConfig
    d = yaml.safe_load((Path(__file__).resolve().parents[2] / "config/tenants/dev-consortium.yaml").read_text(encoding="utf-8"))
    d["handoff"]["routing"] = [{"when": "true", "seller_ids": ["a", "b"], "strategy": "round_robin"}]
    cfg = PlatformConfig.model_validate(d)
    sellers = [SellerContact(uuid.uuid4(), None, None), SellerContact(uuid.uuid4(), None, None)]
    async with tenant_tx(tenant) as conn:
        first = await pick_seller(conn, tenant, cfg, sellers)
        await conn.execute("insert into audit_log (tenant_id, actor_kind, action, target, after) values (%s,'system','handoff.routed','x',%s)",
                           (tenant, json.dumps({"seller": str(first.seller_id)})))
        second = await pick_seller(conn, tenant, cfg, sellers)
    assert first.seller_id != second.seller_id
