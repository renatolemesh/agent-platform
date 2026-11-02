"""Facebook Login reconnection of the Instagram page token (F1-META-03; plan 05 §2).

Flow: console -> dialog URL with a signed, single-use state -> Meta redirects to our callback with `code` ->
code -> short-lived user token -> long-lived user token -> /me/accounts -> page token of the configured page
(page tokens derived from a long-lived user token have no fixed expiry) -> stored encrypted; token status valid;
the channel kill switch is released only if token_guard turned it on (reason 'token_invalid').
API only; tokens never logged."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode
from uuid import UUID

import httpx
import psycopg
from psycopg import AsyncConnection

from agentplatform.core.security.keys import encrypt_field

CALLBACK_PATH = "/console/channels/instagram/callback"
STATE_TTL_S = 600


class OAuthError(Exception):
    pass


@dataclass(frozen=True)
class PageToken:
    page_id: str
    token: str
    scopes: list[str]
    ig_account_id: str | None = None


def _sig(secret: bytes, payload: str) -> str:
    return hmac.new(secret, f"oauth:{payload}".encode(), hashlib.sha256).hexdigest()


def sign_state(secret: bytes, tenant_id: UUID, account_id: UUID, nonce: str, now: float | None = None) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(
        {"t": str(tenant_id), "a": str(account_id), "n": nonce, "exp": int((now or time.time()) + STATE_TTL_S)}).encode()).decode()
    return f"{payload}.{_sig(secret, payload)}"


def read_state(secret: bytes, state: str, now: float | None = None) -> dict:
    if not state or "." not in state:
        raise OAuthError("state_missing")
    payload, sig = state.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sig(secret, payload)):
        raise OAuthError("state_bad_signature")
    data = json.loads(base64.urlsafe_b64decode(payload))
    if data["exp"] < (now or time.time()):
        raise OAuthError("state_expired")
    return data


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def dialog_url(app_id: str, redirect_uri: str, state: str, scopes: str, graph_version: str,
               config_id: str | None = None) -> str:
    """Facebook Login for Business apps may require a login configuration (config_id) instead of a scope list."""
    q = {"client_id": app_id, "redirect_uri": redirect_uri, "state": state, "response_type": "code"}
    q.update({"config_id": config_id} if config_id else {"scope": scopes})
    q = urlencode(q)
    return f"https://www.facebook.com/{graph_version}/dialog/oauth?{q}"


async def exchange_code(code: str, *, app_id: str, app_secret: str, redirect_uri: str, page_id: str | None,
                        ig_account_id: str | None, graph_version: str,
                        transport: httpx.AsyncBaseTransport | None = None, adopt_single: bool = False) -> PageToken:
    """adopt_single (first connection only): when the configured IDs match no granted page, take the one page the user
    granted that has an Instagram professional account; never used on reconnection (no silent account switch)."""
    async with httpx.AsyncClient(base_url=f"https://graph.facebook.com/{graph_version}", transport=transport, timeout=15) as c:
        r = await c.get("/oauth/access_token", params={"client_id": app_id, "client_secret": app_secret,
                                                       "redirect_uri": redirect_uri, "code": code})
        short = (r.json() or {}).get("access_token") if r.status_code < 400 else None
        if not short:
            raise OAuthError("code_exchange_failed")
        r = await c.get("/oauth/access_token", params={"grant_type": "fb_exchange_token", "client_id": app_id,
                                                       "client_secret": app_secret, "fb_exchange_token": short})
        long_lived = (r.json() or {}).get("access_token") if r.status_code < 400 else None
        if not long_lived:
            raise OAuthError("long_lived_exchange_failed")
        r = await c.get("/me/accounts", params={"fields": "id,name,access_token,instagram_business_account",
                                                "access_token": long_lived})
        pages = (r.json() or {}).get("data", []) if r.status_code < 400 else []
        page = next((p for p in pages if (page_id and p.get("id") == page_id)
                     or (ig_account_id and (p.get("instagram_business_account") or {}).get("id") == ig_account_id)), None)
        if page is None and adopt_single:
            with_ig = [p for p in pages if p.get("access_token") and (p.get("instagram_business_account") or {}).get("id")]
            if len(with_ig) > 1:
                raise OAuthError("select_one_page")   # ambiguous: grant only the page of this account
            page = with_ig[0] if with_ig else None
        if not page or not page.get("access_token"):
            raise OAuthError("page_not_granted")      # the user did not select the configured page in the dialog
        r = await c.get("/debug_token", params={"input_token": page["access_token"], "access_token": f"{app_id}|{app_secret}"})
        data = (r.json() or {}).get("data", {}) if r.status_code < 400 else {}
        if not data.get("is_valid"):
            raise OAuthError("page_token_invalid")
        return PageToken(page["id"], page["access_token"], list(data.get("scopes", [])),
                         (page.get("instagram_business_account") or {}).get("id"))


async def store_page_token(conn: AsyncConnection, tenant_id: UUID, account_id: UUID, pt: PageToken,
                           actor: UUID | None, adopt_ids: bool = False) -> None:
    if adopt_ids and pt.ig_account_id:     # first connection: the account takes the IDs of the granted page
        try:
            await conn.execute(
                "update channel_accounts set external_page_id = %s, external_account_id = %s where id = %s and tenant_id = %s",
                (pt.page_id, pt.ig_account_id, account_id, tenant_id))
        except psycopg.errors.UniqueViolation as e:   # this Instagram account is already connected elsewhere
            raise OAuthError("account_in_use") from e
    await conn.execute(
        """update channel_accounts set token_enc = %s, token_kind = 'page', token_status = 'valid', token_expires_at = null,
                  token_last_validated_at = now(), scopes = %s, external_page_id = coalesce(external_page_id, %s)
           where id = %s and tenant_id = %s""",
        (await encrypt_field(conn, tenant_id, pt.token, "channel_accounts.token"), pt.scopes, pt.page_id, account_id, tenant_id))
    released = await (await conn.execute(
        """update kill_switches set is_on = false, reason = 'reconnected', changed_by = %s, changed_at = now()
           where tenant_id = %s and scope = 'channel' and scope_ref = 'instagram' and is_on and reason in ('token_invalid', 'channel_error')
           returning id""", (actor, tenant_id))).fetchone()
    await conn.execute(
        "insert into audit_log (tenant_id, actor, actor_kind, action, target, after) values (%s,%s,'user','channel.reconnect',%s,%s)",
        (tenant_id, actor, str(account_id), json.dumps({"scopes": pt.scopes, "kill_switch_released": bool(released)})))
