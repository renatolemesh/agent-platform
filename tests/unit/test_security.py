import logging
import os

import pytest

from agentplatform.core.security.crypto import Crypto, content_hash
from agentplatform.core.security.pii import contains_placeholder, mask
from agentplatform.core.security.redaction import RedactingFilter, redact


@pytest.fixture
def crypto():
    return Crypto(os.urandom(32), os.urandom(32))


def test_envelope_roundtrip_and_tenant_binding(crypto):
    wrapped = crypto.new_wrapped_dek("tenant-a")
    dek = crypto.unwrap_dek(wrapped, "tenant-a")
    blob = crypto.encrypt(dek, "Maria Silva", context="contacts.display_name")
    assert crypto.decrypt(dek, blob, context="contacts.display_name") == "Maria Silva"
    with pytest.raises(Exception):
        crypto.unwrap_dek(wrapped, "tenant-b")          # DEK bound to its tenant
    with pytest.raises(Exception):
        crypto.decrypt(dek, blob, context="other.field")  # ciphertext bound to its column


def test_lookup_hash_is_normalized_and_kind_scoped(crypto):
    assert crypto.lookup_hash(" A@B.com ", kind="email") == crypto.lookup_hash("a@b.com", kind="email")
    assert crypto.lookup_hash("a@b.com", kind="email") != crypto.lookup_hash("a@b.com", kind="phone")


def test_mask_valid_documents_only():
    r = mask("meu cpf é 529.982.247-25 e o falso 111.111.111-11")
    assert "[CPF]" in r.text and "111.111.111-11" in r.text
    assert r.found["cpf"] == 1
    r = mask("cnpj 11.222.333/0001-81, cartão 4111 1111 1111 1111, email a@b.com, fone (11) 99876-5432")
    for ph in ("[CNPJ]", "[CARTAO]", "[EMAIL]", "[TELEFONE]"):
        assert ph in r.text
    assert r.has_sensitive


def test_mask_health_terms():
    r = mask("tenho diabetes e fiz cirurgia ano passado")
    assert r.found["health"] == 2 and r.has_sensitive


def test_money_is_not_masked_as_phone():
    r = mask("quero uma carta de 80 mil, parcela até 1.500 reais em 180 meses")
    assert not r.has_pii, r.text


def test_placeholder_detection():
    assert contains_placeholder("seu cpf [CPF] está ok")
    assert not contains_placeholder("tudo certo")


def test_redact_secrets_and_pii():
    out = redact("key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX token EAAabcdefghijklmnopqrstuvwxyz cpf 529.982.247-25")
    assert "sk-proj" not in out and "EAAabc" not in out and "529.982" not in out


def test_logging_filter(caplog):
    logger = logging.getLogger("t")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO):
        logger.info("calling with %s", "sk-or-v1-0123456789abcdef0123")
    assert "0123456789abcdef" not in caplog.text


def test_content_hash_stable():
    assert content_hash("olá") == content_hash("olá") != content_hash("ola")
