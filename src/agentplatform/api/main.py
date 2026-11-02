"""HTTP app: Meta webhooks (fast-ack + spool), WhatsApp notifier buttons, lead form, console, health."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from agentplatform.api import console, console_setup, legal
from agentplatform.api.auth import COOKIE, read_session
from agentplatform.api.security import (
    ConsoleSessionMiddleware,
    PublicLimiter,
    RequestSpanMiddleware,
    SecurityHeadersMiddleware,
    check_form_csrf,
    form_csrf_token,
)
from agentplatform.api.webhooks import Spool, build_router, replay_spool
from agentplatform.core.config.registry import ConfigError
from agentplatform.core.db.pool import close_pool, get_pool, platform_tx, tenant_tx
from agentplatform.core.handoff.alerts import apply_seller_action
from agentplatform.core.handoff.notifiers import parse_whatsapp_button, verify_action
from agentplatform.core.security import redaction
from agentplatform.core.tools.lead_form import load_form, submit_form
from agentplatform.runtime import get_runtime
from agentplatform.settings import get_settings

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
CONSENT = "Autorizo o consultor a usar estes dados para me atender sobre esta proposta."


SPOOL_REPLAY_EVERY_S = 30
QUARANTINE_MAX_AGE_S = 30 * 86400


async def _spool_replayer(app: FastAPI) -> None:
    """Automatic replay after a database outage: every SPOOL_REPLAY_EVERY_S, if anything is spooled, try to persist it."""
    while True:
        await asyncio.sleep(SPOOL_REPLAY_EVERY_S)
        spool, adapters, hook = app.state.spool, app.state.adapters, app.state.on_persisted
        spool.purge_quarantine(QUARANTINE_MAX_AGE_S)
        if spool.size():
            try:
                n = await replay_spool(spool, adapters, hook)
                log.info("spool replay: %s event(s) persisted", n)
            except Exception:
                log.exception("spool replay attempt failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    redaction.install()
    await get_pool()
    from agentplatform.core.ops import fx
    if get_settings().fx_source == "ptax":
        asyncio.create_task(fx.refresh())          # warm the USD/BRL display rate (console)
    task = asyncio.create_task(_spool_replayer(app))
    yield
    task.cancel()
    await close_pool()


def create_app(on_persisted=None) -> FastAPI:
    from agentplatform.core.ops.tracing import init_tracing
    init_tracing("api")
    s = get_settings()
    rt = get_runtime()
    spool = Spool(s.spool_dir)
    app = FastAPI(title="Agent Platform", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(ConsoleSessionMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestSpanMiddleware)
    limiter = PublicLimiter()

    @app.exception_handler(ConfigError)
    async def no_configuration(request: Request, exc: ConfigError):
        """A tenant with no version at all: say what to do, instead of a server error, on any screen that needs one."""
        log.warning("console without configuration: %s", exc)
        return console._page(request, "no_config.html", {"user": read_session(request.cookies.get(COOKIE))}, 409)

    action_secret = (s.console_session_secret.get_secret_value() if s.console_session_secret else "dev").encode()

    if on_persisted is None:
        try:
            from agentplatform.jobs.app import defer_ingress
            on_persisted = defer_ingress
        except Exception:      # queue not configured: the periodic sweeper picks events up
            on_persisted = None
    app.state.spool, app.state.adapters, app.state.on_persisted = spool, rt.adapters, on_persisted

    @app.post("/webhooks/whatsapp-notifier")       # before the router: its /webhooks/{channel} would answer 404 for this path
    async def whatsapp_buttons(request: Request):
        body = await request.body()
        secret = s.whatsapp_app_secret.get_secret_value().encode() if s.whatsapp_app_secret else None
        sig = request.headers.get("x-hub-signature-256", "")
        if not secret or not hmac.compare_digest(sig, "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()):
            raise HTTPException(401)
        applied = 0
        for payload in parse_whatsapp_button(body):
            v = verify_action(action_secret, payload)
            if not v:
                continue
            action, hid = v
            async with platform_tx() as conn:
                h = await (await conn.execute("select tenant_id, conversation_id from handoffs where id=%s", (hid,))).fetchone()
            if h:
                async with tenant_tx(h["tenant_id"]) as conn:
                    result = await apply_seller_action(conn, h["tenant_id"], action, hid, None)
                if result == "BOT":
                    await console.answer_pending(h["tenant_id"], h["conversation_id"])
                applied += 1
        return {"applied": applied}

    app.include_router(build_router(rt.adapters, spool, on_persisted))
    app.include_router(console.router)
    app.include_router(console_setup.router)
    app.include_router(legal.build_router(limiter))

    @app.get("/health")
    async def health():
        try:
            async with platform_tx() as conn:
                await conn.execute("select 1")
            return {"ok": True, "spool_bytes": spool.size()}
        except Exception:
            return JSONResponse({"ok": False, "spool_bytes": spool.size()}, status_code=503)

    @app.get("/health/deep")
    async def health_deep():
        """For an outside uptime check. /health decides whether the proxy keeps sending webhooks here, so it must not depend on the
        workers; this one also says whether anyone is processing what arrives."""
        try:
            async with platform_tx() as conn:
                r = await (await conn.execute(
                    "select count(*) as n from procrastinate_workers where last_heartbeat > now() - interval '3 minutes'")).fetchone()
        except Exception:
            return JSONResponse({"db": False, "workers": False}, status_code=503)
        return JSONResponse({"db": True, "workers": r["n"] > 0}, status_code=200 if r["n"] else 503)

    @app.get("/metrics")
    async def metrics(request: Request):
        """Prometheus scrape (bearer token). Values come from the database, so any replica answers the same."""
        from fastapi.responses import Response
        from prometheus_client import CONTENT_TYPE_LATEST

        from agentplatform.core.ops.observability import collect_metrics, disk_used_ratio
        expected = s.metrics_token.get_secret_value() if s.metrics_token else ""
        got = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not expected or not hmac.compare_digest(got, expected):
            raise HTTPException(404)
        async with platform_tx() as conn:
            body = await collect_metrics(conn, {"agent_spool_bytes": float(spool.size()), "agent_disk_used_ratio": disk_used_ratio()})
        return Response(body, media_type=CONTENT_TYPE_LATEST)

    @app.get("/actions/{payload}", response_class=HTMLResponse)
    async def seller_action_page(payload: str):
        """Link from the seller e-mail. GET never changes state (mail scanners prefetch links): it shows a confirm button."""
        if not verify_action(action_secret, payload):
            raise HTTPException(404)
        label = "Assumi o atendimento" if payload.startswith("took:") else "Devolver ao assistente"
        return HTMLResponse(
            '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1"><title>Confirmar</title></head><body style="font-family:'
            f'system-ui;max-width:420px;margin:40px auto;padding:0 16px"><form method="post"><button style="padding:12px 18px">'
            f'{label}</button></form></body></html>')

    @app.post("/actions/{payload}", response_class=HTMLResponse)
    async def seller_action(payload: str):
        v = verify_action(action_secret, payload)
        if not v:
            raise HTTPException(404)
        action, hid = v
        async with platform_tx() as conn:
            h = await (await conn.execute("select tenant_id, conversation_id from handoffs where id=%s", (hid,))).fetchone()
        if not h:
            raise HTTPException(404)
        async with tenant_tx(h["tenant_id"]) as conn:
            result = await apply_seller_action(conn, h["tenant_id"], action, hid, None)
        if result == "BOT":
            await console.answer_pending(h["tenant_id"], h["conversation_id"])
        msg = "A conversa está com você." if result == "HUMAN" else "A conversa voltou para o assistente."
        return HTMLResponse(f'<!doctype html><html lang="pt-BR"><meta charset="utf-8"><body style="font-family:system-ui;'
                            f'max-width:420px;margin:40px auto;padding:0 16px"><p>{msg}</p></body></html>')

    @app.post("/admin/replay-spool")
    async def replay(request: Request):
        console_user = console.require(request, "platform_admin")
        n = await replay_spool(spool, rt.adapters, on_persisted)
        return {"replayed": n, "by": console_user["id"]}

    @app.post("/webhooks/connect/{tenant_slug}")
    async def connect_webhook(tenant_slug: str, request: Request):
        """Connect (Chatwoot) as human inbox: agent messages / status changes move conversation ownership."""
        from agentplatform.core.channels.connect.chatwoot import parse_connect_webhook, verify_connect_webhook
        from agentplatform.core.orchestration.conversations import transition_ownership
        body = await request.body()
        secret = (s.connect_webhook_secret.get_secret_value() if s.connect_webhook_secret else "").encode()
        if not secret or not verify_connect_webhook(secret, request.headers, body):
            raise HTTPException(401)
        async with platform_tx() as conn:
            t = await (await conn.execute("select id from tenants where slug=%s", (tenant_slug,))).fetchone()
        if not t:
            raise HTTPException(404)
        applied = 0
        for sig in parse_connect_webhook(body):
            if not sig.contact_identifier:
                continue
            async with tenant_tx(t["id"]) as conn:
                conv = await (await conn.execute(
                    """select c.id, c.ownership from conversations c join contact_identities ci on ci.contact_id=c.contact_id
                       where ci.external_user_id=%s order by c.last_inbound_at desc nulls last limit 1""",
                    (sig.contact_identifier,))).fetchone()
                if not conv:
                    continue
                target = {"human_message": "HUMAN", "status_open": "HUMAN", "status_pending": "BOT",
                          "status_resolved": "CLOSED"}[sig.kind]
                if conv["ownership"] != target:
                    await transition_ownership(conn, t["id"], conv["id"], target, cause=f"connect:{sig.kind}")
                    applied += 1
        return {"applied": applied}

    @app.post("/webchat/{account}/messages")
    async def webchat_in(account: str, request: Request):
        adapter = rt.adapters.get("webchat")
        if adapter is None:
            raise HTTPException(404)
        body = await request.body()
        if not adapter.verify_signature(request.headers, body):
            raise HTTPException(401)
        from agentplatform.core.orchestration.ingress import persist_events
        created = await persist_events(adapter.parse(body))
        if on_persisted and created:
            await on_persisted(created)
        return {"accepted": len(created)}

    @app.get("/webchat/{account}/messages")
    async def webchat_out(account: str, session_id: str, token: str, after: str | None = None):
        adapter = rt.adapters.get("webchat")
        if adapter is None or not hmac.compare_digest(token, adapter.session_token(account, session_id)):
            raise HTTPException(401)
        from agentplatform.core.orchestration.ingress import resolve_account
        acc = await resolve_account("webchat", account)
        if acc is None:
            raise HTTPException(404)
        async with tenant_tx(acc["tenant_id"]) as conn:
            rows = await (await conn.execute(
                """select m.id, m.text_masked, m.created_at from messages m join conversations c on c.id = m.conversation_id
                   join contact_identities ci on ci.contact_id = c.contact_id and ci.channel = 'webchat'
                   where ci.external_user_id = %s and m.direction in ('out_bot', 'out_human')
                     and (%s::timestamptz is null or m.created_at > %s::timestamptz) order by m.created_at""",
                (session_id, after, after))).fetchall()
        return {"messages": [{"id": str(r["id"]), "text": r["text_masked"], "at": r["created_at"].isoformat()} for r in rows]}

    @app.get("/f/{token}", response_class=HTMLResponse)
    async def form_page(request: Request, token: str):
        if not await limiter.allow(request):
            raise HTTPException(429)
        async with platform_tx() as conn:
            form = await load_form(conn, token)
        if not form:
            return templates.TemplateResponse(request, "form.html", {"expired": True}, status_code=410)
        tr = await rt.tenant(form["tenant_id"])
        return templates.TemplateResponse(request, "form.html", {
            "fields": tr.package.manifest.lead_form_fields, "labels": tr.package.manifest.lead_form_labels, "consent_text": CONSENT,
            "csrf": form_csrf_token(token)})

    @app.post("/f/{token}", response_class=HTMLResponse)
    async def form_submit(request: Request, token: str):
        if not await limiter.allow(request):
            raise HTTPException(429)
        data = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}   # no file uploads
        if not check_form_csrf(token, data.pop("csrf", None)):
            raise HTTPException(403, "invalid form token")
        if data.get("consent") != "1":
            raise HTTPException(400, "consent required")
        async with platform_tx() as conn:
            form = await load_form(conn, token)
        if not form:
            return templates.TemplateResponse(request, "form.html", {"expired": True}, status_code=410)
        tr = await rt.tenant(form["tenant_id"])
        async with tenant_tx(form["tenant_id"]) as conn:
            if not await submit_form(conn, form, data, tr.package.manifest.lead_form_fields, CONSENT):
                return templates.TemplateResponse(request, "form.html", {"done": True})
            conv = await (await conn.execute(
                "select id from conversations where contact_id=%s order by created_at limit 1", (form["contact_id"],))).fetchone()
            from agentplatform.core.handoff.service import build_summary, create_handoff
            from agentplatform.core.orchestration.conversations import utcnow
            lead = await (await conn.execute(
                "update leads set stage=%s where id=%s returning profile", (tr.package.manifest.seller_stage, form["lead_id"]))).fetchone()
            if conv:
                summary = build_summary(data.get("first_name"), tr.package.manifest.seller_stage, "hot_lead", lead["profile"] or {}, None)
                await create_handoff(conn, tr.config, tenant_id=form["tenant_id"], conversation_id=conv["id"],
                                     lead_id=form["lead_id"], reason="hot_lead", summary=summary, now=utcnow())
        return templates.TemplateResponse(request, "form.html", {"done": True})

    return app


app = create_app()
