"""In-process job pump: runs the same job bodies synchronously (tests, sandbox, `agentplatform simulate`).

Emulates procrastinate semantics that matter for correctness:
- queueing_lock: a job is not enqueued while another job with the same lock is still waiting (todo);
- schedule_in: honored (capped) when honor_delays=True, else compressed.
"""
from __future__ import annotations

import asyncio
from collections import deque

from agentplatform.jobs import tasks as T


async def pump(initial: T.Next, max_jobs: int = 200, honor_delays: bool = False, max_delay: float = 5) -> list[str]:
    """max_delay caps every schedule_in (the console simulator uses a small cap so a request does not wait for
    background follow-ups such as the rolling summary)."""
    q: deque = deque()
    ran: list[str] = []

    def enqueue(items: T.Next) -> None:
        for name, kwargs, opts in items:
            ql = opts.get("queueing_lock")
            if ql and any(o.get("queueing_lock") == ql for _, _, o in q):
                continue                          # AlreadyEnqueued
            q.append((name, kwargs, opts))

    enqueue(initial)
    while q and len(ran) < max_jobs:
        name, kwargs, opts = q.popleft()
        delay = (opts.get("schedule_in") or {}).get("seconds", 0)
        if honor_delays and delay:
            await asyncio.sleep(min(delay, max_delay))
        ran.append(name)
        enqueue(await getattr(T, name)(**kwargs))
    return ran
