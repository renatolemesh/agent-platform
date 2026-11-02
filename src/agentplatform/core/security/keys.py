"""Per-tenant DEK access and field encryption helpers."""
from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.security.crypto import Crypto
from agentplatform.settings import get_settings

_crypto: Crypto | None = None
_dek_cache: dict[str, bytes] = {}


def crypto() -> Crypto:
    global _crypto
    if _crypto is None:
        s = get_settings()
        if not s.kek_b64 or not s.hash_pepper_b64:
            raise RuntimeError("KEK_B64 and HASH_PEPPER_B64 must be set")
        _crypto = Crypto.from_b64(s.kek_b64.get_secret_value(), s.hash_pepper_b64.get_secret_value())
    return _crypto


async def ensure_dek(conn: AsyncConnection, tenant_id: UUID) -> bytes:
    key = str(tenant_id)
    if key in _dek_cache:
        return _dek_cache[key]
    row = await (await conn.execute("select wrapped_dek from tenant_deks where tenant_id = %s", (tenant_id,))).fetchone()
    if row is None:
        wrapped = crypto().new_wrapped_dek(key)
        await conn.execute("insert into tenant_deks (tenant_id, wrapped_dek) values (%s, %s) on conflict do nothing",
                           (tenant_id, wrapped))
        row = await (await conn.execute("select wrapped_dek from tenant_deks where tenant_id = %s", (tenant_id,))).fetchone()
    dek = crypto().unwrap_dek(bytes(row["wrapped_dek"]), key)
    _dek_cache[key] = dek
    return dek


async def encrypt_field(conn: AsyncConnection, tenant_id: UUID, value: str | None, context: str) -> bytes | None:
    if value is None:
        return None
    return crypto().encrypt(await ensure_dek(conn, tenant_id), value, context=context)


async def decrypt_field(conn: AsyncConnection, tenant_id: UUID, blob: bytes | None, context: str) -> str | None:
    if blob is None:
        return None
    return crypto().decrypt(await ensure_dek(conn, tenant_id), bytes(blob), context=context)


async def encrypt_blob(conn: AsyncConnection, tenant_id: UUID, data: bytes, context: str) -> bytes:
    return crypto().encrypt_bytes(await ensure_dek(conn, tenant_id), data, context=context)


async def decrypt_blob(conn: AsyncConnection, tenant_id: UUID, blob: bytes, context: str) -> bytes:
    return crypto().decrypt_bytes(await ensure_dek(conn, tenant_id), bytes(blob), context=context)
