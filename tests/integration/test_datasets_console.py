"""Tables the tenant maintains in the console (package DatasetSpec): register in the editor, validation in Portuguese,
draft -> preview -> activate, spreadsheet import with label headers, export, going back to an older version, the
simulator using a draft before it is live, the usage flag as a config draft, and the optional two-person rule."""
import json
import re
import uuid

import pytest

from agentplatform.core.config.registry import load_yaml, registry
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.knowledge import datasets as D
from agentplatform.core.packages.contract import load_package
from agentplatform.runtime import Runtime, set_runtime
from tests.integration.test_console_admin import ROOT, _client, _csrf, _login, _user

pytestmark = pytest.mark.db
COLS = ["plan_id", "name", "goal", "group", "credit", "term_months", "admin_fee_pct", "reserve_fund_pct",
        "insurance_monthly_pct", "installment", "adjustment_index"]


async def _config(tenant, *, two_person=False):
    from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier
    from agentplatform.core.models.fake import ScriptedGateway
    async with platform_tx() as conn:
        slug = (await (await conn.execute("select slug from tenants where id=%s", (tenant,))).fetchone())["slug"]
    cfg = load_yaml(ROOT / "config/tenants/dev-consortium.yaml").model_copy(update={"tenant_slug": slug})
    ops = cfg.operations
    cfg = cfg.model_copy(update={"operations": ops.model_copy(update={
        "datasets": ops.datasets.model_copy(update={"two_person_activation": two_person})})})
    async with tenant_tx(tenant) as conn:
        await registry.publish(conn, tenant, cfg, package_compliance=load_package("consortium").manifest.compliance,
                               activate=True)
    set_runtime(Runtime(adapters={}, gateway=ScriptedGateway({}), inbox=FallbackInbox([LogNotifier()])))


def _grid(rows: list[dict], csrf: str, note: str = "") -> dict:
    """Form body as the editor posts it: one list per column, in row order (+ a trailing blank row)."""
    return {"name": "plans", "note": note, "csrf": csrf,
            **{f"c_{c}": [str(r.get(c, "")) for r in [*rows, {}]] for c in COLS}}


ROW_A = {"plan_id": "IMOVEL-300", "name": "Imóvel 200 meses", "goal": "real_estate", "credit": "R$ 300.000,00",
         "term_months": "200", "admin_fee_pct": "20%", "reserve_fund_pct": "2", "adjustment_index": "INCC"}
ROW_B = {"plan_id": "AUTO-80", "goal": "vehicle", "credit": "80.000", "term_months": "60", "admin_fee_pct": "14,5"}


async def _active(tenant):
    async with tenant_tx(tenant) as conn:
        return await D.in_use(conn, "consortium", "plans", prefer_drafts=False)


async def test_register_preview_activate_import_and_go_back(tenant):
    await _config(tenant)
    email, _ = await _user(tenant)
    async with _client() as c:
        await _login(c, email)
        page = await c.get("/console/datasets")
        assert page.status_code == 200 and "Tabela de planos" in page.text and "Nenhuma versão cadastrada" in page.text

        bad = await c.post("/console/datasets/save", data=_grid([ROW_A, {**ROW_B, "credit": "muito", "plan_id": "IMOVEL-300"}],
                                                                       _csrf(c)))
        assert bad.status_code == 400
        assert "linha 2" in bad.text and "Crédito (R$): número inválido" in bad.text and "repetido" not in bad.text
        assert "IMOVEL-300" in bad.text                                    # what was typed is kept on screen
        assert await _active(tenant) is None

        r = await c.post("/console/datasets/save", data=_grid([ROW_A, ROW_B], _csrf(c), "tabela de outubro"))
        assert r.status_code == 303 and "ok=draft" in r.headers["location"]
        page = await c.get(r.headers["location"])
        assert "Prévia do rascunho v1" in page.text and "Ativar v1" in page.text and "tabela de outubro" in page.text
        assert "R$ 1.830,00" in page.text                                  # 300000 x 1.22 / 200, computed preview column
        assert await _active(tenant) is None                               # nothing live before activation

        # the simulator reads the draft; customers do not
        async with tenant_tx(tenant) as conn:
            sim = await D.in_use(conn, "consortium", "plans", prefer_drafts=True)
        token = D.PREFER_DRAFTS.set(True)
        try:
            async with tenant_tx(tenant) as conn:
                assert (await D.in_use(conn, "consortium", "plans"))["id"] == sim["id"]
        finally:
            D.PREFER_DRAFTS.reset(token)
        assert sim["status"] == "draft"
        assert "rascunho v1" in (await c.get("/console/simulator")).text

        r = await c.post("/console/datasets/activate", data={"name": "plans", "dataset_id": str(sim["id"]), "csrf": _csrf(c)})
        assert r.status_code == 303
        live = await _active(tenant)
        assert live["id"] == sim["id"] and live["version"] == 1
        async with tenant_tx(tenant) as conn:
            rows = await D.rows_of(conn, live["id"])
        assert rows[0]["credit"] == "300000.00" and rows[0]["admin_fee_pct"] == "20" and rows[1]["admin_fee_pct"] == "14.5"

        same = await c.post("/console/datasets/save", data=_grid([ROW_A, ROW_B], _csrf(c)))
        assert same.status_code == 400 and "nada mudou" in same.text

        csv = ("Código;Objetivo;Crédito (R$);Prazo (meses);Taxa de administração total (%);Observação interna\n"
               "IMOVEL-300;Imóvel;300.000,00;200;19;não usar\n").encode("utf-8")
        r = await c.post("/console/datasets/upload", data={"name": "plans", "csrf": _csrf(c)},
                         files={"file": ("planos.csv", csv, "text/csv")})
        assert r.status_code == 303
        page = await c.get(r.headers["location"])
        assert "Observação interna" in page.text                            # unknown column reported, not imported
        assert "0 nova(s) · 1 alterada(s) · 1 removida(s)" in page.text
        v2 = re.search(r'name="dataset_id" value="([0-9a-f-]{36})"><button class="btn-sm btn-primary">', page.text).group(1)
        await c.post("/console/datasets/activate", data={"name": "plans", "dataset_id": v2, "csrf": _csrf(c)})
        assert (await _active(tenant))["version"] == 2

        export = await c.get("/console/datasets/export?name=plans")
        text = export.content.decode("utf-8-sig")
        assert export.status_code == 200 and text.startswith("Código;Nome do plano;Objetivo") and "Imóvel" in text
        assert "300000,00" in text and "Observação" not in text
        model = (await c.get("/console/datasets/export?name=plans&template=1")).content.decode("utf-8-sig")
        assert "IMOVEL-300-200" in model

        r = await c.post("/console/datasets/activate", data={"name": "plans", "dataset_id": str(live["id"]), "csrf": _csrf(c)})
        assert r.status_code == 303 and (await _active(tenant))["version"] == 1   # back to v1
    async with tenant_tx(tenant) as conn:
        actions = [r["action"] for r in await (await conn.execute(
            "select action from audit_log where tenant_id=%s and action like 'dataset.%%' order by id", (tenant,))).fetchall()]
    assert actions == ["dataset.draft", "dataset.activate", "dataset.draft", "dataset.activate", "dataset.activate"]


async def test_flag_draft_discard_roles_and_two_person(tenant):
    await _config(tenant, two_person=True)
    owner, _ = await _user(tenant)
    other, _ = await _user(tenant, name="Sócia")
    reviewer, _ = await _user(tenant, role="reviewer", name="Revisor")
    async with _client() as c:
        await _login(c, owner)
        r = await c.post("/console/datasets/flag", data={"name": "plans", "on": "1", "csrf": _csrf(c)})
        draft_cfg = re.search(r"flagdraft=([0-9a-f-]{36})", r.headers["location"]).group(1)
        async with tenant_tx(tenant) as conn:
            doc = (await (await conn.execute("select status, document from config_versions where id=%s", (draft_cfg,))).fetchone())
        assert doc["status"] == "draft" and doc["document"]["operations"]["feature_flags"]["consortium.simulation_enabled"] is True

        await c.post("/console/datasets/save", data=_grid([ROW_B], _csrf(c)))
        async with tenant_tx(tenant) as conn:
            d = await D.in_use(conn, "consortium", "plans", prefer_drafts=True)
        mine = await c.post("/console/datasets/activate", data={"name": "plans", "dataset_id": str(d["id"]), "csrf": _csrf(c)})
        assert mine.status_code == 409 and "duas pessoas" in mine.text and await _active(tenant) is None
        await c.post("/console/datasets/discard", data={"name": "plans", "csrf": _csrf(c)})
        async with tenant_tx(tenant) as conn:
            assert await D.in_use(conn, "consortium", "plans", prefer_drafts=True) is None
        await c.post("/console/datasets/save", data=_grid([ROW_B], _csrf(c)))
        async with tenant_tx(tenant) as conn:
            d = await D.in_use(conn, "consortium", "plans", prefer_drafts=True)
    async with _client() as c:
        await _login(c, reviewer)
        assert (await c.get("/console/datasets")).status_code == 200            # reviewers see it
        assert (await c.post("/console/datasets/save", data=_grid([ROW_A], _csrf(c)))).status_code == 403
    async with _client() as c:
        await _login(c, other)
        ok = await c.post("/console/datasets/activate", data={"name": "plans", "dataset_id": str(d["id"]), "csrf": _csrf(c)})
        assert ok.status_code == 303 and (await _active(tenant))["id"] == d["id"]


async def _new_tenant():
    tid = uuid.uuid4()
    async with platform_tx() as conn:
        await conn.execute(
            "insert into tenants (id, slug, display_name, segment_package, segment_package_version) values (%s,%s,%s,%s,%s)",
            (tid, f"t-{tid.hex[:8]}", "Other tenant", "consortium", "0.1.0"))
    return tid


async def test_other_tenant_cannot_touch_versions(tenant):
    await _config(tenant)
    email, _ = await _user(tenant)
    async with _client() as c:
        await _login(c, email)
        await c.post("/console/datasets/save", data=_grid([ROW_B], _csrf(c)))
    async with tenant_tx(tenant) as conn:
        d = await D.in_use(conn, "consortium", "plans", prefer_drafts=True)
    other = await _new_tenant()
    await _config(other)
    intruder, _ = await _user(other)
    async with _client() as c:
        await _login(c, intruder)
        r = await c.post("/console/datasets/activate", data={"name": "plans", "dataset_id": str(d["id"]), "csrf": _csrf(c)})
        assert r.status_code == 404
        assert (await c.get(f"/console/datasets/export?name=plans&dataset_id={d['id']}")).status_code == 404
    async with tenant_tx(tenant) as conn:
        assert (await D.in_use(conn, "consortium", "plans", prefer_drafts=True))["status"] == "draft"


def test_rows_json_is_stable():
    from agentplatform_packages.consortium import PlanRow
    valid, errors, _ = D.validate_rows([ROW_A], PlanRow)
    assert not errors and json.loads(json.dumps(valid))[0]["term_months"] == 200
