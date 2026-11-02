"""First Facebook Login connection adopts the IDs of the single granted page with an Instagram account (no manual ID
lookup); a reconnection never switches the account, and an ambiguous grant is refused."""
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import SecretStr

from agentplatform.api import console
from agentplatform.core.db.pool import tenant_tx
from agentplatform.settings import get_settings
from tests.integration.test_console_admin import _back, _client, _csrf, _login, _user

pytestmark = pytest.mark.db


def _meta(pages: list[dict]):
    def handler(req: httpx.Request) -> httpx.Response:
        q = dict(req.url.params)
        if req.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "EAAlong" if q.get("grant_type") else "EAAshort"})
        if req.url.path.endswith("/me/accounts"):
            return httpx.Response(200, json={"data": pages})
        if req.url.path.endswith("/debug_token"):
            return httpx.Response(200, json={"data": {"is_valid": True, "scopes": ["instagram_manage_messages"]}})
        if req.url.path.endswith("/subscribed_apps"):
            return httpx.Response(200, json={"data": []} if req.method == "GET" else {"success": True})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def _connect(c, acc_id) -> str:
    r = await c.post(f"/console/channels/{acc_id}/reconnect", data={"csrf": _csrf(c)})
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    return _back(await c.get("/console/channels/instagram/callback", params={"state": state, "code": "good"}))


async def test_first_connection_adopts_page_then_never_switches(tenant, monkeypatch):
    email, _ = await _user(tenant)
    monkeypatch.setattr(get_settings(), "meta_app_id", "1234")
    monkeypatch.setattr(get_settings(), "meta_app_secret", SecretStr("app-secret"))
    async with tenant_tx(tenant) as conn:
        acc = await (await conn.execute("select id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
    ig, page = f"IG-{uuid.uuid4().hex[:8]}", f"PAGE-{uuid.uuid4().hex[:8]}"      # unique per run (global unique key)
    real = [{"id": page, "access_token": "EAApage", "instagram_business_account": {"id": ig}},
            {"id": "PAGE-NO-IG", "access_token": "EAAx"}]
    async with _client() as c:
        await _login(c, email)
        monkeypatch.setattr(console, "OAUTH_TRANSPORT", _meta(real + [
            {"id": "PAGE-2", "access_token": "EAAy", "instagram_business_account": {"id": "IG-2"}}]))
        back = await _connect(c, acc["id"])
        assert "error=select_one_page" in back                                # two pages with Instagram: ambiguous
        page_text = (await c.get(back)).text                                  # an instruction, not the code
        assert "Mais de uma Página com Instagram" in page_text and "select_one_page" not in page_text
        monkeypatch.setattr(console, "OAUTH_TRANSPORT", _meta(real))
        assert await _connect(c, acc["id"]) == "/console/channels?ok=1"
        async with tenant_tx(tenant) as conn:
            row = await (await conn.execute("select external_page_id, external_account_id from channel_accounts where id=%s",
                                            (acc["id"],))).fetchone()
        assert (row["external_page_id"], row["external_account_id"]) == (page, ig)
        # reconnection granting only another page: refused, IDs kept
        monkeypatch.setattr(console, "OAUTH_TRANSPORT", _meta([
            {"id": "PAGE-2", "access_token": "EAAy", "instagram_business_account": {"id": "IG-2"}}]))
        assert "error=page_not_granted" in await _connect(c, acc["id"])
    async with tenant_tx(tenant) as conn:
        row = await (await conn.execute("select external_account_id from channel_accounts where id=%s", (acc["id"],))).fetchone()
    assert row["external_account_id"] == ig


async def test_first_connection_refuses_account_already_connected(tenant, monkeypatch):
    """The granted Instagram account belongs to another channel account: clear error, nothing stored."""
    email, _ = await _user(tenant)
    monkeypatch.setattr(get_settings(), "meta_app_id", "1234")
    monkeypatch.setattr(get_settings(), "meta_app_secret", SecretStr("app-secret"))
    async with tenant_tx(tenant) as conn:
        acc = await (await conn.execute("select id, external_account_id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        taken = await (await conn.execute(
            "insert into channel_accounts (tenant_id, channel, external_account_id, external_page_id) values (%s,'instagram',%s,%s) "
            "returning external_account_id", (tenant, f"IG-{uuid.uuid4().hex[:8]}", f"P-{uuid.uuid4().hex[:8]}"))).fetchone()
    monkeypatch.setattr(console, "OAUTH_TRANSPORT", _meta([
        {"id": "PAGE-X", "access_token": "EAApage", "instagram_business_account": {"id": taken["external_account_id"]}}]))
    async with _client() as c:
        await _login(c, email)
        assert "error=account_in_use" in await _connect(c, acc["id"])
    async with tenant_tx(tenant) as conn:
        row = await (await conn.execute("select external_account_id, token_enc from channel_accounts where id=%s", (acc["id"],))).fetchone()
    assert row["external_account_id"] == acc["external_account_id"] and row["token_enc"] is None
