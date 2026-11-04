"""Operational jobs: token guard (Facebook Login page token: no fixed expiry, invalidated by events -> debug_token daily,
kill switch + alert on 190) and webhook subscription check (the Messenger Platform unsubscribes after ~1h of failures)."""
from __future__ import annotations

import logging
from uuid import UUID

import httpx
from psycopg import AsyncConnection

from agentplatform.core.config.registry import registry
from agentplatform.core.config.schema import ModelsConfig
from agentplatform.core.security.keys import decrypt_field

log = logging.getLogger(__name__)


async def token_guard(conn: AsyncConnection, tenant_id: UUID, app_id: str | None, app_secret: str | None,
                      graph_version: str = "v26.0", transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    rows = await (await conn.execute(
        "select id, channel, external_page_id, token_enc from channel_accounts where tenant_id=%s and channel='instagram'",
        (tenant_id,))).fetchall()
    out = []
    async with httpx.AsyncClient(base_url=f"https://graph.facebook.com/{graph_version}", transport=transport, timeout=10) as c:
        for r in rows:
            token = await decrypt_field(conn, tenant_id, r["token_enc"], "channel_accounts.token") if r["token_enc"] else None
            status, scopes = "unknown", []
            if not token:
                status = "invalid"
            elif app_id and app_secret:        # without app credentials this check knows nothing about the token
                try:
                    resp = await c.get("/debug_token", params={"input_token": token, "access_token": f"{app_id}|{app_secret}"})
                    body = resp.json() if resp.content else {}
                    data = body.get("data") if isinstance(body, dict) else None
                    if isinstance(data, dict) and data.get("is_valid") is True:      # only an explicit verdict counts: an outage
                        status, scopes = "valid", data.get("scopes", [])             # or a rate limit says nothing about the token
                    elif isinstance(data, dict) and data.get("is_valid") is False:
                        status = "invalid"
                except (httpx.HTTPError, ValueError) as e:
                    log.warning("token check inconclusive for tenant %s: %s", tenant_id, type(e).__name__)
            if status == "unknown":
                out.append({"account": str(r["id"]), "status": "unknown"})
                continue
            await conn.execute("update channel_accounts set token_status=%s, token_last_validated_at=now(), scopes=%s where id=%s",
                               (status, scopes or [], r["id"]))
            if status == "invalid":
                await registry.set_channel_stop(conn, tenant_id, "instagram", "token_invalid")
                log.error("instagram token invalid for tenant %s: channel kill switch ON, reconnection required", tenant_id)
            elif await registry.lift_channel_stop(conn, tenant_id, "instagram", ("token_invalid", "channel_error")):
                # a refusal that persists stops the channel again at the first send; one that passed (temporary restriction) ends here
                log.info("instagram token valid for tenant %s: channel kill switch lifted", tenant_id)
            registry.invalidate_kill_switches()
            out.append({"account": str(r["id"]), "status": status, "scopes": scopes})
    return out


async def missing_app_fields(app_id: str, app_secret: str, required: set[str], graph_version: str = "v26.0",
                             transport: httpx.AsyncBaseTransport | None = None) -> set[str] | None:
    """Fields of the provider's app dashboard that are not on. Without them the event is simply never delivered: a deleted or edited
    message, a thread another app owns, an ad link. None when the answer says nothing either way."""
    async with httpx.AsyncClient(base_url=f"https://graph.facebook.com/{graph_version}", transport=transport, timeout=10) as c:
        try:
            r = await c.get(f"/{app_id}/subscriptions", params={"access_token": f"{app_id}|{app_secret}"})
            subs = r.json().get("data") if r.status_code < 400 else None
        except (httpx.HTTPError, ValueError):
            return None
    if subs is None:
        return None
    have = {f.get("name") for s in subs if s.get("object") == "instagram" and s.get("active") for f in s.get("fields", [])}
    return required - have


async def zero_retention_models(transport: httpx.AsyncBaseTransport | None = None) -> set[str] | None:
    """Models OpenRouter can serve with zero data retention. The list changes by itself: a model can lose its only route, and from then on every call
    to it is refused. None when the answer says nothing either way."""
    async with httpx.AsyncClient(base_url="https://openrouter.ai/api/v1", transport=transport, timeout=15) as c:
        try:
            r = await c.get("/endpoints/zdr")
            routes = r.json().get("data") if r.status_code < 400 else None
        except (httpx.HTTPError, ValueError):
            return None
    return {e.get("model_id") or e.get("model") for e in routes} if routes else None


def models_without_route(models: ModelsConfig, routes: set[str]) -> set[str]:
    nodes = (models.analyze_turn, models.respond, models.output_judge, models.input_classifier, models.summarize, models.comment_intent, models.image_describe)
    return ({m for n in nodes if n for m in (n.primary, *n.fallbacks)} | {models.embedding.model, models.transcription.model}) - routes


async def check_subscription(page_id: str, page_token: str, required_fields: set[str], app_id: str | None = None,
                             graph_version: str = "v26.0", transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """GET /{page}/subscribed_apps; (re)subscribe if our app is missing or lost fields (other apps on the page don't count)."""
    async with httpx.AsyncClient(base_url=f"https://graph.facebook.com/{graph_version}", transport=transport, timeout=10) as c:
        r = await c.get(f"/{page_id}/subscribed_apps", params={"access_token": page_token})
        apps = (r.json() or {}).get("data", []) if r.status_code < 400 else []
        have = {f for a in apps if app_id is None or a.get("id") == app_id for f in a.get("subscribed_fields", [])}
        if required_fields <= have:
            return {"ok": True, "resubscribed": False}
        r2 = await c.post(f"/{page_id}/subscribed_apps",
                          params={"subscribed_fields": ",".join(sorted(required_fields)), "access_token": page_token})
        ok = r2.status_code < 400 and bool((r2.json() or {}).get("success"))
        if not ok:
            log.warning("webhook subscription of page %s failed: %s %s", page_id, r2.status_code, r2.text[:300])
        return {"ok": ok, "resubscribed": True}


async def ensure_subscriptions(conn: AsyncConnection, tenant_id: UUID, fields: set[str], app_id: str | None,
                               graph_version: str = "v26.0", transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """Every connected Instagram page with a valid token stays subscribed to our app's webhooks (the Messenger Platform
    drops the subscription after ~1h of delivery failures)."""
    rows = await (await conn.execute(
        """select id, external_page_id, token_enc from channel_accounts where tenant_id=%s and channel='instagram'
           and token_enc is not null and token_status='valid' and external_page_id is not null""", (tenant_id,))).fetchall()
    out = []
    for r in rows:
        token = await decrypt_field(conn, tenant_id, r["token_enc"], "channel_accounts.token")
        try:
            res = await check_subscription(r["external_page_id"], token, fields, app_id, graph_version, transport)
        except httpx.HTTPError as e:
            res = {"ok": False, "resubscribed": False, "error": type(e).__name__}
        out.append({"account": str(r["id"]), **res})
    return out
