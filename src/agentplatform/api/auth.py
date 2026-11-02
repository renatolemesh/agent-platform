"""Console auth: scrypt password hashes, HMAC-signed session cookie, RBAC roles, CSRF tokens bound to the session,
TOTP second factor (plan 08 §7.2)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

import pyotp
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import HTTPException, Request
from markupsafe import Markup

from agentplatform.settings import get_settings

ROLES = ("platform_admin", "tenant_owner", "seller", "reviewer")
COOKIE = "ap_session"
PENDING_COOKIE = "ap_pending"          # password ok, second factor still due (short-lived)


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_password(pw: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, s, d = stored.split("$")
    dk = hashlib.scrypt(pw.encode(), salt=base64.b64decode(s), n=2 ** 14, r=8, p=1)
    return hmac.compare_digest(dk, base64.b64decode(d))


def _secret() -> bytes:
    s = get_settings().console_session_secret
    return (s.get_secret_value() if s else "dev-only").encode()


def _sign(data: dict, ttl_s: int, purpose: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({**data, "exp": int(time.time()) + ttl_s, "pur": purpose}).encode()).decode()
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _unsign(token: str | None, purpose: str) -> dict | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except ValueError:
        return None
    if data.get("exp", 0) <= time.time() or data.get("pur", "session") != purpose:
        return None
    return data


def make_session(user: dict, ttl_s: int = 8 * 3600) -> str:
    """`sid` is a per-login random id; the CSRF token is derived from it, so it dies with the session."""
    return _sign({**user, "sid": user.get("sid") or secrets.token_urlsafe(16)}, ttl_s, "session")


def read_session(token: str | None) -> dict | None:
    return _unsign(token, "session")


_alive: dict[str, tuple[float, bool]] = {}
_ALIVE_TTL_S = 10.0


async def session_alive(user: dict) -> bool:
    """The cookie says who logged in; only the database says that person can still get in, with that role. Checked on every console request, with a
    short cache so a screen with many requests does not hit the database for each."""
    from agentplatform.core.db.pool import platform_tx
    key = f"{user.get('id')}:{user.get('role')}"
    hit = _alive.get(key)
    if hit and time.monotonic() - hit[0] < _ALIVE_TTL_S:
        return hit[1]
    async with platform_tx() as conn:
        row = await (await conn.execute("select is_active, role from tenant_users where id=%s", (user.get("id"),))).fetchone()
    alive = bool(row and row["is_active"] and row["role"] == user.get("role"))
    _alive[key] = (time.monotonic(), alive)
    return alive


def forget_session(user_id: str) -> None:
    """Deactivated or role changed in this process: it counts now, not when the cache expires. Other processes follow within the TTL."""
    for key in [k for k in _alive if k.startswith(f"{user_id}:")]:
        _alive.pop(key, None)


def make_pending(user: dict, ttl_s: int = 300) -> str:
    return _sign(user, ttl_s, "pending_2fa")


def read_pending(token: str | None) -> dict | None:
    return _unsign(token, "pending_2fa")


ENROLL_PATHS = ("/console/totp", "/console/logout")


def require(request: Request, *roles: str) -> dict:
    user = read_session(request.cookies.get(COOKIE))
    if user is None:
        raise HTTPException(303, headers={"Location": "/console/login"})
    if user.get("must_enroll") and not request.url.path.startswith(ENROLL_PATHS):
        raise HTTPException(303, headers={"Location": "/console/totp"})
    if roles and user["role"] not in roles and user["role"] != "platform_admin":
        raise HTTPException(403)
    return user


# ----- CSRF (synchronizer token derived from the session id; checked on every console POST) -----
def csrf_token(user: dict | None) -> str:
    if not user or not user.get("sid"):
        return ""
    return hmac.new(_secret(), f"csrf:{user['sid']}".encode(), hashlib.sha256).hexdigest()


def csrf_input(user: dict | None) -> Markup:
    return Markup('<input type="hidden" name="csrf" value="{}">').format(csrf_token(user))


async def require_post(request: Request, *roles: str) -> dict:
    user = require(request, *roles)
    sent = request.headers.get("x-csrf-token") or str((await request.form()).get("csrf") or "")
    if not sent or not hmac.compare_digest(sent, csrf_token(user)):
        raise HTTPException(403, "csrf")
    return user


# ----- TOTP -----
def totp_required(role: str) -> bool:
    return role in {r.strip() for r in get_settings().console_totp_required_roles.split(",") if r.strip()}


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, account: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=get_settings().console_totp_issuer)


def totp_check(secret: str, code: str, last_step: int | None, now: float | None = None) -> int | None:
    """Returns the accepted time step (to store, blocking replay) or None. Accepts +-1 step of clock drift."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return None
    t = pyotp.TOTP(secret)
    now = time.time() if now is None else now
    step = int(now // t.interval)
    for s in (step, step - 1, step + 1):
        if last_step is not None and s <= last_step:
            continue
        if hmac.compare_digest(t.generate_otp(s), code):
            return s
    return None


def _platform_totp_key() -> bytes:
    """Platform users have no tenant DEK; derive a dedicated key from the KEK (HKDF, separate info label)."""
    kek = get_settings().kek_b64
    if not kek:
        raise RuntimeError("KEK_B64 must be set")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"console-totp:platform").derive(
        base64.b64decode(kek.get_secret_value()))


async def seal_totp(conn, tenant_id, user_id, secret: str) -> bytes:
    from agentplatform.core.security.keys import crypto, encrypt_field
    ctx = f"tenant_users.totp:{user_id}"
    if tenant_id:
        return await encrypt_field(conn, tenant_id, secret, ctx)
    return crypto().encrypt(_platform_totp_key(), secret, context=ctx)


async def open_totp(conn, tenant_id, user_id, blob) -> str | None:
    from agentplatform.core.security.keys import crypto, decrypt_field
    if blob is None:
        return None
    ctx = f"tenant_users.totp:{user_id}"
    if tenant_id:
        return await decrypt_field(conn, tenant_id, blob, ctx)
    return crypto().decrypt(_platform_totp_key(), bytes(blob), context=ctx)
