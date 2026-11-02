"""Budget reservation/settlement (ADR-011, plan 08 §3).

Scopes:
  key:total, key:day:<UTC date>          process-wide caps for the provider key (tenant_id NULL) — hard limit
  day:<UTC date>, conversation:<id>, turn:<id>, experiment:<id>, ci:<date>   per-tenant caps from PlatformConfig
Reservation is one transaction: every scope must accept `spent + reserved + est <= cap`, else nothing is reserved.
Fail-safe: any database error => denied.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from agentplatform.core.db.pool import platform_tx

log = logging.getLogger(__name__)
_NIL = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class ScopeCap:
    scope: str
    cap: Decimal
    tenant_id: UUID | None


@dataclass
class Reservation:
    id: UUID
    amount: Decimal
    scopes: list[ScopeCap]
    denied: bool = False
    denied_scope: str | None = None


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


class BudgetService:
    def __init__(self, key_total_cap: Decimal, key_daily_cap: Decimal, key_name: str = "key"):
        self.key_total_cap = key_total_cap
        self.key_daily_cap = key_daily_cap
        self.key_name = key_name          # tests use an isolated name so they never touch the real key's balance

    def key_scopes(self) -> list[ScopeCap]:
        return [ScopeCap(f"{self.key_name}:total", self.key_total_cap, None),
                ScopeCap(f"{self.key_name}:day:{utc_day()}", self.key_daily_cap, None)]

    async def reserve(self, amount: Decimal, scopes: list[ScopeCap], ttl_s: int = 120) -> Reservation:
        all_scopes = self.key_scopes() + scopes
        res = Reservation(uuid4(), amount, all_scopes)
        try:
            async with platform_tx() as conn:
                for s in all_scopes:
                    await conn.execute(
                        """insert into budget_balances (tenant_id, scope, cap_usd) values (%s, %s, %s)
                           on conflict (coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid), scope)
                           do update set cap_usd = excluded.cap_usd""",
                        (s.tenant_id, s.scope, s.cap))
                    row = await (await conn.execute(
                        """update budget_balances set reserved_usd = reserved_usd + %s
                           where coalesce(tenant_id, %s::uuid) = coalesce(%s::uuid, %s::uuid) and scope = %s
                             and spent_usd + reserved_usd + %s <= cap_usd
                           returning scope""",
                        (amount, _NIL, s.tenant_id, _NIL, s.scope, amount))).fetchone()
                    if row is None:
                        res.denied, res.denied_scope = True, s.scope
                        raise _Denied()
                await conn.execute(
                    "insert into budget_reservations (id, tenant_id, scopes, amount_usd, expires_at) values (%s, null, %s, %s, %s)",
                    (res.id, [f"{s.tenant_id or ''}|{s.scope}" for s in all_scopes], amount,
                     datetime.now(timezone.utc) + timedelta(seconds=ttl_s)))
                await conn.execute(
                    "insert into budget_ledger (tenant_id, scope, kind, amount_usd) values (null, %s, 'reserve', %s)",
                    (f"{self.key_name}:total", amount))
        except _Denied:
            log.warning("budget denied at scope %s (amount %s)", res.denied_scope, amount)
        except Exception:
            log.exception("budget reservation failed; denying (fail-safe)")
            res.denied, res.denied_scope = True, "error"
        return res

    async def settle(self, res: Reservation, actual: Decimal, *, needs_reconcile: bool = False) -> None:
        if res.denied:
            return
        if actual > res.amount:
            # estimate was not an upper bound: keep the real cost (truth) and alert; caps may be exceeded by the delta
            log.error("budget overrun: actual %s > reserved %s (reservation %s)", actual, res.amount, res.id)
        async with platform_tx() as conn:
            done = await (await conn.execute(
                "update budget_reservations set settled_at = now(), settled_amount_usd = %s, reconciled = %s "
                "where id = %s and settled_at is null returning id", (actual, not needs_reconcile, res.id))).fetchone()
            if done is None:
                return   # already released by the sweeper; the sweeper charged the estimate
            for s in res.scopes:
                await conn.execute(
                    """update budget_balances set reserved_usd = greatest(reserved_usd - %s, 0), spent_usd = spent_usd + %s
                       where coalesce(tenant_id, %s::uuid) = coalesce(%s::uuid, %s::uuid) and scope = %s""",
                    (res.amount, actual, _NIL, s.tenant_id, _NIL, s.scope))
            await conn.execute(
                "insert into budget_ledger (tenant_id, scope, kind, amount_usd) values (null, %s, 'settle', %s)",
                (f"{self.key_name}:total", actual))

    async def release_expired(self) -> int:
        """Sweeper: reservations of dead workers are charged at their estimate (never 0) and released."""
        async with platform_tx() as conn:
            rows = await (await conn.execute(
                "update budget_reservations set settled_at = now(), settled_amount_usd = amount_usd, reconciled = false "
                "where settled_at is null and expires_at < now() returning scopes, amount_usd")).fetchall()
            for r in rows:
                for item in r["scopes"]:
                    tid, scope = item.split("|", 1)
                    await conn.execute(
                        """update budget_balances set reserved_usd = greatest(reserved_usd - %s, 0), spent_usd = spent_usd + %s
                           where coalesce(tenant_id, %s::uuid) = coalesce(nullif(%s, '')::uuid, %s::uuid) and scope = %s""",
                        (r["amount_usd"], r["amount_usd"], _NIL, tid, _NIL, scope))
        return len(rows)

    async def usage(self) -> dict[str, dict[str, Decimal]]:
        async with platform_tx() as conn:
            rows = await (await conn.execute(
                "select scope, cap_usd, spent_usd, reserved_usd from budget_balances where tenant_id is null")).fetchall()
        return {r["scope"]: {"cap": r["cap_usd"], "spent": r["spent_usd"], "reserved": r["reserved_usd"]} for r in rows}


class _Denied(Exception):
    pass


# ----- read models and top-ups for the console (F2-CON-04) -----
async def key_usage(key_name: str = "key") -> list[dict]:
    """Process-wide key scopes (total + recent days) — the hard cap itself is a process setting, never edited here."""
    async with platform_tx() as conn:
        return await (await conn.execute(
            """select scope, cap_usd, spent_usd, reserved_usd from budget_balances
               where tenant_id is null and (scope = %s or scope like %s) order by scope desc limit 15""",
            (f"{key_name}:total", f"{key_name}:day:%"))).fetchall()


async def tenant_usage(conn, tenant_id: UUID, days: int = 14) -> dict:
    """Tenant daily scopes and real cost per day/node/model (v_cost_daily), inside the caller's tenant transaction."""
    daily = await (await conn.execute(
        """select scope, cap_usd, spent_usd, reserved_usd from budget_balances
           where tenant_id = %s and scope like 'day:%%' order by scope desc limit %s""", (tenant_id, days))).fetchall()
    cost = await (await conn.execute(
        """select day, node, model_used, calls, cost_usd from v_cost_daily
           where tenant_id = %s and day >= now() - make_interval(days => %s) order by day desc, cost_usd desc""",
        (tenant_id, days))).fetchall()
    totals = await (await conn.execute(
        """select coalesce((select sum(net_usd) from budget_topups where tenant_id = %s), 0) as credit,
                  coalesce((select sum(cost_usd) from llm_calls where tenant_id = %s), 0) as spent""",
        (tenant_id, tenant_id))).fetchone()
    topups = await (await conn.execute(
        "select gross_usd, fee_usd, net_usd, note, config_version_id, created_at from budget_topups where tenant_id = %s "
        "order by created_at desc limit 20", (tenant_id,))).fetchall()
    return {"daily": daily, "cost": cost, "credit": totals["credit"], "spent": totals["spent"],
            "balance": totals["credit"] - totals["spent"], "topups": topups}


async def record_topup(conn, tenant_id: UUID, gross: Decimal, fee: Decimal, *, note: str | None, actor: UUID | None,
                       config_version_id: UUID | None) -> UUID:
    if gross <= 0 or fee < 0 or fee >= gross:
        raise ValueError("invalid top-up amounts")
    row = await (await conn.execute(
        """insert into budget_topups (tenant_id, gross_usd, fee_usd, net_usd, note, config_version_id, created_by)
           values (%s, %s, %s, %s, %s, %s, %s) returning id""",
        (tenant_id, gross, fee, gross - fee, note, config_version_id, actor))).fetchone()
    await conn.execute(
        "insert into audit_log (tenant_id, actor, actor_kind, action, target, after) values (%s,%s,'user','budget.topup',%s,%s)",
        (tenant_id, actor, str(row["id"]), json.dumps({"gross": str(gross), "fee": str(fee),
                                                        "config_version": str(config_version_id) if config_version_id else None})))
    return row["id"]
