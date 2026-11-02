"""Phase 3: plans table import/activation/rollback, simulation through the real turn."""
from pathlib import Path

import pytest
import yaml

from agentplatform.core.agent.turn import TurnDeps, TurnRunner
from agentplatform.core.config.schema import PlatformConfig
from agentplatform.core.db.pool import platform_tx
from agentplatform.core.knowledge.datasets import activate, import_dataset, parse_table, rollback
from agentplatform.core.models.fake import ScriptedGateway, analysis, draft
from agentplatform_packages.consortium import PlanRow
from tests.integration.test_turn import NOON, PKG, inbound

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
CSV = b"plan_id;goal;credit;term_months;admin_fee_pct;reserve_fund_pct;insurance_monthly_pct\n" \
      b"V80;vehicle;80000;60;14;2;0.04\nV100;vehicle;100000;72;14;2;0.04\nI300;real_estate;300000;200;20;2;0.03\n"


async def _clean(conn):
    await conn.execute("delete from domain_rows where dataset_id in (select id from domain_datasets where package='consortium' and dataset='plans_test')")
    await conn.execute("delete from domain_datasets where package='consortium' and dataset='plans_test'")


async def test_import_preview_activate_rollback(db):
    rows = parse_table(CSV, "planos.csv")
    async with platform_tx() as conn:
        await _clean(conn)
        bad = await import_dataset(conn, tenant_id=None, package="consortium", dataset="plans_test",
                                   rows=rows + [{"plan_id": "X", "goal": "spaceship"}], schema=PlanRow)
        assert bad.errors and bad.dataset_id is None                     # invalid rows block the import
        p1 = await import_dataset(conn, tenant_id=None, package="consortium", dataset="plans_test", rows=rows, schema=PlanRow)
        await activate(conn, p1.dataset_id, None)
        p2 = await import_dataset(conn, tenant_id=None, package="consortium", dataset="plans_test", rows=rows[:2], schema=PlanRow)
        assert p2.removed == 1 and p2.added == 0                         # diff preview
        await activate(conn, p2.dataset_id, None)
        back = await rollback(conn, "consortium", "plans_test", None)
        assert back == p1.dataset_id
        await _clean(conn)


async def test_simulation_numbers_come_from_tool(tenant):
    async with platform_tx() as conn:
        await conn.execute("delete from domain_rows where dataset_id in (select id from domain_datasets where package='consortium' and dataset='plans')")
        await conn.execute("delete from domain_datasets where package='consortium' and dataset='plans'")
        p = await import_dataset(conn, tenant_id=None, package="consortium", dataset="plans", rows=parse_table(CSV, "p.csv"), schema=PlanRow)
        await activate(conn, p.dataset_id, None)
    d = yaml.safe_load((ROOT / "config/tenants/dev-consortium.yaml").read_text(encoding="utf-8"))
    d["operations"]["feature_flags"]["consortium.simulation_enabled"] = True
    cfg = PlatformConfig.model_validate(d)
    conv = await inbound(tenant, ["quero simular um carro de 80 mil em 60 meses"])
    from agentplatform.core.db.pool import tenant_tx
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set stage='qualificando' where contact_id=%s", (conv["contact_id"],))
    g = ScriptedGateway({
        "analyze_turn": [analysis(intents=["simulation_request"], stage="qualificando", evidence=["simular"],
                                  slots=[("goal", "vehicle", "um carro"), ("credit_amount", "80 mil", "carro de 80 mil"),
                                         ("term_months", "60", "em 60 meses")])],
        # the minimum profile is complete, so the pre-registration link of this turn goes in the reply
        "respond": [lambda msgs: draft("Pela tabela vigente, a carta de 80 mil em 60 meses fica em parcelas estimadas de R$ 1.546,67, "
                                       "com taxa de administração. Quer seguir com o pré-cadastro? "
                                       + next(w for w in msgs[0]["content"].split() if "/f/" in w))],
        "output_judge": [{"violations": [], "explanation": ""}]})
    res = await TurnRunner(TurnDeps(config=cfg, package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    tools = res.trace["tools"]
    assert "simulate_consortium" in tools
    # 80000 * (1 + 0.14 + 0.02) / 60 = 1546.67 -> sourced from the tool, guard passes
    assert res.status == "committed" and res.outbox_ids
    async with platform_tx() as conn:
        await conn.execute("update domain_datasets set status='retired' where package='consortium' and dataset='plans'")


async def _plans_active(rows_csv: bytes | None):
    async with platform_tx() as conn:
        await conn.execute("delete from domain_rows where dataset_id in (select id from domain_datasets where package='consortium' and dataset='plans')")
        await conn.execute("delete from domain_datasets where package='consortium' and dataset='plans'")
        if rows_csv:
            p = await import_dataset(conn, tenant_id=None, package="consortium", dataset="plans",
                                     rows=parse_table(rows_csv, "p.csv"), schema=PlanRow)
            await activate(conn, p.dataset_id, None)


def _sim_cfg() -> PlatformConfig:
    d = yaml.safe_load((ROOT / "config/tenants/dev-consortium.yaml").read_text(encoding="utf-8"))
    d["operations"]["feature_flags"]["consortium.simulation_enabled"] = True
    return PlatformConfig.model_validate(d)


async def test_amount_alone_shows_the_table_entry_unasked(tenant):
    """The customer only says the amount: no term question, the code simulates (every goal) and the reply pulls them
    with the rounded entry installment from the table."""
    await _plans_active(b"plan_id;goal;credit;installment;reduced_installment\nI100;real_estate;100000;800,00;397,00\n"
                        b"A100;vehicle;100000;1750;\n")
    conv = await inbound(tenant, ["100k"])
    from agentplatform.core.db.pool import tenant_tx
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set stage='qualificando' where contact_id=%s", (conv["contact_id"],))
    seen = {}

    def respond(msgs):
        seen["prompt"] = msgs[0]["content"] + msgs[1]["content"]
        return draft("Com parcelas a partir de uns R$ 400 você já tem uma carta de 100 mil no plano de imóvel (até a "
                     "contemplação). É pra casa, carro ou outra coisa?")
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["provide_info"], stage="qualificando",
                                                   slots=[("credit_amount", "100k", "100k")])],
                         "respond": [respond], "output_judge": [{"violations": [], "explanation": ""}]})
    try:
        res = await TurnRunner(TurnDeps(config=_sim_cfg(), package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    finally:
        await _plans_active(None)
    assert res.trace["tools"] == ["simulate_consortium"] and res.status == "committed" and res.outbox_ids
    assert "entry_approx" in seen["prompt"] and '"entry_approx": "400"' in seen["prompt"]
    flow_q = seen["prompt"].split("Pergunta do fluxo")[-1].split("\n")[0]
    assert "imóvel" in flow_q and "prazo" not in flow_q              # the flow asks the goal, never the term
    sent = await _sent_text(tenant, conv)
    note = _sim_cfg().compliance.required_disclosures["simulation_estimate"]
    assert note in sent and sent.endswith("É pra casa, carro ou outra coisa?")     # official notice added by code


async def _sent_text(tenant, conv) -> str:
    from agentplatform.core.db.pool import tenant_tx
    async with tenant_tx(tenant) as conn:
        rows = await (await conn.execute("select payload->>'text' as t from outbox where conversation_id=%s and kind='dm_text' "
                                         "order by created_at, effect_key", (conv["id"],))).fetchall()
    return " ".join(r["t"] for r in rows)


async def test_form_link_is_never_lost(tenant):
    """Minimum profile complete: the pre-registration link is created and the stage moves on, so the reply must carry it.
    The model forgets it twice: the code appends it instead of sending a reply without the link."""
    await _plans_active(None)
    conv = await inbound(tenant, ["pra casa, uns 100 mil"])
    from agentplatform.core.db.pool import tenant_tx
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set stage='qualificando' where contact_id=%s", (conv["contact_id"],))
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["provide_info"], stage="qualificando",
                                                   slots=[("goal", "real_estate", "pra casa"), ("credit_amount", "100 mil", "100 mil")])],
                         "respond": [draft("Show! Quer garantir a sua com o pré-cadastro?")] * 2})
    res = await TurnRunner(TurnDeps(config=_sim_cfg(), package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    assert res.status == "committed" and "create_lead_form_link" in res.trace["tools"] and res.trace["attempts"] == 2
    assert "/f/" in await _sent_text(tenant, conv)


async def test_simulation_request_without_table_goes_to_seller(tenant):
    """Simulation on but no active table: an explicit request is handed to the seller (never a number, never a dead end)."""
    await _plans_active(None)
    conv = await inbound(tenant, ["quanto fica a parcela de uma carta de 100 mil pra carro?"])
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["simulation_request"], stage="qualificando",
                                                   slots=[("goal", "vehicle", "carro"), ("credit_amount", "100 mil", "100 mil")])]})
    res = await TurnRunner(TurnDeps(config=_sim_cfg(), package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    assert res.status == "handoff" and res.trace["handoff"] == "simulation_request"
