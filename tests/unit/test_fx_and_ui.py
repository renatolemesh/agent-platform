"""Console display helpers: USD costs shown in BRL (PTAX, with fallback) and package labels for profile values."""
from decimal import Decimal

import httpx

from agentplatform.api import ui
from agentplatform.core.ops import fx
from agentplatform.core.packages.contract import load_package


async def test_ptax_refresh_and_money_in_reais(monkeypatch):
    from agentplatform.settings import get_settings
    monkeypatch.setattr(get_settings(), "fx_source", "ptax")
    monkeypatch.setattr(fx, "_cache", None)

    def handler(req: httpx.Request):
        assert "CotacaoDolarPeriodo" in str(req.url)
        return httpx.Response(200, json={"value": [{"cotacaoCompra": 4.9886, "cotacaoVenda": 4.9892,
                                                     "dataHoraCotacao": "2026-10-09 13:07:30.746597"}]})
    rate = await fx.refresh(transport=httpx.MockTransport(handler))
    assert rate.value == Decimal("4.9892") and rate.source == "PTAX 09/10/2026"
    html = str(ui.money("0.45"))
    assert "R$ 2,25" in html and "US$ 0,4500" in html and "PTAX 09/10/2026" in html
    assert "R$ 0,0040" in str(ui.money("0.0008"))          # fractions of a cent keep 4 decimals


async def test_fallback_when_ptax_fails(monkeypatch):
    from agentplatform.settings import get_settings
    monkeypatch.setattr(get_settings(), "fx_source", "ptax")
    monkeypatch.setattr(get_settings(), "fx_fallback_usd_brl", Decimal("5.50"))
    monkeypatch.setattr(fx, "_cache", None)
    assert await fx.refresh(transport=httpx.MockTransport(lambda r: httpx.Response(503))) is None
    assert fx.current().value == Decimal("5.50") and fx.current().source == "fixa"


def test_profile_values_use_package_labels():
    m = load_package("consortium").manifest
    labels = {"stages": {s.name: s.label for s in m.stages if s.label}, "slots": {s.name: s for s in m.slots}}
    assert ui.slot_label("credit_amount", labels) == "Valor do bem"
    assert ui.slot_value("credit_amount", {"value": "80000", "confidence": 0.95}, labels) == "R$ 80.000"
    assert ui.slot_value("goal", {"value": "vehicle"}, labels) == "Carro ou moto"
    assert ui.slot_confidence({"value": "x", "confidence": 0.95}) == "95%"
    assert ui.stage_label("pronto_para_proposta", labels) == "Pronto para proposta"
    assert ui.contact_name({"first_name": None, "username": None, "channel": "instagram", "external_user_id": "1784123456"}) \
        == "Instagram …123456"


def test_console_times_use_display_timezone():
    from datetime import datetime, timezone
    assert ui.dt(datetime(2026, 10, 10, 16, 50, tzinfo=timezone.utc)) == "10/10 13:50"      # America/Sao_Paulo
    assert ui.dt(None) == "—"


def test_every_facebook_login_error_has_a_clear_message():
    """The channels page never shows an internal code: each OAuthError raised in the code has an instruction."""
    import re
    from pathlib import Path
    src = Path(__file__).resolve().parents[2] / "src" / "agentplatform"
    codes = {c for f in src.rglob("*.py") for c in re.findall(r'OAuthError\("([a-z_]+)"\)', f.read_text(encoding="utf-8"))}
    assert "select_one_page" in codes and codes <= set(ui.OAUTH_ERROR)
    assert "_" not in ui.oauth_error("anything_else")
    assert ui.pause_reason("token_invalid").startswith("o acesso à Meta") and ui.pause_reason("manutenção") == "manutenção."
