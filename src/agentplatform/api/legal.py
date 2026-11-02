"""Public pages and callbacks the channel provider's review requires: privacy policy, data deletion instructions, and the signed
callbacks sent when someone removes the app. The page text is a draft for the data controller's legal review (docs/runbooks/go-live.md);
it stays unpublished (404) until the controller's name and contact are configured."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from agentplatform.core.db.pool import platform_tx
from agentplatform.settings import get_settings

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def parse_signed_request(signed_request: str, secret: str) -> dict | None:
    """Facebook Login signed_request: base64url(signature).base64url(payload), HMAC-SHA256 of the encoded payload."""
    def decode(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    try:
        sig, payload = signed_request.split(".", 1)
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(decode(sig), expected):
            return None
        data = json.loads(decode(payload))
    except ValueError:
        return None
    return data if isinstance(data, dict) and str(data.get("algorithm", "")).upper() == "HMAC-SHA256" else None


def _controller() -> dict:
    s = get_settings()
    if not (s.legal_controller_name and s.legal_contact_email):
        raise HTTPException(404)
    return {"controller": s.legal_controller_name, "contact": s.legal_contact_email}


async def _record(action: str, user_id: str, code: str) -> None:
    """Who asked is a provider id of the person who connected the account; only its hash is kept. The operator handles it (alert)."""
    async with platform_tx() as conn:
        await conn.execute(
            """insert into audit_log (actor_kind, action, target, after)
               values ('system', %s, %s, jsonb_build_object('user_hash', %s::text))""",
            (action, code, hashlib.sha256(user_id.encode()).hexdigest()))


def _verified(signed_request: str) -> dict:
    s = get_settings()
    data = parse_signed_request(signed_request, s.meta_app_secret.get_secret_value()) if s.meta_app_secret else None
    if not data or not data.get("user_id"):
        raise HTTPException(400, "invalid signed_request")
    return data


def _code(user_id: str) -> str:
    secret = get_settings().meta_app_secret.get_secret_value()
    return hmac.new(secret.encode(), f"deletion:{user_id}".encode(), hashlib.sha256).hexdigest()[:20]


def build_router(limiter) -> APIRouter:
    router = APIRouter()

    @router.get("/privacy", response_class=HTMLResponse)
    async def privacy(request: Request):
        return templates.TemplateResponse(request, "privacy.html", _controller())

    @router.get("/data-deletion", response_class=HTMLResponse)
    async def data_deletion(request: Request):
        return templates.TemplateResponse(request, "data_deletion.html", _controller())

    @router.get("/data-deletion/status/{code}", response_class=HTMLResponse)
    async def data_deletion_status(request: Request, code: str):
        if not await limiter.allow(request):
            raise HTTPException(429)
        async with platform_tx() as conn:
            row = await (await conn.execute(
                "select at from audit_log where action = 'meta.data_deletion' and target = %s order by at desc limit 1", (code,))).fetchone()
        if not row:
            raise HTTPException(404)
        return templates.TemplateResponse(request, "data_deletion.html", {**_controller(), "received_at": row["at"], "code": code})

    @router.post("/meta/data-deletion")
    async def meta_data_deletion(signed_request: str = Form(...)):
        user_id = str(_verified(signed_request)["user_id"])
        code = _code(user_id)
        await _record("meta.data_deletion", user_id, code)
        return JSONResponse({"url": f"{get_settings().public_base_url.rstrip('/')}/data-deletion/status/{code}", "confirmation_code": code})

    @router.post("/meta/deauthorize")
    async def meta_deauthorize(signed_request: str = Form(...)):
        user_id = str(_verified(signed_request)["user_id"])
        await _record("meta.deauthorize", user_id, _code(user_id))
        return {"ok": True}

    return router
