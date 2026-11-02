"""Public lead form hardening (F2-FORM-02): CSRF token, single use under concurrent submits, security headers,
per-IP rate limit, no forbidden fields stored."""
import asyncio
import re

import httpx
import pytest

from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier
from agentplatform.core.models.fake import ScriptedGateway
from agentplatform.core.packages.contract import load_package
from agentplatform.core.security.keys import decrypt_field
from agentplatform.core.tools.lead_form import create_link
from agentplatform.runtime import Runtime, TenantRuntime, set_runtime
from tests.integration.helpers import make_conversation
from tests.integration.test_pipeline import fast_cfg

pytestmark = pytest.mark.db


async def _setup(tenant):
    rt = Runtime(adapters={}, gateway=ScriptedGateway({}), inbox=FallbackInbox([LogNotifier()]))
    rt._tenants[str(tenant)] = (1e18, TenantRuntime(tenant, fast_cfg(), None, load_package("consortium"), None))
    set_runtime(rt)
    conv, _ = await make_conversation(tenant)
    async with tenant_tx(tenant) as conn:
        lead = await (await conn.execute("select id from leads where contact_id=%s", (conv["contact_id"],))).fetchone()
        url = await create_link(conn, tenant, lead["id"], "http://t")
    from agentplatform.api.main import create_app
    return create_app(on_persisted=lambda created: None), url.split("http://t")[1], lead["id"]


async def test_form_csrf_single_use_and_headers(tenant):
    app, path, lead_id = await _setup(tenant)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        page = await c.get(path)
        assert page.status_code == 200
        assert "default-src 'none'" in page.headers["content-security-policy"]
        assert page.headers["x-frame-options"] == "DENY" and page.headers["cache-control"] == "no-store"
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
        base = {"first_name": "Ana", "consent": "1", "cpf": "529.982.247-25"}
        assert (await c.post(path, data={**base, "csrf": "0" * 64})).status_code == 403
        r1, r2 = await asyncio.gather(c.post(path, data={**base, "csrf": csrf}), c.post(path, data={**base, "csrf": csrf}))
        assert {r1.status_code, r2.status_code} <= {200, 410}
        assert (await c.get(path)).status_code == 410                      # used
    async with tenant_tx(tenant) as conn:
        forms = await (await conn.execute("select fields_enc from lead_forms where lead_id=%s and submitted_at is not null", (lead_id,))).fetchall()
        consents = await (await conn.execute(
            "select count(*) as n from consents c join leads l on l.contact_id=c.contact_id where l.id=%s and c.purpose='lead_form'", (lead_id,))).fetchone()
        handoffs = await (await conn.execute("select count(*) as n from handoffs where lead_id=%s", (lead_id,))).fetchone()
        stored = await decrypt_field(conn, tenant, forms[0]["fields_enc"], "lead_forms.fields")
    assert len(forms) == 1 and consents["n"] == 1 and handoffs["n"] == 1
    assert "cpf" not in stored and "Ana" in stored


async def test_form_rate_limited_per_ip(tenant):
    app, path, _ = await _setup(tenant)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        codes = [(await c.get(path)).status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and 429 in codes[10:]
