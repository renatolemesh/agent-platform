"""Core tool: single-use, expiring pre-registration link (plan 04 §7). Fields come from the package; never CPF/RG/income."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.security.keys import encrypt_field

FORBIDDEN_FIELDS = {"cpf", "rg", "income", "renda", "assets", "bank_data", "health_data", "salary"}


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def create_link(conn: AsyncConnection, tenant_id: UUID, lead_id: UUID, base_url: str, ttl_h: int = 72) -> str:
    existing = await (await conn.execute(
        "select 1 from lead_forms where lead_id=%s and submitted_at is null and expires_at > now()", (lead_id,))).fetchone()
    token = secrets.token_urlsafe(18)
    if existing:
        await conn.execute("update lead_forms set expires_at = now() where lead_id=%s and submitted_at is null", (lead_id,))
    await conn.execute(
        "insert into lead_forms (tenant_id, lead_id, token_hash, expires_at) values (%s,%s,%s,%s)",
        (tenant_id, lead_id, _hash(token), datetime.now(timezone.utc) + timedelta(hours=ttl_h)))
    return f"{base_url.rstrip('/')}/f/{token}"


async def load_form(conn: AsyncConnection, token: str) -> dict | None:
    return await (await conn.execute(
        """select f.*, l.contact_id from lead_forms f join leads l on l.id = f.lead_id
           where f.token_hash=%s and f.expires_at > now() and f.submitted_at is null""", (_hash(token),))).fetchone()


async def submit_form(conn: AsyncConnection, form: dict, fields: dict[str, str], allowed: list[str], consent_text: str) -> bool:
    """Single use: the first submission wins (row lock + submitted_at guard); returns False for a repeated submit."""
    claimed = await (await conn.execute(
        "update lead_forms set submitted_at=now(), used_at=now() where id=%s and submitted_at is null returning id",
        (form["id"],))).fetchone()
    if not claimed:
        return False
    clean = {k: v.strip()[:200] for k, v in fields.items() if k in allowed and k not in FORBIDDEN_FIELDS and v.strip()}
    consent = await (await conn.execute(
        """insert into consents (tenant_id, contact_id, purpose, legal_basis, status, evidence, channel)
           values (%s,%s,'lead_form','consent','granted',%s,'web') returning id""",
        (form["tenant_id"], form["contact_id"], json.dumps({"form_id": str(form["id"]), "text": consent_text})))).fetchone()
    await conn.execute(
        "update lead_forms set fields_enc=%s, consent_id=%s where id=%s",
        (await encrypt_field(conn, form["tenant_id"], json.dumps(clean, ensure_ascii=False), "lead_forms.fields"),
         consent["id"], form["id"]))
    return True
