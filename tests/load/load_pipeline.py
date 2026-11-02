"""Load test of the real message pipeline (plan 07 §9) — no LLM spend, no network.

N concurrent fake Instagram conversations go through the REAL job bodies (ingress -> debounce/close_batch -> run_turn
with the LangGraph checkpointer -> outbox -> dispatcher) against the dev Postgres, using FakeChannelAdapter and
ScriptedGateway. A small in-process scheduler emulates the procrastinate semantics that matter here:
  * W concurrent worker slots (prod `worker` runs --concurrency=8);
  * `schedule_in` honoured in real time;
  * `queueing_lock`: a job is not enqueued while another job with the same lock is still waiting;
  * `lock`: jobs sharing a lock never run at the same time (per-conversation turn/dispatch serialisation).

Each conversation sends a burst of 3 messages (random gaps 0-150 ms, inside the debounce window), waits for the reply,
then sends 1 more message. Invariants checked at the end (exit code 1 if any fails):
  * exactly one reply per burst per conversation and no duplicate sends;
  * exactly one run_turn per debounce batch (2 per conversation);
  * no lost inbound: every inbound message was consumed by a turn; every outbox row is `sent`.

Not collected by pytest (file name). Run with the dev stack up:
    .venv/Scripts/python tests/load/load_pipeline.py --conversations 50 --workers 8

Reference result (2026-10-10, Windows 11 laptop, Docker Desktop Postgres 17, 50 conversations, 8 workers):
    see docs/runbooks/teste-de-carga.md (numbers are recorded there after each run).
"""
from __future__ import annotations

import argparse
import asyncio
import heapq
import itertools
import json
import random
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT = Path(__file__).resolve().parents[2]

REPLY_1 = "Oi! Sou o assistente virtual do consultor. Você pensa em imóvel ou veículo?"
REPLY_2 = "Funciona assim: você entra num grupo e paga parcelas mensais. Prefere imóvel ou veículo?"


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
    return s[k]


class Scheduler:
    """Tiny procrastinate emulator: real-time schedule_in, queueing_lock, lock, W worker slots."""

    def __init__(self, workers: int, tasks_module, skip: frozenset[str] = frozenset()):
        self.workers = workers
        self.T = tasks_module
        self.skip = skip
        self.heap: list = []
        self.seq = itertools.count()
        self.waiting_qlocks: Counter = Counter()
        self.running_locks: set[str] = set()
        self.running = 0
        self.ran: Counter = Counter()
        self.durations: dict[str, list[float]] = defaultdict(list)
        self.per_conv: dict[tuple[str, str], int] = Counter()
        self.errors: list[str] = []
        self.wake = asyncio.Event()
        self.closed = False

    def enqueue(self, items) -> None:
        now = time.monotonic()
        for name, kwargs, opts in items:
            if name in self.skip:
                self.ran[f"skipped:{name}"] += 1
                continue
            ql = opts.get("queueing_lock")
            if ql and self.waiting_qlocks[ql] > 0:
                continue                                   # AlreadyEnqueued
            due = now + float((opts.get("schedule_in") or {}).get("seconds", 0))
            if ql:
                self.waiting_qlocks[ql] += 1
            heapq.heappush(self.heap, (due, next(self.seq), name, kwargs, opts))
        self.wake.set()

    def _pick(self):
        now = time.monotonic()
        blocked = []
        picked = None
        while self.heap and self.heap[0][0] <= now:
            item = heapq.heappop(self.heap)
            lock = item[4].get("lock")
            if lock and lock in self.running_locks:
                blocked.append(item)
                continue
            picked = item
            break
        for b in blocked:
            heapq.heappush(self.heap, b)
        return picked

    async def _worker(self) -> None:
        while True:
            item = self._pick()
            if item is None:
                if self.closed and not self.heap and self.running == 0:
                    self.wake.set()
                    return
                self.wake.clear()
                timeout = 0.05
                if self.heap:
                    timeout = max(0.0, min(0.05, self.heap[0][0] - time.monotonic()))
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=timeout or 0.001)
                except asyncio.TimeoutError:
                    pass
                continue
            _, _, name, kwargs, opts = item
            ql, lock = opts.get("queueing_lock"), opts.get("lock")
            if ql:
                self.waiting_qlocks[ql] -= 1               # job left `todo`: an equivalent job may be queued again
            if lock:
                self.running_locks.add(lock)
            self.running += 1
            t0 = time.perf_counter()
            try:
                nxt = await getattr(self.T, name)(**kwargs)
            except Exception as e:                         # a failing job is an invariant failure here
                self.errors.append(f"{name}: {type(e).__name__}: {e}")
                nxt = []
            finally:
                self.durations[name].append(time.perf_counter() - t0)
                self.running -= 1
                if lock:
                    self.running_locks.discard(lock)
            self.ran[name] += 1
            if "conversation_id" in kwargs:
                self.per_conv[(name, kwargs["conversation_id"])] += 1
            self.enqueue(nxt)

    async def run_until_idle(self, producers) -> None:
        workers = [asyncio.create_task(self._worker()) for _ in range(self.workers)]
        await producers
        self.closed = True
        self.wake.set()
        await asyncio.gather(*workers)


async def main(n_conv: int, n_workers: int, seed: int) -> int:
    sys.path.insert(0, str(ROOT / "tests" / "integration"))
    import yaml

    from agentplatform.core.channels.events import CanonicalEvent
    from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
    from agentplatform.core.config.schema import PlatformConfig
    from agentplatform.core.db.pool import close_pool, get_pool, platform_tx, tenant_tx
    from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier
    from agentplatform.core.models.fake import ScriptedGateway, analysis, draft
    from agentplatform.core.orchestration.ingress import persist_events
    from agentplatform.core.packages.contract import load_package
    from agentplatform.jobs import tasks as T
    from agentplatform.runtime import Runtime, TenantRuntime, set_runtime

    random.seed(seed)
    await get_pool()
    d = yaml.safe_load((ROOT / "config/tenants/dev-consortium.yaml").read_text(encoding="utf-8"))
    d["concurrency"] = {**(d.get("concurrency") or {}), "debounce_window_s": 0.4, "debounce_max_wait_s": 2.0,
                        "echo_grace_s": 0.1}
    cfg = PlatformConfig.model_validate(d)

    tid = uuid.uuid4()
    acc = f"ig-load-{tid.hex[:8]}"
    async with platform_tx() as conn:
        await conn.execute(
            "insert into tenants (id, slug, display_name, segment_package, segment_package_version) values (%s,%s,%s,%s,%s)",
            (tid, f"load-{tid.hex[:8]}", "Load test tenant", "consortium", "0.1.0"))
        await conn.execute(
            "insert into channel_accounts (tenant_id, channel, external_account_id, external_page_id) values (%s,'instagram',%s,%s)",
            (tid, acc, acc))

    def respond(messages):
        last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        return draft(REPLY_2 if "como funciona" in last_user else REPLY_1)

    gw = ScriptedGateway({
        "analyze_turn": lambda m: analysis(intents=["greeting"], stage="engajado", evidence=["oi"]),
        "respond": respond,
        "output_judge": lambda m: {"violations": [], "explanation": "ok"},
        "summarize": lambda m: {"summary": "Cliente iniciou contato e quer saber como funciona."},
    })
    adapter = FakeChannelAdapter()
    sent_at: dict[str, list[float]] = defaultdict(list)
    orig_send = adapter.send

    async def timed_send(account, msg):
        res = await orig_send(account, msg)
        if msg.kind != "typing":
            sent_at[msg.recipient_external_id].append(time.perf_counter())
        return res
    adapter.send = timed_send  # type: ignore[method-assign]

    rt = Runtime(adapters={"instagram": adapter}, gateway=gw, inbox=FallbackInbox([LogNotifier()]))
    rt._tenants[str(tid)] = (1e18, TenantRuntime(tid, cfg, None, load_package("consortium"), None))
    rt.use_checkpointer = True
    set_runtime(rt)

    sched = Scheduler(n_workers, T)
    first_in: dict[str, float] = {}
    second_in: dict[str, float] = {}
    inbound_total = 0

    def ev(text: str, igsid: str) -> CanonicalEvent:
        mid = uuid.uuid4().hex
        return CanonicalEvent(event_type="MessageReceived", channel="instagram", account_external_id=acc,
                              contact_external_id=igsid, idempotency_key=f"msg:{mid}", mid=mid,
                              provider_ts=datetime.now(timezone.utc), text=text)

    async def send_inbound(igsid: str, text: str) -> None:
        nonlocal inbound_total
        created = await persist_events([ev(text, igsid)])
        inbound_total += len(created)
        sched.enqueue([("ingress", {"tenant_id": str(t), "event_id": e}, {}) for t, e in created])

    async def customer(i: int) -> None:
        igsid = f"IG-LOAD-{tid.hex[:6]}-{i:04d}"
        await asyncio.sleep(random.uniform(0, 1.0))              # arrivals spread over 1 s
        first_in[igsid] = time.perf_counter()
        for text in ("oi", "tudo bem?", "queria saber de consórcio"):
            await send_inbound(igsid, text)
            await asyncio.sleep(random.uniform(0, 0.15))
        deadline = time.perf_counter() + 60
        while not sent_at[igsid] and time.perf_counter() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(random.uniform(0.2, 0.8))           # customer reads, then asks again
        second_in[igsid] = time.perf_counter()
        await send_inbound(igsid, "e como funciona?")
        while len(sent_at[igsid]) < 2 and time.perf_counter() < deadline + 60:
            await asyncio.sleep(0.05)

    t_start = time.perf_counter()
    await sched.run_until_idle(asyncio.gather(*(customer(i) for i in range(n_conv))))
    wall = time.perf_counter() - t_start

    # ---------------- invariants ----------------
    failures: list[str] = list(sched.errors)
    texts_by_recipient: dict[str, list[str]] = defaultdict(list)
    for r in adapter.sent:
        texts_by_recipient[r.msg.recipient_external_id].append(r.msg.text or "")
    for igsid in first_in:
        got = texts_by_recipient.get(igsid, [])
        if got != [REPLY_1, REPLY_2]:
            failures.append(f"{igsid}: replies {got!r}")
    turns_per_conv = [v for (name, _), v in sched.per_conv.items() if name == "run_turn"]
    if len(turns_per_conv) != n_conv or any(v != 2 for v in turns_per_conv):
        failures.append(f"run_turn per conversation: {Counter(turns_per_conv)} (expected {n_conv} x 2)")
    async with tenant_tx(tid) as conn:
        r = await (await conn.execute(
            """select count(*) filter (where direction='in') as inbound,
                      count(*) filter (where direction='in' and turn_id is null) as unconsumed,
                      count(*) filter (where direction='out_bot') as out_bot
               from messages where tenant_id=%s""", (tid,))).fetchone()
        o = await (await conn.execute(
            "select status, count(*) as n from outbox where tenant_id=%s group by status", (tid,))).fetchall()
        t = await (await conn.execute(
            "select status, count(*) as n from turns where tenant_id=%s group by status", (tid,))).fetchall()
    outbox = {x["status"]: x["n"] for x in o}
    turns = {x["status"]: x["n"] for x in t}
    if r["inbound"] != inbound_total or r["unconsumed"]:
        failures.append(f"inbound stored={r['inbound']} expected={inbound_total} unconsumed={r['unconsumed']}")
    if set(outbox) - {"sent"}:
        failures.append(f"outbox statuses {outbox}")

    lat1 = [sent_at[k][0] - first_in[k] for k in first_in if sent_at[k]]
    lat2 = [sent_at[k][1] - second_in[k] for k in second_in if len(sent_at[k]) > 1]
    rt_d = sched.durations.get("run_turn", [])
    summary = {
        "conversations": n_conv, "workers": n_workers, "inbound_messages": inbound_total,
        "replies": sum(len(v) for v in texts_by_recipient.values()), "wall_s": round(wall, 2),
        "throughput_replies_per_s": round(sum(len(v) for v in texts_by_recipient.values()) / wall, 2),
        "jobs": dict(sched.ran),
        "e2e_first_reply_s": {"p50": round(pct(lat1, 50), 3), "p95": round(pct(lat1, 95), 3), "max": round(max(lat1, default=0), 3)},
        "e2e_second_reply_s": {"p50": round(pct(lat2, 50), 3), "p95": round(pct(lat2, 95), 3), "max": round(max(lat2, default=0), 3)},
        "run_turn_s": {"p50": round(pct(rt_d, 50), 3), "p95": round(pct(rt_d, 95), 3),
                       "mean": round(statistics.fmean(rt_d), 3) if rt_d else None},
        "dispatch_s_p95": round(pct(sched.durations.get("dispatch", []), 95), 3),
        "turn_statuses": turns, "outbox_statuses": outbox,
        "invariants": "ok" if not failures else f"{len(failures)} failure(s)",
        "debounce_window_s": cfg.concurrency.debounce_window_s,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    for f in failures[:20]:
        print("FAIL:", f)
    await close_pool()
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--conversations", type=int, default=50)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.conversations, a.workers, a.seed)))
