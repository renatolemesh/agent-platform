"""Operational visibility (plan 10 §1, F1-OBS-01): Prometheus metrics computed from the database at scrape time and
operator alerts evaluated every minute. Everything is derived from durable state, so any API/worker replica answers
the same thing. Alerts are deduplicated through audit_log (action 'ops.alert') with a cooldown from config."""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily
from psycopg import AsyncConnection

from agentplatform.core.config.schema import OpsAlertsConfig

log = logging.getLogger(__name__)


# Caps someone should hear about: the totals and today. Those per turn, conversation or experiment are enforced by the gateway at every call; as
# series or alerts they would be one more row for every turn, forever, and a turn reaching its own cap is not news. Used inside SQL, hence the %%.
_STANDING_SCOPES = """scope not like 'turn:%%' and scope not like 'conversation:%%' and scope not like 'experiment:%%' and scope not like 'ci:%%'
    and (scope not like '%%day:%%' or scope like '%%day:' || to_char(now() at time zone 'utc', 'YYYY-MM-DD'))"""

# ---------- metrics ----------
_QUERIES: list[tuple[str, str, list[str], str]] = [
    ("agent_inbound_pending", "Inbound events not yet processed", ["tenant"],
     "select tenant_id::text as tenant, count(*) as v from inbound_events where status in ('pending','processing') group by 1"),
    ("agent_inbound_oldest_pending_seconds", "Age of the oldest unprocessed inbound event", ["tenant"],
     """select tenant_id::text as tenant, extract(epoch from now() - min(received_at)) as v from inbound_events
        where status in ('pending','processing') group by 1"""),
    ("agent_outbox_items", "Outbox items by status (last 24h)", ["tenant", "status"],
     """select tenant_id::text as tenant, status, count(*) as v from outbox where created_at > now() - interval '1 day'
        group by 1, 2"""),
    ("agent_outbox_oldest_pending_seconds", "Age of the oldest pending/sending/unknown outbox item", ["tenant"],
     """select tenant_id::text as tenant, extract(epoch from now() - min(greatest(created_at, not_before))) as v from outbox
        where status in ('pending','sending','unknown') and not_before <= now() group by 1"""),
    ("agent_turns_last_hour", "Turns finished in the last hour by status", ["tenant", "status"],
     """select tenant_id::text as tenant, status, count(*) as v from turns where started_at > now() - interval '1 hour'
        group by 1, 2"""),
    ("agent_turn_p95_seconds", "p95 turn duration over the last hour", ["tenant"],
     """select tenant_id::text as tenant,
               percentile_cont(0.95) within group (order by extract(epoch from finished_at - started_at)) as v
        from turns where finished_at is not null and started_at > now() - interval '1 hour' group by 1"""),
    ("agent_llm_cost_usd_today", "LLM cost since 00:00 UTC", ["tenant"],
     """select coalesce(tenant_id::text, 'platform') as tenant, coalesce(sum(cost_usd), 0) as v from llm_calls
        where created_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc' group by 1"""),
    ("agent_llm_errors_last_hour", "LLM calls that did not succeed in the last hour", ["tenant"],
     """select coalesce(tenant_id::text, 'platform') as tenant, count(*) as v from llm_calls
        where created_at > now() - interval '1 hour' and status <> 'ok' group by 1"""),
    ("agent_budget_used_ratio", "(spent + reserved) / cap per budget scope", ["tenant", "scope"],
     f"""select coalesce(tenant_id::text, 'key') as tenant, scope, (spent_usd + reserved_usd) / nullif(cap_usd, 0) as v
        from budget_balances where {_STANDING_SCOPES}"""),
    ("agent_handoffs_open", "Handoffs not yet taken by a human", ["tenant"],
     "select tenant_id::text as tenant, count(*) as v from handoffs where taken_at is null group by 1"),
    ("agent_handoffs_sla_breached", "Open handoffs past their SLA", ["tenant"],
     """select tenant_id::text as tenant, count(*) as v from handoffs where taken_at is null and sla_due_at < now()
        group by 1"""),
    ("agent_dead_letters_last_hour", "Dead letters recorded in the last hour", ["tenant"],
     """select coalesce(tenant_id::text, 'platform') as tenant, count(*) as v from dead_letters
        where at > now() - interval '1 hour' group by 1"""),
    ("agent_channel_token_invalid", "Channel accounts whose token is not valid", ["tenant", "channel"],
     """select tenant_id::text as tenant, channel, count(*) as v from channel_accounts
        where coalesce(token_status, 'unknown') = 'invalid' group by 1, 2"""),
    ("agent_kill_switches_on", "Kill switches currently on", ["scope"],
     "select scope, count(*) as v from kill_switches where is_on group by 1"),
    ("agent_review_findings_30d", "Problems found by a person reviewing conversations, last 30 days", ["tenant", "category"],
     """select tenant_id::text as tenant, coalesce(category, 'none') as category, count(*) as v from conversation_reviews
        where verdict = 'problem' and created_at > now() - interval '30 days' group by 1, 2"""),
    ("agent_jobs", "Queue jobs by queue and status", ["queue", "status"],
     """select queue_name as queue, status::text as status, count(*) as v from procrastinate_jobs
        where status in ('todo','doing','failed') group by 1, 2"""),
]


async def collect_metrics(conn: AsyncConnection, extra: dict[str, float] | None = None) -> bytes:
    families = []
    for name, help_, labels, sql in _QUERIES:
        fam = GaugeMetricFamily(name, help_, labels=labels)
        try:
            async with conn.transaction():      # savepoint: a broken query must not take the whole scrape down
                rows = await (await conn.execute(sql)).fetchall()
        except Exception as e:
            log.warning("metric %s failed: %s", name, type(e).__name__)
            continue
        for r in rows:
            fam.add_metric([str(r[k]) for k in labels], float(r["v"] or 0))
        families.append(fam)
    for name, value in (extra or {}).items():
        fam = GaugeMetricFamily(name, name.replace("_", " "))
        fam.add_metric([], value)
        families.append(fam)

    class _C:
        def collect(self):
            return iter(families)

    reg = CollectorRegistry(auto_describe=False)
    reg.register(_C())
    return generate_latest(reg)


# ---------- alerts ----------
@dataclass(frozen=True)
class OpsAlert:
    key: str                  # dedupe key
    tenant_id: UUID | None
    title: str
    detail: str
    sticky: bool = False      # True = alert once per key (budget thresholds), else repeat after the cooldown


async def evaluate_alerts(conn: AsyncConnection, tenant_id: UUID, cfg: OpsAlertsConfig,
                          budget_thresholds: list[float]) -> list[OpsAlert]:
    """Tenant-scoped checks (run inside tenant_tx)."""
    out: list[OpsAlert] = []
    t = str(tenant_id)
    r = await (await conn.execute(
        """select count(*) as n, extract(epoch from now() - min(received_at)) / 60 as age_min from inbound_events
           where status in ('pending','processing','failed') and received_at < now() - make_interval(mins => %s)""",
        (cfg.inbound_backlog_minutes,))).fetchone()
    if r["n"]:
        out.append(OpsAlert(f"inbound_backlog:{t}", tenant_id, "Mensagens recebidas sem processar",
                            f"{r['n']} evento(s) parados há até {int(r['age_min'])} min. Verifique o worker e a fila 'ingress'."))
    r = await (await conn.execute(
        """select count(*) as n from outbox where status in ('pending','sending','unknown')
           and greatest(created_at, not_before) < now() - make_interval(mins => %s)""", (cfg.outbox_stuck_minutes,))).fetchone()
    if r["n"]:
        out.append(OpsAlert(f"outbox_stuck:{t}", tenant_id, "Envios parados no outbox",
                            f"{r['n']} item(ns) há mais de {cfg.outbox_stuck_minutes} min sem envio. Veja o runbook de fila travada."))
    r = await (await conn.execute(
        "select count(*) as n from post_drafts where status in ('failed','unknown') and updated_at > now() - interval '1 day'")).fetchone()
    if r["n"]:
        out.append(OpsAlert(f"posts_attention:{t}", tenant_id, "Posts que não saíram",
                            f"{r['n']} post(s) aprovados não foram publicados ou precisam ser conferidos no Instagram. Veja Posts no console."))
    r = await (await conn.execute(
        "select count(*) as n from turns where status='failed' and started_at > now() - interval '1 hour'")).fetchone()
    if r["n"] >= cfg.turn_failures_per_hour:
        out.append(OpsAlert(f"turn_failures:{t}", tenant_id, "Turnos falhando",
                            f"{r['n']} turno(s) com falha na última hora."))
    r = await (await conn.execute(
        """select count(*) as n from handoffs where notified_at is null and notify_at < now() - make_interval(mins => %s)""",
        (cfg.handoff_undelivered_minutes,))).fetchone()
    if r["n"]:
        out.append(OpsAlert(f"handoff_undelivered:{t}", tenant_id, "Vendedor sem aviso",
                            f"{r['n']} pedido(s) de atendimento sem aviso entregue há mais de {cfg.handoff_undelivered_minutes} min. "
                            "Confira o e-mail e o WhatsApp de avisos do vendedor."))
    r = await (await conn.execute(
        "select count(*) as n from ownership_events where cause = 'standby' and at > now() - interval '24 hours'")).fetchone()
    if r["n"]:
        out.append(OpsAlert(f"standby:{t}", tenant_id, "Outro app atende esta conta",
                            f"{r['n']} conversa(s) chegaram com outro app como principal e foram para o vendedor. "
                            "Confira o roteamento de conversas na Meta Business Suite."))
    for row in await (await conn.execute(
            "select scope_ref, reason from kill_switches where tenant_id = %s and scope = 'channel' and is_on and reason = 'channel_error'",
            (tenant_id,))).fetchall():
        out.append(OpsAlert(f"channel_error:{t}:{row['scope_ref']}", tenant_id, "Canal parado pelo provedor",
                            f"O canal {row['scope_ref']} foi recusado pelo provedor no envio e está pausado. As mensagens ficam pendentes. "
                            "Veja last_error no outbox; reconectar pelo console libera."))
    r = await (await conn.execute(
        "select count(*) as n from llm_calls where tenant_id = %s and status = 'provider_out_of_credit' and created_at > now() - interval '1 hour'",
        (tenant_id,))).fetchone()
    if r["n"]:
        out.append(OpsAlert(f"provider_out_of_credit:{t}", tenant_id, "Provedor de IA sem crédito",
                            "O provedor recusou chamadas por falta de crédito (HTTP 402). Recarregue a conta do provedor."))
    r = await (await conn.execute(
        """select count(*) as live, max(e.last) as last from channel_accounts ca
           left join lateral (select max(received_at) as last from inbound_events ie where ie.channel_account_id = ca.id
                              and ie.event_type <> 'CommentReceived') e on true
           where coalesce(ca.token_status, 'unknown') <> 'invalid' and ca.token_enc is not null""")).fetchone()
    if r["live"]:
        quiet = await (await conn.execute(
            "select %s::timestamptz is null or %s::timestamptz < now() - make_interval(hours => %s) as quiet",
            (r["last"], r["last"], cfg.no_inbound_hours))).fetchone()
        if quiet["quiet"]:
            out.append(OpsAlert(f"no_inbound:{t}", tenant_id, "Nenhum webhook recebido",
                                f"Sem eventos há mais de {cfg.no_inbound_hours} h. Verifique a inscrição do app e o token."))
    for row in await (await conn.execute(
            "select channel, external_account_id from channel_accounts where token_status = 'invalid'")).fetchall():
        out.append(OpsAlert(f"token_invalid:{t}:{row['channel']}:{row['external_account_id']}", tenant_id,
                            "Token do canal inválido", f"Canal {row['channel']} pausado. Reconecte pelo console (Facebook Login)."))
    for row in await (await conn.execute(
            f"select scope, cap_usd, spent_usd, reserved_usd from budget_balances where tenant_id = %s and {_STANDING_SCOPES}", (tenant_id,))).fetchall():
        out.extend(_budget_alerts(tenant_id, row, budget_thresholds))
    return out


def _budget_alerts(tenant_id: UUID | None, row: dict, thresholds: list[float]) -> list[OpsAlert]:
    cap = Decimal(row["cap_usd"] or 0)
    if cap <= 0:
        return []
    ratio = (Decimal(row["spent_usd"]) + Decimal(row["reserved_usd"])) / cap
    crossed = [th for th in sorted(thresholds) if ratio >= Decimal(str(th))]
    if not crossed:
        return []
    th = crossed[-1]
    who = str(tenant_id) if tenant_id else "key"
    # the cap is part of the key: after a top-up the same percentage is news again
    return [OpsAlert(f"budget:{who}:{row['scope']}:{cap:.4f}:{th}", tenant_id, f"Orçamento em {int(th * 100)}%",
                     f"Escopo {row['scope']}: US$ {row['spent_usd']:.4f} gastos de US$ {cap:.2f}.", sticky=True)]


async def _restore_test_alerts(conn: AsyncConnection, cfg: OpsAlertsConfig) -> list[OpsAlert]:
    """The test runs on the host, out of sight: what is noticed here is a failure, or the record going quiet. An overdue test is repeated weekly."""
    r = await (await conn.execute(
        """select (select id from restore_tests where status = 'failed' and finished_at >
                     coalesce((select max(finished_at) from restore_tests where status = 'ok'), 'epoch') order by finished_at desc limit 1) as failed_id,
                  coalesce(max(finished_at) filter (where status = 'ok') < now() - make_interval(days => %s), true) as overdue,
                  to_char(now(), 'IYYY-IW') as week from restore_tests""", (cfg.restore_test_max_age_days,))).fetchone()
    out = []
    if r["failed_id"]:
        out.append(OpsAlert(f"restore_test_failed:{r['failed_id']}", None, "Teste de restore falhou",
                            "O último teste de restore do backup falhou. Veja o log no servidor e o runbook de restore.", sticky=True))
    if r["overdue"]:
        out.append(OpsAlert(f"restore_test_overdue:{r['week']}", None, "Teste de restore atrasado",
                            f"Nenhum teste de restore bem-sucedido nos últimos {cfg.restore_test_max_age_days} dias. "
                            "Um backup nunca restaurado não é garantia. Veja o runbook de restore.", sticky=True))
    return out


def disk_used_ratio(path: str = "/") -> float:
    """The disk this container sits on. On one machine it is the same disk as the database volume and the backups waiting to go out."""
    usage = shutil.disk_usage(path)
    return usage.used / usage.total


async def evaluate_platform_alerts(conn: AsyncConnection, cfg: OpsAlertsConfig, spool_bytes: int, budget_thresholds: list[float],
                                   unreadable_bytes: int = 0, restore_tests_required: bool = False, disk_used: float | None = None) -> list[OpsAlert]:
    """Platform-scoped checks (run inside platform_tx)."""
    out: list[OpsAlert] = []
    if disk_used is not None and disk_used * 100 >= cfg.disk_used_percent:
        out.append(OpsAlert("disk", None, "Disco quase cheio",
                            f"{disk_used:.0%} usado. Com o disco cheio o banco para e nenhuma mensagem é gravada. Libere espaço; veja o runbook do Postgres."))
    if restore_tests_required:
        out.extend(await _restore_test_alerts(conn, cfg))
    if spool_bytes >= cfg.spool_bytes:
        out.append(OpsAlert("spool", None, "Spool de webhooks crescendo",
                            f"{spool_bytes} bytes aguardando replay. O Postgres pode estar fora; veja o runbook do spool."))
    r = await (await conn.execute("select count(*) as n from dead_letters where at > now() - interval '1 hour'")).fetchone()
    if r["n"] >= cfg.dead_letters_per_hour:
        out.append(OpsAlert("dead_letters", None, "Dead letters novas", f"{r['n']} na última hora (tabela dead_letters)."))
    r = await (await conn.execute(
        """select count(distinct job_id) as n from procrastinate_events where type = 'failed' and at > now() - interval '1 hour'""")).fetchone()
    if r["n"]:
        out.append(OpsAlert("jobs_failed", None, "Jobs falhos na fila", f"{r['n']} job(s) falharam de vez na última hora."))
    r = await (await conn.execute(
        """select coalesce(last_failed_time > coalesce(last_archived_time, 'epoch')
                   and last_failed_time < now() - make_interval(mins => %s), false) as failing from pg_stat_archiver""",
        (cfg.archive_failing_minutes,))).fetchone()
    if r and r["failing"]:
        out.append(OpsAlert("wal_archive", None, "Backup do banco falhando",
                            "O envio do WAL para o armazenamento está falhando: sem backup contínuo, e o disco enche. Veja o runbook de backup."))
    if unreadable_bytes:
        out.append(OpsAlert("unreadable_webhooks", None, "Webhooks que não consegui ler",
                            f"{unreadable_bytes} bytes em unreadable.jsonl no spool: formato inesperado do provedor. Pode haver mensagem de cliente ali."))
    for row in await (await conn.execute(
            f"select scope, cap_usd, spent_usd, reserved_usd from budget_balances where tenant_id is null and {_STANDING_SCOPES}")).fetchall():
        out.extend(_budget_alerts(None, row, budget_thresholds))
    return out


async def should_send(conn: AsyncConnection, alert: OpsAlert, cooldown_minutes: int) -> bool:
    """Dedupe through audit_log. The record is written by record_sent only after delivery: an alert that did not get out is tried
    again at the next tick instead of being silenced for the whole cooldown."""
    if alert.sticky:
        seen = await (await conn.execute(
            "select 1 from audit_log where action='ops.alert' and target=%s limit 1", (alert.key,))).fetchone()
    else:
        seen = await (await conn.execute(
            "select 1 from audit_log where action='ops.alert' and target=%s and at > now() - make_interval(mins => %s) limit 1",
            (alert.key, cooldown_minutes))).fetchone()
    return not seen


async def record_sent(conn: AsyncConnection, alert: OpsAlert) -> None:
    await conn.execute(
        """insert into audit_log (tenant_id, actor_kind, action, target, after)
           values (%s, 'system', 'ops.alert', %s, jsonb_build_object('title', %s::text, 'detail', %s::text))""",
        (alert.tenant_id, alert.key, alert.title, alert.detail))


class OpsNotifier:
    """Sends operator alerts by e-mail and/or WhatsApp (utility template), falling back to the log."""

    def __init__(self, email=None, email_to: str | None = None, whatsapp=None, whatsapp_to: str | None = None):
        self.email, self.email_to, self.whatsapp, self.whatsapp_to = email, email_to, whatsapp, whatsapp_to
        self.sent: list[OpsAlert] = []

    async def send(self, alert: OpsAlert) -> bool:
        """True when it was delivered, or when no channel is configured (dev: the log is all there is). False when a configured
        channel failed: not recorded, so the next tick tries again."""
        ok = False
        attempted = False
        text = f"{alert.detail}\n\nTenant: {alert.tenant_id or 'plataforma'}"
        if self.whatsapp and self.whatsapp_to:
            attempted = True
            ok = await self.whatsapp.send_ops(self.whatsapp_to, alert.title, text) or ok
        if self.email and self.email_to:
            attempted = True
            ok = await self.email.send_text(self.email_to, f"[agentplatform] {alert.title}", text) or ok
        if not ok:
            log.warning("OPS ALERT %s: %s — %s", alert.key, alert.title, alert.detail)
        self.sent.append(alert)
        return ok or not attempted
