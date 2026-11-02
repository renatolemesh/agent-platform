"""Phase 4: WhatsApp adapter parsing/sending, lead import with LGPD + suppression + rollback, campaigns with consent."""
import json
import uuid

import httpx
import pytest

from agentplatform.core.campaigns.engine import CampaignSpec, create_campaign, tick
from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
from agentplatform.core.channels.ports import AccountRef, OutboundMessage, SendError
from agentplatform.core.channels.whatsapp.adapter import WhatsAppCloudAdapter
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.imports.leads import import_leads, normalize_phone, rollback_batch
from agentplatform.core.outbox.dispatcher import Dispatcher
from agentplatform.core.security.keys import crypto

WA = {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [
    {"field": "messages", "value": {"metadata": {"phone_number_id": "PN1"},
        "messages": [{"from": "5511999990000", "id": "wamid.1", "timestamp": "1760040000", "type": "text", "text": {"body": "oi"}},
                     {"from": "5511999990000", "id": "wamid.2", "timestamp": "1760040001", "type": "button",
                      "button": {"text": "Assumi", "payload": "took:x"}}],
        "statuses": [{"id": "wamid.out", "status": "delivered", "timestamp": "1760040002", "recipient_id": "5511999990000",
                      "pricing": {"billable": True, "pricing_model": "PMP", "category": "service", "type": "regular"}}]}},
    {"field": "smb_message_echoes", "value": {"metadata": {"phone_number_id": "PN1"},
        "message_echoes": [{"from": "PN1", "to": "5511999990000", "id": "wamid.echo", "timestamp": "1760040003",
                            "type": "text", "text": {"body": "aqui é o vendedor"}}]}}]}]}


def test_whatsapp_parse():
    evs = WhatsAppCloudAdapter("s").parse(json.dumps(WA).encode())
    kinds = [e.event_type for e in evs]
    assert kinds == ["MessageReceived", "Postback", "ConversationStatusChanged", "MessageEcho"]
    assert evs[2].referral["pricing"]["category"] == "service"
    assert evs[3].contact_external_id == "5511999990000"


async def test_whatsapp_template_and_window_error():
    sent = []
    def h(req):
        sent.append(json.loads(req.content))
        if sent[-1]["type"] == "text":
            return httpx.Response(400, json={"error": {"code": 131047}})
        return httpx.Response(200, json={"messages": [{"id": "wamid.t"}]})
    a = WhatsAppCloudAdapter("s", transport=httpx.MockTransport(h))
    acc = AccountRef(uuid.uuid4(), uuid.uuid4(), "PN1", None, "tok")
    with pytest.raises(SendError, match="template_required"):
        await a.send(acc, OutboundMessage("dm_text", "5511999990000", text="oi"))
    r = await a.send(acc, OutboundMessage("template", "5511999990000", template={"name": "followup_v1"}))
    assert r.external_mid == "wamid.t" and sent[-1]["template"]["name"] == "followup_v1"


def test_phone_normalization():
    assert normalize_phone("(11) 99999-0000") == "+5511999990000"
    assert normalize_phone("11 3333 0000") == "+551133330000"
    assert normalize_phone("123") is None


ROWS = [
    {"Nome": "maria silva", "Telefone": "(11) 99876-5432", "Email": "", "Base": "consent", "Origem": "feira", "Finalidade": "marketing", "Evidencia": "form-123"},
    {"Nome": "joao", "Telefone": "11 98888 7777", "Email": "", "Base": "consent", "Origem": "feira", "Finalidade": "marketing", "Evidencia": ""},
    {"Nome": "ana", "Telefone": "", "Email": "ana@", "Base": "consent", "Origem": "feira", "Finalidade": "marketing", "Evidencia": "x"},
    {"Nome": "bia", "Telefone": "11 97777 6666", "Email": "", "Base": "", "Origem": "feira", "Finalidade": "marketing", "Evidencia": "x"},
    {"Nome": "carla", "Telefone": "11 96666 5555", "Email": "", "Base": "legitimate_interest", "Origem": "site", "Finalidade": "marketing", "Evidencia": ""},
]
MAP = {"name": "Nome", "phone": "Telefone", "email": "Email", "legal_basis": "Base", "source": "Origem",
       "purpose": "Finalidade", "consent_evidence": "Evidencia"}


@pytest.mark.db
async def test_import_dry_run_commit_suppression_rollback(tenant):
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into suppression_list (tenant_id, identifier_hash, reason) values (%s,%s,'opt_out')",
                           (tenant, crypto().lookup_hash("+5511966665555", kind="phone")))
        dry = await import_leads(conn, tenant, ROWS, MAP, initial_stage="novo", file_bytes=b"x", dry_run=True)
        assert dry.imported == 1 and dict(dry.rejected) == {3: "consent_evidence_missing", 4: "email_invalid",
                                                            5: "legal_basis_missing_or_invalid", 6: "suppressed"}
        rep = await import_leads(conn, tenant, ROWS, MAP, initial_stage="novo", file_bytes=b"x", dry_run=False)
        assert rep.imported == 1 and rep.batch_id
        assert await rollback_batch(conn, tenant, rep.batch_id) == 1


@pytest.mark.db
async def test_campaign_only_consented_and_template_outside_window(tenant):
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into channel_accounts (tenant_id, channel, external_account_id) values (%s,'whatsapp',%s)",
                           (tenant, f"PN-{tenant.hex[:6]}"))
        await import_leads(conn, tenant, ROWS[:1], MAP, initial_stage="nutricao", file_bytes=b"y", dry_run=False)
        cid = await create_campaign(conn, tenant, CampaignSpec("reativacao", ["nutricao"], "promo_v1", {"promo_v1": 0.5, "promo_v2": 0.5}))
        r = await tick(conn, tenant, cid, __import__("datetime").datetime.now(__import__("datetime").timezone.utc), is_quiet=False)
        assert r["queued"] == 1
        conv = await (await conn.execute("select conversation_id from outbox where effect_key like %s", (f"camp:{cid}:%",))).fetchone()
    a = FakeChannelAdapter(channel="whatsapp")
    from datetime import time as dtime

    from agentplatform.core.outbox.dispatcher import DispatchPolicy
    async def never_quiet(_):
        return DispatchPolicy(quiet_start=dtime(23, 59, 59), quiet_end=dtime(0, 0), holidays_national=False)
    out = await Dispatcher({"whatsapp": a}, policy_for=never_quiet).dispatch_conversation(tenant, conv["conversation_id"])
    assert len(out.sent) == 1 and a.sent[0].msg.kind == "template"     # no 24h window needed for an approved template
