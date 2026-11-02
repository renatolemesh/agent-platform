"""Prompt lab: edit a node's prompt in the console -> immutable tenant version + config draft -> the simulator runs the
draft with the edited prompt, while the active config (customers) is untouched."""
import re

import pytest

from agentplatform.core.agent import prompts as P
from agentplatform.core.db.pool import tenant_tx
from tests.integration.test_console_admin import _client, _csrf, _login, _user
from tests.integration.test_simulator import _setup

pytestmark = pytest.mark.db
MARK = "INSTRUCAO-DE-TESTE-7731"


async def test_edit_prompt_draft_and_simulate(tenant):
    from agentplatform.runtime import get_runtime
    active, _ = await _setup(tenant)
    gw = get_runtime().gateway
    email, _ = await _user(tenant)
    async with _client() as c:
        await _login(c, email)
        page = await c.get("/console/prompts?node=respond")
        assert page.status_code == 200 and "Escrever a resposta" in page.text and "turn_input" in page.text
        base = re.search(r'name="base_version" value="([^"]+)"', page.text).group(1)
        body = P.raw_body(base)

        bad = await c.post("/console/prompts/save", data={"node": "respond", "base_version": base, "csrf": _csrf(c),
                                                          "body": body + "\n{{ variavel_que_nao_existe }}"})
        assert bad.status_code == 400 and "variavel_que_nao_existe" in bad.text

        edited = body.replace("{# role: user #}", f"- {MARK}\n{{# role: user #}}", 1)
        r = await c.post("/console/prompts/save", data={"node": "respond", "base_version": base, "body": edited,
                                                        "note": "teste", "csrf": _csrf(c)})
        assert r.status_code == 303
        label = re.search(r"saved=(respond@t-[0-9a-f]{8})", r.headers["location"]).group(1)
        draft = re.search(r"draft=([0-9a-f-]{36})", r.headers["location"]).group(1)
        shown = await c.get(r.headers["location"])
        assert "Versão salva" in shown.text and MARK in shown.text and f"/console/simulator?v={draft}" in shown.text

        sim = await c.get(f"/console/simulator?v={draft}")
        session = re.search(r'name="s" value="(sim-[0-9a-f]{8})"', sim.text).group(1)
        await c.post("/console/simulator/send", data={"s": session, "v": draft, "text": "oi", "csrf": _csrf(c)})
    respond_prompts = [m for node, m in gw.calls if node == "respond"]
    assert respond_prompts and MARK in respond_prompts[-1][0]["content"]          # the draft's edited prompt was used
    async with tenant_tx(tenant) as conn:
        row = await (await conn.execute("select tenant_id, base_version from prompt_templates where label=%s", (label,))).fetchone()
        doc = await (await conn.execute("select status, document from config_versions where id=%s", (draft,))).fetchone()
        now_active = await (await conn.execute("select id from config_versions where tenant_id=%s and status='active'", (tenant,))).fetchone()
    assert row["tenant_id"] == tenant and row["base_version"] == base
    assert doc["status"] == "draft" and doc["document"]["models"]["respond"]["prompt_version"] == label
    assert now_active["id"] == active                                              # customers still on the active config


def test_validation_rules():
    base = "respond@v3"
    assert P.validate(P.raw_body(base), base) == []
    assert any("seções" in e for e in P.validate("sem seções {{ persona }}", base))
    assert any("sintaxe" in e for e in P.validate("{# role: system #}\n{% if %}\n{# role: user #}\nx", base))
