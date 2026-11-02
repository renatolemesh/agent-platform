"""Envelope encryption (ADR-016): a KEK from secrets wraps one DEK per tenant; fields use AES-256-GCM.

Searchable fields use HMAC-SHA256 with a pepper (`*_hash` columns).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE = 12
_VERSION = b"\x01"


class Crypto:
    def __init__(self, kek: bytes, pepper: bytes):
        if len(kek) != 32:
            raise ValueError("KEK must be 32 bytes")
        if len(pepper) < 16:
            raise ValueError("pepper must be at least 16 bytes")
        self._kek = AESGCM(kek)
        self._pepper = pepper

    @classmethod
    def from_b64(cls, kek_b64: str, pepper_b64: str) -> "Crypto":
        return cls(base64.b64decode(kek_b64), base64.b64decode(pepper_b64))

    # ----- DEK management -----
    def new_wrapped_dek(self, tenant_id: str) -> bytes:
        dek = AESGCM.generate_key(bit_length=256)
        return self._seal(self._kek, dek, aad=f"dek:{tenant_id}".encode())

    def unwrap_dek(self, wrapped: bytes, tenant_id: str) -> bytes:
        return self._open(self._kek, wrapped, aad=f"dek:{tenant_id}".encode())

    # ----- field encryption -----
    def encrypt(self, dek: bytes, plaintext: str, *, context: str) -> bytes:
        return self._seal(AESGCM(dek), plaintext.encode("utf-8"), aad=context.encode())

    def decrypt(self, dek: bytes, blob: bytes, *, context: str) -> str:
        return self._open(AESGCM(dek), blob, aad=context.encode()).decode("utf-8")

    def encrypt_bytes(self, dek: bytes, data: bytes, *, context: str) -> bytes:
        return self._seal(AESGCM(dek), data, aad=context.encode())

    def decrypt_bytes(self, dek: bytes, blob: bytes, *, context: str) -> bytes:
        return self._open(AESGCM(dek), blob, aad=context.encode())

    def lookup_hash(self, value: str, *, kind: str) -> bytes:
        normalized = value.strip().lower()
        return hmac.new(self._pepper, f"{kind}:{normalized}".encode(), hashlib.sha256).digest()

    @staticmethod
    def _seal(aead: AESGCM, data: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(_NONCE)
        return _VERSION + nonce + aead.encrypt(nonce, data, aad)

    @staticmethod
    def _open(aead: AESGCM, blob: bytes, aad: bytes) -> bytes:
        if blob[:1] != _VERSION:
            raise ValueError("unknown ciphertext version")
        nonce, ct = blob[1:1 + _NONCE], blob[1 + _NONCE:]
        return aead.decrypt(nonce, ct, aad)


def content_hash(text: str) -> str:
    """sha256 of the exact approved text (outbox.content_hash, echo matching)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _keygen() -> None:
    print("KEK_B64=" + base64.b64encode(os.urandom(32)).decode())
    print("HASH_PEPPER_B64=" + base64.b64encode(os.urandom(32)).decode())


if __name__ == "__main__" and sys.argv[1:] == ["keygen"]:
    _keygen()
