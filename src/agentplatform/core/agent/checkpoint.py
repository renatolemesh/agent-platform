"""LangGraph checkpointer for production: AsyncPostgresSaver with an encrypted serializer (review #15).
The TurnState holds masked message text; checkpoints are still encrypted at rest with a platform key derived from the KEK."""
from __future__ import annotations

import hashlib
import hmac
import os
from contextlib import asynccontextmanager

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentplatform.settings import get_settings


class AesGcmCipher:
    name = "aesgcm-v1"

    def __init__(self, key: bytes):
        self._a = AESGCM(key)

    def encrypt(self, plaintext: bytes) -> tuple[str, bytes]:
        nonce = os.urandom(12)
        return self.name, nonce + self._a.encrypt(nonce, plaintext, b"lg-checkpoint")

    def decrypt(self, ciphername: str, ciphertext: bytes) -> bytes:
        if ciphername != self.name:
            raise ValueError(f"unknown cipher {ciphername}")
        return self._a.decrypt(ciphertext[:12], ciphertext[12:], b"lg-checkpoint")


def checkpoint_key() -> bytes:
    import base64
    kek = get_settings().kek_b64
    if not kek:
        raise RuntimeError("KEK_B64 required for encrypted checkpoints")
    return hmac.new(base64.b64decode(kek.get_secret_value()), b"langgraph-checkpoints", hashlib.sha256).digest()


@asynccontextmanager
async def encrypted_checkpointer():
    pool = AsyncConnectionPool(get_settings().database_url, min_size=1, max_size=5, open=False,
                               kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0})
    await pool.open()
    try:
        yield AsyncPostgresSaver(pool, serde=EncryptedSerializer(AesGcmCipher(checkpoint_key())))
    finally:
        await pool.close()
