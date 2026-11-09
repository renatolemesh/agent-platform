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
    assert "Atendimento: Ana" in text and ALERT.console_url in text
    took = next(x for x in text.splitlines() if x.startswith("Assumi: ")).split("/actions/")[1]
    assert verify_action(SECRET, took) == ("took", HID)
    ret = next(x for x in text.splitlines() if x.startswith("Devolver ao assistente: ")).split("/actions/")[1]
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
