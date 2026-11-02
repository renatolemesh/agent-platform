"""Lead import (plan 09 §3, phase 4): CSV/XLSX -> column mapping -> dry-run report -> commit -> rollback of the batch.
LGPD per row: source, legal_basis, purpose (+ consent evidence when the basis is consent). Suppressed contacts never
come back. Merge only on strong identifiers (E.164 phone / e-mail)."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from uuid import UUID

import phonenumbers
from psycopg import AsyncConnection

from agentplatform.core.security.keys import crypto, encrypt_field

LEGAL_BASES = {"consent", "legitimate_interest", "contract_preliminary"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)


@dataclass
class ImportReport:
    batch_id: UUID | None
    imported: int = 0
    updated: int = 0
    rejected: list[tuple[int, str]] = field(default_factory=list)
    dry_run: bool = True


def normalize_phone(raw: str) -> str | None:
    try:
        n = phonenumbers.parse(str(raw), "BR")
    except phonenumbers.NumberParseException:
        return None
    return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164) if phonenumbers.is_valid_number(n) else None


def validate_row(row: dict, mapping: dict[str, str]) -> tuple[dict | None, str | None]:
    g = lambda k: str(row.get(mapping.get(k, k), "") or "").strip()
    phone, email = normalize_phone(g("phone")) if g("phone") else None, g("email").lower() or None
    if email and not _EMAIL.match(email):
        return None, "email_invalid"
    if not phone and not email:
        return None, "no_strong_identifier"
    basis = g("legal_basis")
    if basis not in LEGAL_BASES:
        return None, "legal_basis_missing_or_invalid"
    if not g("source") or not g("purpose"):
        return None, "source_or_purpose_missing"
    if basis == "consent" and not g("consent_evidence"):
        return None, "consent_evidence_missing"
    name = g("name").title() or None
    return {"phone": phone, "email": email, "name": name, "first_name": (name or "").split(" ")[0] or None,
            "legal_basis": basis, "source": g("source"), "purpose": g("purpose"),
            "consent_evidence": g("consent_evidence") or None, "tags": [t.strip() for t in g("tags").split(",") if t.strip()]}, None


async def import_leads(conn: AsyncConnection, tenant_id: UUID, rows: list[dict], mapping: dict[str, str], *,
                       initial_stage: str, file_bytes: bytes, dry_run: bool = True, created_by: UUID | None = None) -> ImportReport:
    rep = ImportReport(None, dry_run=dry_run)
    c = crypto()
    valid: list[tuple[int, dict]] = []
    for i, r in enumerate(rows, start=2):
        v, err = validate_row(r, mapping)
        if err:
            rep.rejected.append((i, err))
            continue
        hashes = [c.lookup_hash(v["phone"], kind="phone")] if v["phone"] else []
        hashes += [c.lookup_hash(v["email"], kind="email")] if v["email"] else []
        sup = await (await conn.execute("select 1 from suppression_list where tenant_id=%s and identifier_hash = any(%s)",
                                        (tenant_id, hashes))).fetchone()
        if sup:
            rep.rejected.append((i, "suppressed"))
            continue
        valid.append((i, v))
    if dry_run:
        for _, v in valid:
            existing = await _find_contact(conn, tenant_id, v)
            rep.updated += 1 if existing else 0
            rep.imported += 0 if existing else 1
        return rep
    batch = await (await conn.execute(
        "insert into import_batches (tenant_id, file_sha256, mapping, status, created_by) values (%s,%s,%s,'committed',%s) returning id",
        (tenant_id, hashlib.sha256(file_bytes).hexdigest(), json.dumps(mapping), created_by))).fetchone()
    rep.batch_id = batch["id"]
    for i, v in valid:
        contact = await _find_contact(conn, tenant_id, v)
        if contact:
            cid = contact["id"]
            rep.updated += 1
            status = "updated"
        else:
            row = await (await conn.execute(
                """insert into contacts (tenant_id, first_name, display_name_enc, phone_hash, phone_enc, email_hash, email_enc)
                   values (%s,%s,%s,%s,%s,%s,%s) returning id""",
                (tenant_id, v["first_name"], await encrypt_field(conn, tenant_id, v["name"], "contacts.display_name"),
                 c.lookup_hash(v["phone"], kind="phone") if v["phone"] else None,
                 await encrypt_field(conn, tenant_id, v["phone"], "contacts.phone"),
                 c.lookup_hash(v["email"], kind="email") if v["email"] else None,
                 await encrypt_field(conn, tenant_id, v["email"], "contacts.email")))).fetchone()
            cid = row["id"]
            if v["phone"]:
                await conn.execute(
                    "insert into contact_identities (tenant_id, contact_id, channel, external_user_id) values (%s,%s,'whatsapp',%s) on conflict do nothing",
                    (tenant_id, cid, v["phone"].lstrip("+")))
            await conn.execute("insert into leads (tenant_id, contact_id, stage) values (%s,%s,%s) on conflict do nothing",
                               (tenant_id, cid, initial_stage))
            rep.imported += 1
            status = "imported"
        await conn.execute(
            """insert into consents (tenant_id, contact_id, purpose, legal_basis, status, evidence, channel)
               values (%s,%s,%s,%s,'granted',%s,'import')""",
            (tenant_id, cid, v["purpose"], v["legal_basis"],
             json.dumps({"batch": str(batch["id"]), "row": i, "source": v["source"], "evidence": v["consent_evidence"]})))
        await conn.execute("insert into import_rows (batch_id, tenant_id, row_no, status, contact_id) values (%s,%s,%s,%s,%s)",
                           (batch["id"], tenant_id, i, status, cid))
    for i, reason in rep.rejected:
        await conn.execute("insert into import_rows (batch_id, tenant_id, row_no, status, reason) values (%s,%s,%s,'rejected',%s)",
                           (batch["id"], tenant_id, i, reason))
    await conn.execute("update import_batches set counts=%s where id=%s",
                       (json.dumps({"imported": rep.imported, "updated": rep.updated, "rejected": len(rep.rejected)}), batch["id"]))
    return rep


async def _find_contact(conn, tenant_id, v) -> dict | None:
    c = crypto()
    if v["phone"]:
        r = await (await conn.execute("select id from contacts where tenant_id=%s and phone_hash=%s and merged_into is null",
                                      (tenant_id, c.lookup_hash(v["phone"], kind="phone")))).fetchone()
        if r:
            return r
    if v["email"]:
        return await (await conn.execute("select id from contacts where tenant_id=%s and email_hash=%s and merged_into is null",
                                         (tenant_id, c.lookup_hash(v["email"], kind="email")))).fetchone()
    return None


async def rollback_batch(conn: AsyncConnection, tenant_id: UUID, batch_id: UUID) -> int:
    """Removes contacts CREATED by the batch (consents of updated contacts from this batch are revoked)."""
    created = await (await conn.execute(
        "select contact_id from import_rows where batch_id=%s and status='imported'", (batch_id,))).fetchall()
    ids = [r["contact_id"] for r in created]
    await conn.execute("update consents set status='revoked' where tenant_id=%s and evidence->>'batch' = %s", (tenant_id, str(batch_id)))
    if ids:
        await conn.execute("delete from consents where contact_id = any(%s)", (ids,))
        await conn.execute("delete from leads where contact_id = any(%s)", (ids,))
        await conn.execute("delete from contact_identities where contact_id = any(%s)", (ids,))
        await conn.execute("update import_rows set contact_id = null where batch_id=%s", (batch_id,))
        await conn.execute("delete from contacts where id = any(%s)", (ids,))
    await conn.execute("update import_batches set status='rolled_back' where id=%s", (batch_id,))
    return len(ids)
