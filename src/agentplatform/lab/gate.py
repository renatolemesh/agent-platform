"""The evaluation gate (plan 10 §3), used by the command line and by the button in the console. fast = deterministic, no spending. full = also
the live levels, stopping at budget.experiment_cap_usd: a run that could not finish does not pass. Everything runs on the simulator channel, so
it is safe beside the real workers and before go_live: nothing reaches a customer and no seller is told."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from agentplatform.core.config.registry import config_hash, resolve_with_package
from agentplatform.core.config.schema import NodeModel, PlatformConfig
from agentplatform.core.db.pool import tenant_tx
from agentplatform.core.packages.contract import SegmentPackage, load_package
from agentplatform.lab import runner as L

log = logging.getLogger(__name__)

# What a change must reach to be activated. Platform rules, on purpose not in the tenant's configuration: nobody lowers the bar of their own change.
GATES = {"compliance_block_rate": 1.0, "compliance_false_positive_free": 0.95, "input_guard": 1.0,
         "analyze_slots": 0.75, "respond": 0.85, "rag": 0.8, "scenarios": 0.85}
SCENARIO_REPEATS = 3            # pass^3: a scenario only counts if it passes every time (plan 10)
SIMULATED_CUSTOMER = "customer_simulator@v2"
MAX_FAILURES_KEPT = 12


@dataclass
class GateResult:
    scores: list[L.Score] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    cost: Decimal = Decimal(0)
    experiment_id: str | None = None
    hash: str = ""

    @property
    def passed(self) -> bool:
        return not self.failed

    def summary(self) -> list[dict]:
        return [{"name": s.name, "passed": s.passed, "total": s.total, "rate": round(s.rate, 4), "need": GATES.get(s.name, 1.0),
                 "ok": s.rate >= GATES.get(s.name, 1.0), "failures": s.failures[:MAX_FAILURES_KEPT]} for s in self.scores]


async def run_gate(cfg: PlatformConfig, pkg: SegmentPackage, slug: str, level: str, gateway=None, *, max_scenarios: int | None = None,
                   repeats: int = SCENARIO_REPEATS, verbose: bool = False, output=None) -> GateResult:
    cfg = resolve_with_package(cfg, pkg.manifest.compliance)
    res = GateResult(hash=config_hash(cfg))
    res.scores += [*L.eval_output_guard(cfg, pkg), L.eval_input_guard(cfg, pkg)]
    if level == "full":
        await _live_levels(cfg, pkg, slug, gateway, res, max_scenarios, repeats, verbose, output)
    res.failed = [s.name for s in res.scores if s.rate < GATES.get(s.name, 1.0)]
    res.experiment_id = await L.record_experiment(
        f"gate:{level}", {"config": slug, "config_hash": res.hash, "passed": res.passed, "repeats": repeats},
        {s.name: {"rate": s.rate, "failures": s.failures} for s in res.scores}, res.cost)
    return res


async def _live_levels(cfg, pkg, slug, gateway, res: GateResult, max_scenarios, repeats, verbose, output) -> None:
    cap = cfg.budget.experiment_cap_usd
    a = await L.eval_analyze(cfg, pkg, gateway)
    res.scores.append(a)
    res.cost += a.cost_usd
    r = await L.eval_respond(cfg, pkg, gateway, cap - res.cost)
    if r.total:
        res.scores.append(r)
        res.cost += r.cost_usd
    k = await L.eval_rag(cfg, pkg, gateway)
    if k.total:
        res.scores.append(k)
        res.cost += k.cost_usd
    sim = NodeModel(primary=cfg.models.analyze_turn.primary, prompt_version=SIMULATED_CUSTOMER, temperature=0.7, max_output_tokens=120)
    sc = L.Score("scenarios")
    for scn in L.load_jsonl(f"{pkg.manifest.name}/scenarios-v1.jsonl")[: max_scenarios]:
        ok, why = True, ""
        for attempt in range(repeats):
            if res.cost >= cap:
                ok, why = False, f"parou no teto de US$ {cap} antes de terminar"
                break
            out = await L.run_conversation(slug, scn, gateway, sim, cfg)
            good, reason = L.grade_scenario(scn, out, cfg)
            res.cost += (Decimal(out["final"]["cost"]) if out["final"] else Decimal(0)) + out["sim_cost"]
            if verbose and output:
                output(f"\n--- {scn['id']} #{attempt + 1} ({'OK' if good else 'FAIL'}) ---\n" + "\n".join(out["transcript"]))
            if not good:
                ok, why = False, reason
                break
        sc.add(ok, f"{scn['id']}: {why}" if why else scn["id"])
    res.scores.append(sc)


# ------------------------------------------------------------------ asked for in the console
async def request_evaluation(conn, tenant_id: UUID, version_id: UUID, level: str, user_id: UUID) -> UUID | None:
    """None when this tenant already has one running: two at once would only compete for the same budget."""
    row = await (await conn.execute(
        """insert into eval_requests (tenant_id, config_version_id, level, requested_by) values (%s,%s,%s,%s)
           on conflict (tenant_id) where status in ('queued','running') do nothing returning id""",
        (tenant_id, version_id, level, user_id))).fetchone()
    return row["id"] if row else None


def evaluation_job(tenant_id: UUID, request_id: UUID) -> tuple[str, dict, dict]:
    return ("config_gate", {"tenant_id": str(tenant_id), "request_id": str(request_id)}, {"queueing_lock": f"gate:{tenant_id}"})


async def run_requested(tenant_id: UUID, request_id: UUID, gateway) -> str:
    async with tenant_tx(tenant_id) as conn:
        req = await (await conn.execute(
            """update eval_requests set status='running', started_at=now() where id=%s and tenant_id=%s and status='queued'
               returning config_version_id, level""", (request_id, tenant_id))).fetchone()
        if not req:
            return "skipped"
        row = await (await conn.execute("select document from config_versions where id=%s", (req["config_version_id"],))).fetchone()
        slug = (await (await conn.execute("select slug from tenants where id=%s", (tenant_id,))).fetchone())["slug"]
    try:
        cfg = PlatformConfig.model_validate(row["document"])
        pkg = load_package(cfg.segment_package.name)
        res = await run_gate(cfg, pkg, slug, req["level"], gateway)
    except Exception as e:      # the reason for a person is "try again"; the detail stays in the log
        log.exception("evaluation %s failed", request_id)
        async with tenant_tx(tenant_id) as conn:
            await conn.execute("update eval_requests set status='error', error=%s, finished_at=now() where id=%s", (type(e).__name__, request_id))
        return "error"
    status = "passed" if res.passed else "failed"
    async with tenant_tx(tenant_id) as conn:
        await conn.execute(
            """update eval_requests set status=%s, experiment_id=%s, scores=%s, cost_usd=%s, finished_at=now() where id=%s""",
            (status, res.experiment_id, L.dumps(res.summary()), res.cost, request_id))
        if res.passed and req["level"] == "full":      # the approval in production looks at this, and only for this exact draft
            await conn.execute("update config_versions set eval_run_id=%s where id=%s and status='draft'", (res.experiment_id, req["config_version_id"]))
    return status


async def sweep_requests(conn, tenant_id: UUID, stuck_after_min: int) -> list[tuple[str, dict, dict]]:
    """A job lost with the worker. Queued: ask again. Running for too long: tell the person to try again."""
    await conn.execute(
        """update eval_requests set status='error', error='timeout', finished_at=now()
           where tenant_id=%s and status='running' and started_at < now() - make_interval(mins => %s)""", (tenant_id, stuck_after_min))
    rows = await (await conn.execute(
        "select id from eval_requests where tenant_id=%s and status='queued' and created_at < now() - interval '2 minutes'", (tenant_id,))).fetchall()
    return [evaluation_job(tenant_id, r["id"]) for r in rows]

