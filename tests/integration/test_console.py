import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentplatform.api import console
from agentplatform.api.auth import COOKIE, csrf_token, hash_password, read_session
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.security.keys import crypto, encrypt_field
from tests.integration.helpers import make_conversation

pytestmark = pytest.mark.db


async def test_login_list_take_and_return(tenant):
    conv, _ = await make_conversation(tenant)
    email = f"v-{uuid.uuid4().hex[:6]}@example.com"
    async with tenant_tx(tenant) as conn:
        await conn.execute(
            "insert into tenant_users (tenant_id, email_hash, email_enc, display_name, role, password_hash) values (%s,%s,%s,'Vend','seller',%s)",
            (tenant, crypto().lookup_hash(email, kind="email"), await encrypt_field(conn, tenant, email, "tenant_users.email"),
             hash_password("s3nha-forte")))
    app = FastAPI()
    app.include_router(console.router)
    with TestClient(app) as c:
        assert c.get("/console/", follow_redirects=False).status_code == 303          # not logged in
        assert c.post("/console/login", data={"email": email, "password": "errada"}).status_code == 401
        r = c.post("/console/login", data={"email": email, "password": "s3nha-forte"}, follow_redirects=False)
        assert r.status_code == 303
        page = c.get("/console/")
        assert page.status_code == 200 and "Conversas" in page.text
        csrf = csrf_token(read_session((c.cookies.get(COOKIE) or "").strip('"')))
        assert c.post(f"/console/conversations/{conv['id']}/ownership", data={"to": "HUMAN"}).status_code == 403   # no CSRF
        r = c.post(f"/console/conversations/{conv['id']}/ownership", data={"to": "HUMAN", "csrf": csrf}, follow_redirects=False)
        assert r.status_code == 303
        detail = c.get(f"/console/conversations/{conv['id']}")
        assert "HUMAN" in detail.text and "console:human" in detail.text
        c.post(f"/console/conversations/{conv['id']}/ownership", data={"to": "BOT", "csrf": csrf})
        assert c.post("/console/kill-switch", data={"scope": "tenant", "on": "1", "csrf": csrf}).status_code in (200, 303, 403)
    async with tenant_tx(tenant) as conn:
        audit = await (await conn.execute("select action from audit_log where tenant_id=%s", (tenant,))).fetchall()
        own = await (await conn.execute("select ownership from conversations where id=%s", (conv["id"],))).fetchone()
    assert own["ownership"] == "BOT" and [a["action"] for a in audit].count("conversation.ownership") == 2


async def test_pause_and_resume_one_conversation_only(tenant):
    """Pausing the assistant in one conversation cancels only its pending sends; the others keep going."""
    from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
    from agentplatform.core.outbox.dispatcher import Dispatcher
    from tests.integration.helpers import put, status
    paused, ig1 = await make_conversation(tenant)
    other, ig2 = await make_conversation(tenant)
    pending_paused = await put(tenant, paused, ig1, "resposta que não deve sair")
    email = f"p-{uuid.uuid4().hex[:6]}@example.com"
    async with tenant_tx(tenant) as conn:
        await conn.execute(
            "insert into tenant_users (tenant_id, email_hash, email_enc, display_name, role, password_hash) values (%s,%s,%s,'Vend','seller',%s)",
            (tenant, crypto().lookup_hash(email, kind="email"), await encrypt_field(conn, tenant, email, "tenant_users.email"),
             hash_password("s3nha-forte")))
    app = FastAPI()
    app.include_router(console.router)
    with TestClient(app) as c:
        c.post("/console/login", data={"email": email, "password": "s3nha-forte"})
        csrf = csrf_token(read_session((c.cookies.get(COOKIE) or "").strip('"')))
        assert "Pausar assistente nesta conversa" in c.get(f"/console/conversations/{paused['id']}").text
        r = c.post(f"/console/conversations/{paused['id']}/ownership", data={"to": "PAUSED", "next": "/console/", "csrf": csrf},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/console/"            # quick action from the list
        assert "Retomar assistente" in c.get(f"/console/conversations/{paused['id']}").text
        assert (await status(tenant, pending_paused))["status"] == "cancelled"
        other_item = await put(tenant, other, ig2, "a outra conversa segue")
        out = await Dispatcher({"instagram": FakeChannelAdapter()}).dispatch_conversation(tenant, other["id"])
        assert other_item in out.sent
        r = c.post(f"/console/conversations/{paused['id']}/ownership", data={"to": "BOT", "next": "https://evil.example/", "csrf": csrf},
                   follow_redirects=False)
        assert r.headers["location"] == f"/console/conversations/{paused['id']}"        # no open redirect
    async with tenant_tx(tenant) as conn:
        own = await (await conn.execute("select ownership from conversations where id=%s", (paused["id"],))).fetchone()
    assert own["ownership"] == "BOT"
