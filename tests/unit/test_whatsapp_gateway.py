"""Seller and operator alerts through a WhatsApp text gateway (ADR-026): uazapi, Evolution API, Evolution Go."""
import json
import uuid

import httpx
import pytest

from agentplatform.core.channels.ports import Alert, SellerContact
from agentplatform.core.handoff.notifiers import WhatsAppGatewayNotifier, alert_text, verify_action

SECRET = b"s3cret"
HID = uuid.uuid4()
ALERT = Alert("handoff", "Ana", "pronto_para_proposta", "hot_lead", "deixou WhatsApp (veja no console) | goal=vehicle",
              "https://app.test/console/conversations/x", HID)
SELLER = SellerContact(uuid.uuid4(), "+55 41 99999-0000", None)      # invented


def _gateway(provider, status=200, instance=None):
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(status, json={})
    n = WhatsAppGatewayNotifier(provider, "https://gw.test/", "tok-123", SECRET, "https://app.test", instance=instance,
                                transport=httpx.MockTransport(handler))
    return n, seen


@pytest.mark.parametrize("provider,instance,path,header", [
    ("uazapi", None, "/send/text", "token"),
    ("evolution", "vendas 1", "/message/sendText/vendas%201", "apikey"),
    ("evolution_go", None, "/send/text", "apikey"),
])
async def test_each_gateway_gets_its_path_and_token_header(provider, instance, path, header):
    n, seen = _gateway(provider, instance=instance)
    assert await n.send_alert(SELLER, ALERT) is True
    req = seen[0]
    assert req.url.raw_path.decode() == path and req.headers[header] == "tok-123" and n.name == f"whatsapp_{provider}"
    body = json.loads(req.content)
    assert body["number"] == "5541999990000" and body["text"] == alert_text(ALERT, SECRET, "https://app.test")


def test_the_text_carries_the_console_and_signed_actions_but_no_customer_number():
    text = alert_text(ALERT, SECRET, "https://app.test/")
    assert text.splitlines()[0] == "🔔 hot_lead: Ana" and ALERT.console_url in text
    took = next(x for x in text.splitlines() if "Assumi: " in x).split("/actions/")[1]
    assert verify_action(SECRET, took) == ("took", HID)
    ret = next(x for x in text.splitlines() if "Devolver ao assistente: " in x).split("/actions/")[1]
    assert verify_action(SECRET, ret) == ("return", HID)


async def test_a_refused_send_is_not_delivered_and_the_chain_goes_on():
    n, _ = _gateway("uazapi", status=401)
    assert await n.send_alert(SELLER, ALERT) is False
    n, seen = _gateway("uazapi")
    assert await n.send_alert(SellerContact(uuid.uuid4(), None, "a@b.test"), ALERT) is False and seen == []


async def test_operator_alerts_go_as_text():
    n, seen = _gateway("evolution_go")
    assert await n.send_ops("+55 41 98888-0000", "Fila parada", "detalhe")
    assert json.loads(seen[0].content) == {"number": "5541988880000", "text": "Fila parada\n\ndetalhe"}


def test_evolution_needs_the_instance_name():
    from agentplatform.settings import Settings
    with pytest.raises(ValueError):
        WhatsAppGatewayNotifier("evolution", "https://gw.test", "t", SECRET, "https://app.test")
    with pytest.raises(ValueError):
        Settings(whatsapp_alert_provider="evolution", whatsapp_alert_url="https://gw.test")


def test_the_runtime_uses_the_gateway_for_sellers_and_operators():
    from agentplatform.runtime import Runtime
    from agentplatform.settings import Settings
    rt = object.__new__(Runtime)
    rt.settings = Settings(app_env="dev", whatsapp_alert_provider="uazapi", whatsapp_alert_url="https://gw.test",
                           whatsapp_alert_token="tok", ops_alert_whatsapp="+5541988880000")
    assert isinstance(rt._build_inbox().notifiers[0], WhatsAppGatewayNotifier)
    assert isinstance(rt._build_ops_notifier().whatsapp, WhatsAppGatewayNotifier)
    rt.settings = Settings(app_env="dev", whatsapp_alert_provider="uazapi")          # no URL or token: not configured
    assert not any(isinstance(n, WhatsAppGatewayNotifier) for n in rt._build_inbox().notifiers)


async def test_telegram_is_one_fixed_test_chat_and_never_production():
    from agentplatform.core.handoff.notifiers import TelegramTestNotifier
    from agentplatform.runtime import Runtime
    from agentplatform.settings import Settings
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, json={"ok": True})
    n = TelegramTestNotifier("123:abc", "42", SECRET, "https://app.test", transport=httpx.MockTransport(handler))
    assert await n.send_alert(SellerContact(uuid.uuid4(), None, None), ALERT)          # whoever the seller is
    assert seen[0].url.path == "/bot123:abc/sendMessage"
    assert json.loads(seen[0].content)["chat_id"] == "42" and "hot_lead: Ana" in json.loads(seen[0].content)["text"]
    rt = object.__new__(Runtime)
    rt.settings = Settings(app_env="dev", telegram_bot_token="123:abc", telegram_test_chat_id="42")
    assert isinstance(rt._build_inbox().notifiers[0], TelegramTestNotifier)
    with pytest.raises(ValueError):
        Settings(app_env="prod", telegram_test_chat_id="42", ops_alert_email="o@x.test")


def test_the_alert_reads_as_words_with_the_package_labels_and_never_the_number():
    from agentplatform.core.handoff.alerts import _alert
    from agentplatform.core.packages.contract import load_package
    m = load_package("consortium").manifest
    h = {"id": HID, "conversation_id": "c1", "stage": m.form_stage,
         "summary": {"first_name": "Ana", "profile": {"credit_amount": "100000", "has_bid_funds": "true"}, "whatsapp_left": True,
                     "last_message": "sim"}}
    text = alert_text(_alert("handoff", h, "Lead quente", "https://app.test/console", m), SECRET, "https://app.test")
    lines = text.splitlines()
    assert lines[0] == "🔔 Lead quente: Ana" and lines[1] == f"Etapa: {next(s.label for s in m.stages if s.name == m.form_stage)}"
    assert "R$ 100.000" in text and ": Sim" in text and "has_bid_funds" not in text and "credit_amount" not in text
    assert "Deixou o WhatsApp" in text and "“sim”" in text and "https://app.test/console/conversations/c1" in text
