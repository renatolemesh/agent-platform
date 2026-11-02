"""What the ingest worker would run for one tenant, through the real tasks, with a provider that returns fixed vectors: no network and no spend.
Its own process, like the server, because the browser driver holds the event loop of the test process."""
import asyncio
import sys
from uuid import UUID

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class FixedVectors:
    async def embed(self, spec, texts, ctx):
        return [[0.1] * spec.dims for _ in texts]


async def main(tenant_id: str) -> None:
    from agentplatform.core.db.pool import close_pool, tenant_tx
    from agentplatform.jobs import tasks
    from agentplatform.runtime import Runtime, set_runtime
    set_runtime(Runtime(adapters={}, gateway=FixedVectors(), inbox=object()))
    async with tenant_tx(UUID(tenant_id)) as conn:
        docs = await (await conn.execute("select id, processing from kb_documents where tenant_id=%s and processing in ('queued','indexing')", (tenant_id,))).fetchall()
        faqs = await (await conn.execute("select id from faqs where tenant_id=%s and processing='indexing'", (tenant_id,))).fetchall()
    for d in docs:
        task = tasks.knowledge_extract if d["processing"] == "queued" else tasks.knowledge_index
        await task(tenant_id=tenant_id, document_id=str(d["id"]))
    for f in faqs:
        await tasks.knowledge_index_faq(tenant_id=tenant_id, faq_id=str(f["id"]))
    await close_pool()


asyncio.run(main(sys.argv[1]))
