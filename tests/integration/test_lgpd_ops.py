"""LGPD (export, erasure in cascade, retention) and token guard (kill switch on invalid token)."""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from agentplatform.core.config.registry import load_yaml
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.lgpd.service import erase_subject, export_subject, find_subject, retention_sweep
from agentplatform.core.ops.maintenance import token_guard
from agentplatform.core.orchestration.conversations import get_or_create_conversation, store_inbound_message
from agentplatform.core.security.keys import encrypt_field

pytestmark = pytest.mark.db
CFG = load_yaml(Path(__file__).resolve().parents[2] / "config/tenants/dev-consortium.yaml")


async def _subject(tenant):
    ig = f"IG-LGPD-{uuid.uuid4().hex[:6]}"
    async with tenant_tx(tenant) as conn:
        ca = await (await conn.execute("select id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        conv = await get_or_create_conversation(conn, tenant, ca["id"], "instagram", ig, "novo")
        await store_inbound_message(conn, tenant, conv["id"], mid="m-l1", kind="text", text="meu email é ana@ex.com e quero um carro",
                                    media=None, provider_ts=datetime.now(timezone.utc), inbound_event_id=None,
                                    service_window=timedelta(hours=24))
    return ig, conv


async def test_export_and_erase(tenant):
    ig, conv = await _subject(tenant)
    async with tenant_tx(tenant) as conn:
        cid = await find_subject(conn, tenant, channel_user_id=ig)
        data = await export_subject(conn, tenant, cid, None)
        assert "ana@ex.com" in data["messages"][0]["text"]          # export decrypts the original
        r = await erase_subject(conn, tenant, cid, None)
        assert r["messages"] == 1
        m = await (await conn.execute("select text_enc, text_masked from messages where conversation_id=%s", (conv["id"],))).fetchone()
        sup = await (await conn.execute("select count(*) as n from suppression_list where tenant_id=%s and reason='erasure'", (tenant,))).fetchone()
        audit = await (await conn.execute("select action from audit_log where tenant_id=%s and action like 'lgpd.%%'", (tenant,))).fetchall()
    assert m["text_enc"] is None and m["text_masked"] == "[eliminado]" and sup["n"] >= 1
    assert {a["action"] for a in audit} == {"lgpd.export", "lgpd.erase"}


async def test_retention_sweep(tenant):
    ig, conv = await _subject(tenant)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update messages set created_at = now() - interval '400 days' where conversation_id=%s", (conv["id"],))
        out = await retention_sweep(conn, CFG, tenant)
        m = await (await conn.execute("select text_enc, text_masked from messages where conversation_id=%s", (conv["id"],))).fetchone()
    assert out["messages_original"] >= 1 and m["text_enc"] is None and m["text_masked"]   # masked text kept


async def test_token_guard_invalid_turns_kill_switch_on(tenant):
    async with tenant_tx(tenant) as conn:
        await conn.execute("update channel_accounts set token_enc=%s where tenant_id=%s",
                           (await encrypt_field(conn, tenant, "EAAexpired", "channel_accounts.token"), tenant))
        t = httpx.MockTransport(lambda req: httpx.Response(200, json={"data": {"is_valid": False}}))
        res = await token_guard(conn, tenant, "app", "secret", transport=t)
        ks = await (await conn.execute("select is_on, reason from kill_switches where tenant_id=%s and scope='channel'", (tenant,))).fetchone()
        st = await (await conn.execute("select token_status from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
    assert res[0]["status"] == "invalid" and ks["is_on"] and st["token_status"] == "invalid"


async def test_pages_stay_subscribed_to_our_app(tenant):
    """10-min job: a page whose subscription to OUR app was dropped is re-subscribed; a healthy one is left alone."""
    from agentplatform.core.ops.maintenance import ensure_subscriptions
    async with tenant_tx(tenant) as conn:
        await conn.execute("update channel_accounts set token_enc=%s, token_status='valid' where tenant_id=%s and channel='instagram'",
                           (await encrypt_field(conn, tenant, "EAApage", "channel_accounts.token"), tenant))
        posts = []

        def meta(apps):
            def handler(req: httpx.Request) -> httpx.Response:
                if req.method == "POST":
                    posts.append(dict(req.url.params))
                    return httpx.Response(200, json={"success": True})
                return httpx.Response(200, json={"data": apps})
            return httpx.MockTransport(handler)
        ours = [{"id": "1234", "subscribed_fields": ["feed"]}]
        assert [r["resubscribed"] for r in await ensure_subscriptions(conn, tenant, {"feed"}, "1234", transport=meta(ours))] == [False]
        dropped = [{"id": "999", "subscribed_fields": ["feed"]}]           # only another app is left on the page
        res = await ensure_subscriptions(conn, tenant, {"feed"}, "1234", transport=meta(dropped))
    assert [(r["ok"], r["resubscribed"]) for r in res] == [(True, True)]
    assert posts == [{"subscribed_fields": "feed", "access_token": "EAApage"}]
