"""Browser tests of the console (plan 14 F2-CON-*): a real server process, a real browser and the test database. Spends nothing: the server
gets a key no provider accepts, and requests to any other host are cut. Skipped without a browser, unless E2E_REQUIRED is set (CI)."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="package")
def server(tmp_path_factory):
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    tmp = tmp_path_factory.mktemp("e2e")
    env = {**os.environ, "PUBLIC_BASE_URL": base, "OPENAI_API_KEY": "sk-e2e-no-provider-accepts-this", "OPENROUTER_API_KEY": "sk-e2e-no-provider-accepts-this",
           "SPOOL_DIR": str(tmp / "spool"), "HEARTBEAT_URL": "", "SMTP_URL": "", "PYTHONIOENCODING": "utf-8", "LLM_PROVIDER": "openrouter"}
    log = open(tmp / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name("_server.py")), str(port)], cwd=ROOT, env=env, stdout=log, stderr=log)
    try:
        for _ in range(120):
            if proc.poll() is not None:
                break
            try:
                urllib.request.urlopen(f"{base}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("server did not start")
        if proc.poll() is not None:
            raise RuntimeError("server exited:\n" + (tmp / "server.log").read_text(encoding="utf-8")[-2000:])
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=15)
        log.close()


@dataclass
class World:
    tenant: uuid.UUID = None
    password: str = ""
    users: dict = field(default_factory=dict)
    conv: dict = field(default_factory=dict)
    contact: dict = field(default_factory=dict)
    handoff: uuid.UUID = None
    form_path: str = ""
    phone: str = "+5511999887766"


async def _seed() -> World:
    from agentplatform.cli import _ensure_kb_index
    from agentplatform.core.config.registry import load_yaml
    from agentplatform.core.db.pool import close_pool, platform_tx, tenant_tx
    from agentplatform.core.security.keys import crypto, encrypt_field
    from agentplatform.core.tools.lead_form import create_link
    from tests.integration.helpers import make_conversation
    from tests.integration.test_console_admin import PW, _user
    from tests.integration.test_dm_scope import _publish

    w = World(tenant=uuid.uuid4(), password=PW)
    tid = w.tenant
    _ensure_kb_index(load_yaml(ROOT / "config/tenants/dev-consortium.yaml"))
    async with platform_tx() as conn:
        await conn.execute("insert into tenants (id, slug, display_name, segment_package, segment_package_version) values (%s,%s,'E2E','consortium','0.1.0')",
                           (tid, f"e2e-{tid.hex[:8]}"))
        await conn.execute("insert into channel_accounts (tenant_id, channel, external_account_id, external_page_id) values (%s,'instagram',%s,%s)",
                           (tid, f"ig-{tid.hex[:10]}", f"page-{tid.hex[:10]}"))
    await _publish(tid)
    for key, role in (("owner", "tenant_owner"), ("approver", "tenant_owner"), ("seller", "seller"), ("reviewer", "reviewer"),
                      ("totp", "seller"), ("newpass", "seller")):
        w.users[key] = (await _user(tid, role=role, name=key.capitalize()))[0]
    for key in ("ownership", "trace", "review", "lgpd", "action", "wide"):
        conv, _ = await make_conversation(tid)
        w.conv[key], w.contact[key] = conv["id"], conv["contact_id"]
        async with tenant_tx(tid) as conn:
            await conn.execute("update contacts set first_name=%s where id=%s", (f"Cliente {key}", conv["contact_id"]))
            await conn.execute("insert into messages (tenant_id, conversation_id, direction, kind, text_masked, provider_ts) values (%s,%s,'out_bot','text',%s,now())",
                               (tid, conv["id"], f"Resposta do assistente para {key}"))
    await make_conversation(tid, igsid=f"LAB-{uuid.uuid4().hex[:6]}")
    async with tenant_tx(tid) as conn:
        await conn.execute(
            """insert into turns (tenant_id, conversation_id, run_epoch, input_message_ids, status, lease_id, lease_expires_at, stage_before, stage_after, llm_calls, cost_usd, trace)
               values (%s,%s,1,'{}','committed',gen_random_uuid(),now(),'novo','novo',2,0.001,%s)""",
            (tid, w.conv["trace"], json.dumps({"guidelines": ["marca-do-trace"]})))
        c = crypto()
        await conn.execute(
            "update contacts set display_name_enc=%s, phone_hash=%s, phone_enc=%s, email_hash=%s, email_enc=%s where id=%s",
            (await encrypt_field(conn, tid, "Cliente Lgpd da Silva", "contacts.display_name"), c.lookup_hash(w.phone, kind="phone"),
             await encrypt_field(conn, tid, w.phone, "contacts.phone"), c.lookup_hash("titular@example.com", kind="email"),
             await encrypt_field(conn, tid, "titular@example.com", "contacts.email"), w.contact["lgpd"]))
        lead = await (await conn.execute("select id from leads where contact_id=%s", (w.contact["action"],))).fetchone()
        await conn.execute("update conversations set ownership='AWAITING_HUMAN', handoff_pending=true where id=%s", (w.conv["action"],))
        row = await (await conn.execute(
            """insert into handoffs (tenant_id, conversation_id, lead_id, reason, priority, summary, notify_at, sla_due_at)
               values (%s,%s,%s,'explicit_request','normal','{}'::jsonb,now(),now() + interval '30 minutes') returning id""",
            (tid, w.conv["action"], lead["id"]))).fetchone()
        w.handoff = row["id"]
        turn = await (await conn.execute("select id from turns where conversation_id=%s", (w.conv["trace"],))).fetchone()
        await conn.execute("insert into curation_queue (tenant_id, question_masked, turn_id, frequency) values (%s,'Posso pagar com boleto?',%s,3)", (tid, turn["id"]))
        form_lead = await (await conn.execute("select id from leads where contact_id=%s", (w.contact["review"],))).fetchone()
        w.form_path = (await create_link(conn, tid, form_lead["id"], "http://t")).split("http://t")[1]
    await close_pool()
    return w


@pytest.fixture(scope="package")
def world(server):
    return asyncio.run(_seed())


@pytest.fixture(scope="package")
def browser(world):       # seeded first: asyncio.run does not work while the browser driver holds the event loop
    pytest.importorskip("playwright")
    from playwright.sync_api import Error, sync_playwright
    policy = asyncio.get_event_loop_policy()
    if sys.platform == "win32":      # the driver is a subprocess: that needs the proactor loop, unlike the database driver in the other tests
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        pw = sync_playwright().start()
    finally:
        asyncio.set_event_loop_policy(policy)
    found = None
    for options in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            found = pw.chromium.launch(headless=True, **options)
            break
        except Error:
            continue
    if found is None:
        pw.stop()
        if os.environ.get("E2E_REQUIRED"):
            pytest.fail("no browser available: run `playwright install chromium`")
        pytest.skip("no browser available")
    yield found
    found.close()
    pw.stop()


@pytest.fixture
def visit(browser, server, world):
    """Opens a clean browser session. Any JavaScript error on any page fails the test."""
    contexts, errors = [], []

    def _open(*, width: int = 1280, height: int = 800):
        ctx = browser.new_context(base_url=server, viewport={"width": width, "height": height}, accept_downloads=True)
        contexts.append(ctx)
        ctx.route("**/*", lambda route: route.continue_() if route.request.url.startswith(server) else route.abort())
        page = ctx.new_page()
        page.set_default_timeout(10_000)
        page.on("pageerror", lambda e: errors.append(str(e)))
        return page
    yield _open
    for ctx in contexts:
        ctx.close()
    assert not errors, errors


@pytest.fixture
def login(world):
    def _login(page, who: str, password: str | None = None):
        page.goto("/console/login")
        page.fill('input[name="email"]', world.users[who])
        page.fill('input[name="password"]', password or world.password)
        page.get_by_role("button", name="Entrar").click()
        return page
    return _login


@pytest.fixture
def run_jobs(world):
    def _run():
        env = {**os.environ, "LLM_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "sk-e2e-no-provider-accepts-this", "PYTHONIOENCODING": "utf-8"}
        subprocess.run([sys.executable, str(Path(__file__).with_name("_knowledge_jobs.py")), str(world.tenant)], cwd=ROOT, env=env, check=True, timeout=120)
    return _run


@pytest.fixture
def sql():
    import psycopg
    from psycopg.rows import dict_row

    def _sql(query: str, *args):
        with psycopg.connect(os.environ["MIGRATION_DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn:
            cur = conn.execute(query, args)
            return cur.fetchall() if cur.description else None
    return _sql
