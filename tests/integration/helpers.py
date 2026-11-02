import uuid
from datetime import datetime, timedelta, timezone

from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.orchestration.conversations import get_or_create_conversation, store_inbound_message
from agentplatform.core.outbox.store import enqueue


async def make_conversation(tenant_id, igsid=None, window_open=True):
    igsid = igsid or f"IGSID-{uuid.uuid4().hex[:8]}"
    async with tenant_tx(tenant_id) as conn:
        ca = await (await conn.execute("select id from channel_accounts where tenant_id=%s", (tenant_id,))).fetchone()
        conv = await get_or_create_conversation(conn, tenant_id, ca["id"], "instagram", igsid, "novo")
        if window_open:
            await store_inbound_message(conn, tenant_id, conv["id"], mid=f"m-{uuid.uuid4().hex[:6]}", kind="text",
                                        text="oi", media=None, provider_ts=datetime.now(timezone.utc) - timedelta(seconds=5),
                                        inbound_event_id=None, service_window=timedelta(hours=24))
            await conn.execute("update messages set turn_id = gen_random_uuid() where conversation_id=%s", (conv["id"],))
        conv = await (await conn.execute("select * from conversations where id=%s", (conv["id"],))).fetchone()
    return conv, igsid


async def put(tenant_id, conv, igsid, text, key=None, **kw):
    async with tenant_tx(tenant_id) as conn:
        c = await (await conn.execute("select send_epoch from conversations where id=%s", (conv["id"],))).fetchone()
        return await enqueue(conn, tenant_id=tenant_id, conversation_id=conv["id"], effect_key=key or f"k-{uuid.uuid4()}",
                             kind=kw.pop("kind", "dm_text"), text=text, recipient=igsid, send_epoch=kw.pop("send_epoch", c["send_epoch"]),
                             priority=0, max_staleness_s=kw.pop("max_staleness_s", 120), **kw)


async def status(tenant_id, oid):
    async with tenant_tx(tenant_id) as conn:
        return await (await conn.execute("select status, cancel_reason, external_mid from outbox where id=%s", (oid,))).fetchone()


def make_pdf(pages: list[str]) -> bytes:
    """A real PDF with these pages of text (synthetic material for tests; an empty page has no text at all, like a scan)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    kids = []
    for text in pages:
        kids.append(f"{len(objects) + 1} 0 R")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objects) + 2} 0 R /Resources << /Font << /F1 3 0 R >> >> >>")
        lines = "".join(f"({ln.replace(chr(92), '').replace('(', '').replace(')', '')}) Tj T* " for ln in text.splitlines())
        stream = f"BT /F1 9 Tf 12 TL 30 760 Td {lines}ET" if text else ""
        objects.append(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin-1")
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode("latin-1")
    out += f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("latin-1")
    return out
