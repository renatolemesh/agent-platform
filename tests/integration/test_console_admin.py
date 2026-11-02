"""Console admin features: CSRF, TOTP 2FA (+ lockout, reset), LGPD screen, budget top-up, dashboards, config two-person
approval + canary, lead import upload, campaigns view, Facebook Login reconnection (Meta mocked)."""
import html
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pyotp
import pytest
import yaml
from fastapi import FastAPI
from pydantic import SecretStr

from agentplatform.api import console
from agentplatform.api.auth import COOKIE, csrf_token, hash_password, read_session
from agentplatform.core.config.registry import canary_bucket, load_yaml, registry
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.models.budget import BudgetService
from agentplatform.core.packages.contract import load_package
from agentplatform.core.security.keys import crypto, decrypt_field, encrypt_field
from agentplatform.settings import get_settings
from tests.integration.helpers import make_conversation

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
PW = "s3nha-forte-123"


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(console.router)
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://t")


async def _user(tenant, role="tenant_owner", name="Dona"):
    email = f"u-{uuid.uuid4().hex[:8]}@example.com"
    if tenant is None:
        async with platform_tx() as conn:
            row = await (await conn.execute(
                "insert into tenant_users (tenant_id, email_hash, email_enc, display_name, role, password_hash) "
                "values (null,%s,%s,%s,%s,%s) returning id",
                (crypto().lookup_hash(email, kind="email"), b"-", name, role, hash_password(PW)))).fetchone()
    else:
        async with tenant_tx(tenant) as conn:
            row = await (await conn.execute(
                "insert into tenant_users (tenant_id, email_hash, email_enc, display_name, role, password_hash) "
                "values (%s,%s,%s,%s,%s,%s) returning id",
                (tenant, crypto().lookup_hash(email, kind="email"),
                 await encrypt_field(conn, tenant, email, "tenant_users.email"), name, role, hash_password(PW)))).fetchone()
    return email, row["id"]


async def _login(c: httpx.AsyncClient, email: str) -> httpx.Response:
    return await c.post("/console/login", data={"email": email, "password": PW})


def _csrf(c: httpx.AsyncClient) -> str:
    return csrf_token(read_session((c.cookies.get(COOKIE) or "").strip('"')))     # cookie may be RFC-quoted


def _back(r: httpx.Response) -> str:
    """Where the OAuth callback page sends the browser: a same-origin refresh, so the strict session cookie goes along."""
    assert r.status_code == 200 and r.headers["referrer-policy"] == "no-referrer"
    return html.unescape(re.search(r'content="0;url=([^"]+)"', r.text).group(1))


async def _tenant_config(tenant):
    """Publish an active config for the test tenant (runtime resolves package and caps from it)."""
    async with platform_tx() as conn:
        slug = (await (await conn.execute("select slug from tenants where id=%s", (tenant,))).fetchone())["slug"]
    cfg = load_yaml(ROOT / "config/tenants/dev-consortium.yaml").model_copy(update={"tenant_slug": slug})
    async with tenant_tx(tenant) as conn:
        await registry.publish(conn, tenant, cfg, package_compliance=load_package(cfg.segment_package.name).manifest.compliance,
                               activate=True)
    return slug, cfg


async def test_csrf_is_required_on_posts(tenant):
    email, _ = await _user(tenant)
    async with _client() as c:
        await _login(c, email)
        assert (await c.post("/console/kill-switch", data={"scope": "tenant", "on": "0"})).status_code == 403
        r = await c.post("/console/kill-switch", data={"scope": "tenant", "on": "0", "csrf": _csrf(c)})
        assert r.status_code == 303
        r = await c.post("/console/kill-switch", data={"scope": "tenant", "on": "0"}, headers={"x-csrf-token": _csrf(c)})
        assert r.status_code == 303                                   # HTMX header variant
        assert (await c.post("/console/kill-switch", data={"scope": "tenant", "on": "0", "csrf": "forged"})).status_code == 403


async def test_totp_enroll_login_lockout_and_reset(tenant, monkeypatch):
    email, uid = await _user(tenant, role="seller", name="Vend")
    async with _client() as c:
        await _login(c, email)
        page = await c.get("/console/totp")
        secret = re.search(r"<code>([A-Z2-7]{16,})</code>", page.text).group(1)
        assert "<svg" in page.text
        r = await c.post("/console/totp/verify", data={"code": "000000", "csrf": _csrf(c)})
        assert r.status_code == 400
        page = await c.get("/console/totp")                                      # wrong code -> new secret issued
        secret = re.search(r"<code>([A-Z2-7]{16,})</code>", page.text).group(1)
        r = await c.post("/console/totp/verify", data={"code": pyotp.TOTP(secret).now(), "csrf": _csrf(c)})
        assert r.status_code == 303
    async with platform_tx() as conn:
        row = await (await conn.execute("select totp_secret_enc, totp_enabled_at from tenant_users where id=%s", (uid,))).fetchone()
    assert row["totp_enabled_at"] and secret.encode() not in bytes(row["totp_secret_enc"])     # stored encrypted

    async with _client() as c:
        r = await _login(c, email)
        assert r.status_code == 303 and r.headers["location"] == "/console/login/totp"
        assert (await c.get("/console/", follow_redirects=False)).status_code == 303            # no session yet
        assert (await c.post("/console/login/totp", data={"code": "123456"})).status_code == 401
        nxt = pyotp.TOTP(secret).at(time.time() + 30)                                          # step after enrollment
        r = await c.post("/console/login/totp", data={"code": nxt})
        assert r.status_code == 303 and (await c.get("/console/")).status_code == 200
    async with _client() as c:                                                                  # replay of the same code
        await _login(c, email)
        assert (await c.post("/console/login/totp", data={"code": nxt})).status_code == 401

    admin_email, _ = await _user(None, role="platform_admin", name="Admin")
    async with _client() as c:
        await _login(c, admin_email)
        assert "Vend" in (await c.get("/console/users")).text
        r = await c.post(f"/console/users/{uid}/totp-reset", data={"csrf": _csrf(c)})
        assert r.status_code == 303
    async with platform_tx() as conn:
        row = await (await conn.execute("select totp_enabled_at, failed_logins from tenant_users where id=%s", (uid,))).fetchone()
    assert row["totp_enabled_at"] is None and row["failed_logins"] == 0

    monkeypatch.setattr(get_settings(), "console_totp_required_roles", "seller")
    async with _client() as c:
        await _login(c, email)
        r = await c.get("/console/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/console/totp"              # must enroll first

    monkeypatch.setattr(get_settings(), "console_login_max_failures", 3)
    async with _client() as c:
        for _ in range(3):
            await c.post("/console/login", data={"email": email, "password": "errada"})
        r = await _login(c, email)
        assert r.status_code == 401 and "bloqueada" in r.text


async def test_lgpd_search_export_erase(tenant):
    email, _ = await _user(tenant)
    conv, igsid = await make_conversation(tenant)
    async with _client() as c:
        await _login(c, email)
        r = await c.post("/console/lgpd/search", data={"channel_user_id": igsid, "csrf": _csrf(c)})
        assert "Titular encontrado" in r.text
        cid = re.search(r"/console/lgpd/([0-9a-f-]{36})/export", r.text).group(1)
        exp = await c.post(f"/console/lgpd/{cid}/export", data={"csrf": _csrf(c)})
        assert exp.status_code == 200 and "attachment" in exp.headers["content-disposition"]
        assert exp.json()["messages"][0]["text"] == "oi"
        assert (await c.post(f"/console/lgpd/{cid}/erase", data={"confirm": "sim", "csrf": _csrf(c)})).status_code == 400
        r = await c.post(f"/console/lgpd/{cid}/erase", data={"confirm": "ELIMINAR", "csrf": _csrf(c)})
        assert r.status_code == 200 and "Dados eliminados" in r.text
    async with tenant_tx(tenant) as conn:
        m = await (await conn.execute("select text_masked from messages where conversation_id=%s", (conv["id"],))).fetchone()
        actions = {a["action"] for a in await (await conn.execute(
            "select action from audit_log where tenant_id=%s", (tenant,))).fetchall()}
    assert m["text_masked"] == "[eliminado]" and {"lgpd.search", "lgpd.export", "lgpd.erase"} <= actions


async def test_config_two_person_canary_promote_rollback(tenant):
    slug, cfg = await _tenant_config(tenant)
    a_email, _ = await _user(tenant, name="Ana")
    b_email, _ = await _user(tenant, name="Bia")
    doc = json.loads(cfg.model_dump_json())
    doc["budget"]["daily_cap_usd"] = "0.30"
    text = yaml.safe_dump(doc, allow_unicode=True)
    async with _client() as a, _client() as b:
        await _login(a, a_email)
        await _login(b, b_email)
        bad = await a.post("/console/config/preview", data={"yaml_text": text.replace(slug, "outro-tenant"), "csrf": _csrf(a)})
        assert bad.status_code == 400 and "não é deste tenant" in bad.text
        pv = await a.post("/console/config/preview", data={"yaml_text": text, "csrf": _csrf(a)})
        assert pv.status_code == 200 and "daily_cap_usd" in pv.text and "0.30" in pv.text
        r = await a.post("/console/config/propose", data={"yaml_text": text, "canary_percent": "50", "csrf": _csrf(a)})
        assert r.status_code == 303
        async with tenant_tx(tenant) as conn:
            draft = await (await conn.execute(
                "select id from config_versions where tenant_id=%s and status='draft' order by version desc limit 1", (tenant,))).fetchone()
        assert (await a.post(f"/console/config/{draft['id']}/approve", data={"csrf": _csrf(a)})).status_code == 409   # proposer
        assert (await b.post(f"/console/config/{draft['id']}/approve", data={"csrf": _csrf(b)})).status_code == 303
        async with tenant_tx(tenant) as conn:
            inside = next(u for u in (uuid.uuid4() for _ in range(500)) if canary_bucket(u) < 50)
            outside = next(u for u in (uuid.uuid4() for _ in range(500)) if canary_bucket(u) >= 50)
            assert str((await registry.for_conversation(conn, tenant, "dev", inside)).config.budget.daily_cap_usd) == "0.30"
            assert str((await registry.for_conversation(conn, tenant, "dev", outside)).config.budget.daily_cap_usd) != "0.30"
        assert "Canário em andamento" in (await a.get("/console/config")).text
        assert (await a.post("/console/config/canary/promote", data={"csrf": _csrf(a)})).status_code == 303
        async with tenant_tx(tenant) as conn:
            registry._cache.pop(str(tenant), None)
            assert str((await registry.active(conn, tenant, "dev")).config.budget.daily_cap_usd) == "0.30"
        assert (await a.post("/console/config/rollback", data={"csrf": _csrf(a)})).status_code == 303
        async with tenant_tx(tenant) as conn:
            assert str((await registry.active(conn, tenant, "dev")).config.budget.daily_cap_usd) != "0.30"


async def test_budget_topup_proposes_config_and_keeps_key_cap(tenant):
    await _tenant_config(tenant)
    email, _ = await _user(tenant)
    before = {k: v["cap"] for k, v in (await BudgetService(0, 0).usage()).items()}
    async with _client() as c:
        await _login(c, email)
        assert (await c.post("/console/budget/topup", data={"gross_usd": "0", "csrf": _csrf(c)})).status_code == 400
        r = await c.post("/console/budget/topup", data={"gross_usd": "10", "fee_usd": "0.55", "note": "recarga", "csrf": _csrf(c)})
        assert r.status_code == 303
        page = await c.get("/console/budget")
        assert page.status_code == 200 and "9,45" in page.text and "LLM_TOTAL_CAP_USD" in page.text
    async with tenant_tx(tenant) as conn:
        t = await (await conn.execute("select net_usd, config_version_id from budget_topups where tenant_id=%s", (tenant,))).fetchone()
        v = await (await conn.execute("select status, document from config_versions where id=%s", (t["config_version_id"],))).fetchone()
    assert str(t["net_usd"]).startswith("9.45") and v["status"] == "draft"
    assert float(v["document"]["budget"]["net_credit_usd"]) > 9.4
    after = {k: v["cap"] for k, v in (await BudgetService(0, 0).usage()).items()}
    assert {k: after[k] for k in before if k.startswith("key:")} == {k: v for k, v in before.items() if k.startswith("key:")}


async def test_dashboards_and_campaigns_render(tenant):
    from agentplatform.core.campaigns.engine import CampaignSpec, create_campaign
    email, _ = await _user(tenant)
    await make_conversation(tenant)
    async with tenant_tx(tenant) as conn:
        await create_campaign(conn, tenant, CampaignSpec("Reativação", ["novo"], "reativacao_v1", {"reativacao_v1": 1.0}))
    async with _client() as c:
        await _login(c, email)
        d = await c.get("/console/dashboards")
        assert d.status_code == 200 and "Funil por estágio" in d.text and "Novo" in d.text
        k = await c.get("/console/campaigns")
        assert k.status_code == 200 and "Reativação" in k.text


async def test_import_upload_preview_commit_rollback(tenant):
    await _tenant_config(tenant)
    email, _ = await _user(tenant)
    suffix = uuid.uuid4().int % 10000
    csv_text = ("telefone;nome;legal_basis;source;purpose;consent_evidence\n"
                f"(11) 99876-{suffix:04d};maria souza;legitimate_interest;feira;contato comercial;\n"
                f";sem id;legitimate_interest;feira;contato comercial;\n"
                f"(11) 99877-{suffix:04d};joao;consent;site;marketing;\n")
    async with _client() as c:
        await _login(c, email)
        r = await c.post("/console/imports/preview", data={"map_phone": "telefone", "map_name": "nome", "csrf": _csrf(c)},
                         files={"file": ("leads.csv", csv_text.encode(), "text/csv")})
        assert r.status_code == 200 and 'data-imported="1"' in r.text
        assert "no_strong_identifier" in r.text and "consent_evidence_missing" in r.text
        b64 = re.search(r'name="file_b64" value="([^"]+)"', r.text).group(1)
        mapping = re.search(r'name="mapping" value="([^"]+)"', r.text).group(1).replace("&#34;", '"')
        r = await c.post("/console/imports/commit", data={"file_b64": b64, "file_name": "leads.csv", "mapping": mapping,
                                                          "csrf": _csrf(c)})
        assert r.status_code == 303
        page = await c.get("/console/imports")
        batch = re.search(r"/console/imports/([0-9a-f-]{36})/rollback", page.text).group(1)
        assert (await c.post(f"/console/imports/{batch}/rollback", data={"csrf": _csrf(c)})).status_code == 303
    async with tenant_tx(tenant) as conn:
        st = await (await conn.execute("select status, counts from import_batches where id=%s", (batch,))).fetchone()
    assert st["status"] == "rolled_back" and st["counts"]["imported"] == 1


def _meta_mock(page_id: str, subscribed: list | None = None, subscribe_ok: bool = True):
    def handler(req: httpx.Request) -> httpx.Response:
        q = dict(req.url.params)
        if req.url.path.endswith("/subscribed_apps"):
            if req.method == "GET":      # another app subscribed to the page does not count as ours
                return httpx.Response(200, json={"data": [{"id": "999", "name": "Outro app", "subscribed_fields": ["feed"]}]})
            if subscribed is not None:
                subscribed.append((req.url.path.rsplit("/", 2)[-2], q))
            return httpx.Response(200, json={"success": True}) if subscribe_ok else httpx.Response(403, json={"error": {"code": 200}})
        if req.url.path.endswith("/oauth/access_token"):
            if q.get("grant_type") == "fb_exchange_token":
                return httpx.Response(200, json={"access_token": "EAAlong", "expires_in": 5184000})
            return httpx.Response(200, json={"access_token": "EAAshort"} if q.get("code") == "good" else {"error": {}})
        if req.url.path.endswith("/me/accounts"):
            assert q["access_token"] == "EAAlong"
            return httpx.Response(200, json={"data": [{"id": "other", "access_token": "EAAother"},
                                                      {"id": page_id, "access_token": "EAApage", "name": "Pagina"}]})
        if req.url.path.endswith("/debug_token"):
            return httpx.Response(200, json={"data": {"is_valid": True, "scopes": ["instagram_manage_messages"]}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def test_facebook_login_reconnect(tenant, monkeypatch):
    email, _ = await _user(tenant)
    monkeypatch.setattr(get_settings(), "meta_app_id", "1234")
    monkeypatch.setattr(get_settings(), "meta_app_secret", SecretStr("app-secret"))
    async with tenant_tx(tenant) as conn:
        acc = await (await conn.execute("select id, external_page_id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        await conn.execute("update channel_accounts set token_status='invalid' where id=%s", (acc["id"],))
        await conn.execute("insert into kill_switches (tenant_id, scope, scope_ref, is_on, reason) values (%s,'channel','instagram',true,'token_invalid')",
                           (tenant,))
    subscribed: list = []
    monkeypatch.setattr(console, "OAUTH_TRANSPORT", _meta_mock(acc["external_page_id"], subscribed))
    async with _client() as c:
        await _login(c, email)
        assert "Reconectar com o Facebook" in (await c.get("/console/channels")).text
        r = await c.post(f"/console/channels/{acc['id']}/reconnect", data={"csrf": _csrf(c)})
        assert r.status_code == 303
        dialog = urlparse(r.headers["location"])
        q = parse_qs(dialog.query)
        assert dialog.netloc == "www.facebook.com" and q["client_id"] == ["1234"]
        assert q["redirect_uri"][0].endswith("/console/channels/instagram/callback")
        state = q["state"][0]
        bad = await c.get("/console/channels/instagram/callback", params={"state": state + "x", "code": "good"})
        assert "error=state_bad_signature" in _back(bad)
        ok = await c.get("/console/channels/instagram/callback", params={"state": state, "code": "good"})
        assert _back(ok) == "/console/channels?ok=1"
        again = await c.get("/console/channels/instagram/callback", params={"state": state, "code": "good"})
        assert "error=" in _back(again)                                     # single use (and cookie consumed)
        # the page is subscribed to our app's webhooks right away (another app's subscription does not count)
        assert subscribed == [(acc["external_page_id"], {"subscribed_fields": "feed", "access_token": "EAApage"})]
        # Meta refuses the subscription: still connected, with a warning (the 10-min job retries)
        monkeypatch.setattr(console, "OAUTH_TRANSPORT", _meta_mock(acc["external_page_id"], subscribe_ok=False))
        r = await c.post(f"/console/channels/{acc['id']}/reconnect", data={"csrf": _csrf(c)})
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
        back = _back(await c.get("/console/channels/instagram/callback", params={"state": state, "code": "good"}))
        assert back == "/console/channels?ok=1&warn=subscribe_failed"
        assert "inscrição da Página nos avisos" in (await c.get(back)).text
    async with tenant_tx(tenant) as conn:
        row = await (await conn.execute("select token_enc, token_status, scopes from channel_accounts where id=%s", (acc["id"],))).fetchone()
        ks = await (await conn.execute("select is_on, reason from kill_switches where tenant_id=%s and scope='channel'", (tenant,))).fetchone()
        audit = await (await conn.execute("select after from audit_log where tenant_id=%s and action='channel.reconnect'", (tenant,))).fetchone()
        token = await decrypt_field(conn, tenant, row["token_enc"], "channel_accounts.token")
    assert token == "EAApage" and row["token_status"] == "valid" and row["scopes"] == ["instagram_manage_messages"]
    assert ks["is_on"] is False and audit["after"]["kill_switch_released"] is True


async def test_platform_admin_picks_a_tenant(tenant):
    await _tenant_config(tenant)
    admin_email, admin_id = await _user(None, role="platform_admin", name="Admin")
    async with _client() as c:
        await _login(c, admin_email)
        r = await c.get("/console/budget", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/console/tenants"
        assert str(tenant) in (await c.get("/console/tenants")).text
        assert (await c.post("/console/tenants/select", data={"tenant_id": str(tenant)})).status_code == 403   # no CSRF
        r = await c.post("/console/tenants/select", data={"tenant_id": str(tenant), "csrf": _csrf(c)})
        assert r.status_code == 303
        assert (await c.get("/console/budget")).status_code == 200
    async with tenant_tx(tenant) as conn:
        a = await (await conn.execute("select actor from audit_log where tenant_id=%s and action='console.act_as_tenant'", (tenant,))).fetchone()
    assert str(a["actor"]) == str(admin_id)
