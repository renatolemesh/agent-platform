"""What the analysis of what was missing found: correction of personal data, the daily review, monthly restore test, questions offered in a
new direct, the seller's guide and the direct key never indexing client material."""
import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import psycopg
import pytest
from pydantic import SecretStr

from agentplatform.core.channels.instagram.adapter import InstagramGraphAdapter
from agentplatform.core.channels.ports import AccountRef, SendError, TokenInvalidError
from agentplatform.core.config.registry import load_yaml, resolve_with_package
from agentplatform.core.config.schema import ModelRef, OpsAlertsConfig, ReviewConfig
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.lgpd.service import (
    CorrectionRefused,
    correct_subject,
    erase_subject,
    export_subject,
    find_subject,
    retention_sweep,
)
from agentplatform.core.models.budget import BudgetService
from agentplatform.core.models.gateway import CallContext, ModelCallError, ModelGateway
from agentplatform.core.ops import review
from agentplatform.core.ops.observability import collect_metrics, evaluate_platform_alerts
from agentplatform.core.outbox.dispatcher import DispatchPolicy
from agentplatform.core.packages.contract import SlotSpec, load_package
from agentplatform.core.security.keys import crypto, encrypt_field
from agentplatform.settings import Settings, get_settings
from tests.integration.helpers import make_conversation
from tests.integration.test_console_admin import PW, _client, _user
from tests.integration.test_dm_scope import _csrf, _publish

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
PKG = load_package("consortium")
CFG = resolve_with_package(load_yaml(ROOT / "config/tenants/dev-consortium.yaml"), PKG.manifest.compliance)
PHONE = "+5511999887766"
SLOTS = [SlotSpec(name="amount", type="money"), SlotSpec(name="goal", type="enum", values=["a", "b"]), SlotSpec(name="note", type="text")]


async def subject(tenant, phone=PHONE, email="ana@example.com"):
    conv, igsid = await make_conversation(tenant)
    c = crypto()
    async with tenant_tx(tenant) as conn:
        await conn.execute(
            "update contacts set first_name='Ana', display_name_enc=%s, phone_hash=%s, phone_enc=%s, email_hash=%s, email_enc=%s where id=%s",
            (await encrypt_field(conn, tenant, "Ana Lima", "contacts.display_name"), c.lookup_hash(phone, kind="phone"),
             await encrypt_field(conn, tenant, phone, "contacts.phone"), c.lookup_hash(email, kind="email"),
             await encrypt_field(conn, tenant, email, "contacts.email"), conv["contact_id"]))
        await conn.execute("update leads set profile=%s where contact_id=%s",
                           (json.dumps({"amount": {"value": "100000", "confidence": 0.9}, "goal": {"value": "a", "confidence": 0.9}}), conv["contact_id"]))
        await conn.execute("update conversations set summary='resumo com o valor antigo' where id=%s", (conv["id"],))
    return conv


def fields(**over):
    return {"first_name": "Ana", "name": "Ana Lima", "phone": PHONE, "email": "ana@example.com", "profile": {}, "slots": SLOTS, **over}


async def correct(tenant, cid, **over):
    async with tenant_tx(tenant) as conn:
        return await correct_subject(conn, tenant, cid, None, **fields(**over))


# ------------------------------------------------------------------ right of correction
async def test_correction_replaces_the_data_and_the_person_is_found_by_the_new_phone(tenant):
    conv = await subject(tenant)
    cid = conv["contact_id"]
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into contact_identities (tenant_id, contact_id, channel, external_user_id) values (%s,%s,'whatsapp',%s)", (tenant, cid, PHONE))
    changed = await correct(tenant, cid, first_name="Anna", name="Anna Lima", phone="11 98888-7777", email="ANNA@EXAMPLE.COM",
                            profile={"amount": "250000", "goal": "b"})
    assert set(changed) == {"first_name", "name", "phone", "email", "profile.amount", "profile.goal"}
    async with tenant_tx(tenant) as conn:
        assert await find_subject(conn, tenant, phone="+5511988887777") == cid
        assert await find_subject(conn, tenant, email="anna@example.com") == cid
        assert await find_subject(conn, tenant, phone=PHONE) is None
        exported = await export_subject(conn, tenant, cid, None)
        profile = (await (await conn.execute("select profile from leads where contact_id=%s", (cid,))).fetchone())["profile"]
        summary = (await (await conn.execute("select summary from conversations where id=%s", (conv["id"],))).fetchone())["summary"]
        wa = await (await conn.execute("select external_user_id from contact_identities where contact_id=%s and channel='whatsapp'", (cid,))).fetchone()
        audit = await (await conn.execute("select after from audit_log where action='lgpd.correct'")).fetchone()
    assert exported["contact"] == {"first_name": "Anna", "name": "Anna Lima", "phone": "+5511988887777", "email": "anna@example.com"}
    assert profile["amount"]["value"] == "250000" and profile["amount"]["confidence"] == 1.0 and profile["goal"]["value"] == "b"
    assert summary is None, "the old value must not come back through the summary"
    assert wa["external_user_id"] == "+5511988887777"
    assert "Anna" not in json.dumps(audit["after"]) and "250000" not in json.dumps(audit["after"]), "the audit log holds field names only"


async def test_same_values_change_nothing(tenant):
    conv = await subject(tenant)
    assert await correct(tenant, conv["contact_id"], profile={"amount": "100000", "goal": "a"}) == []
    async with tenant_tx(tenant) as conn:
        assert (await (await conn.execute("select summary from conversations where id=%s", (conv["id"],))).fetchone())["summary"]
        assert not await (await conn.execute("select 1 from audit_log where action='lgpd.correct'")).fetchone()


async def test_empty_field_erases_that_value(tenant):
    conv = await subject(tenant)
    cid = conv["contact_id"]
    changed = await correct(tenant, cid, phone="", email="", profile={"goal": ""})
    assert set(changed) == {"phone", "email", "profile.goal"}
    async with tenant_tx(tenant) as conn:
        row = await (await conn.execute("select phone_hash, phone_enc, email_hash, email_enc from contacts where id=%s", (cid,))).fetchone()
        profile = (await (await conn.execute("select profile from leads where contact_id=%s", (cid,))).fetchone())["profile"]
    assert all(v is None for v in row.values())
    assert "goal" not in profile and profile["amount"]["value"] == "100000"


@pytest.mark.parametrize("over,field,reason", [({"phone": "123"}, "phone", "invalid"), ({"email": "sem-arroba"}, "email", "invalid"),
                                               ({"profile": {"goal": "z"}}, "goal", "invalid")])
async def test_invalid_value_leaves_everything_as_it_was(tenant, over, field, reason):
    conv = await subject(tenant)
    with pytest.raises(CorrectionRefused) as e:
        await correct(tenant, conv["contact_id"], first_name="Outro", **over)
    assert (e.value.field, e.value.reason) == (field, reason)
    async with tenant_tx(tenant) as conn:
        assert (await (await conn.execute("select first_name from contacts where id=%s", (conv["contact_id"],))).fetchone())["first_name"] == "Ana"


async def test_a_phone_of_someone_else_is_refused(tenant):
    other = await subject(tenant)
    conv, _ = await make_conversation(tenant)
    with pytest.raises(CorrectionRefused) as e:
        async with tenant_tx(tenant) as conn:
            await correct_subject(conn, tenant, conv["contact_id"], None, first_name="", name="", phone=PHONE, email="", profile={}, slots=SLOTS)
    assert e.value.reason == "in_use" and other


async def test_document_typed_in_a_free_text_field_is_masked(tenant):
    conv = await subject(tenant)
    await correct(tenant, conv["contact_id"], profile={"note": "meu cpf 529.982.247-25"})
    async with tenant_tx(tenant) as conn:
        profile = (await (await conn.execute("select profile from leads where contact_id=%s", (conv["contact_id"],))).fetchone())["profile"]
    assert "529.982" not in profile["note"]["value"]


async def test_correction_screen_for_the_owner_only_and_without_browser_copy(tenant):
    await _publish(tenant)
    conv = await subject(tenant)
    cid = conv["contact_id"]
    email, _ = await _user(tenant)
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        csrf = await _csrf(c, "/console/lgpd")
        page = await c.post("/console/lgpd/search", data={"csrf": csrf, "phone": PHONE})
        assert "Corrigir dados" in page.text and PHONE in page.text and page.headers["cache-control"] == "no-store"
        slots = {"slot:credit_amount": "300000", "slot:goal": "", "slot:city": "Campinas"}
        ok = await c.post(f"/console/lgpd/{cid}/correct", data={"csrf": csrf, "first_name": "Anna", "name": "Ana Lima", "phone": PHONE, "email": "ana@example.com", **slots})
        assert ok.status_code == 200 and "Dados corrigidos" in ok.text and "Campinas" in ok.text
        bad = await c.post(f"/console/lgpd/{cid}/correct", data={"csrf": csrf, "first_name": "Outra", "phone": "123", "email": "ana@example.com"})
        assert bad.status_code == 400 and "Telefone inválido" in bad.text
    async with tenant_tx(tenant) as conn:
        assert (await (await conn.execute("select first_name from contacts where id=%s", (cid,))).fetchone())["first_name"] == "Anna"
    seller, _ = await _user(tenant, role="seller", name="V")
    async with _client() as c:
        await c.post("/console/login", data={"email": seller, "password": PW})
        csrf = await _csrf(c, "/console/posts")
        assert (await c.post(f"/console/lgpd/{cid}/correct", data={"csrf": csrf, "first_name": "X"})).status_code == 403


# ------------------------------------------------------------------ daily review
async def answered(tenant, *, igsid=None, at=None, handoff=False):
    conv, who = await make_conversation(tenant, igsid=igsid)
    at = at or datetime.now(timezone.utc)
    async with tenant_tx(tenant) as conn:
        await conn.execute("insert into messages (tenant_id, conversation_id, direction, kind, text_masked, provider_ts) values (%s,%s,'out_bot','text','oi',%s)",
                           (tenant, conv["id"], at))
        if handoff:
            lead = await (await conn.execute("select id from leads where contact_id=%s", (conv["contact_id"],))).fetchone()
            await conn.execute("insert into handoffs (tenant_id, conversation_id, lead_id, reason, priority, summary, notify_at, sla_due_at) "
                               "values (%s,%s,%s,'explicit_request','normal','{}'::jsonb,now(),now())", (tenant, conv["id"], lead["id"]))
    return conv


async def test_review_note_is_masked_and_a_problem_always_has_a_category(tenant):
    conv = await answered(tenant)
    async with tenant_tx(tenant) as conn:
        await review.record(conn, tenant, conv["id"], uuid.uuid4(), "problem", "nao-existe", "cliente 529.982.247-25 reclamou", ReviewConfig())
    async with tenant_tx(tenant) as conn:
        await review.record(conn, tenant, conv["id"], uuid.uuid4(), "ok", "tone", "", ReviewConfig())
        with pytest.raises(ValueError):
            await review.record(conn, tenant, conv["id"], uuid.uuid4(), "talvez", "", "", ReviewConfig())
        rows = await review.for_conversation(conn, conv["id"])
    assert rows[0]["verdict"] == "ok" and rows[0]["category"] is None and rows[0]["note_masked"] is None
    assert rows[1]["category"] == "other" and "529.982" not in rows[1]["note_masked"]


async def test_sample_brings_conversations_with_a_signal_first_and_leaves_the_lab_out(tenant):
    plain = [await answered(tenant) for _ in range(3)]
    flagged = await answered(tenant, handoff=True)
    await answered(tenant, igsid=f"LAB-{uuid.uuid4().hex[:6]}")
    await answered(tenant, at=datetime.now(timezone.utc) - timedelta(days=3))
    now = datetime.now(timezone.utc)
    async with tenant_tx(tenant) as conn:
        await review.record(conn, tenant, plain[0]["id"], uuid.uuid4(), "problem", "compliance", "", ReviewConfig())
        await review.record(conn, tenant, plain[1]["id"], uuid.uuid4(), "problem", "tone", "", ReviewConfig())
        s = await review.day_sample(conn, now.date(), "UTC", ReviewConfig())
        small = await review.day_sample(conn, now.date(), "UTC", ReviewConfig(daily_sample=2))
        found = await review.findings(conn, ReviewConfig())
    assert s["total"] == 4 and s["reviewed"] == 2 and s["problems"] == 2
    assert s["rows"][0]["id"] == flagged["id"] and s["rows"][0]["handoff_flag"]
    assert len(small["rows"]) == 2 and small["total"] == 4
    assert found["violations"] == 1 and len(found["rows"]) == 2


async def test_review_from_the_console_and_the_metric(tenant):
    await _publish(tenant)
    conv = await answered(tenant)
    other = uuid.uuid4()
    async with platform_tx() as conn:
        await conn.execute("insert into tenants (id, slug, display_name, segment_package, segment_package_version) values (%s,%s,'o','consortium','0.1.0')",
                           (other, f"o-{other.hex[:8]}"))
        await conn.execute("insert into channel_accounts (tenant_id, channel, external_account_id) values (%s,'instagram',%s)", (other, f"ig-{other.hex[:10]}"))
    foreign, _ = await make_conversation(other)
    email, _ = await _user(tenant, role="seller", name="V")
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        page = await c.get(f"/console/conversations/{conv['id']}")
        assert "Salvar revisão" in page.text
        csrf = await _csrf(c, f"/console/conversations/{conv['id']}")
        r = await c.post(f"/console/conversations/{conv['id']}/review", data={"csrf": csrf, "verdict": "problem", "category": "compliance", "note": "falou demais"})
        assert r.status_code == 303
        assert (await c.post(f"/console/conversations/{foreign['id']}/review", data={"csrf": csrf, "verdict": "ok"})).status_code == 404
        assert (await c.post(f"/console/conversations/{conv['id']}/review", data={"csrf": csrf, "verdict": "x"})).status_code == 400
        day = await c.get("/console/reviews")
        assert day.status_code == 200 and "falou demais" in day.text and "Regra de conformidade" in day.text
        assert 'data-kpi="violations">1<' in day.text
        assert (await c.get("/console/reviews?day=ontem")).status_code == 400
    async with platform_tx() as conn:
        assert f'agent_review_findings_30d{{category="compliance",tenant="{tenant}"}} 1.0' in (await collect_metrics(conn)).decode()


async def test_review_notes_follow_export_erasure_and_retention(tenant):
    conv = await answered(tenant)
    async with tenant_tx(tenant) as conn:
        await review.record(conn, tenant, conv["id"], uuid.uuid4(), "problem", "tone", "nota antiga", ReviewConfig())
        assert (await export_subject(conn, tenant, conv["contact_id"], None))["reviews"][0]["note"] == "nota antiga"
        await conn.execute("update conversation_reviews set created_at = now() - interval '400 days' where conversation_id=%s", (conv["id"],))
        await retention_sweep(conn, CFG, tenant)
        assert (await review.for_conversation(conn, conv["id"]))[0]["note_masked"] is None
        await review.record(conn, tenant, conv["id"], uuid.uuid4(), "problem", "tone", "nota nova", ReviewConfig())
        await erase_subject(conn, tenant, conv["contact_id"], None)
        assert "nota nova" not in [r["note_masked"] for r in await review.for_conversation(conn, conv["id"])]


# ------------------------------------------------------------------ monthly restore test
def _restore_rows(*rows):
    with psycopg.connect(get_settings().migration_database_url, autocommit=True) as oc:
        oc.execute("delete from restore_tests")
        for status, age_days in rows:
            oc.execute("insert into restore_tests (status, summary, finished_at) values (%s,'{}',now() - make_interval(days => %s))", (status, age_days))


async def _keys(required=True):
    async with platform_tx() as conn:
        out = await evaluate_platform_alerts(conn, OpsAlertsConfig(), 0, [], restore_tests_required=required)
    return [a.key.split(":")[0] for a in out if a.key.startswith("restore_test")]


@pytest.mark.parametrize("rows,expected", [
    ([], ["restore_test_overdue"]),                                      # never tested: a backup nobody restored is no guarantee
    ([("ok", 3)], []),
    ([("ok", 40)], ["restore_test_overdue"]),                            # cron died
    ([("ok", 40), ("failed", 2)], ["restore_test_failed", "restore_test_overdue"]),
    ([("ok", 10), ("failed", 2)], ["restore_test_failed"]),
    ([("failed", 10), ("ok", 2)], []),                                   # fixed since
])
async def test_restore_test_alerts(db, rows, expected):
    _restore_rows(*rows)
    try:
        assert await _keys() == expected
        assert await _keys(required=False) == [], "only production has backups to test"
    finally:
        _restore_rows()


@pytest.mark.parametrize("code,status", [(0, "ok"), (1, "failed")])
def test_monthly_script_records_the_outcome_and_keeps_the_exit_code(tmp_path, code, status):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    out = tmp_path / "psql.txt"
    (tmp_path / "restore.sh").write_text(f'#!/usr/bin/env bash\necho noise\necho \'{{"restore_test":"{status}"}}\'\nexit {code}\n', newline="\n")
    (tmp_path / "psql.sh").write_text(f'#!/usr/bin/env bash\n{{ printf "%s\\n" "$@"; cat; }} > "{tmp_path.as_posix()}/psql.txt"\n', newline="\n")
    env = {"RESTORE_CMD": f"bash {tmp_path.as_posix()}/restore.sh", "PSQL_CMD": f"bash {tmp_path.as_posix()}/psql.sh", "PATH": os.environ["PATH"]}
    r = subprocess.run([bash, "scripts/backup/restore_test_monthly.sh"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == code, r.stderr
    recorded = out.read_text()
    assert f"status={status}" in recorded and "insert into restore_tests" in recorded and "noise" not in recorded
    assert f'{{"restore_test":"{status}"}}' in r.stdout


def test_a_result_that_cannot_be_recorded_is_not_silent(tmp_path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("no bash")
    (tmp_path / "restore.sh").write_text('#!/usr/bin/env bash\necho \'{"restore_test":"ok"}\'\n', newline="\n")
    (tmp_path / "psql.sh").write_text('#!/usr/bin/env bash\nexit 2\n', newline="\n")
    env = {"RESTORE_CMD": f"bash {tmp_path.as_posix()}/restore.sh", "PSQL_CMD": f"bash {tmp_path.as_posix()}/psql.sh", "PATH": os.environ["PATH"]}
    r = subprocess.run([bash, "scripts/backup/restore_test_monthly.sh"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 3 and "could not be recorded" in r.stderr


# ------------------------------------------------------------------ questions offered in a new direct
def _account(token="tok"):
    return AccountRef(uuid.uuid4(), uuid.uuid4(), "ig", "page", token)


def _adapter(status=200, body=None):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(status, json=body if body is not None else {"result": "success"})
    return InstagramGraphAdapter("s", transport=httpx.MockTransport(handler)), seen


async def test_adapter_publishes_and_removes_questions():
    a, seen = _adapter()
    await a.set_ice_breakers(_account(), ["Como funciona?", "Quanto custa?"])
    await a.set_ice_breakers(_account(), [])
    post, delete = seen
    assert post.method == "POST" and post.url.path.endswith("/me/messenger_profile") and post.url.params["platform"] == "instagram"
    item = json.loads(post.content)["ice_breakers"][0]
    assert item["locale"] == "default" and [c["question"] for c in item["call_to_actions"]] == ["Como funciona?", "Quanto custa?"]
    assert len({c["payload"] for c in item["call_to_actions"]}) == 2
    assert delete.method == "DELETE" and json.loads(delete.content) == {"fields": ["ice_breakers"]}
    with pytest.raises(TokenInvalidError):
        await a.set_ice_breakers(_account(token=None), ["x"])
    refused, _ = _adapter(400, {"error": {"code": 100, "message": "no"}})
    with pytest.raises(SendError):
        await refused.set_ice_breakers(_account(), ["x"])


def test_questions_pass_the_same_checks_as_automatic_replies():
    from agentplatform.core.comments.settings import validate_questions
    rules = lambda qs: [p.rule for p in validate_questions(CFG, qs, PKG.manifest.allowed_urls)]      # noqa: E731
    assert rules(PKG.manifest.quick_questions) == [], "the package's suggestions must be publishable"
    assert rules([]) == ["missing_question"]
    assert "too_many_questions" in rules(["a?", "b?", "c?", "d?", "e?"])
    assert "too_long" in rules(["x" * 81])
    assert "numbers_provenance" in rules(["Parcelas de R$ 300?"])


class FakeQuestions:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def set_ice_breakers(self, account, questions):
        if self.fail:
            raise SendError("refused")
        self.calls.append(questions)


@pytest.fixture
def questions_env(monkeypatch):
    from agentplatform.core.comments import catalog
    from agentplatform.runtime import get_runtime

    def install(fail=False):
        fake = FakeQuestions(fail)
        monkeypatch.setitem(get_runtime().adapters, "instagram", fake)
        return fake

    async def accounts(tid):
        return [("instagram", _account())]
    monkeypatch.setattr(catalog, "accounts", accounts)
    return install


async def _ready(tenant, synced=True):
    await _publish(tenant)
    async with tenant_tx(tenant) as conn:
        ca = await (await conn.execute("select id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        await conn.execute("insert into dm_settings (channel_account_id, tenant_id, scope, contacts_synced_at) values (%s,%s,'origin_and_new',%s)",
                           (ca["id"], tenant, datetime.now(timezone.utc) if synced else None))
    email, _ = await _user(tenant, role="seller", name="V")
    return email


async def _saved(tenant):
    async with tenant_tx(tenant) as conn:
        return await (await conn.execute("select quick_questions, quick_questions_published_at from dm_settings where tenant_id=%s", (tenant,))).fetchone()


async def test_seller_publishes_and_takes_off_the_questions(tenant, questions_env):
    fake = questions_env()
    email = await _ready(tenant)
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        page = await c.get("/console/posts")
        assert "Não publicadas" in page.text and PKG.manifest.quick_questions[0] in page.text, "the package's suggestions come filled in"
        csrf = await _csrf(c)
        r = await c.post("/console/posts/direct/questions", data={"csrf": csrf, "questions": "Como funciona?\n\n  Fale comigo  \n"})
        assert r.status_code == 303 and fake.calls == [["Como funciona?", "Fale comigo"]]
        s = await _saved(tenant)
        assert s["quick_questions"] == ["Como funciona?", "Fale comigo"] and s["quick_questions_published_at"]
        assert "Retirar do Instagram" in (await c.get("/console/posts")).text
        r = await c.post("/console/posts/direct/questions", data={"csrf": csrf, "questions": "Como funciona?", "action": "remove"})
        assert r.status_code == 303 and fake.calls[-1] == []
        assert (await _saved(tenant))["quick_questions_published_at"] is None
    async with tenant_tx(tenant) as conn:
        assert await (await conn.execute("select 1 from audit_log where action='dm.quick_questions'")).fetchone()


async def test_a_refused_text_keeps_what_was_typed_and_publishes_nothing(tenant, questions_env):
    fake = questions_env()
    email = await _ready(tenant)
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        r = await c.post("/console/posts/direct/questions", data={"csrf": await _csrf(c), "questions": "Parcela de R$ 300?"})
    assert r.status_code == 400 and "Parcela de R$ 300?" in r.text and "Pergunta pronta" in r.text and fake.calls == []


async def test_questions_only_go_up_when_the_assistant_can_answer(tenant, questions_env, monkeypatch):
    from agentplatform import runtime
    fake = questions_env()
    email = await _ready(tenant, synced=False)
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        csrf = await _csrf(c)
        r = await c.post("/console/posts/direct/questions", data={"csrf": csrf, "questions": "Como funciona?"})
        assert r.status_code == 400 and "Espere a lista" in r.text
        async with tenant_tx(tenant) as conn:
            await conn.execute("update dm_settings set contacts_synced_at = now() where tenant_id=%s", (tenant,))
        monkeypatch.setattr(runtime.TenantRuntime, "policy", lambda self: DispatchPolicy(live=False))
        r = await c.post("/console/posts/direct/questions", data={"csrf": csrf, "questions": "Como funciona?"})
        assert r.status_code == 400 and "ainda não está ligado" in r.text
    assert fake.calls == []


async def test_provider_refusal_changes_nothing_here(tenant, questions_env):
    questions_env(fail=True)
    email = await _ready(tenant)
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        r = await c.post("/console/posts/direct/questions", data={"csrf": await _csrf(c), "questions": "Como funciona?"})
    assert r.status_code == 502 and "Nada mudou" in r.text
    assert (await _saved(tenant))["quick_questions_published_at"] is None


async def test_a_tap_reaches_the_assistant_as_a_message_from_someone_new(tenant):
    from agentplatform.core.channels.events import CanonicalEvent
    from agentplatform.core.orchestration.dm_scope import entry_allowed
    await _publish(tenant)
    async with tenant_tx(tenant) as conn:
        ca = await (await conn.execute("select id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        await conn.execute("insert into dm_settings (channel_account_id, tenant_id, scope, contacts_synced_at) values (%s,%s,'origin_only',now())", (ca["id"], tenant))
        ev = CanonicalEvent(event_type="Postback", channel="instagram", account_external_id="ig", contact_external_id="NEW", idempotency_key="pb:1",
                            provider_ts=datetime.now(timezone.utc), text="Como funciona?", postback_payload="quick_question:0")
        assert await entry_allowed(conn, ca["id"], ev)


# ------------------------------------------------------------------ guide
async def test_guide_names_the_buttons_that_exist(tenant):
    await _publish(tenant)
    email, _ = await _user(tenant, role="seller", name="V")
    screen = (ROOT / "src/agentplatform/api/templates/conversation.html").read_text(encoding="utf-8")
    async with _client() as c:
        await c.post("/console/login", data={"email": email, "password": PW})
        page = await c.get("/console/help")
    assert page.status_code == 200 and f"{CFG.handoff.sla_minutes} minutos" in page.text
    for label in ("Assumir conversa", "Retomar assistente", "Pausar assistente nesta conversa", "Encerrar", "Nunca atender esta pessoa", "Marcar venda"):
        assert label in screen and label in page.text, label


# ------------------------------------------------------------------ direct key
async def test_direct_key_never_indexes_documents(db):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}], "usage": {"prompt_tokens": 1}})
    s = Settings(llm_provider="openai", openai_api_key=SecretStr("sk-test"))
    gw = ModelGateway(BudgetService(Decimal("1"), Decimal("1"), key_name=f"test-{uuid.uuid4().hex[:8]}"), s, transport=httpx.MockTransport(handler))
    spec = ModelRef(model="openai/text-embedding-3-small", dims=1024)
    with pytest.raises(ModelCallError):
        await gw.embed(spec, ["trecho de um documento"], CallContext(None, "ingest", purpose="ingest"))
    assert calls == []
    await gw.embed(spec, ["pergunta sintética"], CallContext(None, "turn", purpose="turn"))
    assert len(calls) == 1
