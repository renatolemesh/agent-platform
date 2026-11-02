"""Console (plan 03 §3.11 / F2-CON): conversations (state, summary, ownership, take/return), turn trace, curation queue,
documents, kill switches, budget and top-ups, dashboards, config (two-person approval, canary, rollback), LGPD subject
rights, lead imports, campaigns, channel reconnection (Facebook Login), users and TOTP 2FA.
Server-rendered Jinja + HTMX; RBAC; CSRF on every POST; every human action audited."""
from __future__ import annotations

import base64
import csv
import difflib
import hashlib
import io
import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import segno
import yaml
from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import ValidationError

from agentplatform.api import ui
from agentplatform.api.auth import (
    COOKIE,
    PENDING_COOKIE,
    check_password,
    csrf_input,
    csrf_token,
    hash_password,
    make_pending,
    make_session,
    new_totp_secret,
    open_totp,
    read_pending,
    require,
    require_post,
    seal_totp,
    totp_check,
    totp_required,
    totp_uri,
)
from agentplatform.core.comments.origin import load_origin
from agentplatform.core.config.registry import ConfigError, normalized, registry, resolve_with_package
from agentplatform.core.config.schema import BudgetConfig, CommentsConfig, PlatformConfig, ReviewConfig
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.handoff.alerts import return_to_bot
from agentplatform.core.ops import review as review_service
from agentplatform.core.ops.maintenance import check_subscription
from agentplatform.core.ops.turn_explain import explain_turns
from agentplatform.core.orchestration.conversations import transition_ownership
from agentplatform.core.orchestration.dm_scope import (
    SCOPES,
    flag_of,
    load_scope,
    restart_snapshot,
    set_flag,
    snapshot_contacts,
)
from agentplatform.core.security.keys import crypto
from agentplatform.settings import get_settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["csrf_input"] = csrf_input
templates.env.globals["csrf_token"] = csrf_token
ui.install(templates.env)
router = APIRouter(prefix="/console")

IMPORT_FIELDS = ("phone", "email", "name", "legal_basis", "source", "purpose", "consent_evidence", "tags")
ERASE_CONFIRM = "ELIMINAR"


async def _audit(conn, tenant_id, actor, action, target, after=None):
    await conn.execute(
        "insert into audit_log (tenant_id, actor, actor_kind, action, target, after) values (%s,%s,'user',%s,%s,%s)",
        (tenant_id, actor, action, target, json.dumps(after, default=str) if after else None))


log = logging.getLogger(__name__)
MIN_PASSWORD_LEN = 12


async def answer_pending(tid: UUID, cid: UUID) -> None:
    """Best effort: if the queue does not take it now, the periodic sweep does."""
    from agentplatform.jobs.app import defer_jobs
    try:
        await defer_jobs([("close_batch", {"tenant_id": str(tid), "conversation_id": str(cid)}, {"queueing_lock": f"cb:{cid}"})])
    except Exception:
        log.warning("could not queue the batch check for a conversation given back; the sweep will pick it up")


def _tid(user: dict) -> UUID:
    """Tenant of the screen: the user's own, or the tenant a platform admin chose in /console/tenants."""
    tid = user.get("tenant_id") or (user.get("acting_tenant") if user.get("role") == "platform_admin" else None)
    if not tid:
        raise HTTPException(303, headers={"Location": "/console/tenants"})
    return UUID(tid)


def _page(request: Request, name: str, ctx: dict, status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _set_session(resp, user_row: dict, *, must_enroll: bool = False) -> None:
    secure = get_settings().public_base_url.startswith("https://")
    resp.set_cookie(COOKIE, make_session({
        "id": str(user_row["id"]), "tenant_id": str(user_row["tenant_id"]) if user_row["tenant_id"] else None,
        "role": user_row["role"], "name": user_row["display_name"], "must_enroll": must_enroll}),
        httponly=True, samesite="strict", secure=secure)
    resp.delete_cookie(PENDING_COOKIE)


async def _login_failed(user_id: UUID) -> None:
    s = get_settings()
    async with platform_tx() as conn:
        await conn.execute(
            """update tenant_users set failed_logins = failed_logins + 1,
                      locked_until = case when failed_logins + 1 >= %s then now() + make_interval(mins => %s) else locked_until end
               where id = %s""", (s.console_login_max_failures, s.console_login_lock_minutes, user_id))


# ======================= login, 2FA, logout =======================
@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return _page(request, "login.html", {"error": None})


@router.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    h = crypto().lookup_hash(email, kind="email")
    async with platform_tx() as conn:
        u = await (await conn.execute(
            """select id, tenant_id, role, display_name, password_hash, totp_enabled_at, (locked_until > now()) as locked
               from tenant_users where email_hash=%s and is_active""", (h,))).fetchone()
    if u and u["locked"]:
        return _page(request, "login.html", {"error": "Conta bloqueada temporariamente. Tente mais tarde."}, 401)
    if not u or not check_password(password, u["password_hash"]):
        if u:
            await _login_failed(u["id"])
        return _page(request, "login.html", {"error": "Credenciais inválidas"}, 401)
    if u["totp_enabled_at"]:
        resp = RedirectResponse("/console/login/totp", status_code=303)
        resp.set_cookie(PENDING_COOKIE, make_pending({"id": str(u["id"])}), httponly=True, samesite="strict",
                        secure=get_settings().public_base_url.startswith("https://"), max_age=300)
        return resp
    async with platform_tx() as conn:
        await conn.execute("update tenant_users set failed_logins = 0, locked_until = null where id = %s", (u["id"],))
    resp = RedirectResponse("/console/", status_code=303)
    _set_session(resp, u, must_enroll=totp_required(u["role"]))
    return resp


@router.get("/login/totp", response_class=HTMLResponse)
async def login_totp_form(request: Request):
    if not read_pending(request.cookies.get(PENDING_COOKIE)):
        return RedirectResponse("/console/login", status_code=303)
    return _page(request, "login_totp.html", {"error": None})


@router.post("/login/totp")
async def login_totp(request: Request, code: str = Form(...)):
    pending = read_pending(request.cookies.get(PENDING_COOKIE))
    if not pending:
        return RedirectResponse("/console/login", status_code=303)
    uid = UUID(pending["id"])
    async with platform_tx() as conn:
        u = await (await conn.execute(
            """select id, tenant_id, role, display_name, totp_secret_enc, totp_last_step, (locked_until > now()) as locked
               from tenant_users where id=%s and is_active and totp_enabled_at is not null""", (uid,))).fetchone()
        secret = await open_totp(conn, u["tenant_id"], u["id"], u["totp_secret_enc"]) if u and not u["locked"] else None
        step = totp_check(secret, code, u["totp_last_step"]) if secret else None
        if step is not None:
            await conn.execute("update tenant_users set totp_last_step=%s, failed_logins=0, locked_until=null where id=%s",
                               (step, uid))
    if step is None:
        if u:
            await _login_failed(uid)
        return _page(request, "login_totp.html", {"error": "Código inválido"}, 401)
    resp = RedirectResponse("/console/", status_code=303)
    _set_session(resp, u)
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/console/login", status_code=303)
    resp.delete_cookie(COOKIE)
    resp.delete_cookie(PENDING_COOKIE)
    return resp


@router.get("/password", response_class=HTMLResponse)
async def password_page(request: Request):
    return _page(request, "password.html", {"user": require(request), "min_len": MIN_PASSWORD_LEN})


@router.post("/password", response_class=HTMLResponse)
async def password_change(request: Request, current: str = Form(...), new: str = Form(...)):
    user = await require_post(request)
    uid = UUID(user["id"])
    async with platform_tx() as conn:
        u = await (await conn.execute("select tenant_id, password_hash from tenant_users where id=%s", (uid,))).fetchone()
    error = None
    if not check_password(current, u["password_hash"]):
        error = "A senha atual não confere."
    elif len(new) < MIN_PASSWORD_LEN:
        error = f"A nova senha precisa ter pelo menos {MIN_PASSWORD_LEN} caracteres."
    if error:
        return _page(request, "password.html", {"user": user, "min_len": MIN_PASSWORD_LEN, "error": error}, 400)
    async with platform_tx() as conn:
        await conn.execute("update tenant_users set password_hash=%s where id=%s", (hash_password(new), uid))
        await _audit(conn, u["tenant_id"], uid, "user.password_change", str(uid), {})
    return _page(request, "password.html", {"user": user, "min_len": MIN_PASSWORD_LEN, "saved": True})


@router.get("/totp", response_class=HTMLResponse)
async def totp_page(request: Request):
    """Enrollment: a fresh secret is stored encrypted but only enabled after a valid code."""
    user = require(request)
    uid = UUID(user["id"])
    async with platform_tx() as conn:
        u = await (await conn.execute("select tenant_id, totp_enabled_at from tenant_users where id=%s", (uid,))).fetchone()
        if u["totp_enabled_at"]:
            return _page(request, "totp.html", {"user": user, "enabled": True})
        secret = new_totp_secret()
        await conn.execute("update tenant_users set totp_secret_enc=%s, totp_last_step=null where id=%s",
                           (await seal_totp(conn, u["tenant_id"], uid, secret), uid))
    uri = totp_uri(secret, user["name"])
    svg = segno.make(uri, error="m").svg_inline(scale=4)
    return _page(request, "totp.html", {"user": user, "enabled": False, "secret": secret, "qr": Markup(svg),
                                        "required": user.get("must_enroll"), "error": None})


@router.post("/totp/verify")
async def totp_verify(request: Request, code: str = Form(...)):
    user = await require_post(request)
    uid = UUID(user["id"])
    async with platform_tx() as conn:
        u = await (await conn.execute(
            "select id, tenant_id, role, display_name, totp_secret_enc, totp_last_step from tenant_users where id=%s",
            (uid,))).fetchone()
        secret = await open_totp(conn, u["tenant_id"], uid, u["totp_secret_enc"])
        step = totp_check(secret, code, u["totp_last_step"]) if secret else None
        if step is None:
            return _page(request, "totp.html", {"user": user, "enabled": False, "error": "Código inválido. Gere um novo QR."}, 400)
        await conn.execute("update tenant_users set totp_enabled_at=now(), totp_last_step=%s where id=%s", (step, uid))
        await _audit(conn, u["tenant_id"], uid, "user.totp_enabled", str(uid))
    resp = RedirectResponse("/console/", status_code=303)
    _set_session(resp, u)
    return resp


@router.get("/tenants", response_class=HTMLResponse)
async def tenants_page(request: Request):
    user = require(request, "platform_admin")
    async with platform_tx() as conn:
        rows = await (await conn.execute(
            "select id, slug, display_name, status from tenants order by status, slug")).fetchall()
    return _page(request, "tenants.html", {"user": user, "rows": rows})


@router.post("/tenants/select")
async def tenants_select(request: Request, tenant_id: str = Form(...)):
    user = await require_post(request, "platform_admin")
    if user["role"] != "platform_admin":
        raise HTTPException(403)
    async with platform_tx() as conn:
        t = await (await conn.execute("select id from tenants where id = %s", (UUID(tenant_id),))).fetchone()
        if not t:
            raise HTTPException(404)
        await _audit(conn, t["id"], UUID(user["id"]), "console.act_as_tenant", str(t["id"]))
    resp = RedirectResponse("/console/", status_code=303)
    secure = get_settings().public_base_url.startswith("https://")
    resp.set_cookie(COOKIE, make_session({**{k: v for k, v in user.items() if k not in ("exp", "pur")},
                                          "acting_tenant": str(t["id"])}), httponly=True, samesite="strict", secure=secure)
    return resp


@router.get("/users", response_class=HTMLResponse)
async def users(request: Request):
    user = require(request, "tenant_owner")
    async with platform_tx() as conn:
        if user["role"] == "platform_admin":
            rows = await (await conn.execute(
                "select u.id, u.display_name, u.role, u.totp_enabled_at, u.locked_until, t.slug from tenant_users u "
                "left join tenants t on t.id = u.tenant_id where u.is_active order by t.slug nulls first, u.display_name")).fetchall()
        else:
            rows = await (await conn.execute(
                "select u.id, u.display_name, u.role, u.totp_enabled_at, u.locked_until, null as slug from tenant_users u "
                "where u.tenant_id = %s and u.is_active order by u.display_name", (_tid(user),))).fetchall()
    if user["role"] == "platform_admin" and not user.get("acting_tenant"):
        return _page(request, "users.html", {"user": user, "rows": rows})
    from agentplatform.api.console_setup import users_context
    return _page(request, "users.html", {"user": user, **await users_context(user)})


@router.post("/users/{uid}/totp-reset")
async def totp_reset(request: Request, uid: UUID):
    """Recovery of a lost authenticator: platform admin only; the user re-enrolls on next login if 2FA is required."""
    user = await require_post(request, "platform_admin")
    if user["role"] != "platform_admin":
        raise HTTPException(403)
    async with platform_tx() as conn:
        t = await (await conn.execute(
            "update tenant_users set totp_secret_enc=null, totp_enabled_at=null, totp_last_step=null, failed_logins=0, "
            "locked_until=null where id=%s returning tenant_id", (uid,))).fetchone()
        if t:
            await _audit(conn, t["tenant_id"], UUID(user["id"]), "user.totp_reset", str(uid))
    return RedirectResponse("/console/users", status_code=303)


# ======================= conversations =======================
async def _labels(tid: UUID) -> dict:
    """Display labels from the tenant's segment package (stage and slot names); empty if no package is loadable."""
    from agentplatform.runtime import get_runtime
    try:
        m = (await get_runtime().tenant_for_setup(tid)).package.manifest
    except Exception:
        return {"stages": {}, "slots": {}}
    return {"stages": {st.name: st.label for st in m.stages if st.label}, "slots": {sl.name: sl for sl in m.slots}}


@router.get("/", response_class=HTMLResponse)
async def conversations(request: Request):
    user = require(request)
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        rows = await (await conn.execute(
            """select c.id, c.ownership, c.handoff_pending, c.last_inbound_at, c.last_reply_at, c.summary, c.entrypoint, l.stage,
                      ci.username, ci.channel, ci.external_user_id, ct.first_name,
                      (select count(*) from comment_actions a where a.conversation_id = c.id and a.attention and a.resolved_at is null) as pending_comments,
                      (c.last_inbound_at > coalesce(c.last_reply_at, 'epoch') and c.last_inbound_at < now() - interval '1 hour') as unanswered
               from conversations c join leads l on l.contact_id = c.contact_id
               join contacts ct on ct.id = c.contact_id join contact_identities ci on ci.contact_id = c.contact_id
               where ci.channel <> 'simulator' and ci.external_user_id not like 'LAB-%%'
                 and (c.entrypoint is distinct from 'comment_spam' or c.last_inbound_at is not null)
               order by coalesce(c.last_inbound_at, c.created_at) desc limit 100""")).fetchall()
        unnotified = (await (await conn.execute(
            """select count(*) as n from handoffs h join conversations c on c.id = h.conversation_id
               join contact_identities ci on ci.contact_id = c.contact_id
               where h.taken_at is null and ci.channel <> 'simulator' and ci.external_user_id not like 'LAB-%%'
                 and (h.notified_via = 'log' or (h.notified_at is null and h.notify_at < now() - interval '2 minutes'))""")).fetchone())["n"]
        budget = await (await conn.execute(
            "select scope, cap_usd, spent_usd, reserved_usd from budget_balances where tenant_id is null and scope like 'key:%%' order by scope")).fetchall()
        paused = await (await conn.execute(
            """select scope, scope_ref, reason, changed_at from kill_switches where is_on
               and (scope = 'global' or (scope = 'tenant' and scope_ref = %s) or (tenant_id = %s and scope in ('feature', 'channel')))
               order by changed_at desc""", (str(tid), tid))).fetchall()
    return _page(request, "conversations.html", {"user": user, "rows": rows, "budget": budget, "paused": paused,
                                                 "unnotified": unnotified, "labels": await _labels(tid)})


@router.get("/conversations/{cid}", response_class=HTMLResponse)
async def conversation(request: Request, cid: UUID):
    user = require(request)
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        conv = await (await conn.execute(
            """select c.*, l.stage, l.profile, l.opted_out_at, ct.first_name, ci.username, ci.channel, ci.external_user_id
               from conversations c join leads l on l.contact_id=c.contact_id join contacts ct on ct.id = c.contact_id
               left join contact_identities ci on ci.contact_id = c.contact_id where c.id=%s limit 1""",
            (cid,))).fetchone()
        if not conv:
            raise HTTPException(404)
        msgs = await (await conn.execute(
            "select direction, kind, text_masked, provider_ts from messages where conversation_id=%s order by provider_ts", (cid,))).fetchall()
        contact_flag = await flag_of(conn, conv["channel_account_id"], conv["external_user_id"]) if conv["external_user_id"] else None
        origin = await load_origin(conn, cid, (await (await conn.execute(
            "select id from leads where contact_id=%s", (conv["contact_id"],))).fetchone())["id"], await _comment_limits(tid))
        turns = await (await conn.execute(
            """select t.id, t.status, t.stage_before, t.stage_after, t.llm_calls, t.cost_usd, t.trace, t.started_at, v.version as config_version
               from turns t left join config_versions v on v.id = t.config_version_id where t.conversation_id=%s order by t.started_at desc limit 20""",
            (cid,))).fetchall()
        events = await (await conn.execute(
            "select from_state, to_state, cause, at from ownership_events where conversation_id=%s order by at desc limit 20", (cid,))).fetchall()
        reviews = await review_service.for_conversation(conn, cid)
        explained = await explain_turns(conn, turns)
    return _page(request, "conversation.html", {"user": user, "c": conv, "msgs": msgs, "turns": turns, "events": events,
                                                "origin": origin, "contact_flag": contact_flag, "labels": await _labels(tid),
                                                "reviews": reviews, "review_cfg": await _review_config(tid), "explained": explained})


async def _review_config(tid: UUID) -> ReviewConfig:
    try:
        return (await _comment_config(tid)).operations.review
    except ConfigError:
        return ReviewConfig()


@router.post("/conversations/{cid}/review")
async def review_conversation(request: Request, cid: UUID, verdict: str = Form(...), category: str = Form(""), note: str = Form(""), next: str = Form("")):
    user = await require_post(request, "tenant_owner", "seller", "reviewer")
    tid = _tid(user)
    cfg = await _review_config(tid)
    async with tenant_tx(tid) as conn:
        if not await (await conn.execute("select 1 from conversations where id=%s", (cid,))).fetchone():
            raise HTTPException(404)
        try:
            await review_service.record(conn, tid, cid, UUID(user["id"]), verdict, category, note, cfg)
        except ValueError:
            raise HTTPException(400, "invalid") from None
        await _audit(conn, tid, UUID(user["id"]), "conversation.review", str(cid), {"verdict": verdict})
    back = next if next.startswith("/console/") and "//" not in next else f"/console/conversations/{cid}"
    return RedirectResponse(back, status_code=303)


@router.get("/reviews", response_class=HTMLResponse)
async def reviews_page(request: Request, day: str = ""):
    user = require(request, "tenant_owner", "seller", "reviewer")
    tid = _tid(user)
    config = await _comment_config(tid)
    timezone = config.sending.quiet_hours.timezone
    today = datetime.now(ZoneInfo(timezone)).date()
    try:
        chosen = date.fromisoformat(day) if day else today
    except ValueError:
        raise HTTPException(400, "invalid day") from None
    cfg = config.operations.review
    async with tenant_tx(tid) as conn:
        sample = await review_service.day_sample(conn, chosen, timezone, cfg)
        found = await review_service.findings(conn, cfg)
    return _page(request, "reviews.html", {"user": user, "cfg": cfg, "sample": sample, "found": found, "day": chosen, "today": today,
                                           "prev_day": chosen - timedelta(days=1), "next_day": chosen + timedelta(days=1)})


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    user = require(request)
    tid = _tid(user)
    try:
        cfg = await _comment_config(tid)
        handoff, quiet = cfg.handoff, cfg.sending.quiet_hours
    except ConfigError:
        handoff, quiet = None, None
    return _page(request, "help.html", {"user": user, "handoff": handoff, "quiet": quiet})


@router.post("/conversations/{cid}/ownership")
async def set_ownership(request: Request, cid: UUID, to: str = Form(...), next: str = Form("")):
    user = await require_post(request, "tenant_owner", "seller")
    if to not in ("HUMAN", "BOT", "PAUSED", "CLOSED"):
        return HTMLResponse("invalid", status_code=400)
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        if to == "BOT":
            await return_to_bot(conn, tid, cid, cause="console:bot", actor=UUID(user["id"]))
        else:
            await transition_ownership(conn, tid, cid, to, cause=f"console:{to.lower()}", actor=UUID(user["id"]))
        if to == "HUMAN":
            await conn.execute("update handoffs set taken_at = coalesce(taken_at, now()), taken_by=%s where conversation_id=%s",
                               (UUID(user["id"]), cid))
        await _audit(conn, tid, UUID(user["id"]), "conversation.ownership", str(cid), {"to": to})
    if to == "BOT":
        await answer_pending(tid, cid)
    back = next if next.startswith("/console/") and "//" not in next else f"/console/conversations/{cid}"
    return RedirectResponse(back, status_code=303)


@router.post("/conversations/{cid}/won")
async def mark_won(request: Request, cid: UUID):
    """Sale happens in the official system; only the seller marks the won stage."""
    user = await require_post(request, "tenant_owner", "seller")
    tid = _tid(user)
    from agentplatform.runtime import get_runtime
    won = (await get_runtime().tenant_for_setup(tid)).package.manifest.won_stage
    async with tenant_tx(tid) as conn:
        lead = await (await conn.execute(
            "select l.id, l.stage from leads l join conversations c on c.contact_id=l.contact_id where c.id=%s", (cid,))).fetchone()
        await conn.execute("update leads set stage=%s, stage_changed_at=now() where id=%s", (won, lead["id"]))
        await conn.execute(
            "insert into lead_stage_history (tenant_id, lead_id, from_stage, to_stage, proposed_by, applied) values (%s,%s,%s,%s,'seller',true)",
            (tid, lead["id"], lead["stage"], won))
        await _audit(conn, tid, UUID(user["id"]), "lead.won", str(lead["id"]))
    return RedirectResponse(f"/console/conversations/{cid}", status_code=303)


# ======================= knowledge base (F2-CON-03, ADR-021) =======================
KB_ROLES = ("tenant_owner", "reviewer")


async def _kb_runtime(tid: UUID):
    from agentplatform.runtime import get_runtime
    return await get_runtime().tenant_for_setup(tid)


def _can_index() -> bool:
    """The direct key has no zero data retention: client material never goes through it, so approving needs the production provider."""
    return get_settings().llm_provider == "openrouter"


async def _queue(jobs: list) -> None:
    """Best effort: if the queue does not take it now, the periodic sweep does."""
    from agentplatform.jobs.app import defer_jobs
    try:
        await defer_jobs(jobs)
    except Exception:
        log.warning("could not queue %d knowledge job(s); the sweep will pick them up", len(jobs))


async def _documents_page(request: Request, user: dict, tid: UUID, *, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    tr = await _kb_runtime(tid)
    async with tenant_tx(tid) as conn:
        rows = await (await conn.execute(
            """select d.id, d.tenant_id, d.file_name, d.status, d.visibility, d.audience, d.pages, d.version_label, d.effective_to, d.processing,
                      d.processing_error, s.title,
                      (select count(*) from kb_chunks c where c.document_id = d.id and c.excluded_at is null
                         and c.index_id = (select id from kb_indexes where name = %(idx)s)) as chunks,
                      (select count(*) from kb_conflicts k join kb_documents o on o.id = k.other_document_id join kb_documents m on m.id = k.document_id
                        where (k.document_id = d.id or k.other_document_id = d.id) and k.status = 'open' and o.status = 'approved' and m.status = 'approved') as conflicts
               from kb_documents d join kb_sources s on s.id = d.source_id
               order by (d.status in ('in_review', 'blocked_pii') or d.processing <> 'done') desc, (d.status = 'approved') desc, s.title""",
            {"idx": tr.config.rag.active_index_id})).fetchall()
    async with tenant_tx(tid) as conn:
        reindex = await (await conn.execute(
            """select id, model, status, total, done, error, draft_version_id from kb_reindex_runs where tenant_id=%s
               and (status in ('running','failed') or (status='ready' and finished_at > now() - interval '7 days')) order by created_at desc limit 1""", (tid,))).fetchone()
    return _page(request, "documents.html", {"user": user, "rows": rows, "error": error or request.query_params.get("erro"), "can_index": _can_index(), "tid": str(tid),
                                             "reindex": reindex, "embedding": tr.config.models.embedding, "embedding_models": ui.embedding_choices(tr.config.models.embedding.model),
                                             "max_mb": tr.config.operations.knowledge.max_upload_bytes // 1_000_000,
                                             "processing": any(r["processing"] in ("queued", "indexing") for r in rows) or bool(reindex and reindex["status"] == "running")}, status_code)


@router.get("/documents", response_class=HTMLResponse)
async def documents(request: Request):
    user = require(request, *KB_ROLES)
    return await _documents_page(request, user, _tid(user))


@router.post("/documents/upload", response_class=HTMLResponse)
async def documents_upload(request: Request, file: UploadFile | None = None, title: str = Form(""), mask_identifiers: str = Form("")):
    from agentplatform.core.knowledge import admin as K
    user = await require_post(request, *KB_ROLES)
    tid = _tid(user)
    tr = await _kb_runtime(tid)
    cfg = tr.config.operations.knowledge
    if file is None or not file.filename:
        return await _documents_page(request, user, tid, error=ui.kb_refusal("not_pdf"), status_code=400)
    data = await file.read(cfg.max_upload_bytes + 1)
    try:
        async with tenant_tx(tid) as conn:
            did = await K.accept_upload(conn, tid, UUID(user["id"]), file.filename, title, data, cfg, [*tr.package.manifest.never_import_name_hashes, *tr.config.operations.knowledge.never_import_name_hashes],
                                        tr.package.manifest.name, mask_identifiers=mask_identifiers == "1")
            await _audit(conn, tid, UUID(user["id"]), "kb.document_upload", str(did), {"bytes": len(data), "mask_identifiers": mask_identifiers == "1"})
    except K.Refused as e:
        return await _documents_page(request, user, tid, error=ui.kb_refusal(e.reason, e.detail), status_code=400)
    await _queue([K.extract_job(tid, did)])
    return RedirectResponse(f"/console/documents/{did}", status_code=303)


async def _document_page(request: Request, user: dict, tid: UUID, doc_id: UUID, *, error: str | None = None, only_alerts: bool = False,
                         status_code: int = 200) -> HTMLResponse:
    from agentplatform.core.knowledge.admin import chunk_flags
    tr = await _kb_runtime(tid)
    async with tenant_tx(tid) as conn:
        doc = await (await conn.execute(
            """select d.*, s.title, u.display_name as approver from kb_documents d join kb_sources s on s.id = d.source_id
               left join tenant_users u on u.id = d.approved_by where d.id=%s""", (doc_id,))).fetchone()
        if not doc:
            raise HTTPException(404)
        rows = await (await conn.execute(
            """select id, ordinal, page, heading_path, text, excluded_at from kb_chunks
               where document_id=%s and index_id = (select id from kb_indexes where name=%s) order by ordinal""",
            (doc_id, tr.config.rag.active_index_id))).fetchall()
        others = await (await conn.execute(
            """select d.id, s.title from kb_documents d join kb_sources s on s.id = d.source_id
               where d.tenant_id=%s and d.status='approved' and d.id<>%s order by s.title""", (tid, doc_id))).fetchall()
        conflicts = await (await conn.execute(
            """select k.id, k.similarity, a.text as text, a.page as page, b.text as other_text, b.page as other_page,
                      case when k.document_id=%(d)s then k.other_document_id else k.document_id end as other_id,
                      (select s.title from kb_documents o join kb_sources s on s.id=o.source_id
                        where o.id = case when k.document_id=%(d)s then k.other_document_id else k.document_id end) as other_title
               from kb_conflicts k join kb_chunks a on a.id=k.chunk_id join kb_chunks b on b.id=k.other_chunk_id
               join kb_documents x on x.id=k.document_id join kb_documents y on y.id=k.other_document_id
               where (k.document_id=%(d)s or k.other_document_id=%(d)s) and k.status='open' and x.status='approved' and y.status='approved'
               order by k.similarity desc limit 20""", {"d": doc_id})).fetchall()
    chunks = [{**c, "flags": chunk_flags(c["text"], tr.config)} for c in rows]
    return _page(request, "document.html", {
        "user": user, "doc": doc, "own": doc["tenant_id"] == tid, "others": others, "error": error, "conflicts": conflicts, "can_index": _can_index(), "only_alerts": only_alerts,
        "chunks": [c for c in chunks if c["flags"]] if only_alerts else chunks, "total": len(chunks), "alerts": sum(1 for c in chunks if c["flags"]),
        "excluded": sum(1 for c in chunks if c["excluded_at"]), "processing": doc["processing"] in ("queued", "indexing")}, status_code)


@router.get("/documents/{doc_id}", response_class=HTMLResponse)
async def document_page(request: Request, doc_id: UUID, f: str = ""):
    user = require(request, *KB_ROLES)
    return await _document_page(request, user, _tid(user), doc_id, only_alerts=f == "alert")


@router.post("/documents/{doc_id}/approve", response_class=HTMLResponse)
async def document_approve(request: Request, doc_id: UUID, title: str = Form(""), visibility: str = Form(""), audience: str = Form(""),
                           version_label: str = Form(""), effective_from: str = Form(""), effective_to: str = Form(""), supersedes: str = Form("")):
    from agentplatform.core.knowledge import admin as K
    user = await require_post(request, *KB_ROLES)
    tid = _tid(user)
    try:
        if not _can_index():
            raise K.Refused("direct_key")
        async with tenant_tx(tid) as conn:
            await K.request_approval(conn, tid, doc_id, UUID(user["id"]), K.Approval(title, visibility, audience, version_label, effective_from, effective_to, supersedes))
            await _audit(conn, tid, UUID(user["id"]), "kb.document_approve", str(doc_id), {"visibility": visibility, "audience": audience})
    except K.Refused as e:
        return await _document_page(request, user, tid, doc_id, error=ui.kb_refusal(e.reason, e.detail), status_code=400)
    await _queue([K.index_job(tid, doc_id)])
    return RedirectResponse(f"/console/documents/{doc_id}", status_code=303)


@router.post("/documents/{doc_id}/retire")
async def document_retire(request: Request, doc_id: UUID):
    from agentplatform.core.knowledge import admin as K
    user = await require_post(request, *KB_ROLES)
    tid = _tid(user)
    try:
        async with tenant_tx(tid) as conn:
            await K.retire_document(conn, tid, doc_id)
            await _audit(conn, tid, UUID(user["id"]), "kb.document_retire", str(doc_id))
    except K.Refused:
        raise HTTPException(404) from None
    return RedirectResponse(f"/console/documents/{doc_id}", status_code=303)


@router.post("/documents/{doc_id}/chunks/{chunk_id}")
async def document_chunk(request: Request, doc_id: UUID, chunk_id: UUID, excluded: str = Form("1"), f: str = Form("")):
    from agentplatform.core.knowledge import admin as K
    user = await require_post(request, *KB_ROLES)
    tid = _tid(user)
    try:
        async with tenant_tx(tid) as conn:
            needs_vector = await K.set_chunk_excluded(conn, tid, doc_id, chunk_id, UUID(user["id"]), excluded == "1")
            await _audit(conn, tid, UUID(user["id"]), "kb.chunk_excluded", str(chunk_id), {"excluded": excluded == "1", "document": str(doc_id)})
    except K.Refused:
        raise HTTPException(404) from None
    if needs_vector:
        await _queue([K.index_job(tid, doc_id)])
    return RedirectResponse(f"/console/documents/{doc_id}" + ("?f=alert" if f == "alert" else "") + f"#c{chunk_id}", status_code=303)


async def _faqs_page(request: Request, user: dict, tid: UUID, *, edit: UUID | None = None, form: dict | None = None, problems=(), error: str | None = None,
                     saved: str | None = None, status_code: int = 200) -> HTMLResponse:
    async with tenant_tx(tid) as conn:
        rows = await (await conn.execute(
            """select f.id, f.tenant_id, f.canonical_question, f.answer, f.status, f.valid_to, f.processing, f.processing_error, f.approved_at,
                      coalesce((select array_agg(v.question order by v.question) from faq_variants v
                                where v.faq_id = f.id and lower(v.question) <> lower(f.canonical_question)), '{}') as variants
               from faqs f order by (f.tenant_id is null), (f.status = 'draft') desc, (f.status = 'retired'), f.canonical_question""")).fetchall()
    if form is None:
        current = next((r for r in rows if r["id"] == edit and r["tenant_id"] == tid), None)
        form = ({"id": current["id"], "question": current["canonical_question"], "answer": current["answer"], "variants": current["variants"],
                 "valid_to": current["valid_to"].isoformat() if current["valid_to"] else ""}
                if current else {"id": None, "question": "", "answer": "", "variants": [], "valid_to": ""})
    return _page(request, "faqs.html", {"user": user, "rows": rows, "form": form, "problems": problems, "error": error, "saved": saved, "can_index": _can_index(), "tid": tid,
                                        "processing": any(r["processing"] == "indexing" for r in rows)}, status_code)


@router.get("/faqs", response_class=HTMLResponse)
async def faqs_page(request: Request, edit: UUID | None = None, saved: str | None = None):
    user = require(request, *KB_ROLES)
    return await _faqs_page(request, user, _tid(user), edit=edit, saved=saved)


@router.post("/faqs", response_class=HTMLResponse)
async def faqs_save(request: Request, faq_id: str = Form(""), question: str = Form(""), answer: str = Form(""), variants: str = Form(""),
                    valid_to: str = Form(""), action: str = Form("save")):
    from agentplatform.core.knowledge import admin as K
    user = await require_post(request, *KB_ROLES)
    tid = _tid(user)
    tr = await _kb_runtime(tid)
    others = [v.strip() for v in variants.splitlines() if v.strip()]
    fid = UUID(faq_id) if faq_id else None
    form = {"id": fid, "question": question, "answer": answer, "variants": others, "valid_to": valid_to}
    problems = K.validate_faq(tr.config, question, answer, others, tr.allowed_urls)
    if problems:
        return await _faqs_page(request, user, tid, form=form, problems=problems, status_code=400)
    approve = action == "approve"
    if approve and not _can_index():
        return await _faqs_page(request, user, tid, form=form, error=ui.kb_refusal("direct_key"), status_code=400)
    try:
        async with tenant_tx(tid) as conn:
            saved_id = await K.save_faq(conn, tid, UUID(user["id"]), fid, question, answer, others, valid_to, approve)
            await _audit(conn, tid, UUID(user["id"]), "kb.faq_approve" if approve else "kb.faq_save", str(saved_id))
    except K.Refused as e:
        return await _faqs_page(request, user, tid, form=form, error=ui.kb_refusal(e.reason, e.detail), status_code=400)
    if approve:
        await _queue([K.faq_job(tid, saved_id)])
    return RedirectResponse("/console/faqs?" + urlencode({"saved": "Pergunta aprovada." if approve else "Rascunho salvo: o assistente ainda não usa."}), status_code=303)


@router.post("/faqs/{faq_id}/retire")
async def faqs_retire(request: Request, faq_id: UUID):
    from agentplatform.core.knowledge import admin as K
    user = await require_post(request, *KB_ROLES)
    tid = _tid(user)
    try:
        async with tenant_tx(tid) as conn:
            await K.retire_faq(conn, tid, faq_id)
            await _audit(conn, tid, UUID(user["id"]), "kb.faq_retire", str(faq_id))
    except K.Refused:
        raise HTTPException(404) from None
    return RedirectResponse("/console/faqs?" + urlencode({"saved": "Pergunta retirada: o assistente não usa mais."}), status_code=303)


async def _curation_page(request: Request, user: dict, tid: UUID, *, problems=(), error: str | None = None, draft: dict | None = None, saved: str | None = None,
                         status_code: int = 200) -> HTMLResponse:
    async with tenant_tx(tid) as conn:
        rows = await (await conn.execute(
            """select q.id, q.question_masked, q.created_at, t.conversation_id,
                      q.frequency + coalesce((select sum(s.frequency) from curation_queue s where s.cluster_id=q.id and s.status='open'), 0) as frequency,
                      array(select s.question_masked from curation_queue s where s.cluster_id=q.id and s.status='open' order by s.created_at) as similar
               from curation_queue q left join turns t on t.id = q.turn_id
               where q.status='open' and q.cluster_id is null order by frequency desc, q.created_at limit 200""")).fetchall()
        docs = await (await conn.execute(
            "select d.id, s.title from kb_documents d join kb_sources s on s.id = d.source_id where d.status='approved' order by s.title")).fetchall()
        faqs = await (await conn.execute("select id, canonical_question from faqs where tenant_id=%s and status <> 'retired' order by canonical_question", (tid,))).fetchall()
    return _page(request, "curation.html", {"user": user, "rows": rows, "docs": docs, "faqs": faqs, "problems": problems, "error": error, "draft": draft or {},
                                            "saved": saved, "can_index": _can_index()}, status_code)


@router.get("/curation", response_class=HTMLResponse)
async def curation(request: Request, saved: str | None = None):
    user = require(request, *KB_ROLES)
    return await _curation_page(request, user, _tid(user), saved=saved)


def _curation_done(message: str) -> RedirectResponse:
    return RedirectResponse("/console/curation?" + urlencode({"saved": message}), status_code=303)


@router.post("/curation/{item_id}/faq", response_class=HTMLResponse)
async def curation_faq(request: Request, item_id: UUID, question: str = Form(""), answer: str = Form("")):
    """The question becomes an approved answer, so the next person gets it straight away."""
    from agentplatform.core.knowledge import admin as K
    user = await require_post(request, *KB_ROLES)
    tid = _tid(user)
    tr = await _kb_runtime(tid)
    draft = {"id": str(item_id), "question": question, "answer": answer}
    problems = K.validate_faq(tr.config, question, answer, [], tr.allowed_urls)
    if problems:
        return await _curation_page(request, user, tid, problems=problems, draft=draft, status_code=400)
    approve = _can_index()
    try:
        async with tenant_tx(tid) as conn:
            await K.open_question(conn, tid, item_id)
            similar = await K.similar_questions(conn, tid, item_id)
            fid = await K.save_faq(conn, tid, UUID(user["id"]), None, question, answer, similar, "", approve)
            await K.close_question(conn, tid, item_id, "resolved", {"action": "faq", "faq_id": str(fid), "by": user["id"]})
            await _audit(conn, tid, UUID(user["id"]), "kb.curation_faq", str(item_id), {"faq": str(fid)})
    except K.Refused as e:
        return await _curation_page(request, user, tid, error=ui.kb_refusal(e.reason, e.detail), draft=draft, status_code=400)
    if approve:
        await _queue([K.faq_job(tid, fid)])
    return _curation_done("Resposta aprovada: da próxima vez o assistente responde sozinho." if approve else "Salva como rascunho em Perguntas frequentes: aqui não dá para aprovar.")


@router.post("/curation/{item_id}/variant", response_class=HTMLResponse)
async def curation_variant(request: Request, item_id: UUID, faq_id: str = Form("")):
    """Another way of asking something already answered."""
    from agentplatform.core.knowledge import admin as K
    user = await require_post(request, *KB_ROLES)
    tid = _tid(user)
    try:
        async with tenant_tx(tid) as conn:
            target = K.parse_id(faq_id)
            item = await K.open_question(conn, tid, item_id)
            needs_vector = await K.add_variant(conn, tid, target, item["question_masked"])
            await K.close_question(conn, tid, item_id, "resolved", {"action": "variant", "faq_id": str(target), "by": user["id"]})
            await _audit(conn, tid, UUID(user["id"]), "kb.curation_variant", str(item_id), {"faq": str(target)})
    except K.Refused as e:
        return await _curation_page(request, user, tid, error=ui.kb_refusal(e.reason, e.detail), status_code=400)
    if needs_vector:
        await _queue([K.faq_job(tid, target)])
    return _curation_done("Pergunta anexada à resposta que já existia.")


@router.post("/curation/{item_id}/document", response_class=HTMLResponse)
async def curation_document(request: Request, item_id: UUID, document_id: str = Form("")):
    """A document approved since then answers it: recorded so the question leaves the list with the reason."""
    from agentplatform.core.knowledge import admin as K
    user = await require_post(request, *KB_ROLES)
    tid = _tid(user)
    try:
        async with tenant_tx(tid) as conn:
            target = K.parse_id(document_id)
            if not await (await conn.execute("select 1 from kb_documents where id=%s and status='approved'", (target,))).fetchone():
                raise K.Refused("not_found")
            await K.close_question(conn, tid, item_id, "resolved", {"action": "document", "document_id": str(target), "by": user["id"]})
            await _audit(conn, tid, UUID(user["id"]), "kb.curation_document", str(item_id), {"document": str(target)})
    except K.Refused as e:
        return await _curation_page(request, user, tid, error=ui.kb_refusal(e.reason, e.detail), status_code=400)
    return _curation_done("Pergunta ligada ao documento.")


@router.post("/curation/{item_id}/ungroup")
async def curation_ungroup(request: Request, item_id: UUID, question: str = Form("")):
    from agentplatform.core.knowledge import admin as K
    user = await require_post(request, *KB_ROLES)
    tid = _tid(user)
    try:
        async with tenant_tx(tid) as conn:
            await K.ungroup_question(conn, tid, item_id, question)
    except K.Refused:
        raise HTTPException(404) from None
    return _curation_done("Pergunta separada do grupo.")


@router.post("/curation/{item_id}/out-of-scope", response_class=HTMLResponse)
async def curation_out_of_scope(request: Request, item_id: UUID):
    from agentplatform.core.knowledge import admin as K
    user = await require_post(request, *KB_ROLES)
    tid = _tid(user)
    try:
        async with tenant_tx(tid) as conn:
            await K.close_question(conn, tid, item_id, "out_of_scope", {"action": "out_of_scope", "by": user["id"]})
            await _audit(conn, tid, UUID(user["id"]), "kb.curation_out_of_scope", str(item_id))
    except K.Refused as e:
        return await _curation_page(request, user, tid, error=ui.kb_refusal(e.reason, e.detail), status_code=400)
    return _curation_done("Pergunta marcada como fora do assunto.")


@router.post("/kill-switch")
async def kill_switch(request: Request, scope: str = Form(...), on: str = Form(...), reason: str = Form("console")):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    from agentplatform.core.outbox.dispatcher import FEATURES
    if scope not in ("tenant", *FEATURES):
        raise HTTPException(400, "escopo inválido")
    ref = str(tid) if scope == "tenant" else scope
    async with platform_tx() as conn:
        await conn.execute(
            """insert into kill_switches (tenant_id, scope, scope_ref, is_on, reason, changed_by)
               values (%s,%s,%s,%s,%s,%s)
               on conflict (scope, coalesce(scope_ref, ''), coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'))
               do update set is_on=excluded.is_on, reason=excluded.reason, changed_by=excluded.changed_by, changed_at=now()""",
            (tid, "tenant" if scope == "tenant" else "feature", ref, on == "1", reason, UUID(user["id"])))
        await _audit(conn, tid, UUID(user["id"]), "kill_switch", ref, {"on": on == "1"})
    registry.invalidate_kill_switches()
    return RedirectResponse("/console/", status_code=303)


# ======================= LGPD (F2-LGPD-02) =======================
@router.get("/lgpd", response_class=HTMLResponse)
async def lgpd_page(request: Request):
    user = require(request, "tenant_owner")
    return _page(request, "lgpd.html", {"user": user, "subject": None, "searched": False, "confirm_word": ERASE_CONFIRM})


@router.post("/lgpd/search", response_class=HTMLResponse)
async def lgpd_search(request: Request, channel_user_id: str = Form(""), phone: str = Form(""), email: str = Form("")):
    from agentplatform.core.lgpd.service import find_comment_author, find_subject
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    subject = None
    async with tenant_tx(tid) as conn:
        cid = await find_subject(conn, tid, channel_user_id=channel_user_id.strip() or None, phone=phone.strip() or None,
                                 email=email.strip() or None)
        if cid:
            subject = await (await conn.execute(
                """select ct.id, ct.first_name, ct.created_at, l.stage, l.opted_out_at,
                          (select count(*) from conversations c where c.contact_id = ct.id) as conversations,
                          (select count(*) from messages m join conversations c on c.id = m.conversation_id where c.contact_id = ct.id) as messages,
                          (select array_agg(distinct ci.channel) from contact_identities ci where ci.contact_id = ct.id) as channels
                   from contacts ct left join leads l on l.contact_id = ct.id where ct.id = %s""", (cid,))).fetchone()
        author = None if cid or not channel_user_id.strip() else await find_comment_author(conn, tid, channel_user_id)
        await _audit(conn, tid, UUID(user["id"]), "lgpd.search", "subject", {"found": bool(cid or author)})
    return await _lgpd_subject_page(request, user, tid, subject, comment_author=author)


async def _lgpd_subject_page(request: Request, user: dict, tid: UUID, subject: dict | None, *, status_code: int = 200, **extra) -> HTMLResponse:
    """Shows personal data in clear text, so the browser keeps no copy of it."""
    from agentplatform.core.lgpd.service import subject_details
    details = None
    if subject:
        async with tenant_tx(tid) as conn:
            details = await subject_details(conn, tid, subject["id"])
    labels = await _labels(tid)
    resp = _page(request, "lgpd.html", {"user": user, "subject": subject, "searched": True, "confirm_word": ERASE_CONFIRM, "details": details,
                                        "slots": list(labels["slots"].values()), **extra}, status_code)
    resp.headers["Cache-Control"] = "no-store"
    return resp


CORRECTION_REFUSED = {"phone": "Telefone inválido. Use o número com DDD.", "email": "E-mail inválido.", "contact": "Titular não encontrado."}
CORRECTION_IN_USE = {"phone": "Este telefone já é de outro contato. Nada foi alterado.", "email": "Este e-mail já é de outro contato. Nada foi alterado."}


@router.post("/lgpd/{contact_id}/correct", response_class=HTMLResponse)
async def lgpd_correct(request: Request, contact_id: UUID, first_name: str = Form(""), name: str = Form(""), phone: str = Form(""), email: str = Form("")):
    from agentplatform.core.lgpd.service import CorrectionRefused, correct_subject
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    form = await request.form()
    profile = {k.removeprefix("slot:"): str(v) for k, v in form.items() if k.startswith("slot:")}
    labels = await _labels(tid)
    error, changed = None, []
    try:
        async with tenant_tx(tid) as conn:      # all or nothing: a refused value leaves everything as it was
            changed = await correct_subject(conn, tid, contact_id, UUID(user["id"]), first_name=first_name, name=name, phone=phone, email=email,
                                            profile=profile, slots=list(labels["slots"].values()))
    except CorrectionRefused as e:
        error = (CORRECTION_IN_USE if e.reason == "in_use" else CORRECTION_REFUSED).get(e.field) or f"Valor inválido em {ui.slot_label(e.field, labels)}."
    async with tenant_tx(tid) as conn:
        subject = await (await conn.execute(
            """select ct.id, ct.first_name, ct.created_at, l.stage, l.opted_out_at,
                      (select count(*) from conversations c where c.contact_id = ct.id) as conversations,
                      (select count(*) from messages m join conversations c on c.id = m.conversation_id where c.contact_id = ct.id) as messages,
                      (select array_agg(distinct ci.channel) from contact_identities ci where ci.contact_id = ct.id) as channels
               from contacts ct left join leads l on l.contact_id = ct.id where ct.id = %s""", (contact_id,))).fetchone()
    if not subject:
        raise HTTPException(404)
    return await _lgpd_subject_page(request, user, tid, subject, error=error, corrected=None if error else changed, status_code=400 if error else 200)


@router.post("/lgpd/comments/erase", response_class=HTMLResponse)
async def lgpd_erase_comments(request: Request, author: str = Form(""), confirm: str = Form("")):
    from agentplatform.core.lgpd.service import erase_comment_author
    user = await require_post(request, "tenant_owner")
    if confirm.strip() != ERASE_CONFIRM:
        return _page(request, "lgpd.html", {"user": user, "subject": None, "searched": False, "confirm_word": ERASE_CONFIRM,
                                            "error": f"Digite {ERASE_CONFIRM} para confirmar a eliminação."}, 400)
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        n = await erase_comment_author(conn, tid, author)
        await _audit(conn, tid, UUID(user["id"]), "lgpd.erase_comments", "comment_author", {"comments": n})
    return _page(request, "lgpd.html", {"user": user, "subject": None, "searched": False, "confirm_word": ERASE_CONFIRM,
                                        "erased": {"comments": n}})


@router.post("/lgpd/{contact_id}/export")
async def lgpd_export(request: Request, contact_id: UUID):
    from agentplatform.core.lgpd.service import export_subject
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        data = await export_subject(conn, tid, contact_id, UUID(user["id"]))
    return JSONResponse(json.loads(json.dumps(data, default=str)),
                        headers={"Content-Disposition": f'attachment; filename="titular-{contact_id}.json"',
                                 "Cache-Control": "no-store"})


@router.post("/lgpd/{contact_id}/erase", response_class=HTMLResponse)
async def lgpd_erase(request: Request, contact_id: UUID, confirm: str = Form("")):
    from agentplatform.core.lgpd.service import erase_subject
    user = await require_post(request, "tenant_owner")
    if confirm.strip() != ERASE_CONFIRM:
        return _page(request, "lgpd.html", {"user": user, "subject": None, "searched": False, "confirm_word": ERASE_CONFIRM,
                                            "error": f"Digite {ERASE_CONFIRM} para confirmar a eliminação."}, 400)
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        result = await erase_subject(conn, tid, contact_id, UUID(user["id"]))
    return _page(request, "lgpd.html", {"user": user, "subject": None, "searched": False, "confirm_word": ERASE_CONFIRM,
                                        "erased": result})


# ======================= budget (F2-CON-04) =======================
@router.get("/budget", response_class=HTMLResponse)
async def budget_page(request: Request):
    from agentplatform.core.models.budget import key_usage, tenant_usage
    user = require(request, "tenant_owner")
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        usage = await tenant_usage(conn, tid)
    from agentplatform.runtime import get_runtime
    cfg = (await get_runtime().tenant_for_setup(tid)).config.budget
    return _page(request, "budget.html", {"user": user, "key": await key_usage(), "u": usage, "cfg": cfg})


@router.post("/budget/topup")
async def budget_topup(request: Request, gross_usd: str = Form(...), fee_usd: str = Form("0"), note: str = Form("")):
    """Records the credit purchase and PROPOSES a config version raising the tenant's total cap by the net amount;
    a second person approves it on the config screen. The provider-key hard cap (process setting) is never touched."""
    from agentplatform.core.models.budget import record_topup
    from agentplatform.runtime import get_runtime
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    try:
        gross, fee = Decimal(gross_usd.replace(",", ".")), Decimal((fee_usd or "0").replace(",", "."))
        if gross <= 0 or fee < 0 or fee >= gross:
            raise InvalidOperation
    except InvalidOperation:
        raise HTTPException(400, "valores inválidos") from None
    tr = await get_runtime().tenant_for_change(tid)
    b = tr.config.budget
    net = gross - fee
    new_cfg = tr.config.model_copy(update={"budget": b.model_copy(update={
        "pilot_total_cap_usd": (b.pilot_total_cap_usd + net) if b.pilot_total_cap_usd is not None else None,
        "net_credit_usd": (b.net_credit_usd or Decimal(0)) + net})})
    async with tenant_tx(tid) as conn:
        vid = await registry.propose(conn, tid, new_cfg, package_compliance=tr.package.manifest.compliance,
                                     created_by=UUID(user["id"]))
        await record_topup(conn, tid, gross, fee, note=note or None, actor=UUID(user["id"]), config_version_id=vid)
    return RedirectResponse("/console/budget", status_code=303)


# ======================= prompt lab =======================
PROMPT_NODES = [
    ("respond", "Escrever a resposta", "Gera o texto enviado ao cliente: tom, estrutura, quando perguntar, como explicar."),
    ("analyze_turn", "Entender a mensagem", "Lê a mensagem e extrai intenções, dados do cliente (perfil) e sugestão de estágio."),
    ("output_judge", "Revisar a resposta", "Segunda leitura da resposta antes do envio: promessas, riscos, tom."),
    ("summarize", "Resumir a conversa", "Resumo curto que o vendedor e o assistente usam como contexto."),
]


async def _prompt_versions(tid: UUID, node: str) -> list[dict]:
    from agentplatform.core.agent import prompts as P
    async with tenant_tx(tid) as conn:
        rows = await (await conn.execute(
            """select label, version, base_version, note, created_at from prompt_templates
               where tenant_id=%s and name=%s order by version desc limit 30""", (tid, node))).fetchall()
    return [*({"label": r["label"], "kind": "edited", "note": r["note"], "base": r["base_version"], "at": r["created_at"],
               "n": r["version"]} for r in rows),
            *({"label": v, "kind": "seed", "note": None, "base": None, "at": None, "n": None} for v in P.seed_versions(node))]


async def _prompt_page(request: Request, user: dict, node: str, version: str | None, *, body: str | None = None,
                       errors: list[str] | None = None, saved: str | None = None, draft: str | None = None,
                       note: str = "", status_code: int = 200):
    from agentplatform.core.agent import prompts as P
    from agentplatform.runtime import get_runtime
    tid = _tid(user)
    if node not in {n for n, _, _ in PROMPT_NODES}:
        raise HTTPException(404)
    cfg = (await get_runtime().tenant_for_change(tid)).config
    active = {n: getattr(cfg.models, n).prompt_version for n, _, _ in PROMPT_NODES}
    versions = await _prompt_versions(tid, node)
    labels = {v["label"] for v in versions}
    version = version if version in labels else active[node]
    async with tenant_tx(tid) as conn:
        await P.preload(conn, [version, *(v["base"] for v in versions if v["base"])])
    original = P.raw_body(version)
    diff = None
    if saved:
        base = next((v["base"] for v in versions if v["label"] == saved), None)
        if base:
            diff = "\n".join(difflib.unified_diff(P.raw_body(base).splitlines(), P.raw_body(saved).splitlines(),
                                                  base, saved, lineterm="", n=2))
    return _page(request, "prompts.html", {
        "user": user, "nodes": PROMPT_NODES, "node": node, "active": active, "versions": versions, "version": version,
        "body": body if body is not None else original, "variables": sorted(P.variables(version)), "errors": errors or [],
        "saved": saved, "draft": draft, "diff": diff, "note": note}, status_code)


@router.get("/prompts", response_class=HTMLResponse)
async def prompts_page(request: Request, node: str = "respond", version: str = "", saved: str = "", draft: str = ""):
    user = require(request, "tenant_owner", "reviewer")
    return await _prompt_page(request, user, node, version or None, saved=saved or None, draft=draft or None)


async def _draft_with_prompt(tid: UUID, user: dict, node: str, label: str) -> UUID:
    """Config draft = active config with this node pointing to the prompt version (two-person approval in Config)."""
    from agentplatform.runtime import get_runtime
    tr = await get_runtime().tenant_for_change(tid)
    models = tr.config.models
    new_cfg = tr.config.model_copy(update={"models": models.model_copy(update={
        node: getattr(models, node).model_copy(update={"prompt_version": label})})})
    async with tenant_tx(tid) as conn:
        return await registry.propose(conn, tid, new_cfg, package_compliance=tr.package.manifest.compliance,
                                      created_by=UUID(user["id"]))


@router.post("/prompts/save")
async def prompts_save(request: Request, node: str = Form(...), base_version: str = Form(...), body: str = Form(...),
                       note: str = Form("")):
    from agentplatform.core.agent import prompts as P
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    if node not in {n for n, _, _ in PROMPT_NODES} or P.name_of(base_version) != node:
        raise HTTPException(400, "etapa ou versão inválida")
    body = body.replace("\r\n", "\n")
    async with tenant_tx(tid) as conn:
        await P.preload(conn, [base_version])
    errors = P.validate(body, base_version)
    if body.strip() == P.raw_body(base_version).strip():
        errors.append("nada mudou em relação à versão de origem")
    if errors:
        return await _prompt_page(request, user, node, base_version, body=body, errors=errors, note=note, status_code=400)
    async with tenant_tx(tid) as conn:
        label = await P.save_version(conn, tid, node, body, base_version, note.strip()[:300] or None, UUID(user["id"]))
        await _audit(conn, tid, UUID(user["id"]), "prompt.save", label, {"base": base_version})
    draft = await _draft_with_prompt(tid, user, node, label)
    return RedirectResponse(f"/console/prompts?node={node}&version={label}&saved={label}&draft={draft}", status_code=303)


@router.post("/prompts/use")
async def prompts_use(request: Request, node: str = Form(...), version: str = Form(...)):
    """Draft config pointing to an existing version (e.g. go back to a seed, or retry an older edit)."""
    from agentplatform.core.agent import prompts as P
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    if node not in {n for n, _, _ in PROMPT_NODES} or P.name_of(version) != node:
        raise HTTPException(400, "etapa ou versão inválida")
    if version not in {v["label"] for v in await _prompt_versions(tid, node)}:
        raise HTTPException(404)
    draft = await _draft_with_prompt(tid, user, node, version)
    return RedirectResponse(f"/console/prompts?node={node}&version={version}&draft={draft}", status_code=303)


# ======================= tables the tenant maintains (package datasets, F3-DATA-01) =======================
DATASET_BLANK_ROWS = 3


async def _dataset_spec(tid: UUID, name: str | None):
    from agentplatform.core.packages.contract import package_datasets
    from agentplatform.runtime import get_runtime
    tr = await get_runtime().tenant_for_setup(tid)
    specs = package_datasets(tr.package)
    spec = next((d for d in specs if d.name == name), None) if name else (specs[0] if specs else None)
    return tr, specs, spec


def _ds_cell(v, col) -> str:
    if v is None:
        return ""
    return str(v).replace(".", ",") if col.kind == "number" else str(v)


def _ds_key(spec, row: dict) -> str:
    return str(row.get(spec.key)) if spec.key else json.dumps(row, sort_keys=True)


def _ds_preview(spec, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        try:
            out.append(spec.preview(spec.row_model.model_validate(r)) if spec.preview else {})
        except Exception:      # a preview column must never break the page
            out.append({})
    return out


async def _dataset_page(request: Request, user: dict, name: str | None, *, grid: list[dict] | None = None,
                        errors: list[str] | None = None, note: str = "", status_code: int = 200) -> HTMLResponse:
    from agentplatform.core.knowledge import datasets as D
    tid = _tid(user)
    tr, specs, spec = await _dataset_spec(tid, name)
    if not specs:
        return _page(request, "datasets.html", {"user": user, "specs": [], "spec": None})
    if spec is None:
        raise HTTPException(404)
    pkg = tr.package.manifest.name
    cols = D.columns(spec.row_model)
    async with tenant_tx(tid) as conn:
        live = await D.in_use(conn, pkg, spec.name, prefer_drafts=False)
        live_rows = await D.rows_of(conn, live["id"]) if live else []
        vers = await D.versions(conn, tid, pkg, spec.name)
        draft = next((v for v in vers if v["status"] == "draft"), None)
        draft_rows = await D.rows_of(conn, draft["id"]) if draft else []
    live_by = {_ds_key(spec, r): r for r in live_rows}
    marks = ["new" if _ds_key(spec, r) not in live_by else ("changed" if live_by[_ds_key(spec, r)] != r else "")
             for r in draft_rows]
    removed = [r for k, r in live_by.items() if k not in {_ds_key(spec, d) for d in draft_rows}] if draft else []
    if grid is None:
        grid = [{c.name: _ds_cell(r.get(c.name), c) for c in cols} for r in (draft_rows or live_rows)]
    q = request.query_params
    return _page(request, "datasets.html", {
        "user": user, "specs": specs, "spec": spec, "cols": cols, "live": live, "live_rows": live_rows,
        "live_preview": _ds_preview(spec, live_rows), "live_is_platform": bool(live and live["tenant_id"] is None),
        "draft": draft, "draft_rows": draft_rows, "draft_preview": _ds_preview(spec, draft_rows), "marks": marks,
        "removed": removed, "versions": vers, "grid": grid + [{} for _ in range(DATASET_BLANK_ROWS)],
        "flag_on": bool(tr.config.operations.feature_flags.get(spec.feature_flag)) if spec.feature_flag else None,
        "dcfg": tr.config.operations.datasets, "errors": errors or [], "note": note,
        "msg": q.get("ok", ""), "ignored": [c for c in q.get("ignored", "").split("|") if c],
        "flag_draft": q.get("flagdraft", "")}, status_code)


@router.get("/datasets", response_class=HTMLResponse)
async def datasets_page(request: Request, name: str = ""):
    user = require(request, "tenant_owner", "reviewer")
    return await _dataset_page(request, user, name or None)


async def _dataset_commit(request: Request, user: dict, name: str, rows: list[dict], note: str, *, first_row: int,
                          grid: list[dict] | None, source: str):
    """Validate -> replace any pending draft with a new draft version (nothing reaches customers until activated)."""

    from agentplatform.core.knowledge import datasets as D
    tid = _tid(user)
    tr, _, spec = await _dataset_spec(tid, name)
    if spec is None:
        raise HTTPException(404)
    pkg, dcfg = tr.package.manifest.name, tr.config.operations.datasets
    errors = []
    filled = [r for r in rows if any(str(v or "").strip() for v in r.values())]
    if len(filled) > dcfg.max_rows:
        errors.append(f"a tabela tem {len(filled)} linhas; o máximo é {dcfg.max_rows}")
    async with tenant_tx(tid) as conn:
        dry = await D.import_dataset(conn, tenant_id=tid, package=pkg, dataset=spec.name, rows=rows,
                                     schema=spec.row_model, key=spec.key, first_row=first_row, dry_run=True)
        has_own_active = await (await conn.execute(
            "select 1 from domain_datasets where tenant_id=%s and package=%s and dataset=%s and status='active'",
            (tid, pkg, spec.name))).fetchone()
    errors += dry.errors
    if not errors and dry.valid == 0:
        errors.append("a tabela está vazia")
    if not errors and has_own_active and dry.added == 0 and dry.removed == 0:
        errors.append("nada mudou em relação à versão em uso")
    if errors:
        return await _dataset_page(request, user, spec.name, grid=grid, errors=errors, note=note, status_code=400)
    async with tenant_tx(tid) as conn:
        await D.discard_drafts(conn, tid, pkg, spec.name)
        p = await D.import_dataset(conn, tenant_id=tid, package=pkg, dataset=spec.name, rows=rows, schema=spec.row_model,
                                   key=spec.key, first_row=first_row, created_by=UUID(user["id"]),
                                   note=note.strip()[:300] or None)
        await _audit(conn, tid, UUID(user["id"]), "dataset.draft", f"{spec.name}@v{p.version}",
                     {"rows": p.valid, "added": p.added, "removed": p.removed, "source": source,
                      "checksum": p.checksum, "ignored_columns": dry.ignored_columns})
    qs = {"name": spec.name, "ok": "draft"}
    if dry.ignored_columns:
        qs["ignored"] = "|".join(dry.ignored_columns)[:500]
    return RedirectResponse(f"/console/datasets?{urlencode(qs)}", status_code=303)


@router.post("/datasets/save")
async def datasets_save(request: Request, name: str = Form(...), note: str = Form("")):
    """The editable grid: one input list per column (c_<column>), rows in screen order; blank rows are skipped."""
    from agentplatform.core.knowledge import datasets as D
    user = await require_post(request, "tenant_owner")
    _, _, spec = await _dataset_spec(_tid(user), name)
    if spec is None:
        raise HTTPException(404)
    form = await request.form()
    cols = D.columns(spec.row_model)
    lists = {c.name: [str(v) for v in form.getlist(f"c_{c.name}")] for c in cols}
    n = max((len(v) for v in lists.values()), default=0)
    rows = [{c: (lists[c][i] if i < len(lists[c]) else "") for c in lists} for i in range(n)]
    grid = [r for r in rows if any(v.strip() for v in r.values())]
    return await _dataset_commit(request, user, name, rows, note, first_row=1, grid=grid, source="editor")


@router.post("/datasets/upload")
async def datasets_upload(request: Request, file: UploadFile, name: str = Form(...), note: str = Form("")):
    from agentplatform.core.knowledge import datasets as D
    user = await require_post(request, "tenant_owner")
    tr, _, spec = await _dataset_spec(_tid(user), name)
    if spec is None:
        raise HTTPException(404)
    data = await file.read()
    if len(data) > tr.config.operations.datasets.max_upload_bytes:
        return await _dataset_page(request, user, name, errors=["arquivo grande demais"], note=note, status_code=413)
    try:
        rows = D.parse_table(data, file.filename or "")
    except Exception:
        return await _dataset_page(request, user, name, note=note, status_code=400, errors=[
            "não consegui ler o arquivo: envie .xlsx ou .csv com os nomes das colunas na 1ª linha"])
    return await _dataset_commit(request, user, name, rows, note or (file.filename or ""), first_row=2, grid=None,
                                 source="upload")


async def _dataset_row(conn, tid: UUID, pkg: str, name: str, dataset_id: UUID) -> dict:
    row = await (await conn.execute(
        """select id, status, version, created_by from domain_datasets
           where id=%s and tenant_id=%s and package=%s and dataset=%s""", (dataset_id, tid, pkg, name))).fetchone()
    if row is None:
        raise HTTPException(404)
    return row


@router.post("/datasets/activate")
async def datasets_activate(request: Request, name: str = Form(...), dataset_id: str = Form(...)):
    """Draft -> in use (or an older version back in use). Takes effect on the next customer turn."""
    from agentplatform.core.knowledge import datasets as D
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    try:
        ds_id = UUID(dataset_id)
    except ValueError as e:
        raise HTTPException(404) from e
    tr, _, spec = await _dataset_spec(tid, name)
    if spec is None:
        raise HTTPException(404)
    pkg = tr.package.manifest.name
    async with tenant_tx(tid) as conn:
        row = await _dataset_row(conn, tid, pkg, name, ds_id)
        if row["status"] not in ("draft", "retired"):
            raise HTTPException(409, "esta versão já está em uso")
        two_person = tr.config.operations.datasets.two_person_activation
        if row["status"] == "draft" and two_person and str(row["created_by"]) == str(user["id"]):
            return await _dataset_page(request, user, name, status_code=409, errors=[
                "regra de duas pessoas ligada: outra pessoa precisa ativar a versão que você criou"])
        await D.activate(conn, row["id"], UUID(user["id"]))
        await _audit(conn, tid, UUID(user["id"]), "dataset.activate", f"{name}@v{row['version']}",
                     {"from_status": row["status"]})
    return RedirectResponse(f"/console/datasets?name={name}&ok=activated", status_code=303)


@router.post("/datasets/discard")
async def datasets_discard(request: Request, name: str = Form(...)):
    from agentplatform.core.knowledge import datasets as D
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    tr, _, spec = await _dataset_spec(tid, name)
    if spec is None:
        raise HTTPException(404)
    async with tenant_tx(tid) as conn:
        n = await D.discard_drafts(conn, tid, tr.package.manifest.name, name)
        await _audit(conn, tid, UUID(user["id"]), "dataset.discard", name, {"drafts": n})
    return RedirectResponse(f"/console/datasets?name={name}&ok=discarded", status_code=303)


@router.get("/datasets/export")
async def datasets_export(request: Request, name: str, dataset_id: str = "", template: str = ""):
    """Spreadsheet of a version (default: the one in use), or the model with example rows."""
    from fastapi.responses import Response

    from agentplatform.core.knowledge import datasets as D
    user = require(request, "tenant_owner", "reviewer")
    tid = _tid(user)
    tr, _, spec = await _dataset_spec(tid, name)
    if spec is None:
        raise HTTPException(404)
    pkg = tr.package.manifest.name
    if template:
        rows, suffix = [json.loads(spec.row_model.model_validate(r).model_dump_json()) for r in spec.example], "modelo"
    else:
        async with tenant_tx(tid) as conn:
            ds = None
            if dataset_id:
                try:
                    ds = await (await conn.execute(
                        "select id, version from domain_datasets where id=%s and package=%s and dataset=%s",
                        (UUID(dataset_id), pkg, name))).fetchone()
                except ValueError:
                    pass
            else:
                ds = await D.in_use(conn, pkg, name, prefer_drafts=False)
            if ds is None:
                raise HTTPException(404)
            rows, suffix = await D.rows_of(conn, ds["id"]), f"v{ds['version']}"
    return Response(D.to_csv(rows, spec.row_model), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}-{suffix}.csv"'})


async def _draft_with_flag(tid: UUID, user: dict, flag: str, on: bool) -> UUID:
    """Config draft = active config with one feature flag changed (two-person approval in Config)."""
    from agentplatform.runtime import get_runtime
    tr = await get_runtime().tenant_for_setup(tid)
    ops = tr.config.operations
    new_cfg = tr.config.model_copy(update={"operations": ops.model_copy(update={
        "feature_flags": {**ops.feature_flags, flag: on}})})
    async with tenant_tx(tid) as conn:
        return await registry.propose(conn, tid, new_cfg, package_compliance=tr.package.manifest.compliance,
                                      created_by=UUID(user["id"]))


@router.post("/datasets/flag")
async def datasets_flag(request: Request, name: str = Form(...), on: str = Form(...)):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    _, _, spec = await _dataset_spec(tid, name)
    if spec is None or not spec.feature_flag:
        raise HTTPException(404)
    draft = await _draft_with_flag(tid, user, spec.feature_flag, on == "1")
    return RedirectResponse(f"/console/datasets?name={name}&flagdraft={draft}", status_code=303)


# ======================= simulator (agent calibration) =======================
SIM_MAX_CHARS = 1000


def _sim_version(v: str | None) -> UUID | None:
    try:
        return UUID(v) if v and v != "active" else None
    except ValueError:
        return None


def _sim_url(session: str, v: str | None, error: str | None = None) -> str:
    q = {"s": session, "v": v or "active"}
    if error:
        q["erro"] = error
    return "/console/simulator?" + urlencode(q)


@router.get("/simulator", response_class=HTMLResponse)
async def simulator_page(request: Request, s: str = "", v: str = "active", erro: str = ""):
    from agentplatform.lab import simulator as sim
    user = require(request, "tenant_owner", "reviewer")
    tid = _tid(user)
    session = s if sim.valid_session(s) else sim.new_session()
    env = get_settings().app_env
    return _page(request, "simulator.html", {
        "user": user, "session": session, "v": v, "versions": await sim.versions(tid, env), "error": erro,
        "snap": await sim.snapshot(tid, session), "recent": await sim.recent_sessions(tid), "labels": await _labels(tid),
        "table_drafts": await _dataset_drafts(tid)})


async def _dataset_drafts(tid: UUID) -> list[dict]:
    """Tables with a draft newer than the version in use (the simulator uses the draft)."""
    from agentplatform.core.knowledge import datasets as D
    tr, specs, _ = await _dataset_spec(tid, None)
    out = []
    async with tenant_tx(tid) as conn:
        for spec in specs:
            d = await D.in_use(conn, tr.package.manifest.name, spec.name, prefer_drafts=True)
            if d and d["status"] == "draft":
                out.append({"name": spec.name, "label": spec.label, "version": d["version"]})
    return out


@router.post("/simulator/send")
async def simulator_send(request: Request, s: str = Form(...), v: str = Form("active"), text: str = Form(...)):
    from agentplatform.lab import simulator as sim
    user = await require_post(request, "tenant_owner", "reviewer")
    tid = _tid(user)
    if not sim.valid_session(s):
        raise HTTPException(400, "sessão inválida")
    text = text.strip()[:SIM_MAX_CHARS]
    if not text:
        return RedirectResponse(_sim_url(s, v), status_code=303)
    try:
        await sim.send(tid, s, [text], _sim_version(v))
    except ConfigError as e:
        return RedirectResponse(_sim_url(s, v, str(e)), status_code=303)
    await _audit_sim(tid, user, s, v, 1)
    return RedirectResponse(_sim_url(s, v), status_code=303)


@router.post("/simulator/replay")
async def simulator_replay(request: Request, s: str = Form(...), v: str = Form("active")):
    """Same customer messages, new session, chosen config version: compare two versions side by side."""
    from agentplatform.lab import simulator as sim
    user = await require_post(request, "tenant_owner", "reviewer")
    tid = _tid(user)
    if not sim.valid_session(s):
        raise HTTPException(400, "sessão inválida")
    texts = await sim.customer_messages(tid, s)
    new = sim.new_session()
    if texts:
        try:
            await sim.send(tid, new, texts, _sim_version(v))
        except ConfigError as e:
            return RedirectResponse(_sim_url(s, v, str(e)), status_code=303)
        await _audit_sim(tid, user, new, v, len(texts))
    return RedirectResponse(_sim_url(new, v), status_code=303)


async def _audit_sim(tid: UUID, user: dict, session: str, v: str, messages: int) -> None:
    async with tenant_tx(tid) as conn:
        await _audit(conn, tid, UUID(user["id"]), "simulator.run", session, {"version": v, "messages": messages})


# ======================= dashboards (F3-DASH-01) =======================
@router.get("/dashboards", response_class=HTMLResponse)
async def dashboards(request: Request):
    user = require(request, "tenant_owner", "reviewer")
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        q = lambda sql: conn.execute(sql, (tid,))
        funnel = await (await q("select stage, leads, qualified, opted_out from v_funnel where tenant_id=%s order by leads desc")).fetchall()
        attribution = await (await q(
            "select entrypoint, source_ref, leads, qualified, pre_registrations from v_attribution where tenant_id=%s order by leads desc limit 50")).fetchall()
        variants = await (await q(
            "select private_variant, sent, replied from v_private_reply_variants where tenant_id=%s order by sent desc")).fetchall()
        sla = await (await q(
            "select reason, handoffs, p50_minutes_to_take, sla_breached from v_handoff_sla where tenant_id=%s order by handoffs desc")).fetchall()
        cost = await (await q(
            "select day, sum(calls) as calls, sum(cost_usd) as cost_usd from v_cost_daily where tenant_id=%s "
            "and day >= now() - interval '30 days' group by day order by day desc")).fetchall()
    return _page(request, "dashboards.html", {"user": user, "funnel": funnel, "attribution": attribution,
                                              "variants": variants, "sla": sla, "cost": cost, "labels": await _labels(tid)})


# ======================= config (F2-CON-04) =======================
def _yaml(doc: dict) -> str:
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=120)


async def _config_ctx(conn, tid: UUID, env: str) -> dict:
    rows = await (await conn.execute(
        """select v.id, v.version, v.status, v.canary_percent, v.created_by, v.approved_by, v.created_at, v.document, v.base_version_id, v.note,
                  u.display_name as author from config_versions v left join tenant_users u on u.id = v.created_by
           where v.tenant_id=%s and v.env=%s and v.status <> 'discarded' order by v.version desc limit 30""", (tid, env))).fetchall()
    active = next((r for r in rows if r["status"] == "active"), None)
    evals = await (await conn.execute(
        """select distinct on (config_version_id) config_version_id, status, level, scores, cost_usd, finished_at from eval_requests
           where tenant_id=%s order by config_version_id, created_at desc""", (tid,))).fetchall()
    by_version = {e["config_version_id"]: e for e in evals}
    bases = {b["id"]: b["document"] for b in await (await conn.execute(
        "select id, document from config_versions where id = any(%s)", ([r["base_version_id"] for r in rows if r["base_version_id"]],))).fetchall()}
    drafts = []
    for r in rows:
        if r["status"] != "draft":
            continue
        before = bases.get(r["base_version_id"]) or (active["document"] if active else None)
        drafts.append({**r, "stale": bool(active) and r["base_version_id"] != active["id"], "evaluation": by_version.get(r["id"]),
                       "changes": ui.config_changes(normalized(before), normalized(r["document"])) if before else []})
    cap = (PlatformConfig.model_validate(active["document"]) if active else None)
    return {"versions": rows, "active": active, "active_yaml": _yaml(active["document"]) if active else "", "drafts": drafts,
            "evaluating": any(e["status"] in ("queued", "running") for e in evals),
            "budget_cap": cap.budget.experiment_cap_usd if cap else BudgetConfig().experiment_cap_usd,
            "canary": next((r for r in rows if r["status"] == "canary"), None), "env": env}


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request, proposta: str = "", igual: str = ""):
    user = require(request, "tenant_owner")
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        ctx = await _config_ctx(conn, tid, get_settings().app_env)
    return _page(request, "config.html", {"user": user, **ctx, "proposed": proposta, "same": bool(igual)})


async def _config_failure(request: Request, user: dict, tid: UUID, e: ConfigError, status_code: int = 409) -> HTMLResponse:
    async with tenant_tx(tid) as conn:
        ctx = await _config_ctx(conn, tid, get_settings().app_env)
    return _page(request, "config.html", {"user": user, **ctx, "failure": ui.config_error(e.code, e.detail or str(e))}, status_code)


async def _candidate(tid: UUID, text: str) -> tuple[PlatformConfig | None, str | None, dict | None]:
    """Parse + validate + resolve with the package (union of compliance); returns (cfg, error, resolved_doc)."""
    from agentplatform.core.packages.contract import load_package
    try:
        cfg = PlatformConfig.model_validate(yaml.safe_load(text) or {})
    except (yaml.YAMLError, ValidationError) as e:
        return None, str(e), None
    async with tenant_tx(tid) as conn:
        slug = (await (await conn.execute("select slug from tenants where id=%s", (tid,))).fetchone())["slug"]
    if cfg.tenant_slug != slug:
        return None, f"tenant_slug '{cfg.tenant_slug}' não é deste tenant ('{slug}')", None
    if cfg.environment != get_settings().app_env:
        return None, f"environment '{cfg.environment}' difere do ambiente deste servidor ('{get_settings().app_env}')", None
    try:
        pkg = load_package(cfg.segment_package.name)
        resolved = resolve_with_package(cfg, pkg.manifest.compliance)
    except Exception as e:      # unknown package, monotonicity...
        return None, str(e), None
    return cfg, None, json.loads(resolved.model_dump_json())


@router.post("/config/preview", response_class=HTMLResponse)
async def config_preview(request: Request, yaml_text: str = Form(""), file: UploadFile | None = None):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    if file is not None and file.filename:
        yaml_text = (await file.read()).decode("utf-8")
    cfg, error, resolved = await _candidate(tid, yaml_text)
    async with tenant_tx(tid) as conn:
        ctx = await _config_ctx(conn, tid, get_settings().app_env)
    diff = None
    if resolved is not None:
        diff = "".join(difflib.unified_diff(ctx["active_yaml"].splitlines(True), _yaml(resolved).splitlines(True),
                                            "ativa", "proposta")) or "(sem diferenças)"
    return _page(request, "config.html", {"user": user, **ctx, "candidate": yaml_text, "error": error, "diff": diff},
                 400 if error else 200)


@router.post("/config/propose")
async def config_propose(request: Request, yaml_text: str = Form(...), canary_percent: int = Form(0)):
    from agentplatform.core.packages.contract import load_package
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    cfg, error, _ = await _candidate(tid, yaml_text)
    if error:
        raise HTTPException(400, error)
    async with tenant_tx(tid) as conn:
        try:
            await registry.propose(conn, tid, cfg, package_compliance=load_package(cfg.segment_package.name).manifest.compliance,
                                   created_by=UUID(user["id"]), canary_percent=canary_percent)
        except ConfigError as e:
            raise HTTPException(400, str(e)) from e
    return RedirectResponse("/console/config", status_code=303)


@router.post("/config/{vid}/approve", response_class=HTMLResponse)
async def config_approve(request: Request, vid: UUID):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    try:
        async with tenant_tx(tid) as conn:
            await registry.approve(conn, tid, vid, UUID(user["id"]))
    except ConfigError as e:
        return await _config_failure(request, user, tid, e)
    return RedirectResponse("/console/config", status_code=303)


@router.post("/config/{vid}/rebase", response_class=HTMLResponse)
async def config_rebase(request: Request, vid: UUID):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    try:
        async with tenant_tx(tid) as conn:
            await registry.rebase(conn, tid, vid, UUID(user["id"]))
    except (ConfigError, ValidationError) as e:
        failure = e if isinstance(e, ConfigError) else ConfigError(str(e), code="merge_invalid")
        return await _config_failure(request, user, tid, failure)
    return RedirectResponse("/console/config", status_code=303)


@router.post("/config/{vid}/discard", response_class=HTMLResponse)
async def config_discard(request: Request, vid: UUID):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    try:
        async with tenant_tx(tid) as conn:
            await registry.discard(conn, tid, vid, UUID(user["id"]))
    except ConfigError as e:
        return await _config_failure(request, user, tid, e)
    return RedirectResponse("/console/config", status_code=303)


@router.post("/config/{vid}/evaluate", response_class=HTMLResponse)
async def config_evaluate(request: Request, vid: UUID, level: str = Form("fast")):
    """Runs in the worker, on the simulator channel: nothing reaches a customer, and the page follows it."""
    from agentplatform.lab.gate import evaluation_job, request_evaluation
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    if level not in ("fast", "full"):
        raise HTTPException(400)
    async with tenant_tx(tid) as conn:
        row = await (await conn.execute("select 1 from config_versions where id=%s and tenant_id=%s and status='draft'", (vid, tid))).fetchone()
        if not row:
            raise HTTPException(404)
        req = await request_evaluation(conn, tid, vid, level, UUID(user["id"]))
        if req:
            await _audit(conn, tid, UUID(user["id"]), "config.evaluate", str(vid), {"level": level})
    if not req:
        return await _config_failure(request, user, tid, ConfigError("already running", code="evaluation_running"))
    await _queue([evaluation_job(tid, req)])
    return RedirectResponse("/console/config", status_code=303)


@router.post("/config/rollback", response_class=HTMLResponse)
async def config_rollback(request: Request):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    try:
        async with tenant_tx(tid) as conn:
            back_to = await registry.rollback(conn, tid, get_settings().app_env)
            await _audit(conn, tid, UUID(user["id"]), "config.rollback", str(back_to))
    except ConfigError as e:
        return await _config_failure(request, user, tid, e)
    return RedirectResponse("/console/config", status_code=303)


@router.post("/config/canary/{action}", response_class=HTMLResponse)
async def config_canary(request: Request, action: str):
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    try:
        async with tenant_tx(tid) as conn:
            if action == "promote":
                await registry.promote_canary(conn, tid, get_settings().app_env, UUID(user["id"]))
            elif action == "abort":
                await registry.abort_canary(conn, tid, get_settings().app_env, UUID(user["id"]))
            else:
                raise HTTPException(404)
    except ConfigError as e:
        return await _config_failure(request, user, tid, e)
    return RedirectResponse("/console/config", status_code=303)


# ======================= lead imports (F4-IMP-01) =======================
def _parse_table(name: str, data: bytes) -> list[dict]:
    if name.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        it = ws.iter_rows(values_only=True)
        header = [str(h or "").strip() for h in next(it, [])]
        return [{header[i]: ("" if v is None else str(v)) for i, v in enumerate(r) if i < len(header) and header[i]}
                for r in it if any(v not in (None, "") for v in r)]
    text = data.decode("utf-8-sig", errors="replace")
    dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t") if text.strip() else csv.excel
    return [dict(r) for r in csv.DictReader(io.StringIO(text), dialect=dialect)]


@router.get("/imports", response_class=HTMLResponse)
async def imports_page(request: Request):
    user = require(request, "tenant_owner")
    async with tenant_tx(_tid(user)) as conn:
        batches = await (await conn.execute(
            "select id, status, counts, created_at from import_batches order by created_at desc limit 30")).fetchall()
    return _page(request, "imports.html", {"user": user, "batches": batches, "fields": IMPORT_FIELDS})


@router.post("/imports/preview", response_class=HTMLResponse)
async def imports_preview(request: Request, file: UploadFile):
    from agentplatform.core.imports.leads import import_leads
    from agentplatform.runtime import get_runtime
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    data = await file.read()
    if len(data) > get_settings().console_import_max_bytes:
        raise HTTPException(413, "arquivo grande demais")
    try:
        rows = _parse_table(file.filename or "", data)
    except Exception as e:
        raise HTTPException(400, f"arquivo ilegível: {e}") from e
    form = await request.form()
    columns = list(rows[0].keys()) if rows else []
    mapping = {f: str(form.get(f"map_{f}") or (f if f in columns else "")) for f in IMPORT_FIELDS}
    mapping = {k: v for k, v in mapping.items() if v}
    initial = (await get_runtime().tenant_for_setup(tid)).package.manifest.initial_stage
    async with tenant_tx(tid) as conn:
        rep = await import_leads(conn, tid, rows, mapping, initial_stage=initial, file_bytes=data, dry_run=True)
        await _audit(conn, tid, UUID(user["id"]), "import.dry_run", hashlib.sha256(data).hexdigest()[:16],
                     {"rows": len(rows), "valid": rep.imported + rep.updated, "rejected": len(rep.rejected)})
    return _page(request, "imports.html", {
        "user": user, "batches": [], "fields": IMPORT_FIELDS, "columns": columns, "mapping": mapping, "report": rep,
        "file_b64": base64.b64encode(data).decode(), "file_name": file.filename, "total": len(rows)})


@router.post("/imports/commit")
async def imports_commit(request: Request, file_b64: str = Form(...), file_name: str = Form(...), mapping: str = Form(...)):
    from agentplatform.core.imports.leads import import_leads
    from agentplatform.runtime import get_runtime
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    data = base64.b64decode(file_b64)
    if len(data) > get_settings().console_import_max_bytes:
        raise HTTPException(413, "arquivo grande demais")
    rows = _parse_table(file_name, data)
    initial = (await get_runtime().tenant_for_setup(tid)).package.manifest.initial_stage
    async with tenant_tx(tid) as conn:
        rep = await import_leads(conn, tid, rows, json.loads(mapping), initial_stage=initial, file_bytes=data,
                                 dry_run=False, created_by=UUID(user["id"]))
        await _audit(conn, tid, UUID(user["id"]), "import.commit", str(rep.batch_id),
                     {"imported": rep.imported, "updated": rep.updated, "rejected": len(rep.rejected)})
    return RedirectResponse("/console/imports", status_code=303)


@router.post("/imports/{batch_id}/rollback")
async def imports_rollback(request: Request, batch_id: UUID):
    from agentplatform.core.imports.leads import rollback_batch
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        n = await rollback_batch(conn, tid, batch_id)
        await _audit(conn, tid, UUID(user["id"]), "import.rollback", str(batch_id), {"removed": n})
    return RedirectResponse("/console/imports", status_code=303)


# ======================= campaigns (F4-CMP-02, read-only) =======================
@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_page(request: Request):
    user = require(request, "tenant_owner", "reviewer")
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        camps = await (await conn.execute(
            "select id, name, status, template, limits, created_at from campaigns order by created_at desc limit 50")).fetchall()
        stats = await (await conn.execute(
            """select t.campaign_id, coalesce(t.variant, '-') as variant, count(*) as targets,
                      count(*) filter (where o.status = 'sent') as sent,
                      count(*) filter (where o.status in ('failed', 'cancelled')) as failed,
                      count(*) filter (where t.status = 'pending') as pending,
                      count(*) filter (where exists (select 1 from messages m where m.conversation_id = o.conversation_id
                                                       and m.direction = 'in' and m.provider_ts > o.sent_at)) as replied,
                      count(*) filter (where l.opted_out_at is not null and l.opted_out_at > c.created_at) as opted_out
               from campaign_targets t join campaigns c on c.id = t.campaign_id
               left join outbox o on o.id = t.outbox_id
               left join leads l on l.contact_id = t.contact_id
               group by t.campaign_id, t.variant order by t.campaign_id, variant""")).fetchall()
    by_camp: dict = {}
    for s in stats:
        by_camp.setdefault(s["campaign_id"], []).append(s)
    return _page(request, "campaigns.html", {"user": user, "camps": camps, "stats": by_camp})


# ======================= posts and comments: what each post does with comments (ADR-018) =======================
def _lines(v: str) -> list[str]:
    return [x.strip() for x in v.replace("\r", "").split("\n") if x.strip()]


def _words(v: str) -> list[str]:
    return [x.strip() for x in v.replace("\n", ",").split(",") if x.strip()]


def _variants(a: str, b: str) -> dict[str, str]:
    return {k: v.strip() for k, v in (("A", a), ("B", b)) if v.strip()}


async def _comment_config(tid: UUID):
    from agentplatform.runtime import get_runtime
    return (await get_runtime().tenant_for_setup(tid)).config


async def _comment_limits(tid: UUID) -> CommentsConfig:
    """Display limits; a tenant still without an active config can open its conversations."""
    try:
        return (await _comment_config(tid)).comments
    except ConfigError:
        return CommentsConfig()


async def _comment_context(tid: UUID):
    from agentplatform.runtime import get_runtime
    tr = await get_runtime().tenant_for_setup(tid)
    return tr.config, tr.package.manifest


async def _media_account(conn) -> dict | None:
    return await (await conn.execute("select * from channel_accounts where channel = 'instagram' order by id limit 1")).fetchone()


async def _funnel(conn, tid: UUID, won_stage: str) -> dict:
    rows = await (await conn.execute(
        """select a.media_id, count(*) as comments,
                  count(*) filter (where a.attention and a.resolved_at is null) as attention,
                  count(o.id) filter (where o.status = 'sent') as directs,
                  count(distinct a.conversation_id) filter (where o.status = 'sent' and exists (
                     select 1 from messages m where m.conversation_id = a.conversation_id and m.direction = 'in'
                     and m.kind <> 'comment' and m.provider_ts > o.sent_at)) as replied,
                  count(distinct a.conversation_id) filter (where exists (
                     select 1 from lead_forms f where f.lead_id = l.id and f.submitted_at is not null)) as forms,
                  count(distinct a.conversation_id) filter (where l.stage = %s) as won
           from comment_actions a left join outbox o on o.id = a.private_reply_outbox_id
           left join conversations c on c.id = a.conversation_id left join leads l on l.contact_id = c.contact_id
           where a.tenant_id = %s and a.decision <> 'ignored_self' group by a.media_id""", (won_stage, tid))).fetchall()
    return {r["media_id"]: r for r in rows}


_NO_FUNNEL = {"comments": 0, "attention": 0, "directs": 0, "replied": 0, "forms": 0, "won": 0}


def _settings_form(row: dict | None) -> dict:
    r = row or {}
    return {"default_active": bool(r.get("default_active")), "default_mode": r.get("default_mode") or "both",
            "trigger_kind": r.get("trigger_kind") or "keyword", "keywords": list(r.get("keywords") or []),
            "public_variants": list(r.get("public_variants") or []), "private_variants": dict(r.get("private_variants") or {}),
            "repeat_variants": list(r.get("repeat_variants") or []), "hide_spam": bool(r.get("hide_spam"))}


async def _posts_page(request: Request, user: dict, tid: UUID, *, form: dict | None = None, problems=(), saved: str | None = None,
                      notice: str | None = None, status_code: int = 200, questions: list[str] | None = None, question_problems=()) -> HTMLResponse:
    from agentplatform.core.comments.settings import effective, effective_for, load_settings
    from agentplatform.core.orchestration.conversations import utcnow
    cfg, manifest = await _comment_context(tid)
    async with tenant_tx(tid) as conn:
        account = await _media_account(conn)
        settings = await load_settings(conn, account["id"]) if account else None
        dm = await load_scope(conn, account["id"]) if account else None
        rows = await (await conn.execute(
            "select * from channel_media where media_kind <> 'story' or posted_at > now() - interval '1 day' "
            "order by coalesce(posted_at, created_at) desc limit 60")).fetchall()
        funnel = await _funnel(conn, tid, manifest.won_stage)
        media = []
        for r in rows:
            media.append({**r, "effective": await effective_for(conn, r["channel_account_id"], r, utcnow()),
                          "funnel": funnel.get(r["external_media_id"], _NO_FUNNEL)})
    if form is None:
        form = _settings_form(settings)
        if not settings and manifest.comment_templates:      # first visit: the package's texts, ready to review and save
            t = manifest.comment_templates[0]
            form.update(keywords=list(t.keywords), public_variants=list(t.public_variants), private_variants=dict(t.private_variants),
                        repeat_variants=list(t.repeat_variants))
    return _page(request, "posts.html", {"user": user, "account": account, "dm": dm, "form": form, "media": media, "problems": problems,
                                         "saved": saved, "notice": notice, "templates": manifest.comment_templates,
                                         "min_public": cfg.comments.min_public_variants,
                                         "questions": questions if questions is not None else list((dm or {}).get("quick_questions") or manifest.quick_questions),
                                         "question_problems": question_problems, "q_max": cfg.comments.quick_questions_max,
                                         "q_chars": cfg.comments.quick_question_max_chars,
                                         "default": effective(settings, None, utcnow())}, status_code)


@router.get("/posts", response_class=HTMLResponse)
async def posts_page(request: Request, saved: str | None = None, notice: str | None = None):
    user = require(request, "tenant_owner", "seller")
    return await _posts_page(request, user, _tid(user), saved=saved, notice=notice)


@router.post("/posts/sync")
async def posts_sync(request: Request):
    from agentplatform.core.comments.catalog import CATALOG_ERRORS, accounts, sync_account
    from agentplatform.runtime import get_runtime
    user = await require_post(request, "tenant_owner", "seller")
    tid = _tid(user)
    cfg = await _comment_config(tid)
    n, failed = 0, False
    for channel, account in await accounts(tid):
        adapter = get_runtime().adapters.get(channel)
        if adapter is None or not hasattr(adapter, "list_media"):
            continue
        try:
            if adapter.capabilities().mixed_inbox:
                await snapshot_contacts(tid, adapter, account, cfg.comments)
            n += await sync_account(tid, adapter, account, cfg.comments)
        except CATALOG_ERRORS:
            failed = True
    query = {"notice": "A Meta não respondeu agora. A lista tenta de novo sozinha a cada 5 minutos."} if failed else {"saved": f"{n} publicações atualizadas."}
    return RedirectResponse("/console/posts?" + urlencode(query), status_code=303)


@router.post("/posts/direct")
async def posts_direct(request: Request, scope: str = Form(...)):
    user = await require_post(request, "tenant_owner", "seller")
    if scope not in SCOPES:
        raise HTTPException(400)
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        account = await _media_account(conn)
        if not account:
            raise HTTPException(400)
        await conn.execute(
            """insert into dm_settings (channel_account_id, tenant_id, scope, updated_by) values (%s,%s,%s,%s)
               on conflict (channel_account_id) do update set scope=excluded.scope, updated_by=excluded.updated_by, updated_at=now()""",
            (account["id"], tid, scope, UUID(user["id"])))
        await _audit(conn, tid, UUID(user["id"]), "dm.scope", str(account["id"]), {"scope": scope})
    return RedirectResponse("/console/posts?" + urlencode({"saved": "Atendimento no direct salvo."}), status_code=303)


@router.post("/posts/direct/questions", response_class=HTMLResponse)
async def posts_direct_questions(request: Request, questions: str = Form(""), action: str = Form("publish")):
    """Questions offered when someone opens a new direct. A tap reaches the assistant as a message, so they only go up when it can answer."""
    from agentplatform.core.comments.catalog import CATALOG_ERRORS, accounts
    from agentplatform.core.comments.settings import render_text, validate_questions
    from agentplatform.runtime import get_runtime
    user = await require_post(request, "tenant_owner", "seller")
    tid = _tid(user)
    tr = await get_runtime().tenant_for_setup(tid)
    typed = [q.strip() for q in questions.splitlines() if q.strip()]
    remove = action == "remove"
    if not remove:
        problems = validate_questions(tr.config, typed, tr.allowed_urls)
        if problems:
            return await _posts_page(request, user, tid, questions=typed, question_problems=problems, status_code=400)
    async with tenant_tx(tid) as conn:
        row = await _media_account(conn)
        if not row:
            raise HTTPException(400)
        dm = await load_scope(conn, row["id"])
    if not remove and not tr.policy().live:
        return await _posts_page(request, user, tid, questions=typed, status_code=400,
                                 notice="O assistente ainda não está ligado nesta conta: quem tocasse em uma pergunta ficaria sem resposta.")
    if not remove and dm["scope"] != "all" and not dm["contacts_synced_at"]:
        return await _posts_page(request, user, tid, questions=typed, status_code=400,
                                 notice="Espere a lista de quem já conversa com a conta ficar pronta: até lá, quem tocasse em uma pergunta ficaria sem resposta.")
    adapter = get_runtime().adapters.get("instagram")
    account = next((a for channel, a in await accounts(tid) if channel == "instagram"), None)
    if account is None or not hasattr(adapter, "set_ice_breakers"):
        return await _posts_page(request, user, tid, questions=typed, status_code=400, notice="Conecte a conta em Canais para publicar as perguntas.")
    seller = tr.config.conversation.persona.seller_display_name
    try:
        await adapter.set_ice_breakers(account, [] if remove else [render_text(q, seller) for q in typed])
    except CATALOG_ERRORS as e:
        log.warning("quick questions refused for tenant %s: %s", tid, type(e).__name__)
        return await _posts_page(request, user, tid, questions=typed, status_code=502, notice="A Meta não aceitou agora. Nada mudou no Instagram; tente de novo em alguns minutos.")
    async with tenant_tx(tid) as conn:
        await conn.execute(
            """insert into dm_settings (channel_account_id, tenant_id, quick_questions, quick_questions_published_at, updated_by)
               values (%(a)s,%(t)s,%(q)s,case when %(on)s then now() end,%(u)s)
               on conflict (channel_account_id) do update set quick_questions=excluded.quick_questions,
                 quick_questions_published_at=excluded.quick_questions_published_at, updated_by=excluded.updated_by, updated_at=now()""",
            {"a": row["id"], "t": tid, "q": json.dumps(typed), "on": not remove, "u": UUID(user["id"])})
        await _audit(conn, tid, UUID(user["id"]), "dm.quick_questions", str(row["id"]), {"published": len(typed) if not remove else 0})
    return RedirectResponse("/console/posts?" + urlencode({"saved": "Perguntas retiradas do Instagram." if remove else "Perguntas publicadas no Instagram."}), status_code=303)


@router.post("/posts/direct/relist")
async def posts_direct_relist(request: Request):
    """Read the list of who already talks to the account again, e.g. when the provider's approval makes the whole list visible."""
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        account = await _media_account(conn)
        if not account:
            raise HTTPException(400)
        await restart_snapshot(conn, tid, account["id"])
        await _audit(conn, tid, UUID(user["id"]), "dm.relist", str(account["id"]), {})
    return RedirectResponse("/console/posts?" + urlencode({"saved": "Lista reiniciada. Ela é refeita sozinha em até 5 minutos, e ninguém novo é atendido até terminar."}),
                            status_code=303)


@router.post("/posts/settings", response_class=HTMLResponse)
async def posts_settings(request: Request, default_active: str = Form("0"), default_mode: str = Form("both"),
                         trigger_kind: str = Form("keyword"), keywords: str = Form(""), public_variants: str = Form(""),
                         private_a: str = Form(""), private_b: str = Form(""), repeat_variants: str = Form(""),
                         hide_spam: str = Form(""), use_template: str = Form("")):
    from agentplatform.core.comments.settings import load_settings, validate
    user = await require_post(request, "tenant_owner", "seller")
    tid = _tid(user)
    cfg, manifest = await _comment_context(tid)
    if default_mode not in ("register", "public", "private", "both") or trigger_kind not in ("keyword", "any", "intent"):
        raise HTTPException(400)
    form = {"default_active": default_active == "1", "default_mode": default_mode, "trigger_kind": trigger_kind,
            "keywords": _words(keywords), "public_variants": _lines(public_variants), "private_variants": _variants(private_a, private_b),
            "repeat_variants": _lines(repeat_variants), "hide_spam": hide_spam == "1"}
    template = next((t for t in manifest.comment_templates if t.id == use_template), None)
    if template:          # fills the form only; nothing changes until saved
        form.update(keywords=list(template.keywords), public_variants=list(template.public_variants),
                    private_variants=dict(template.private_variants), repeat_variants=list(template.repeat_variants))
        return await _posts_page(request, user, tid, form=form, notice="Modelo preenchido. Revise e clique em Salvar padrão.")
    problems = validate(cfg, mode=default_mode if form["default_active"] else "off", trigger_kind=trigger_kind, keywords=form["keywords"],
                        public=form["public_variants"], private=form["private_variants"], repeat=form["repeat_variants"],
                        allowed_urls=[*manifest.allowed_urls, *cfg.conversation.allowed_urls])
    if problems:
        return await _posts_page(request, user, tid, form=form, problems=problems, status_code=400)
    async with tenant_tx(tid) as conn:
        account = await _media_account(conn)
        if not account:
            raise HTTPException(400)
        prev = await load_settings(conn, account["id"])
        await conn.execute(
            """insert into comment_settings (channel_account_id, tenant_id, default_active, default_mode, trigger_kind, keywords, public_variants,
                                             private_variants, repeat_variants, hide_spam, active_since, updated_by, updated_at)
               values (%(ca)s,%(t)s,%(on)s,%(mode)s,%(tk)s,%(kw)s,%(pub)s,%(pri)s,%(rep)s,%(hide)s,case when %(on)s then now() end,%(u)s,now())
               on conflict (channel_account_id) do update set default_active=%(on)s, default_mode=%(mode)s, trigger_kind=%(tk)s,
                 keywords=%(kw)s, public_variants=%(pub)s, private_variants=%(pri)s, repeat_variants=%(rep)s, hide_spam=%(hide)s,
                 active_since=case when %(on)s and not comment_settings.default_active then now() else comment_settings.active_since end,
                 updated_by=%(u)s, updated_at=now()""",
            {"ca": account["id"], "t": tid, "on": form["default_active"], "mode": default_mode, "tk": trigger_kind, "kw": form["keywords"],
             "pub": form["public_variants"], "pri": json.dumps(form["private_variants"]), "rep": form["repeat_variants"],
             "hide": form["hide_spam"], "u": UUID(user["id"])})
        await _audit(conn, tid, UUID(user["id"]), "comments.default", str(account["id"]),
                     {"active": form["default_active"], "mode": default_mode, "was_active": bool(prev and prev["default_active"])})
    return RedirectResponse("/console/posts?" + urlencode({"saved": "Padrão salvo."}), status_code=303)


def _date(v) -> str:
    return ui.dt(v, "%Y-%m-%d") if v else ""


async def _post_page(request: Request, user: dict, tid: UUID, media_id: UUID, *, form: dict | None = None, problems=(),
                     saved: str | None = None, status_code: int = 200) -> HTMLResponse:
    from agentplatform.core.comments.settings import effective, effective_for, load_settings
    from agentplatform.core.orchestration.conversations import utcnow
    cfg, manifest = await _comment_context(tid)
    async with tenant_tx(tid) as conn:
        m = await (await conn.execute("select * from channel_media where id=%s", (media_id,))).fetchone()
        if not m:
            raise HTTPException(404)
        auto = await (await conn.execute("select * from media_automations where media_id=%s", (media_id,))).fetchone()
        settings = await load_settings(conn, m["channel_account_id"])
        eff = await effective_for(conn, m["channel_account_id"], m, utcnow())
        funnel = (await _funnel(conn, tid, manifest.won_stage)).get(m["external_media_id"], _NO_FUNNEL)
        comments = await _comment_rows(conn, tid, media=m["external_media_id"], limit=30)
    a = auto or {}
    form = form or {"mode": a.get("mode") or "default", "trigger_kind": a.get("trigger_kind") or "", "keywords": list(a.get("keywords") or []),
                    "public_variants": list(a.get("public_variants") or []), "private_variants": dict(a.get("private_variants") or {}),
                    "valid_from": _date(a.get("valid_from")), "valid_to": _date(a.get("valid_to")),
                    "context_note": a.get("context_note_masked") or "", "context_slots": dict(a.get("context_slots") or {})}
    specs = {s.name: s for s in manifest.slots}
    default_mode = effective(settings, None, utcnow()).mode
    return _page(request, "post.html", {
        "user": user, "m": m, "eff": eff, "form": form, "problems": problems, "saved": saved, "funnel": funnel, "comments": comments,
        "inherited": _settings_form(settings), "default_label": ui.mode_label(default_mode), "templates": manifest.comment_templates,
        "context_slots": [specs[n] for n in manifest.media_context_slots if specs[n].type == "enum"],
        "backfill_days": cfg.comments.backfill_window_days, "back": f"/console/posts/{media_id}"}, status_code)


@router.get("/posts/{media_id}", response_class=HTMLResponse)
async def post_page(request: Request, media_id: UUID, saved: str | None = None):
    user = require(request, "tenant_owner", "seller")
    return await _post_page(request, user, _tid(user), media_id, saved=saved)


def _day(v: str, end: bool = False):
    from datetime import datetime, time
    from zoneinfo import ZoneInfo
    if not v:
        return None
    try:
        return datetime.combine(datetime.strptime(v, "%Y-%m-%d").date(), time(23, 59, 59) if end else time(0),
                                ZoneInfo(get_settings().display_timezone))
    except ValueError:
        raise HTTPException(400) from None


@router.post("/posts/{media_id}", response_class=HTMLResponse)
async def post_save(request: Request, media_id: UUID):
    from agentplatform.core.comments.settings import load_settings, validate
    from agentplatform.core.security.pii import mask
    user = await require_post(request, "tenant_owner", "seller")
    tid = _tid(user)
    cfg, manifest = await _comment_context(tid)
    f = await request.form()

    def field(name: str) -> str:
        return str(f.get(name) or "")

    mode, trigger = field("mode") or "default", field("trigger_kind") or None
    if mode not in ("default", "off", "register", "public", "private", "both") or trigger not in (None, "keyword", "any", "intent"):
        raise HTTPException(400)
    allowed = {n: set(s.values) for n in manifest.media_context_slots for s in manifest.slots if s.name == n}
    slots = {n: field(f"slot_{n}") for n in allowed if field(f"slot_{n}") in allowed[n]}
    form = {"mode": mode, "trigger_kind": trigger or "", "keywords": _words(field("keywords")), "public_variants": _lines(field("public_variants")),
            "private_variants": _variants(field("private_a"), field("private_b")), "valid_from": field("valid_from"),
            "valid_to": field("valid_to"), "context_note": field("context_note").strip(), "context_slots": slots}
    template = next((t for t in manifest.comment_templates if t.id == field("use_template")), None)
    if template:          # fills the form only; nothing changes until saved
        form.update(keywords=list(template.keywords), public_variants=list(template.public_variants), private_variants=dict(template.private_variants))
        return await _post_page(request, user, tid, media_id, form=form, saved="Modelo preenchido. Revise e clique em Salvar.")
    async with tenant_tx(tid) as conn:
        m = await (await conn.execute("select * from channel_media where id=%s", (media_id,))).fetchone()
        if not m:
            raise HTTPException(404)
        settings = _settings_form(await load_settings(conn, m["channel_account_id"]))
        effective_mode = settings["default_mode"] if mode == "default" and settings["default_active"] else ("off" if mode == "default" else mode)
        problems = validate(cfg, mode=effective_mode, trigger_kind=trigger or settings["trigger_kind"],
                            keywords=form["keywords"] or settings["keywords"], public=form["public_variants"] or settings["public_variants"],
                            private=form["private_variants"] or settings["private_variants"], repeat=[], allowed_urls=[*manifest.allowed_urls, *cfg.conversation.allowed_urls])
        if not problems:
            prev = await (await conn.execute("select mode from media_automations where media_id=%s", (media_id,))).fetchone()
            was_on = bool(prev) and prev["mode"] not in ("default", "off")
            await conn.execute(
                """insert into media_automations (media_id, tenant_id, mode, trigger_kind, keywords, public_variants, private_variants,
                                                  context_note_masked, context_slots, valid_from, valid_to, active_since, updated_by, updated_at)
                   values (%(m)s,%(t)s,%(mode)s,%(tk)s,%(kw)s,%(pub)s,%(pri)s,%(note)s,%(sl)s,%(vf)s,%(vt)s,now(),%(u)s,now())
                   on conflict (media_id) do update set mode=%(mode)s, trigger_kind=%(tk)s, keywords=%(kw)s, public_variants=%(pub)s,
                     private_variants=%(pri)s, context_note_masked=%(note)s, context_slots=%(sl)s, valid_from=%(vf)s, valid_to=%(vt)s,
                     active_since=case when %(was_on)s then media_automations.active_since else now() end, updated_by=%(u)s, updated_at=now()""",
                {"m": media_id, "t": tid, "mode": mode, "tk": trigger, "kw": form["keywords"], "pub": form["public_variants"],
                 "pri": json.dumps(form["private_variants"]), "note": mask(form["context_note"]).text[:cfg.comments.origin_max_chars] or None,
                 "sl": json.dumps(slots), "vf": _day(form["valid_from"]), "vt": _day(form["valid_to"], end=True), "u": UUID(user["id"]),
                 "was_on": was_on})
            await _audit(conn, tid, UUID(user["id"]), "comments.post", m["external_media_id"], {"mode": mode, "trigger": trigger})
    if problems:
        return await _post_page(request, user, tid, media_id, form=form, problems=problems, status_code=400)
    return RedirectResponse(f"/console/posts/{media_id}?" + urlencode({"saved": "Post salvo."}), status_code=303)


@router.post("/posts/{media_id}/backfill")
async def post_backfill(request: Request, media_id: UUID):
    from datetime import timedelta

    from agentplatform.core.comments.settings import effective_for
    from agentplatform.core.orchestration.conversations import utcnow
    from agentplatform.jobs.app import defer_jobs
    user = await require_post(request, "tenant_owner", "seller")
    tid = _tid(user)
    cfg = await _comment_config(tid)
    now = utcnow()
    async with tenant_tx(tid) as conn:
        m = await (await conn.execute("select * from channel_media where id=%s", (media_id,))).fetchone()
        if not m:
            raise HTTPException(404)
        if (await effective_for(conn, m["channel_account_id"], m, now)).mode == "off":
            return RedirectResponse(f"/console/posts/{media_id}", status_code=303)
        await conn.execute(
            """insert into media_automations (media_id, tenant_id, backfill_since, updated_by) values (%s,%s,%s,%s)
               on conflict (media_id) do update set backfill_since=excluded.backfill_since, updated_by=excluded.updated_by, updated_at=now()""",
            (media_id, tid, now - timedelta(days=cfg.comments.backfill_window_days), UUID(user["id"])))
        await _audit(conn, tid, UUID(user["id"]), "comments.backfill", m["external_media_id"])
    await defer_jobs([("comment_backfill", {"tenant_id": str(tid), "media_id": m["external_media_id"]}, {})])
    return RedirectResponse(f"/console/posts/{media_id}?" + urlencode({"saved": "Comentários enviados para resposta. Em instantes aparecem abaixo."}), status_code=303)


async def _comment_rows(conn, tid: UUID, *, media: str | None = None, only: str = "all", limit: int = 100) -> list[dict]:
    where = {"attention": "a.attention and a.resolved_at is null", "all": "true",
             "noreply": "o.status = 'sent' and not replied.yes"}.get(only, "true")
    return await (await conn.execute(
        f"""select a.*, m.caption_masked, (o.status = 'sent' and not replied.yes) as no_reply
            from comment_actions a
            left join channel_media m on m.tenant_id = a.tenant_id and m.external_media_id = a.media_id
            left join outbox o on o.id = a.private_reply_outbox_id
            left join lateral (select exists (select 1 from messages x where x.conversation_id = a.conversation_id and x.direction = 'in'
                               and x.kind <> 'comment' and x.provider_ts > coalesce(o.sent_at, a.created_at)) as yes) replied on true
            where a.tenant_id = %s and a.decision <> 'ignored_self' and {where} and (%s::text is null or a.media_id = %s)
            order by coalesce(a.comment_ts, a.created_at) desc limit %s""", (tid, media, media, limit))).fetchall()


@router.get("/comments", response_class=HTMLResponse)
async def comments_page(request: Request, f: str = "attention", media: str | None = None):
    user = require(request, "tenant_owner", "seller")
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        rows = await _comment_rows(conn, tid, media=media, only=f)
        attention = (await (await conn.execute(
            "select count(*) as n from comment_actions where tenant_id=%s and attention and resolved_at is null", (tid,))).fetchone())["n"]
    return _page(request, "comments.html", {"user": user, "comments": rows, "f": f, "media": media, "attention": attention,
                                            "back": f"/console/comments?f={f}"})


@router.post("/comments/{action_id}/allow")
async def comment_allow(request: Request, action_id: UUID, next: str = Form("")):
    """The seller says this person may be served: the flag changes and this comment goes through the automation again."""
    from agentplatform.jobs.app import defer_jobs
    user = await require_post(request, "tenant_owner", "seller")
    tid = _tid(user)
    event = None
    async with tenant_tx(tid) as conn:
        a = await (await conn.execute(
            """select a.*, m.channel_account_id from comment_actions a join channel_media m on m.tenant_id=a.tenant_id and m.external_media_id=a.media_id
               where a.id=%s and a.tenant_id=%s and a.decision in ('known_contact','blocked_contact')""", (action_id, tid))).fetchone()
        if a:
            await set_flag(conn, tid, a["channel_account_id"], a["author_external_id"], "allowed", "console", UUID(user["id"]))
            event = await (await conn.execute("select id from inbound_events where tenant_id=%s and idempotency_key=%s",
                                              (tid, f"cmt:{a['comment_id']}"))).fetchone()
            if event:
                await conn.execute("delete from comment_actions where id=%s", (action_id,))
            await _audit(conn, tid, UUID(user["id"]), "dm.contact_allowed", str(action_id))
    if event:
        await defer_jobs([("comment", {"tenant_id": str(tid), "event_id": event["id"]}, {})])
    back = next if next.startswith("/console/") and "//" not in next else "/console/comments"
    return RedirectResponse(back, status_code=303)


@router.post("/conversations/{cid}/contact")
async def conversation_contact(request: Request, cid: UUID, status: str = Form(...)):
    """Never serve this person, or take that back. Never serving also takes the assistant out of the conversation right away."""
    user = await require_post(request, "tenant_owner", "seller")
    if status not in ("blocked", "allowed"):
        raise HTTPException(400)
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        c = await (await conn.execute(
            """select c.channel_account_id, c.ownership, ci.external_user_id from conversations c
               join contact_identities ci on ci.contact_id=c.contact_id where c.id=%s limit 1""", (cid,))).fetchone()
        if not c:
            raise HTTPException(404)
        await set_flag(conn, tid, c["channel_account_id"], c["external_user_id"], status, "console", UUID(user["id"]))
        if status == "blocked" and c["ownership"] != "HUMAN":
            await transition_ownership(conn, tid, cid, "HUMAN", cause="console:blocked", actor=UUID(user["id"]))
        await _audit(conn, tid, UUID(user["id"]), "dm.contact_" + status, str(cid))
    return RedirectResponse(f"/console/conversations/{cid}", status_code=303)


@router.post("/comments/{action_id}/resolve")
async def comment_resolve(request: Request, action_id: UUID, next: str = Form("")):
    user = await require_post(request, "tenant_owner", "seller")
    tid = _tid(user)
    async with tenant_tx(tid) as conn:
        await conn.execute("update comment_actions set resolved_at=now(), resolved_by=%s where id=%s and tenant_id=%s",
                           (UUID(user["id"]), action_id, tid))
        await _audit(conn, tid, UUID(user["id"]), "comments.resolve", str(action_id))
    back = next if next.startswith("/console/") and "//" not in next else "/console/comments"
    return RedirectResponse(back, status_code=303)


# ======================= channels: Facebook Login reconnection (F1-META-03) =======================
@router.get("/channels", response_class=HTMLResponse)
async def channels_page(request: Request, ok: str | None = None, error: str | None = None, warn: str | None = None):
    user = require(request, "tenant_owner")
    async with tenant_tx(_tid(user)) as conn:
        accounts = await (await conn.execute(
            "select id, channel, external_account_id, external_page_id, token_status, token_last_validated_at, scopes, "
            "(token_enc is not null) as has_token from channel_accounts order by channel")).fetchall()
        ks = await (await conn.execute(
            "select scope_ref, is_on, reason from kill_switches where tenant_id=%s and scope='channel'", (_tid(user),))).fetchall()
    return _page(request, "channels.html", {"user": user, "accounts": accounts, "ks": ks, "ok": ok, "error": error,
                                            "warn": warn, "app_configured": bool(get_settings().meta_app_id and get_settings().meta_app_secret)})


def _oauth_secret() -> bytes:
    s = get_settings()
    return (s.meta_app_secret.get_secret_value() if s.meta_app_secret else "").encode() + b"|" + (
        s.console_session_secret.get_secret_value() if s.console_session_secret else "dev-only").encode()


@router.post("/channels/{account_id}/reconnect")
async def channel_reconnect(request: Request, account_id: UUID):
    from agentplatform.core.channels.instagram import oauth
    user = await require_post(request, "tenant_owner")
    tid = _tid(user)
    s = get_settings()
    if not (s.meta_app_id and s.meta_app_secret):
        raise HTTPException(400, "META_APP_ID/META_APP_SECRET não configurados")
    nonce = oauth.new_nonce()
    async with tenant_tx(tid) as conn:
        acc = await (await conn.execute("select id from channel_accounts where id=%s and channel='instagram'", (account_id,))).fetchone()
        if not acc:
            raise HTTPException(404)
        await conn.execute("insert into oauth_states (nonce, tenant_id, channel_account_id, created_by) values (%s,%s,%s,%s)",
                           (nonce, tid, account_id, UUID(user["id"])))
        await _audit(conn, tid, UUID(user["id"]), "channel.reconnect_start", str(account_id))
    url = oauth.dialog_url(s.meta_app_id, s.public_base_url.rstrip("/") + oauth.CALLBACK_PATH,
                           oauth.sign_state(_oauth_secret(), tid, account_id, nonce), s.meta_oauth_scopes, s.meta_graph_version,
                           config_id=s.meta_oauth_config_id)
    resp = RedirectResponse(url, status_code=303)
    # Lax: must survive the top-level redirect back from facebook.com (the strict session cookie does not)
    resp.set_cookie("ap_oauth", nonce, httponly=True, samesite="lax", max_age=oauth.STATE_TTL_S,
                    secure=s.public_base_url.startswith("https://"), path="/console/channels")
    return resp


OAUTH_TRANSPORT = None      # tests inject an httpx.MockTransport


@router.get("/channels/instagram/callback")
async def channel_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    """No session needed (SameSite=strict is not sent on the redirect from Meta): the signed single-use state binds
    tenant, account and the initiating browser (ap_oauth cookie)."""
    from agentplatform.core.channels.instagram import oauth
    s = get_settings()
    try:
        st = oauth.read_state(_oauth_secret(), state)
        if not request.cookies.get("ap_oauth") or request.cookies.get("ap_oauth") != st["n"]:
            raise oauth.OAuthError("browser_mismatch")
        if error or not code:
            raise oauth.OAuthError("denied_by_user")
        tid, account_id = UUID(st["t"]), UUID(st["a"])
        async with tenant_tx(tid) as conn:
            row = await (await conn.execute(
                """update oauth_states set used_at = now() where nonce=%s and tenant_id=%s and channel_account_id=%s
                   and used_at is null and created_at > now() - make_interval(secs => %s) returning created_by""",
                (st["n"], tid, account_id, oauth.STATE_TTL_S))).fetchone()
            if not row:
                raise oauth.OAuthError("state_used_or_unknown")
            acc = await (await conn.execute(
                "select external_page_id, external_account_id, token_enc is null as first from channel_accounts where id=%s",
                (account_id,))).fetchone()
        pt = await oauth.exchange_code(code, app_id=s.meta_app_id or "", app_secret=s.meta_app_secret.get_secret_value()
                                       if s.meta_app_secret else "", redirect_uri=s.public_base_url.rstrip("/") + oauth.CALLBACK_PATH,
                                       page_id=acc["external_page_id"], ig_account_id=acc["external_account_id"],
                                       graph_version=s.meta_graph_version, transport=OAUTH_TRANSPORT,
                                       adopt_single=acc["first"])
        async with tenant_tx(tid) as conn:
            await oauth.store_page_token(conn, tid, account_id, pt, row["created_by"], adopt_ids=acc["first"])
        registry.invalidate_kill_switches()
        # without the page subscription Meta delivers no webhook; the 10-min job retries if this attempt fails
        fields = {f.strip() for f in s.meta_page_subscribed_fields.split(",") if f.strip()}
        try:
            sub = await check_subscription(pt.page_id, pt.token, fields, s.meta_app_id, s.meta_graph_version, OAUTH_TRANSPORT)
        except httpx.HTTPError:
            sub = {"ok": False}
        resp = _back_to_channels("ok=1" if sub["ok"] else "ok=1&warn=subscribe_failed")
        resp.delete_cookie("ap_oauth", path="/console/channels")
    except oauth.OAuthError as e:
        # the browser-binding cookie is kept on errors, so a forged callback cannot cancel a legitimate flow
        resp = _back_to_channels(urlencode({"error": str(e)}))
    return resp


def _back_to_channels(query: str) -> HTMLResponse:
    """The redirect from Meta is a cross-site navigation, so a 303 would reach the channels page without the strict
    session cookie (login screen, result lost). A refresh started by this same-origin page carries the session."""
    url = escape(f"/console/channels?{query}")
    return HTMLResponse(f'<!doctype html><meta http-equiv="refresh" content="0;url={url}"><a href="{url}">Voltar ao console</a>',
                        headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})
