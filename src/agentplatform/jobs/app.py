"""procrastinate app (ADR-004). Only run_turn uses the per-conversation lock; echo/media/dispatch/follow-ups don't.
Job arguments carry IDs only (procrastinate tables have no RLS/encryption)."""
from __future__ import annotations

import asyncio
import logging

import procrastinate
from procrastinate import RetryStrategy

from agentplatform.core.ops.tracing import init_tracing, span
from agentplatform.core.security import redaction
from agentplatform.jobs import tasks as T
from agentplatform.settings import get_settings

log = logging.getLogger(__name__)

redaction.install()
init_tracing("worker")
app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=get_settings().database_url))
RETRY = RetryStrategy(max_attempts=5, exponential_wait=2)
# Jobs with a queueing lock are never retried by the queue: the retry would collide with the equivalent job queued meanwhile. They
# are safe to lose: the periodic sweeps recreate close_batch, and the other two are best effort.
NO_QUEUE_RETRY = {"close_batch", "fetch_username", "summarize", "knowledge_extract", "knowledge_index", "knowledge_index_faq", "knowledge_reindex", "config_gate"}

QUEUE = {"ingress": "ingress", "close_batch": "turns", "run_turn": "turns", "dispatch": "dispatch", "reconcile": "dispatch",
         "echo": "echo", "comment": "comments", "comment_backfill": "comments", "hydrate_media": "comments",
         "fetch_username": "comments", "transcribe_pending": "media", "summarize": "maintenance",
         "knowledge_extract": "ingest", "knowledge_index": "ingest", "knowledge_index_faq": "ingest", "knowledge_reindex": "ingest", "config_gate": "lab"}


async def _defer(nxt: T.Next) -> None:
    for name, kwargs, opts in nxt:
        task = TASKS[name]
        try:
            await task.configure(**opts).defer_async(**kwargs)
        except procrastinate.exceptions.AlreadyEnqueued:
            pass   # an equivalent job is already queued; it will see the latest state when it runs


def _wrap(name: str, fn):
    retry = False if name in NO_QUEUE_RETRY else RETRY

    @app.task(name=name, queue=QUEUE[name], retry=retry, pass_context=True)
    async def job(context, **kwargs):
        try:
            with span(f"job.{name}", tenant_id=kwargs.get("tenant_id"), conversation_id=kwargs.get("conversation_id")):
                nxt = await fn(**kwargs)
        except Exception as e:
            if retry is False or context.job.attempts >= RETRY.max_attempts:     # last attempt: nobody will run this again
                await T.record_dead_letter(name, kwargs, e)
            raise
        await _defer(nxt)
    return job


TASKS = {name: _wrap(name, getattr(T, name)) for name in QUEUE}


@app.periodic(cron="* * * * *")
@app.task(name="periodic_minute", queue="maintenance", queueing_lock="periodic_minute", pass_context=False)
async def periodic_minute(timestamp: int):
    failed = False
    for step in (T.sweep_inbound, T.tenants_tick, T.budget_sweeper, T.ops_alerts):
        try:                           # each step on its own: a failing alert never stops inbound or stalled jobs from recovering
            await _defer(await step())
        except Exception:
            failed = True
            log.exception("periodic step %s failed", step.__name__)
    try:
        await T.retry_stalled(app)
    except Exception:
        failed = True
        log.exception("periodic step retry_stalled failed")
    if not failed:
        await T.heartbeat()            # the outside watchdog only hears from us when the whole loop works


@app.periodic(cron="*/10 * * * *")
@app.task(name="periodic_subscriptions", queue="maintenance", queueing_lock="periodic_subscriptions", pass_context=False)
async def periodic_subscriptions(timestamp: int):
    await _defer(await T.webhook_subscriptions())


@app.periodic(cron="*/5 * * * *")
@app.task(name="periodic_media", queue="maintenance", queueing_lock="periodic_media", pass_context=False)
async def periodic_media(timestamp: int):
    await _defer(await T.media_sync())


@app.periodic(cron="30 3 * * *")
@app.task(name="periodic_daily", queue="maintenance", queueing_lock="periodic_daily", pass_context=False)
async def periodic_daily(timestamp: int):
    await _defer(await T.daily_maintenance())


_open_lock: asyncio.Lock | None = None


async def _ensure_open() -> None:
    """The queue connection stays open between calls in the API process: opening a pool per webhook costs more than handling the
    webhook. Everything there shares it, so nobody may use `async with app.open_async()`: leaving that block closes it for the others."""
    global _open_lock
    _open_lock = _open_lock or asyncio.Lock()
    async with _open_lock:
        await app.open_async()         # does nothing when already open


async def defer_jobs(nxt: T.Next) -> None:
    """Called by the console (outside a worker)."""
    await _ensure_open()
    await _defer(nxt)


async def defer_ingress(created: list[tuple]) -> None:
    """Called by the webhook after persisting events (best effort; the sweeper is the safety net)."""
    await _ensure_open()
    await _defer([("ingress", {"tenant_id": str(t), "event_id": e}, {}) for t, e in created])
