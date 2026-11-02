"""Campaign target lifecycle: send results and WhatsApp delivery receipts feed the auto-pause (F4-CMP-01/02)."""
import json
import uuid
from datetime import datetime, timezone

import pytest

from agentplatform.core.campaigns.engine import (
    CampaignSpec,
    apply_delivery_status,
    campaign_metrics,
    create_campaign,
    tick,
)
from agentplatform.core.channels.whatsapp.adapter import WhatsAppCloudAdapter
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.imports.leads import import_leads
from agentplatform.core.orchestration.ingress import persist_events, route_event

pytestmark = pytest.mark.db
MAP = {"name": "Nome", "phone": "Telefone", "legal_basis": "Base", "source": "Origem", "purpose": "Finalidade",
       "consent_evidence": "Evidencia"}


async def _campaign(tenant, n=5):
    pn = f"PN-{tenant.hex[:6]}"
    rows = [{"Nome": f"c{i}", "Telefone": f"11 9{8000 + i:04d} {1000 + i:04d}", "Base": "consent", "Origem": "feira",
             "Finalidade": "marketing", "Evidencia": "ficha assinada"} for i in range(n)]
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into channel_accounts (tenant_id, channel, external_account_id) values (%s,'whatsapp',%s)", (tenant, pn))
        rep = await import_leads(conn, tenant, rows, MAP, initial_stage="nutricao", file_bytes=uuid.uuid4().bytes, dry_run=False)
        assert rep.imported == n
        cid = await create_campaign(conn, tenant, CampaignSpec("reativ", ["nutricao"], "promo_v1", {"promo_v1": 1.0}, min_sample=4))
        assert (await tick(conn, tenant, cid, datetime.now(timezone.utc), is_quiet=False))["queued"] == n
        items = await (await conn.execute("select id from outbox where effect_key like %s order by id", (f"camp:{cid}:%",))).fetchall()
    return pn, cid, [i["id"] for i in items]


async def test_send_results_trigger_auto_pause_and_metrics(tenant):
    _, cid, items = await _campaign(tenant)
    async with tenant_tx(tenant) as conn:
        for i, oid in enumerate(items[:3]):
            await conn.execute("update outbox set status='sent', external_mid=%s, sent_at=now() where id=%s", (f"wamid.c{i}-{cid}", oid))
        await conn.execute("update outbox set status='failed', last_error='HTTP 400 code=131050' where id=%s", (items[3],))
        await conn.execute("update outbox set status='failed', last_error='HTTP 400 code=131026' where id=%s", (items[4],))
        r = await tick(conn, tenant, cid, datetime.now(timezone.utc), is_quiet=False)
        assert r["status"] == "paused_auto" and r["opted_out"] == 1 and r["failed"] == 1
        # delivery receipts advance but never regress
        assert await apply_delivery_status(conn, tenant, f"wamid.c0-{cid}", "read", None) == "read"
        assert await apply_delivery_status(conn, tenant, f"wamid.c0-{cid}", "delivered", None) == "read"
        m = (await campaign_metrics(conn, cid))[0]
        revoked = await (await conn.execute(
            "select count(*) as n from consents where tenant_id=%s and purpose='marketing' and status='revoked'", (tenant,))).fetchone()
    assert (m["targets"], m["sent"], m["read"], m["failed"], m["opted_out"]) == (5, 3, 1, 1, 1)
    assert revoked["n"] == 0          # send-time 131050 marks the target; the receipt path below revokes consent


async def test_whatsapp_failed_receipt_through_ingress_revokes_marketing(tenant):
    pn, cid, items = await _campaign(tenant, n=1)
    mid = f"wamid.x-{cid}"
    async with tenant_tx(tenant) as conn:
        await conn.execute("update outbox set status='sent', external_mid=%s, sent_at=now() where id=%s", (mid, items[0]))
    body = {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": pn}, "statuses": [{"id": mid, "status": "failed", "timestamp": "1760040002",
        "recipient_id": "5511980001000", "errors": [{"code": 131050, "title": "user stopped marketing"}]}]}}]}]}
    events = WhatsAppCloudAdapter("secret").parse(json.dumps(body).encode())
    created = await persist_events(events)
    for t, e in created:
        await route_event(t, e, {"whatsapp": WhatsAppCloudAdapter("secret")}, "novo")
    async with tenant_tx(tenant) as conn:
        target = await (await conn.execute("select status from campaign_targets where campaign_id=%s", (cid,))).fetchone()
        ob = await (await conn.execute("select last_error from outbox where id=%s", (items[0],))).fetchone()
        revoked = await (await conn.execute(
            "select count(*) as n from consents where tenant_id=%s and purpose='marketing' and status='revoked'", (tenant,))).fetchone()
    assert target["status"] == "opted_out" and ob["last_error"] == "delivery_failed:131050" and revoked["n"] == 1
