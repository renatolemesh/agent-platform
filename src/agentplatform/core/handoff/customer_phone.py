"""The customer's own WhatsApp number, left in the conversation for the seller to call (HandoffConfig.ask_customer_whatsapp).

The number is read from the original message (kept encrypted), never from what the model sees: masking turns it into [TELEFONE]
before any prompt. It is stored encrypted on the contact, like an imported lead's phone, and only the console shows it.
"""
from __future__ import annotations

import re
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.imports.leads import normalize_phone
from agentplatform.core.security.keys import crypto, decrypt_field, encrypt_field
from agentplatform.core.security.pii import find_phones

PHONE_TOKEN = "[TELEFONE]"


def extra_words(masked_texts: list[str]) -> int:
    """Words the customer wrote besides the number(s)."""
    rest = " ".join(t.replace(PHONE_TOKEN, " ") for t in masked_texts)
    return len(re.findall(r"\w+", rest))


def first_phone(texts: list[str]) -> str | None:
    """The first valid number in E.164, or None."""
    for t in texts:
        for raw in find_phones(t):
            phone = normalize_phone(raw)
            if phone:
                return phone
    return None


async def save_from_messages(conn: AsyncConnection, tenant_id: UUID, contact_id: UUID, message_ids: list[str]) -> bool:
    """Keeps the newest number the customer wrote in these messages. A number that is another contact's is still kept, without the
    lookup hash (it would merge two people). Returns whether a number was found and kept."""
    rows = await (await conn.execute(
        "select text_enc from messages where id = any(%s::uuid[]) and direction='in' order by provider_ts, created_at",
        (message_ids,))).fetchall()
    texts = [t for t in [await decrypt_field(conn, tenant_id, r["text_enc"], "messages.text") for r in rows] if t]
    phone = first_phone(list(reversed(texts)))
    if not phone:
        return False
    h = crypto().lookup_hash(phone, kind="phone")
    other = await (await conn.execute(
        "select 1 from contacts where tenant_id=%s and phone_hash=%s and id<>%s and merged_into is null", (tenant_id, h, contact_id))).fetchone()
    await conn.execute("update contacts set phone_enc=%s, phone_hash=%s where id=%s",
                       (await encrypt_field(conn, tenant_id, phone, "contacts.phone"), None if other else h, contact_id))
    await conn.execute(
        """update handoffs set summary = summary || '{"whatsapp_left": true}'::jsonb
           where conversation_id in (select id from conversations where contact_id=%s) and taken_at is null and not is_notice""",
        (contact_id,))
    return True
