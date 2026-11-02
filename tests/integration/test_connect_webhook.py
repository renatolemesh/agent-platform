"""Connect (Chatwoot) webhook: signed agent reply moves the conversation to HUMAN; status `pending` returns it to BOT."""
import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from pydantic import SecretStr

from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.orchestration.conversations import get_or_create_conversation
from agentplatform.settings import get_settings

pytestmark = pytest.mark.db
SECRET = b"connect-test-secret"


def _signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    return body, {"x-chatwoot-signature": hmac.new(SECRET, body, hashlib.sha256).hexdigest(),
                  "content-type": "application/json"}


async def test_connect_webhook_moves_ownership(tenant, monkeypatch):
    monkeypatch.setattr(get_settings(), "connect_webhook_secret", SecretStr(SECRET.decode()))
    from agentplatform.api.main import create_app
    app = create_app(on_persisted=lambda created: None)
    ig = f"IG-CW-{uuid.uuid4().hex[:6]}"
    async with tenant_tx(tenant) as conn:
        ca = await (await conn.execute("select id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        conv = await get_or_create_conversation(conn, tenant, ca["id"], "instagram", ig, "novo")
    async with platform_tx() as conn:
        slug = (await (await conn.execute("select slug from tenants where id=%s", (tenant,))).fetchone())["slug"]

    human = {"event": "message_created", "message_type": "outgoing", "private": False, "sender": {"type": "user"},
             "conversation": {"id": 7, "meta": {"sender": {"identifier": ig}}}}
    back = {"event": "conversation_status_changed", "status": "pending",
            "conversation": {"id": 7, "meta": {"sender": {"identifier": ig}}}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        body, h = _signed(human)
        assert (await c.post(f"/webhooks/connect/{slug}", content=body, headers={**h, "x-chatwoot-signature": "bad"})).status_code == 401
        r = await c.post(f"/webhooks/connect/{slug}", content=body, headers=h)
        assert r.json() == {"applied": 1}
        async with tenant_tx(tenant) as conn:
            assert (await (await conn.execute("select ownership from conversations where id=%s", (conv["id"],))).fetchone())["ownership"] == "HUMAN"
        body, h = _signed(back)
        assert (await c.post(f"/webhooks/connect/{slug}", content=body, headers=h)).json() == {"applied": 1}
    async with tenant_tx(tenant) as conn:
        assert (await (await conn.execute("select ownership from conversations where id=%s", (conv["id"],))).fetchone())["ownership"] == "BOT"
