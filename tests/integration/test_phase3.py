"""Phase 3: plans table import/activation/rollback, simulation through the real turn."""
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agentplatform.core.agent.turn import TurnDeps, TurnRunner
from agentplatform.core.config.schema import PlatformConfig
from agentplatform.core.db.pool import platform_tx
from agentplatform.core.knowledge.datasets import activate, import_dataset, parse_table, rollback
from agentplatform.core.models.fake import ScriptedGateway, analysis, draft
from agentplatform.core.packages.contract import StageSpec, TransitionSpec
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
        "respond": [draft("Pela tabela vigente, a carta de 80 mil em 60 meses fica em parcelas estimadas de R$ 1.546,67, "
                          "com taxa de administração. Posso chamar o consultor para seguir com você?")],
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
        return draft(f"{OFFER_I100} É pra casa, carro ou outra coisa?")
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["provide_info"], stage="qualificando",
                                                   slots=[("credit_amount", "100k", "100k")])],
                         "respond": [respond], "output_judge": [{"violations": [], "explanation": ""}]})
    try:
        res = await TurnRunner(TurnDeps(config=_sim_cfg(), package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    finally:
        await _plans_active(None)
    assert res.trace["tools"] == ["simulate_consortium"] and res.status == "committed" and res.outbox_ids
    assert "required_text" in seen["prompt"] and "entry_approx" not in seen["prompt"]
    flow_q = seen["prompt"].split("Pergunta do fluxo")[-1].split("\n")[0]
    assert "imóvel" in flow_q and "prazo" not in flow_q              # the flow asks the goal, never the term
    sent = await _sent_text(tenant, conv)
    note = _sim_cfg().compliance.required_disclosures["simulation_estimate"]
    assert note in sent and sent.endswith("É pra casa, carro ou outra coisa?")     # official notice added by code
    assert OFFER_I100 in sent


async def test_the_offer_goes_out_as_written_and_the_person_is_offered_after_it(tenant):
    """The model hid the installment after contemplation, even with the rule in the prompt: the offer is the code's, not its wording."""
    await _plans_active(b"plan_id;goal;credit;term_months;installment;reduced_installment\nA95;vehicle;95000;72;1583,33;980,00\n")
    igsid = f"IG-{uuid.uuid4().hex[:8]}"
    conv = await inbound(tenant, ["carro de uns 100k"], igsid=igsid)
    from agentplatform.core.db.pool import tenant_tx
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set stage='qualificando' where contact_id=%s", (conv["contact_id"],))
    seen = {}

    def second(msgs):
        seen["prompt"] = msgs[0]["content"] + msgs[1]["content"]
        return draft("Posso chamar o consultor para seguir com você?")
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["provide_info"], stage="pronto_para_proposta",
                                                   slots=[("goal", "vehicle", "carro"), ("credit_amount", "100k", "100k")]),
                                          analysis(intents=["provide_info"], stage="pronto_para_proposta")],
                         "respond": [draft("Com parcelas a partir de uns R$ 980 você já tem uma carta de R$ 95 mil. Quer que eu chame o consultor?"), second],
                         "output_judge": [{"violations": [], "explanation": ""}, {"violations": [], "explanation": ""}]})
    try:
        deps = TurnDeps(config=_sim_cfg(), package=PKG, gateway=g, knowledge=None, now=NOON)
        res = await TurnRunner(deps).run(tenant, conv["id"])
        assert res.status == "committed"
        sent = await _sent_text(tenant, conv)
        note = _sim_cfg().compliance.required_disclosures["simulation_estimate"]
        assert sent.endswith("Ótimo! Temos carta de R$ 95.000,00 para carro ou moto, em 72 meses, com parcela de R$ 980,00 até a "
                             f"contemplação e R$ 1.583,33 depois. {note} Faz sentido pra você?")
        assert "a partir de uns" not in sent and "chame o consultor" not in sent

        await inbound(tenant, ["sim"], igsid=igsid)
        res = await TurnRunner(deps).run(tenant, conv["id"])
        assert res.status == "committed"                              # a yes to "makes sense" is not yet a yes to the person
        assert "Posso chamar o consultor" in seen["prompt"].split("Pergunta do fluxo")[-1].split(chr(10))[0]
    finally:
        await _plans_active(None)


OFFER_I100 = "Ótimo! Temos carta de R$ 100.000,00 para imóvel, com parcela de R$ 397,00 até a contemplação e R$ 800,00 depois."


async def _sent_text(tenant, conv) -> str:
    from agentplatform.core.db.pool import tenant_tx
    async with tenant_tx(tenant) as conn:
        rows = await (await conn.execute("select payload->>'text' as t from outbox where conversation_id=%s and kind='dm_text' "
                                         "order by created_at, effect_key", (conv["id"],))).fetchall()
    return " ".join(r["t"] for r in rows)


def _package_that_sends_a_form():
    """The core still sends a pre-registration link for a package that asks for it. This segment does not."""
    m = PKG.manifest
    return SimpleNamespace(tools=PKG.tools, manifest=m.model_copy(update={
        "ready_question": None, "form_sent_stage": "form_sent", "stages": [*m.stages, StageSpec(name="form_sent", goal="Aguardar")],
        "transitions": [*m.transitions, TransitionSpec(frm=m.form_stage, to="form_sent")]}))


async def _lead_about_to_be_ready(tenant, text):
    await _plans_active(None)
    conv = await inbound(tenant, [text])
    from agentplatform.core.db.pool import tenant_tx
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set stage='qualificando' where contact_id=%s", (conv["contact_id"],))
    return conv


async def _stage_and_forms(tenant, conv) -> tuple[str, int]:
    from agentplatform.core.db.pool import tenant_tx
    async with tenant_tx(tenant) as conn:
        lead = await (await conn.execute("select id, stage from leads where contact_id=%s", (conv["contact_id"],))).fetchone()
        forms = await (await conn.execute("select count(*) as n from lead_forms where lead_id=%s", (lead["id"],))).fetchone()
    return lead["stage"], forms["n"]


async def test_someone_ready_gets_no_form_and_is_asked_about_the_seller(tenant):
    conv = await _lead_about_to_be_ready(tenant, "pra casa, uns 100 mil")
    prompts = []

    def reply(msgs):
        prompts.append(msgs[0]["content"])
        return draft("Show! Posso chamar o consultor para seguir com você?")
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["provide_info"], stage="qualificando",
                                                   slots=[("goal", "real_estate", "pra casa"), ("credit_amount", "100 mil", "100 mil")])],
                         "respond": [reply], "output_judge": [{"violations": [], "explanation": ""}]})
    res = await TurnRunner(TurnDeps(config=_sim_cfg(), package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    assert res.status == "committed" and "create_lead_form_link" not in res.trace["tools"]
    assert PKG.manifest.ready_question in prompts[0] and "/f/" not in prompts[0]
    assert await _stage_and_forms(tenant, conv) == ("pronto_para_proposta", 0)
    assert "/f/" not in await _sent_text(tenant, conv)


async def test_saying_no_with_the_last_answer_does_not_start_the_next_step(tenant):
    """Nothing is asked, and the reminder of that stage is not scheduled for someone who just said not now."""
    conv = await _lead_about_to_be_ready(tenant, "pra casa, uns 100 mil, mas só ano que vem")
    prompts = []

    def reply(msgs):
        prompts.append(msgs[0]["content"])
        return draft("Tranquilo, quando fizer sentido é só chamar.")
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["provide_info", "not_interested"], stage="qualificando",
                                                   slots=[("goal", "real_estate", "pra casa"), ("credit_amount", "100 mil", "100 mil")])],
                         "respond": [reply], "output_judge": [{"violations": [], "explanation": ""}]})
    res = await TurnRunner(TurnDeps(config=_sim_cfg(), package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    assert res.status == "committed" and PKG.manifest.ready_question not in prompts[0]
    assert await _stage_and_forms(tenant, conv) == ("qualificando", 0)


async def test_saying_yes_to_the_seller_is_a_hot_lead(tenant):
    conv = await _lead_about_to_be_ready(tenant, "pode sim")
    from agentplatform.core.db.pool import tenant_tx
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set stage='pronto_para_proposta' where contact_id=%s", (conv["contact_id"],))
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["ready_to_buy"], stage="pronto_para_proposta")]})
    await TurnRunner(TurnDeps(config=_sim_cfg(), package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    async with tenant_tx(tenant) as conn:
        h = await (await conn.execute("select reason from handoffs where conversation_id=%s", (conv["id"],))).fetchall()
    assert [r["reason"] for r in h] == ["hot_lead"]
    assert (await _stage_and_forms(tenant, conv))[1] == 0


@pytest.mark.parametrize("offer", ["Show! Posso chamar o consultor para seguir com você?",
                                   "A parcela depende do prazo. Quer que eu chame o consultor para te mostrar as opções?"])
async def test_a_lone_yes_to_the_offer_calls_the_seller_even_if_the_classifier_sees_nothing(tenant, offer):
    conv = await _lead_about_to_be_ready(tenant, "bora")
    from agentplatform.core.db.pool import tenant_tx
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set stage='pronto_para_proposta' where contact_id=%s", (conv["contact_id"],))
        lead = await (await conn.execute("select tenant_id from leads where contact_id=%s", (conv["contact_id"],))).fetchone()
        await conn.execute("insert into messages (tenant_id, conversation_id, direction, kind, text_masked, provider_ts) "
                           "values (%s,%s,'out_bot','text',%s,now() - interval '1 minute')",
                           (lead["tenant_id"], conv["id"], offer))
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["greeting"], stage="pronto_para_proposta")]})
    await TurnRunner(TurnDeps(config=_sim_cfg(), package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    async with tenant_tx(tenant) as conn:
        h = await (await conn.execute("select reason from handoffs where conversation_id=%s", (conv["id"],))).fetchall()
    assert [r["reason"] for r in h] == ["hot_lead"]


async def test_the_offer_is_not_made_again_while_it_is_in_the_conversation(tenant):
    conv = await _lead_about_to_be_ready(tenant, "e quanto tempo demora?")
    from agentplatform.core.db.pool import tenant_tx
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set stage='pronto_para_proposta' where contact_id=%s", (conv["contact_id"],))
        lead = await (await conn.execute("select tenant_id from leads where contact_id=%s", (conv["contact_id"],))).fetchone()
        for minutes, text in ((3, "Quer que eu chame o consultor para te mostrar as opções?"), (1, "Depende do grupo.")):
            await conn.execute("insert into messages (tenant_id, conversation_id, direction, kind, text_masked, provider_ts) "
                               "values (%s,%s,'out_bot','text',%s,now() - make_interval(mins => %s))",
                               (lead["tenant_id"], conv["id"], text, minutes))
    prompts = []

    def reply(msgs):
        prompts.append(msgs[0]["content"])
        return draft("Costuma depender do grupo.")
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["provide_info"], stage="pronto_para_proposta")],
                         "respond": [reply], "output_judge": [{"violations": [], "explanation": ""}]})
    await TurnRunner(TurnDeps(config=_sim_cfg(), package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    assert prompts and PKG.manifest.ready_question not in prompts[0]


async def test_form_link_is_never_lost(tenant):
    """A package that sends a form. Minimum profile complete: the link is created and the stage moves on, so the reply must carry it.
    The model forgets it twice: the code appends it instead of sending a reply without the link."""
    await _plans_active(None)
    conv = await inbound(tenant, ["pra casa, uns 100 mil"])
    from agentplatform.core.db.pool import tenant_tx
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set stage='qualificando' where contact_id=%s", (conv["contact_id"],))
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["provide_info"], stage="qualificando",
                                                   slots=[("goal", "real_estate", "pra casa"), ("credit_amount", "100 mil", "100 mil")])],
                         "respond": [draft("Show! Quer garantir a sua com o pré-cadastro?")] * 2})
    res = await TurnRunner(TurnDeps(config=_sim_cfg(), package=_package_that_sends_a_form(), gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
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
