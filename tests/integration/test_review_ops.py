"""Operation: token check, model gateway under failure, alerts that tell the truth, production activation, public routes (independent review)."""
import base64
import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, SecretStr, ValidationError

from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
from agentplatform.core.config.registry import ConfigError, _only_operational_change, config_hash, load_yaml, registry
from agentplatform.core.config.schema import ModelRef, NodeModel, PlatformConfig
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.models import gateway as G
from agentplatform.core.models.budget import BudgetService, ScopeCap
from agentplatform.core.models.gateway import BudgetDenied, CallContext, ModelCallError, ModelGateway
from agentplatform.core.ops.maintenance import token_guard
from agentplatform.core.ops.observability import (
    OpsAlert,
    OpsNotifier,
    evaluate_alerts,
    evaluate_platform_alerts,
    should_send,
)
from agentplatform.core.security.keys import encrypt_field
from agentplatform.settings import Settings
from tests.integration.test_turn import CFG, PKG, inbound, runner

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]


async def rows(tenant, sql, *args):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute(sql, args)).fetchall()


# ------------------------------------------------------------------ token check
async def _guard(tenant, handler, app=("id", "secret"), with_token=True):
    async with tenant_tx(tenant) as conn:
        if with_token:
            await conn.execute("update channel_accounts set token_enc=%s where tenant_id=%s",
                               (await encrypt_field(conn, tenant, "tok", "channel_accounts.token"), tenant))
        return await token_guard(conn, tenant, app[0], app[1], transport=httpx.MockTransport(handler))


async def _stop(tenant):
    async with platform_tx() as conn:
        r = await (await conn.execute("select is_on, reason from kill_switches where tenant_id=%s and scope='channel'", (tenant,))).fetchone()
    return r


async def _clean(tenant):
    async with platform_tx() as conn:
        await conn.execute("delete from kill_switches where tenant_id=%s", (tenant,))
    registry.invalidate_kill_switches()


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="<html>error</html>"), httpx.Response(400, json={"error": {"code": 4, "message": "rate limit"}}),
    httpx.Response(200, json={}), httpx.Response(200, text="not json"),
])
async def test_an_inconclusive_check_does_not_stop_the_channel(tenant, response):
    out = await _guard(tenant, lambda r: response)
    assert out[0]["status"] == "unknown" and await _stop(tenant) is None


async def test_network_failure_or_missing_app_credentials_do_not_stop_the_channel(tenant):
    def down(request):
        raise httpx.ConnectError("down")
    assert (await _guard(tenant, down))[0]["status"] == "unknown"
    assert (await _guard(tenant, down, app=(None, None)))[0]["status"] == "unknown"
    assert await _stop(tenant) is None


async def test_the_provider_saying_invalid_stops_it_and_saying_valid_releases_it(tenant):
    try:
        await _guard(tenant, lambda r: httpx.Response(200, json={"data": {"is_valid": False}}))
        assert await _stop(tenant) == {"is_on": True, "reason": "token_invalid"}
        assert (await rows(tenant, "select token_status from channel_accounts where tenant_id=%s", tenant))[0]["token_status"] == "invalid"
        await _guard(tenant, lambda r: httpx.Response(200, json={"data": {"is_valid": True, "scopes": ["a"]}}))
        assert (await _stop(tenant))["is_on"] is False
        assert (await rows(tenant, "select token_status from channel_accounts where tenant_id=%s", tenant))[0]["token_status"] == "valid"
    finally:
        await _clean(tenant)


async def test_a_stop_someone_decided_is_not_lifted_by_the_check(tenant):
    try:
        async with platform_tx() as conn:
            await conn.execute("insert into kill_switches (tenant_id, scope, scope_ref, is_on, reason) values (%s,'channel','instagram',true,'manual')", (tenant,))
        await _guard(tenant, lambda r: httpx.Response(200, json={"data": {"is_valid": True}}))
        assert (await _stop(tenant))["is_on"] is True
    finally:
        await _clean(tenant)


async def test_retention_still_runs_when_the_token_check_blows_up(tenant, monkeypatch):
    from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier
    from agentplatform.core.lgpd import service as lgpd
    from agentplatform.core.ops import maintenance
    from agentplatform.jobs import tasks
    from agentplatform.runtime import Runtime, TenantRuntime, set_runtime
    rt = Runtime(adapters={"instagram": FakeChannelAdapter()}, gateway=None, inbox=FallbackInbox([LogNotifier()]))
    rt._tenants[str(tenant)] = (1e18, TenantRuntime(tenant, CFG, None, PKG, None))
    set_runtime(rt)

    async def boom(*a, **k):
        raise ValueError("provider said something odd")
    monkeypatch.setattr(maintenance, "token_guard", boom)
    swept = []
    original = lgpd.retention_sweep

    async def sweep(conn, cfg, tid):
        if tid == tenant:
            swept.append(tid)
        return await original(conn, cfg, tid)
    monkeypatch.setattr(lgpd, "retention_sweep", sweep)
    await tasks.daily_maintenance()
    assert swept == [tenant]


@pytest.mark.parametrize("response, expected", [
    (httpx.Response(200, json={"data": [{"object": "instagram", "active": True, "fields": [{"name": "messages"}]}]}), {"standby"}),
    (httpx.Response(200, json={"data": [{"object": "instagram", "active": True, "fields": [{"name": "messages"}, {"name": "standby"}]}]}), set()),
    (httpx.Response(200, json={"data": [{"object": "instagram", "active": False, "fields": [{"name": "messages"}, {"name": "standby"}]}]}), {"messages", "standby"}),
    (httpx.Response(500, text="x"), None),
    (httpx.Response(200, text="not json"), None),
])
async def test_webhook_fields_off_in_the_app_are_noticed(response, expected):
    from agentplatform.core.ops.maintenance import missing_app_fields
    got = await missing_app_fields("1", "s", {"messages", "standby"}, transport=httpx.MockTransport(lambda r: response))
    assert got == expected


@pytest.mark.parametrize("response, expected", [
    (httpx.Response(200, json={"data": [{"model_id": "a/um"}, {"model_id": "a/dois"}]}), {"a/um", "a/dois"}),
    (httpx.Response(200, json={"data": []}), None),                      # an empty list says nothing: no false alarm
    (httpx.Response(500, text="x"), None),
    (httpx.Response(200, text="not json"), None),
])
async def test_zero_retention_routes_are_read(response, expected):
    from agentplatform.core.ops.maintenance import zero_retention_models
    assert await zero_retention_models(transport=httpx.MockTransport(lambda r: response)) == expected


def test_a_model_without_a_zero_retention_route_is_noticed_even_as_a_fallback():
    from agentplatform.core.config.schema import ModelRef, ModelsConfig
    from agentplatform.core.ops.maintenance import models_without_route
    node = lambda p, *f: NodeModel(primary=p, fallbacks=list(f), prompt_version="respond@v1")
    models = ModelsConfig(analyze_turn=node("a/um", "b/reserva"), respond=node("a/um"), output_judge=node("c/juiz"), summarize=node("a/um"),
                          embedding=ModelRef(model="d/busca", dims=1024), transcription=ModelRef(model="e/voz"))
    assert models_without_route(models, {"a/um", "c/juiz", "d/busca", "e/voz"}) == {"b/reserva"}
    assert models_without_route(models, {"a/um", "b/reserva", "c/juiz", "d/busca"}) == {"e/voz"}


async def test_the_console_does_not_offer_what_has_no_zero_retention_route(monkeypatch):
    from agentplatform.api import console_setup
    from agentplatform.settings import get_settings
    monkeypatch.setattr(get_settings(), "llm_provider", "openrouter")
    names = [m["name"] for m in console_setup._model_choices()["transcription"]]
    assert names == ["openai/whisper-large-v3-turbo"]
    monkeypatch.setattr(get_settings(), "llm_provider", "openai")
    assert "openai/gpt-4o-mini-transcribe" in [m["name"] for m in console_setup._model_choices()["transcription"]]


# ------------------------------------------------------------------ gateway
class Out(BaseModel):
    answer: str


def spec(retries=1) -> NodeModel:
    return NodeModel(primary="openai/gpt-5-nano", prompt_version="respond@v1", retries=retries, max_output_tokens=10)


def gateway(handler, provider="openai", total=Decimal("1")):
    s = Settings(llm_provider=provider, openai_api_key=SecretStr("x"), openrouter_api_key=SecretStr("x"))
    return ModelGateway(BudgetService(total, total, key_name=f"test-{uuid.uuid4().hex[:8]}"), s, transport=httpx.MockTransport(handler))


OK = {"model": "gpt-5-nano", "choices": [{"message": {"content": '{"answer": "oi"}'}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(G, "RETRY_WAIT_S", 0)


async def _ctx():
    return CallContext(None, "test", purpose="ci", scopes=[ScopeCap(f"t:{uuid.uuid4().hex[:6]}", Decimal("1"), None)])


@pytest.mark.parametrize("first", [httpx.Response(500, text="x"), httpx.Response(429, text="x")])
async def test_a_provider_hiccup_gets_one_more_try(db, first):
    answers = [first, httpx.Response(200, json=OK)]
    calls = []

    def handler(request):
        calls.append(1)
        return answers.pop(0)
    r = await gateway(handler).chat(spec(), [{"role": "user", "content": "oi"}], await _ctx(), Out)
    assert r.parsed.answer == "oi" and len(calls) == 2


async def test_a_timeout_gets_one_more_try(db):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json=OK)
    assert (await gateway(handler).chat(spec(), [{"role": "user", "content": "oi"}], await _ctx(), Out)).parsed.answer == "oi"


@pytest.mark.parametrize("response", [
    httpx.Response(400, text="bad request"),
    httpx.Response(200, json={"model": "x", "choices": [{"message": {"content": "isto não é json"}}]}),
    httpx.Response(200, json={"unexpected": "shape"}),
    httpx.Response(200, text="not json at all"),
])
async def test_what_would_fail_again_is_not_retried_and_becomes_a_model_error(db, response):
    calls = []

    def handler(request):
        calls.append(1)
        return response
    with pytest.raises(ModelCallError):
        await gateway(handler).chat(spec(), [{"role": "user", "content": "oi"}], await _ctx(), Out)
    assert len(calls) == 1


async def test_retries_are_limited(db):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, text="x")
    with pytest.raises(ModelCallError):
        await gateway(handler).chat(spec(retries=2), [{"role": "user", "content": "oi"}], await _ctx(), Out)
    assert len(calls) == 3


async def test_provider_out_of_credit_is_a_budget_stop_and_is_recorded(tenant):
    ctx = await _ctx()
    ctx.tenant_id = tenant
    with pytest.raises(BudgetDenied) as e:
        await gateway(lambda r: httpx.Response(402, text="no credit")).chat(spec(), [{"role": "user", "content": "oi"}], ctx, Out)
    assert e.value.scope == "provider_credit"
    async with tenant_tx(tenant) as conn:
        alerts = await evaluate_alerts(conn, tenant, CFG.operations.alerts, [])
    assert any(a.key.startswith("provider_out_of_credit") for a in alerts)


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(500, text="x"), lambda r: httpx.Response(200, json={"no": "data"}), lambda r: httpx.Response(200, text="x"),
])
async def test_embedding_failures_all_become_a_model_error(db, handler):
    ref = ModelRef(model="openai/text-embedding-3-small", dims=1024)
    with pytest.raises(ModelCallError):
        await gateway(handler).embed(ref, ["oi"], await _ctx())


async def test_voice_goes_to_openrouter_with_the_retention_policy(db):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        seen["path"] = request.url.path
        return httpx.Response(200, json={"text": "quero um carro"})
    ref = ModelRef(model="openai/whisper-large-v3-turbo")
    text = await gateway(handler, provider="openrouter").transcribe(ref, b"OggS\x00audio", "a.ogg", 5, await _ctx(), language="pt")
    assert text == "quero um carro" and seen["path"].endswith("/audio/transcriptions")
    assert seen["provider"] == {"zdr": True, "data_collection": "deny"}
    assert seen["model"] == "openai/whisper-large-v3-turbo" and seen["language"] == "pt"
    assert seen["input_audio"] == {"data": base64.b64encode(b"OggS\x00audio").decode(), "format": "ogg"}


@pytest.mark.parametrize("head, fmt", [(b"OggS....", "ogg"), (b"\x00\x00\x00\x18ftypmp42", "m4a"), (b"RIFF....", "wav"), (b"ID3.....", "mp3")])
def test_audio_format_comes_from_the_file(head, fmt):
    assert G._audio_format(head) == fmt


def test_production_refuses_to_start_without_what_nobody_would_notice_is_missing():
    with pytest.raises(ValidationError, match="OPS_ALERT"):
        Settings(app_env="prod", llm_provider="openrouter", ops_alert_email=None, ops_alert_whatsapp=None)
    with pytest.raises(ValidationError, match="LLM_PROVIDER"):
        Settings(app_env="prod", llm_provider="openai", ops_alert_email="ops@x.test")
    assert Settings(app_env="prod", llm_provider="openrouter", ops_alert_email="ops@x.test").app_env == "prod"


def test_lists_in_the_environment_are_plain_text(monkeypatch):
    monkeypatch.setenv("AUTOMATION_APP_IDS", "111, 222")
    assert Settings().automation_app_ids == ["111", "222"]
    monkeypatch.setenv("AUTOMATION_APP_IDS", "")
    assert Settings().automation_app_ids == []


# ------------------------------------------------------------------ alerts
async def test_turn_failure_alert_is_alive(tenant):
    from agentplatform.core.config.schema import OpsAlertsConfig
    for _ in range(2):
        conv = await inbound(tenant, ["oi"])
        await runner({"analyze_turn": [RuntimeError("boom")]}).run(tenant, conv["id"])
    async with tenant_tx(tenant) as conn:
        alerts = await evaluate_alerts(conn, tenant, OpsAlertsConfig(turn_failures_per_hour=2), [])
    assert any(a.key.startswith("turn_failures") for a in alerts)


async def test_seller_not_told_reaches_the_operator(tenant):
    from agentplatform.core.config.schema import OpsAlertsConfig
    conv = await inbound(tenant, ["oi"])
    await runner({"analyze_turn": [RuntimeError("boom")]}).run(tenant, conv["id"])
    async with tenant_tx(tenant) as conn:
        await conn.execute("update handoffs set notify_at = now() - interval '1 hour' where conversation_id=%s", (conv["id"],))
        alerts = await evaluate_alerts(conn, tenant, OpsAlertsConfig(), [])
    assert any(a.key.startswith("handoff_undelivered") for a in alerts)


async def test_comments_from_polling_do_not_hide_that_directs_stopped_arriving(tenant):
    from datetime import datetime, timezone

    from agentplatform.core.channels.events import CanonicalEvent
    from agentplatform.core.config.schema import OpsAlertsConfig
    from agentplatform.core.orchestration.ingress import persist_events
    acc = (await rows(tenant, "select external_account_id from channel_accounts where tenant_id=%s", tenant))[0]["external_account_id"]
    async with tenant_tx(tenant) as conn:
        await conn.execute("update channel_accounts set token_enc=%s where tenant_id=%s",
                           (await encrypt_field(conn, tenant, "tok", "channel_accounts.token"), tenant))
    await persist_events([CanonicalEvent(event_type="CommentReceived", channel="instagram", account_external_id=acc, contact_external_id="U",
                                         idempotency_key=f"cmt:{uuid.uuid4()}", comment_id="c", text="x", provider_ts=datetime.now(timezone.utc))])
    async with tenant_tx(tenant) as conn:
        alerts = await evaluate_alerts(conn, tenant, OpsAlertsConfig(), [])
    assert any(a.key.startswith("no_inbound") for a in alerts)


async def test_budget_alert_comes_back_after_a_top_up(tenant):
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into budget_balances (tenant_id, scope, cap_usd, spent_usd) values (%s,'total',1,0.9)", (tenant,))
        first = [a for a in await evaluate_alerts(conn, tenant, CFG.operations.alerts, [0.8]) if a.key.startswith("budget")]
        await conn.execute("update budget_balances set cap_usd = 2, spent_usd = 1.8 where tenant_id=%s and scope='total'", (tenant,))
        second = [a for a in await evaluate_alerts(conn, tenant, CFG.operations.alerts, [0.8]) if a.key.startswith("budget")]
    assert first[0].key != second[0].key


async def test_platform_checks_run_against_the_real_catalog(db):
    from agentplatform.core.config.schema import OpsAlertsConfig
    async with platform_tx() as conn:
        out = await evaluate_platform_alerts(conn, OpsAlertsConfig(), 0, [1.0], unreadable_bytes=10)
    assert any(a.key == "unreadable_webhooks" for a in out)


class Mail:
    def __init__(self, ok):
        self.ok = ok

    async def send_text(self, to, subject, body):
        return self.ok


async def test_an_alert_that_did_not_get_out_is_tried_again(tenant):
    a = OpsAlert(f"k-{uuid.uuid4()}", tenant, "t", "d")
    assert await OpsNotifier(Mail(False), "ops@x.test").send(a) is False
    assert await OpsNotifier(Mail(True), "ops@x.test").send(a) is True
    assert await OpsNotifier().send(a) is True                    # dev: no channel configured, the log is all there is
    async with tenant_tx(tenant) as conn:
        assert await should_send(conn, a, 60)
        assert await should_send(conn, a, 60)                     # nothing recorded until it is delivered


# ------------------------------------------------------------------ production activation
def test_budget_and_flags_need_no_new_evaluation_but_behaviour_does():
    a = json.loads(CFG.model_dump_json())
    assert _only_operational_change(a, {**a, "budget": {**a["budget"], "daily_cap_usd": "9"}})
    assert _only_operational_change(a, {**a, "operations": {**a["operations"], "feature_flags": {"go_live": True}}})
    assert not _only_operational_change(a, {**a, "operations": {**a["operations"], "canary_percent": 5}})
    changed = json.loads(CFG.model_dump_json())
    changed["models"]["respond"]["prompt_version"] = "respond@v1"
    assert not _only_operational_change(a, changed)


def prod_cfg() -> PlatformConfig:
    return load_yaml(ROOT / "config/tenants/prod-consortium.example.yaml")


def test_the_production_template_is_valid_and_complete():
    from agentplatform.core.models import pricing
    c = prod_cfg()
    assert c.environment == "prod" and not c.models.allow_same_family_judge
    assert not c.operations.feature_flags.get("go_live")
    assert c.budget.daily_cap_usd < Decimal("2.40")               # below the key's daily cap in prod.env.example
    for node in ("analyze_turn", "respond", "output_judge", "summarize", "comment_intent"):
        m = getattr(c.models, node)
        assert (ROOT / "prompts" / f"{m.prompt_version}.j2").exists()
        for model in (m.primary, *m.fallbacks):
            assert pricing.chat_price(model)
    assert pricing.embedding_cost(c.models.embedding.model, 1) is not None
    assert pricing.transcription_cost(c.models.transcription.model, 1) is not None


def test_go_live_is_refused_while_a_subprocessor_is_pending():
    doc = json.loads(prod_cfg().model_dump_json())
    doc["operations"]["feature_flags"]["go_live"] = True
    with pytest.raises(ValidationError, match="go_live"):
        PlatformConfig.model_validate(doc)


async def test_production_config_only_activates_with_a_gate(tenant):
    c = prod_cfg()
    async with tenant_tx(tenant) as conn:
        with pytest.raises(ConfigError):
            await registry.publish(conn, tenant, c, package_compliance=PKG.manifest.compliance, activate=True)
        async with conn.transaction():
            vid = await registry.publish(conn, tenant, c, package_compliance=PKG.manifest.compliance, eval_run_id=uuid.uuid4(), activate=True)
    assert (await rows(tenant, "select status from config_versions where id=%s", vid))[0]["status"] == "active"


async def test_a_top_up_in_production_can_be_approved_without_a_new_gate(tenant):
    c = prod_cfg()
    author, approver = uuid.uuid4(), uuid.uuid4()
    run = uuid.uuid4()
    async with tenant_tx(tenant) as conn:
        await registry.publish(conn, tenant, c, package_compliance=PKG.manifest.compliance, eval_run_id=run, activate=True)
        topup = c.model_copy(update={"budget": c.budget.model_copy(update={"pilot_total_cap_usd": Decimal("4.60"), "net_credit_usd": Decimal("5.00")})})
        vid = await registry.propose(conn, tenant, topup, package_compliance=PKG.manifest.compliance, created_by=author)
        assert await registry.approve(conn, tenant, vid, approver) == "active"
    assert (await rows(tenant, "select eval_run_id from config_versions where id=%s", vid))[0]["eval_run_id"] == run


async def test_a_behaviour_change_in_production_still_needs_the_gate(tenant):
    c = prod_cfg()
    async with tenant_tx(tenant) as conn:
        await registry.publish(conn, tenant, c, package_compliance=PKG.manifest.compliance, eval_run_id=uuid.uuid4(), activate=True)
        changed = c.model_copy(update={"models": c.models.model_copy(update={"respond": c.models.respond.model_copy(update={"prompt_version": "respond@v6"})})})
        vid = await registry.propose(conn, tenant, changed, package_compliance=PKG.manifest.compliance, created_by=uuid.uuid4())
        with pytest.raises(ConfigError):
            await registry.approve(conn, tenant, vid, uuid.uuid4())


async def test_production_without_an_active_config_does_not_run_on_a_file(tenant, monkeypatch):
    from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier
    from agentplatform.runtime import Runtime
    rt = Runtime(adapters={"instagram": FakeChannelAdapter()}, gateway=None, inbox=FallbackInbox([LogNotifier()]))
    (ROOT / "config/tenants" / f"t-{tenant.hex[:8]}.yaml").write_text((ROOT / "config/tenants/dev-consortium.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    try:
        assert (await rt.tenant(tenant)).config_version_id is None        # dev: the file is a convenience
        rt._tenants.clear()
        monkeypatch.setattr(rt.settings, "app_env", "prod")
        with pytest.raises(ConfigError):
            await rt.tenant(tenant)
    finally:
        (ROOT / "config/tenants" / f"t-{tenant.hex[:8]}.yaml").unlink()


async def test_activation_refuses_a_file_changed_after_the_gate(tenant, monkeypatch):
    import argparse

    from agentplatform import cli
    from agentplatform.core.config.registry import resolve_with_package
    slug = f"t-{tenant.hex[:8]}"
    path = ROOT / "config/tenants" / f"{slug}.yaml"
    path.write_text((ROOT / "config/tenants/prod-consortium.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    async def experiment(dataset, variants):
        async with platform_tx() as conn:
            r = await (await conn.execute("insert into experiments (dataset, variants, status) values (%s,%s,'finished') returning id",
                                          (dataset, json.dumps(variants)))).fetchone()
        return str(r["id"])
    monkeypatch.setattr("agentplatform.core.db.pool.close_pool", lambda: _noop())
    try:
        good = config_hash(resolve_with_package(load_yaml(path), PKG.manifest.compliance))
        for bad in (await experiment("gate:fast", {"passed": True, "config_hash": good}),
                    await experiment("gate:full", {"passed": False, "config_hash": good}),
                    await experiment("gate:full", {"passed": True, "config_hash": "other"})):
            with pytest.raises(ConfigError):
                await cli._config_activate(argparse.Namespace(slug=slug, experiment=bad))
        ok = await experiment("gate:full", {"passed": True, "config_hash": good})
        await cli._config_activate(argparse.Namespace(slug=slug, experiment=ok))
        assert (await rows(tenant, "select status, eval_run_id::text as run from config_versions where env='prod'"))[0] == {"status": "active", "run": ok}
    finally:
        path.unlink()


async def _noop():
    return None


# ------------------------------------------------------------------ queue connection of the API process
async def test_a_console_action_does_not_close_the_connection_webhooks_use(db):
    """Found reading the code after the real test: `async with app.open_async()` closed the connection other callers were keeping open."""
    from agentplatform.jobs import app as J
    probe = ("summarize", {"tenant_id": str(uuid.uuid4()), "conversation_id": str(uuid.uuid4())}, {"queue": f"__test_{uuid.uuid4().hex[:8]}"})
    try:
        await J.defer_jobs([probe])
        pool = J.app.connector._async_pool
        assert pool is not None
        await J.defer_jobs([probe])
        await J.defer_ingress([])
        assert J.app.connector._async_pool is pool
    finally:
        await J.app.close_async()
        J._open_lock = None


# ------------------------------------------------------------------ public routes
def signed(payload: dict, secret: str) -> str:
    def enc(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    p = enc(json.dumps(payload).encode())
    return enc(hmac.new(secret.encode(), p.encode(), hashlib.sha256).digest()) + "." + p


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from agentplatform.api.main import create_app
    from agentplatform.settings import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "meta_app_secret", SecretStr("app-secret"))
    monkeypatch.setattr(s, "legal_controller_name", "Empresa Exemplo")
    monkeypatch.setattr(s, "legal_contact_email", "privacidade@exemplo.test")
    with TestClient(create_app(on_persisted=None)) as c:
        yield c


def test_public_pages_exist_for_the_review(client):
    r = client.get("/privacy")
    assert r.status_code == 200 and "Empresa Exemplo" in r.text and "privacidade@exemplo.test" in r.text
    assert "controladora" in r.text and "operadora" in r.text          # the app serves other companies (ADR-025)
    assert client.get("/data-deletion").status_code == 200


def test_public_pages_stay_hidden_until_the_controller_is_configured(client, monkeypatch):
    from agentplatform.settings import get_settings
    monkeypatch.setattr(get_settings(), "legal_controller_name", None)
    assert client.get("/privacy").status_code == 404


def test_deletion_callback_answers_as_the_provider_expects(client):
    r = client.post("/meta/data-deletion", data={"signed_request": signed({"algorithm": "HMAC-SHA256", "user_id": "42"}, "app-secret")})
    assert r.status_code == 200
    code = r.json()["confirmation_code"]
    assert r.json()["url"].endswith(f"/data-deletion/status/{code}")
    assert code in client.get(f"/data-deletion/status/{code}").text
    assert client.get("/data-deletion/status/nothing-here").status_code == 404
    assert client.post("/meta/deauthorize", data={"signed_request": signed({"algorithm": "HMAC-SHA256", "user_id": "42"}, "app-secret")}).status_code == 200


def test_a_forged_signed_request_is_refused(client):
    for bad in (signed({"algorithm": "HMAC-SHA256", "user_id": "42"}, "other-secret"), "garbage", "a.b"):
        assert client.post("/meta/data-deletion", data={"signed_request": bad}).status_code == 400


def test_giving_a_conversation_back_queues_the_answer_right_away(monkeypatch):
    import asyncio

    from agentplatform.api import console
    from agentplatform.jobs import app as J
    seen = []

    async def fake(nxt):
        seen.extend(nxt)
    monkeypatch.setattr(J, "defer_jobs", fake)
    tid, cid = uuid.uuid4(), uuid.uuid4()
    asyncio.run(console.answer_pending(tid, cid))
    assert seen == [("close_batch", {"tenant_id": str(tid), "conversation_id": str(cid)}, {"queueing_lock": f"cb:{cid}"})]

    async def down(nxt):
        raise OSError("queue down")
    monkeypatch.setattr(J, "defer_jobs", down)
    asyncio.run(console.answer_pending(tid, cid))           # never breaks the seller's click: the sweep picks it up


def test_seller_buttons_are_reachable(client):
    r = client.post("/webhooks/whatsapp-notifier", content=b"{}")
    assert r.status_code == 401                # its own handler answered, not the generic channel route's 404


def test_deep_health_says_whether_anyone_is_working(client):
    r = client.get("/health/deep")
    assert r.json()["db"] is True and r.status_code in (200, 503) and client.get("/health").status_code == 200


def test_proxy_routes_what_the_app_serves():
    caddy = (ROOT / "docker/Caddyfile").read_text(encoding="utf-8")
    for path in ("/actions/*", "/privacy", "/data-deletion/*", "/meta/*", "/health/*", "/webhooks/*"):
        assert path in caddy


def test_compose_gives_the_database_a_way_out_and_the_app_no_owner_password():
    import yaml
    doc = yaml.safe_load((ROOT / "docker/compose.prod.yaml").read_text(encoding="utf-8"))
    s = doc["services"]
    assert "egress" in s["postgres"]["networks"] and "ports" not in s["postgres"]
    for name in ("api", "worker", "worker-bg"):
        assert "pg_owner_password" not in s[name]["secrets"]
    assert "pg_owner_password" in s["migrate"]["secrets"]


# ------------------------------------------------------------------ gateway: the chain on OpenRouter, as measured with the real provider
def chain_spec(primary, *fallbacks, retries=1) -> NodeModel:
    return NodeModel(primary=primary, fallbacks=list(fallbacks), prompt_version="respond@v1", retries=retries, max_output_tokens=10)


OK_FROM = lambda m: {"model": m, "choices": [{"message": {"content": '{"answer": "oi"}'}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.000001}}


async def test_a_model_slower_than_our_timeout_goes_to_its_fallback_without_waiting_twice(db):
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body["model"])
        if body["model"] == "mistralai/mistral-small-2603":
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json=OK_FROM(body["model"]))
    r = await gateway(handler, "openrouter").chat(chain_spec("mistralai/mistral-small-2603", "google/gemini-3.1-flash-lite"), [{"role": "user", "content": "x"}], await _ctx(), Out)
    assert r.model_used == "google/gemini-3.1-flash-lite" and seen == ["mistralai/mistral-small-2603", "google/gemini-3.1-flash-lite"]


async def test_a_fallback_refused_for_the_parameters_of_the_first_is_called_with_its_own(db):
    """OpenRouter answers 404 when the first model of the list sends `temperature` and the next one does not accept it."""
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if body["model"] == "mistralai/mistral-small-2603":
            return httpx.Response(404, json={"error": {"message": "No endpoints found that can handle the requested parameters."}})
        return httpx.Response(200, json=OK_FROM(body["model"]))
    r = await gateway(handler, "openrouter").chat(chain_spec("mistralai/mistral-small-2603", "openai/gpt-5-nano"), [{"role": "user", "content": "x"}], await _ctx(), Out)
    assert r.model_used == "openai/gpt-5-nano"
    assert "temperature" in bodies[0] and "reasoning_effort" not in bodies[0]
    assert "temperature" not in bodies[1] and bodies[1]["reasoning_effort"] == "minimal" and bodies[1]["models"] == ["openai/gpt-5-nano"]


async def test_no_credit_stops_at_the_first_model(db):
    seen = []

    def handler(req):
        seen.append(json.loads(req.content)["model"])
        return httpx.Response(402, json={"error": {"message": "no credit"}})
    with pytest.raises(BudgetDenied):
        await gateway(handler, "openrouter").chat(chain_spec("mistralai/mistral-small-2603", "openai/gpt-5-nano"), [{"role": "user", "content": "x"}], await _ctx(), Out)
    assert seen == ["mistralai/mistral-small-2603"]


async def test_a_provider_that_only_sends_blank_bytes_still_times_out(db):
    """Measured on the real provider: blank bytes every few seconds, so a timeout per read never fires and a stuck call held the turn for 40 s."""
    import asyncio
    import time
    seen = []

    class Blank(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(200):
                await asyncio.sleep(0.05)
                yield b" "

    def handler(req):
        body = json.loads(req.content)
        seen.append(body["model"])
        if body["model"] == "mistralai/mistral-small-2603":
            return httpx.Response(200, stream=Blank())
        return httpx.Response(200, json=OK_FROM(body["model"]))
    slow = chain_spec("mistralai/mistral-small-2603", "google/gemini-3.1-flash-lite").model_copy(update={"timeout_s": 0.2})
    t0 = time.monotonic()
    r = await gateway(handler, "openrouter").chat(slow, [{"role": "user", "content": "x"}], await _ctx(), Out)
    assert r.model_used == "google/gemini-3.1-flash-lite" and seen == ["mistralai/mistral-small-2603", "google/gemini-3.1-flash-lite"]
    assert time.monotonic() - t0 < 2, "the limit is the limit, however alive the connection looks"


async def test_one_question_has_the_short_limit_and_a_batch_the_long_one(db, monkeypatch):
    used = []

    def handler(req):
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.1]} for i in range(len(json.loads(req.content)["input"]))], "usage": {"prompt_tokens": 1}})
    gw = gateway(handler, "openrouter")
    original = gw._post

    async def spy(path, timeout_s, **kw):
        used.append(timeout_s)
        return await original(path, timeout_s, **kw)
    monkeypatch.setattr(gw, "_post", spy)
    spec_ = ModelRef(model="qwen/qwen3-embedding-8b", dims=1, timeout_s=5, batch_timeout_s=90)
    await gw.embed(spec_, ["um"], await _ctx())
    await gw.embed(spec_, ["um", "dois"], await _ctx())
    assert used == [5, 90]
