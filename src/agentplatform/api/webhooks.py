"""Meta webhook: verify -> persist -> 200 (plan 07 §1). If Postgres is down, append to a durable local spool and
still return 200 (the Messenger Platform unsubscribes the app after ~1h of failures)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Query, Request, Response

from agentplatform.core.channels.ports import ChannelAdapter, PartialParse
from agentplatform.core.orchestration.ingress import persist_events
from agentplatform.settings import get_settings

log = logging.getLogger(__name__)

INTERNAL_CHANNELS = {"simulator"}      # in-process channels (console simulator): never reachable from outside
OnPersisted = Callable[[list[tuple]], Awaitable[None]]


class Spool:
    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "webhooks.jsonl"

    def append(self, channel: str, body: bytes) -> None:
        rec = json.dumps({"ts": time.time(), "channel": channel, "body": body.decode("utf-8")}) + "\n"
        with open(self.file, "a", encoding="utf-8") as f:
            f.write(rec)
            f.flush()
            os.fsync(f.fileno())

    def claim(self) -> list[Path]:
        """Moves the live spool aside (atomic rename) and returns every claimed batch, including batches left by a
        replay that crashed. A batch file is deleted only after all its events are persisted."""
        if self.file.exists() and self.file.stat().st_size:
            self.file.rename(self.file.with_suffix(f".{time.time_ns()}.replay"))
        return sorted(self.dir.glob("webhooks.*.replay"))

    @staticmethod
    def read(batch: Path) -> list[dict]:
        with open(batch, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def size(self) -> int:
        """Bytes waiting for replay (live spool + claimed batches not yet persisted)."""
        files = [self.file, *self.dir.glob("webhooks.*.replay")]
        return sum(f.stat().st_size for f in files if f.exists())

    def quarantine(self, channel: str, body: bytes) -> None:
        """A signed delivery we could not read: kept for a human, never replayed (it would fail again and block the spool)."""
        rec = json.dumps({"ts": time.time(), "channel": channel, "body": body.decode("utf-8", "replace")}) + "\n"
        with open(self.dir / "unreadable.jsonl", "a", encoding="utf-8") as f:
            f.write(rec)
            f.flush()
            os.fsync(f.fileno())

    def quarantine_size(self) -> int:
        f = self.dir / "unreadable.jsonl"
        return f.stat().st_size if f.exists() else 0

    def purge_quarantine(self, max_age_s: float) -> bool:
        """Customer text in clear on disk: it does not outlive the retention of raw events."""
        f = self.dir / "unreadable.jsonl"
        if f.exists() and time.time() - f.stat().st_mtime > max_age_s:
            f.unlink()
            return True
        return False


def build_router(adapters: dict[str, ChannelAdapter], spool: Spool, on_persisted: OnPersisted | None = None) -> APIRouter:
    router = APIRouter()

    @router.get("/webhooks/{channel}")
    async def verify(channel: str, hub_mode: str = Query(alias="hub.mode"), hub_verify_token: str = Query(alias="hub.verify_token"),
                     hub_challenge: str = Query(alias="hub.challenge")):
        expected = get_settings().meta_verify_token
        if hub_mode == "subscribe" and expected and hub_verify_token == expected.get_secret_value():
            return Response(content=hub_challenge, media_type="text/plain")
        raise HTTPException(403)

    @router.post("/webhooks/{channel}")
    async def receive(channel: str, request: Request):
        adapter = None if channel in INTERNAL_CHANNELS else adapters.get("instagram" if channel == "meta" else channel)
        if adapter is None:
            raise HTTPException(404)
        body = await request.body()
        if not adapter.verify_signature(request.headers, body):
            raise HTTPException(401, "bad signature")
        try:
            events = adapter.parse(body)
        except PartialParse as e:
            log.error("webhook with %s unreadable item(s); delivery set aside", e.failures)
            spool.quarantine(adapter.channel, body)
            events = e.events
        except Exception as e:              # signed by the provider, so answering 500 would only make it stop delivering
            log.error("unreadable webhook (%s); delivery set aside", type(e).__name__)
            spool.quarantine(adapter.channel, body)
            return {"status": "quarantined"}
        try:
            created = await asyncio.wait_for(persist_events(events), get_settings().webhook_persist_timeout_s)
        except Exception:                   # includes the timeout: idempotency keys make a later replay harmless
            log.exception("persist failed; spooling webhook")
            spool.append(adapter.channel, body)
            return {"status": "spooled"}
        if on_persisted and created:
            try:
                await on_persisted(created)
            except Exception:
                log.exception("post-persist hook failed (events are safe in inbound_events; sweeper will pick them up)")
        return {"status": "ok", "accepted": len(created)}

    return router


async def replay_spool(spool: Spool, adapters: dict[str, ChannelAdapter], on_persisted: OnPersisted | None = None) -> int:
    """Re-persists spooled webhooks (persist_events is idempotent, so a batch replayed twice is harmless). Stops at the
    first failure and keeps the batch on disk for the next attempt."""
    n = 0
    for batch in spool.claim():
        try:
            for it in spool.read(batch):
                body = it["body"].encode("utf-8")
                try:
                    events = adapters[it["channel"]].parse(body)
                except PartialParse as e:
                    spool.quarantine(it["channel"], body)
                    events = e.events
                except Exception:           # would fail forever: it must not hold back the deliveries behind it
                    log.exception("unreadable spooled webhook; set aside")
                    spool.quarantine(it["channel"], body)
                    continue
                created = await persist_events(events)
                n += len(created)
                if on_persisted and created:
                    try:
                        await on_persisted(created)
                    except Exception:
                        log.exception("post-persist hook failed during replay (sweeper will pick the events up)")
        except Exception:
            log.exception("spool replay failed; batch %s kept for the next attempt", batch.name)
            break
        batch.unlink()
    return n
