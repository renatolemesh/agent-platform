"""Rotate the KEK (ADR-016): re-wrap every tenant DEK with a new KEK in ONE transaction. Field ciphertexts don't change.

    OLD_KEK_B64=... NEW_KEK_B64=... MIGRATION_DATABASE_URL=... python scripts/ops/rewrap_deks.py            # dry run
    OLD_KEK_B64=... NEW_KEK_B64=... MIGRATION_DATABASE_URL=... python scripts/ops/rewrap_deks.py --apply

Keys may also come from files (OLD_KEK_FILE / NEW_KEK_FILE, e.g. Docker secrets). The dry run unwraps and re-wraps
everything, verifies each new blob, and rolls back. With --apply it commits; then deploy the new KEK_B64 secret and
restart api/worker/worker-bg (they cache unwrapped DEKs in memory only). The HASH_PEPPER is NOT rotated here: changing
it invalidates every *_hash lookup column (see docs/runbooks/rotacao-de-chaves.md).
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = b"\x01"
_NONCE = 12


def _key(var: str) -> bytes:
    raw = os.environ.get(var)
    if not raw and os.environ.get(var.replace("_B64", "_FILE")):
        raw = Path(os.environ[var.replace("_B64", "_FILE")]).read_text().strip()
    if not raw:
        sys.exit(f"{var} (or {var.replace('_B64', '_FILE')}) is required")
    key = base64.b64decode(raw)
    if len(key) != 32:
        sys.exit(f"{var} must decode to 32 bytes")
    return key


def _open(kek: bytes, blob: bytes, aad: bytes) -> bytes:
    if blob[:1] != _VERSION:
        raise ValueError("unknown ciphertext version")
    return AESGCM(kek).decrypt(blob[1:1 + _NONCE], blob[1 + _NONCE:], aad)


def _seal(kek: bytes, data: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(_NONCE)
    return _VERSION + nonce + AESGCM(kek).encrypt(nonce, data, aad)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="commit (default: dry run, rolls back)")
    args = ap.parse_args()
    old, new = _key("OLD_KEK_B64"), _key("NEW_KEK_B64")
    if old == new:
        sys.exit("OLD and NEW KEK are identical")
    dsn = os.environ.get("MIGRATION_DATABASE_URL") or sys.exit("MIGRATION_DATABASE_URL is required")
    with psycopg.connect(dsn) as conn:                      # one transaction; owner role
        conn.execute("select set_config('app.platform', 'on', true)")
        rows = conn.execute("select tenant_id, wrapped_dek from tenant_deks order by tenant_id for update").fetchall()
        for tenant_id, wrapped in rows:
            aad = f"dek:{tenant_id}".encode()
            dek = _open(old, bytes(wrapped), aad)              # raises if OLD_KEK is wrong: nothing is written
            blob = _seal(new, dek, aad)
            assert _open(new, blob, aad) == dek
            conn.execute("update tenant_deks set wrapped_dek=%s where tenant_id=%s", (blob, tenant_id))
        conn.execute(
            "insert into audit_log (actor_kind, action, target, after) values ('system', 'security.kek_rotated', 'tenant_deks', %s)",
            (psycopg.types.json.Jsonb({"tenants": len(rows), "applied": args.apply}),))
        if args.apply:
            conn.commit()
            print(f"re-wrapped {len(rows)} tenant DEK(s) with the new KEK (committed)")
        else:
            conn.rollback()
            print(f"dry run OK: {len(rows)} tenant DEK(s) unwrap with OLD_KEK and re-wrap with NEW_KEK (rolled back)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
