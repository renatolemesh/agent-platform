"""The follow-ups page: who stopped answering, what goes out by itself and the rules behind it."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from agentplatform.core.config.schema import Cadence, CadenceStep, FollowupsConfig
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.followups.engine import on_bot_message
from tests.integration.helpers import make_conversation
from tests.integration.test_config_console import CFG, RAW, client, publish, signed_in, version
from tests.integration.test_console_edit import _form

pytestmark = pytest.mark.db
RULES = [Cadence(id="no_reply", label="Parou de responder", trigger={"event": "bot_message_unanswered", "stages": ["novo"]},
                 steps=[CadenceStep(delay="4h", content_kind="template", template_key="no_reply_nudge")])]


async def _setup(tenant, rules=RULES):
    await publish(tenant, RAW.model_copy(update={"followups": FollowupsConfig(enabled=True, cadences=rules)}))


async def _draft(tenant, resp) -> dict:
    assert resp.status_code == 303, resp.text[:400]
    return (await version(tenant, uuid.UUID(resp.headers["location"].split("proposta=")[1])))["document"]["followups"]


async def _went_quiet(tenant, hours=48, **kw):
    conv, _ = await make_conversation(tenant, **kw)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update conversations set last_inbound_at = now() - make_interval(hours => %s), last_reply_at = now() - make_interval(hours => %s) where id=%s",
                           (hours + 1, hours, conv["id"]))
    return conv


async def test_every_role_sees_the_page_and_only_the_owner_changes_it(tenant):
    await _setup(tenant)
    async with client() as c:
        await signed_in(c, tenant, role="seller", name="Vend")
        page = await c.get("/console/followups")
        assert page.status_code == 200 and "Parou de responder" in page.text and 'name="rule_0_label"' not in page.text
        assert (await c.post("/console/followups", data={})).status_code == 403
    async with client() as c:
        await signed_in(c, tenant, role="reviewer", name="Rev")
        page = await c.get("/console/followups")
        assert page.status_code == 200 and 'name="rule_0_label"' not in page.text
    async with client() as c:
        await signed_in(c, tenant)
        assert 'name="rule_0_label"' in (await c.get("/console/followups")).text


async def test_an_unchanged_form_creates_no_draft(tenant):
    await _setup(tenant, [Cadence(id="no_reply", trigger={"event": "stage_entered", "stage": "novo"},
                                  steps=[CadenceStep(delay="4h", content_kind="template", template_key="no_reply_nudge")])])
    async with client() as c:
        await signed_in(c, tenant)
        form = _form((await c.get("/console/followups")).text)
        r = await c.post("/console/followups", data=form)
    assert r.status_code == 303 and "igual=1" in r.headers["location"]


async def test_a_new_rule_goes_to_a_draft_with_its_own_words(tenant):
    await _setup(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        form = _form((await c.get("/console/followups")).text)
        r = await c.post("/console/followups", data={**form, "rule_1_label": "Ação de Maio", "rule_1_event": "stage_entered", "rule_1_stage_engajado": "on", "rule_1_enabled": "on",
                                                     "rule_1_0_delay": "2h", "rule_1_0_text": "Oi! Posso ajudar em algo?", "rule_1_1_delay": "6h", "rule_1_1_text": "Sigo por aqui."})
    fu = await _draft(tenant, r)
    new = fu["cadences"][1]
    assert new["id"] == "acao_de_maio" and new["label"] == "Ação de Maio" and new["trigger"] == {"event": "stage_entered", "stages": ["engajado"]} and new["max_attempts"] == 2
    assert [s["delay"] for s in new["steps"]] == ["2h", "6h"]
    assert fu["texts"][new["steps"][0]["template_key"]] == "Oi! Posso ajudar em algo?"


@pytest.mark.parametrize(("change", "says"), [
    ({"rule_0_0_delay": "logo"}, "30m, 4h ou 2d"),
    ({"rule_0_0_delay": "1m"}, "tempo mínimo"),
    ({"rule_0_0_delay": "24h"}, "janela do canal"),
    ({"rule_0_0_text": ""}, "texto não pode ficar em branco"),
    ({"rule_0_label": ""}, "dê um nome"),
    ({"rule_0_stage_novo": None}, "ao menos uma etapa"),
    ({"stalled_after": "logo"}, "Contato parado"),
])
async def test_what_could_never_work_is_refused_with_a_reason(tenant, change, says):
    await _setup(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        form = _form((await c.get("/console/followups")).text)
        data = {k: v for k, v in {**form, **change}.items() if v is not None}
        r = await c.post("/console/followups", data=data)
    assert r.status_code == 400 and says in r.text


async def test_attempts_are_checked_against_the_window_together(tenant):
    await _setup(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        form = _form((await c.get("/console/followups")).text)
        r = await c.post("/console/followups", data={**form, "rule_0_0_delay": "20h", "rule_0_1_delay": "5h", "rule_0_1_text": "De novo."})
    assert r.status_code == 400 and "janela do canal" in r.text


async def test_a_rule_can_be_turned_off_or_removed(tenant):
    await _setup(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        form = _form((await c.get("/console/followups")).text)
        off = {k: v for k, v in form.items() if k != "rule_0_enabled"}
        fu = await _draft(tenant, await c.post("/console/followups", data=off))
        assert fu["cadences"][0]["enabled"] is False
        fu = await _draft(tenant, await c.post("/console/followups", data={**form, "rule_0_remove": "on"}))
    assert fu["cadences"] == []


async def test_a_text_that_breaks_the_rules_is_refused(tenant):
    await _setup(tenant)
    async with client() as c:
        await signed_in(c, tenant)
        form = _form((await c.get("/console/followups")).text)
        r = await c.post("/console/followups", data={**form, "rule_0_0_text": "Preencha aqui: https://site-qualquer.example/ficha"})
    assert r.status_code == 400 and 'class="alert bad"' in r.text


async def test_stalled_lists_who_went_quiet_and_only_them(tenant):
    await _setup(tenant)
    quiet = await _went_quiet(tenant)
    recent = await _went_quiet(tenant, hours=1)
    answered, _ = await make_conversation(tenant)          # the person spoke last
    with_seller = await _went_quiet(tenant)
    out = await _went_quiet(tenant)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update conversations set ownership='HUMAN' where id=%s", (with_seller["id"],))
        await conn.execute("update leads set opted_out_at=now() where contact_id=%s", (out["contact_id"],))
    async with client() as c:
        await signed_in(c, tenant)
        page = (await c.get("/console/followups")).text
        wider = (await c.get("/console/followups?parado=30m")).text
        junk = await c.get("/console/followups?parado=logo")
    assert f'/console/conversations/{quiet["id"]}' in page
    for gone in (recent, answered, with_seller, out):
        assert f'/console/conversations/{gone["id"]}' not in page
    assert f'/console/conversations/{recent["id"]}' in wider
    assert junk.status_code == 200 and f'/console/conversations/{quiet["id"]}' in junk.text


async def test_stalled_says_when_only_a_person_can_resume(tenant):
    await _setup(tenant)
    await _went_quiet(tenant, window_open=False)
    async with client() as c:
        await signed_in(c, tenant)
        page = (await c.get("/console/followups")).text
    assert "Janela fechada: retomar à mão" in page


async def test_scheduled_and_history_show_the_rule_and_the_reason(tenant):
    await _setup(tenant)
    conv, _ = await make_conversation(tenant)
    other, _ = await make_conversation(tenant)
    cfg = CFG.model_copy(update={"followups": FollowupsConfig(cadences=RULES)})
    now = datetime.now(timezone.utc)
    async with tenant_tx(tenant) as conn:
        await on_bot_message(conn, cfg, tenant, conv["id"], now)
        await on_bot_message(conn, cfg, tenant, other["id"], now - timedelta(hours=1))
        await conn.execute("update followup_runs set status='stopped', stop_reason='inbound_message' where conversation_id=%s", (other["id"],))
    async with client() as c:
        await signed_in(c, tenant)
        page = (await c.get("/console/followups")).text
    upcoming, history = page.split("Próximos envios")[1].split("Histórico")
    assert f'/console/conversations/{conv["id"]}' in upcoming and "Parou de responder" in upcoming and f'/console/conversations/{other["id"]}' not in upcoming
    assert f'/console/conversations/{other["id"]}' in history and "A pessoa respondeu" in history and "Cancelado" in history


async def test_another_tenant_never_shows_up(tenant):
    await _setup(tenant)
    other = uuid.uuid4()
    async with platform_tx() as conn:
        await conn.execute("insert into tenants (id, slug, display_name, segment_package, segment_package_version) values (%s,%s,%s,%s,%s)",
                           (other, f"t-{other.hex[:8]}", "Other tenant", "consortium", "0.1.0"))
        await conn.execute("insert into channel_accounts (tenant_id, channel, external_account_id, external_page_id) values (%s,'instagram',%s,%s)",
                           (other, f"ig-{other.hex[:10]}", f"page-{other.hex[:10]}"))
    theirs = await _went_quiet(other)
    async with client() as c:
        await signed_in(c, tenant)
        page = (await c.get("/console/followups")).text
    assert str(theirs["id"]) not in page
