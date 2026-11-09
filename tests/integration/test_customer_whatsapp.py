"""The customer's WhatsApp, asked with the handoff message (handoff.ask_customer_whatsapp): the number only reaches the console."""
import json
import uuid

import pytest

from agentplatform.core.agent.turn import TurnDeps, TurnRunner
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.handoff.alerts import _summary_line
from agentplatform.core.models.fake import ScriptedGateway, analysis
from agentplatform.core.orchestration.conversations import transition_ownership
from agentplatform.core.security.keys import decrypt_field, encrypt_field
from tests.integration.test_phase3 import _plans_active, _sim_cfg
from tests.integration.test_turn import NOON, PKG, inbound

pytestmark = pytest.mark.db
NUMBER = "41 99876-5432"           # invented
E164 = "+5541998765432"


def _cfg(on=True):
    c = _sim_cfg()
    return c.model_copy(update={"handoff": c.handoff.model_copy(update={"ask_customer_whatsapp": on})})


async def _ready_says_yes(tenant, cfg):
    """A lead at the last step says yes to the seller: hot lead, and the handoff message goes out."""
    await _plans_active(None)
    igsid = f"IG-{uuid.uuid4().hex[:8]}"
    conv = await inbound(tenant, ["pode sim"], igsid=igsid)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set stage='pronto_para_proposta' where contact_id=%s", (conv["contact_id"],))
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["ready_to_buy"], stage="pronto_para_proposta")]})
    await TurnRunner(TurnDeps(config=cfg, package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    return conv, igsid


async def _answer(tenant, cfg, igsid, text):
    """The customer writes again; no model may be called for a number alone."""
    conv = await inbound(tenant, [text], igsid=igsid)
    g = ScriptedGateway({})
    await TurnRunner(TurnDeps(config=cfg, package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    return conv, g


async def _state(tenant, conv):
    async with tenant_tx(tenant) as conn:
        ct = await (await conn.execute("select phone_enc, phone_hash from contacts where id=%s", (conv["contact_id"],))).fetchone()
        h = await (await conn.execute("select summary from handoffs where conversation_id=%s and not is_notice", (conv["id"],))).fetchone()
        sent = await (await conn.execute("select kind, payload->>'text' t from outbox where conversation_id=%s order by created_at",
                                         (conv["id"],))).fetchall()
        msgs = await (await conn.execute("select text_masked from messages where conversation_id=%s and direction='in'", (conv["id"],))).fetchall()
        phone = await decrypt_field(conn, tenant, ct["phone_enc"], "contacts.phone") if ct["phone_enc"] else None
    return phone, ct["phone_hash"], h["summary"] if h else None, [(r["kind"], r["t"]) for r in sent], [m["text_masked"] for m in msgs]


async def test_the_handoff_asks_for_the_whatsapp_and_the_number_is_kept_for_the_console_only(tenant):
    cfg = _cfg()
    conv, igsid = await _ready_says_yes(tenant, cfg)
    _, _, summary, sent, _ = await _state(tenant, conv)
    ask = cfg.handoff.ask_whatsapp_text.format(seller=cfg.conversation.persona.seller_display_name)
    assert sent[-1][0] == "handoff_expectation" and sent[-1][1].endswith(ask) and summary["whatsapp_asked"] is True

    conv, g = await _answer(tenant, cfg, igsid, f"{NUMBER} pode me chamar")
    phone, phone_hash, summary, sent, masked = await _state(tenant, conv)
    assert phone == E164 and phone_hash is not None and summary["whatsapp_left"] is True
    assert sent[-1] == ("handoff_expectation", cfg.handoff.whatsapp_saved_message.format(seller=cfg.conversation.persona.seller_display_name))
    assert g.calls == []                                                    # no model saw it
    assert all("99876" not in (m or "") for m in masked) and any("[TELEFONE]" in (m or "") for m in masked)
    assert "deixou WhatsApp" in _summary_line(summary) and "99876" not in _summary_line(summary)
    assert "99876" not in json.dumps(summary)


async def test_off_by_default_nothing_is_asked_and_no_number_is_kept(tenant):
    cfg = _cfg(on=False)
    assert _sim_cfg().handoff.ask_customer_whatsapp is False
    conv, igsid = await _ready_says_yes(tenant, cfg)
    _, _, summary, sent, _ = await _state(tenant, conv)
    assert "WhatsApp" not in sent[-1][1] and "whatsapp_asked" not in summary
    conv, _ = await _answer(tenant, cfg, igsid, NUMBER)
    assert (await _state(tenant, conv))[0] is None


async def test_a_contact_that_already_has_a_number_is_not_asked(tenant):
    cfg = _cfg()
    await _plans_active(None)
    igsid = f"IG-{uuid.uuid4().hex[:8]}"
    conv = await inbound(tenant, ["pode sim"], igsid=igsid)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update leads set stage='pronto_para_proposta' where contact_id=%s", (conv["contact_id"],))
        await conn.execute("update contacts set phone_enc=%s where id=%s",
                           (await encrypt_field(conn, tenant, "+5511912345678", "contacts.phone"), conv["contact_id"]))
    g = ScriptedGateway({"analyze_turn": [analysis(intents=["ready_to_buy"], stage="pronto_para_proposta")]})
    await TurnRunner(TurnDeps(config=cfg, package=PKG, gateway=g, knowledge=None, now=NOON)).run(tenant, conv["id"])
    _, _, summary, sent, _ = await _state(tenant, conv)
    assert "WhatsApp" not in sent[-1][1] and "whatsapp_asked" not in summary


async def test_once_the_seller_took_it_the_number_is_kept_without_a_reply(tenant):
    cfg = _cfg()
    conv, igsid = await _ready_says_yes(tenant, cfg)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update handoffs set taken_at=now() where conversation_id=%s", (conv["id"],))
        await transition_ownership(conn, tenant, conv["id"], "HUMAN", cause="test")
    before = len((await _state(tenant, conv))[3])
    conv, g = await _answer(tenant, cfg, igsid, NUMBER)
    phone, _, _, sent, _ = await _state(tenant, conv)
    assert phone == E164 and len(sent) == before and g.calls == []


async def test_a_number_with_a_stop_request_is_not_kept(tenant):
    cfg = _cfg()
    conv, igsid = await _ready_says_yes(tenant, cfg)
    conv, _ = await _answer(tenant, cfg, igsid, f"não quero mais receber mensagens, para de me mandar {NUMBER}")
    assert (await _state(tenant, conv))[0] is None


async def test_a_number_that_belongs_to_another_contact_is_kept_without_merging_them(tenant):
    from agentplatform.core.security.keys import crypto
    cfg = _cfg()
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into contacts (tenant_id, first_name, phone_hash) values (%s,'outra',%s)",
                           (tenant, crypto().lookup_hash(E164, kind="phone")))
    conv, igsid = await _ready_says_yes(tenant, cfg)
    conv, _ = await _answer(tenant, cfg, igsid, NUMBER)
    phone, phone_hash, *_ = await _state(tenant, conv)
    assert phone == E164 and phone_hash is None
