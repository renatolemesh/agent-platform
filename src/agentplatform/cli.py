"""agentplatform CLI: migrate | bootstrap | ingest | simulate | budget."""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from agentplatform.paths import ROOT


def cmd_migrate(_args) -> None:
    import subprocess

    import psycopg

    from agentplatform.settings import get_settings
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True)
    owner = get_settings().migration_database_url
    # procrastinate schema (owner); app_rw gets privileges via default privileges
    with psycopg.connect(owner, autocommit=True) as conn:
        exists = conn.execute("select to_regclass('procrastinate_jobs')").fetchone()[0]
        if not exists:
            import procrastinate
            schema = procrastinate.schema.SchemaManager.get_schema()
            conn.execute(schema)
            print("procrastinate schema applied")
    # LangGraph checkpointer tables (owner)
    async def _lg():
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        async with AsyncPostgresSaver.from_conn_string(owner) as saver:
            await saver.setup()
    asyncio.run(_lg())
    with psycopg.connect(owner, autocommit=True) as conn:
        conn.execute("grant select, insert, update, delete on all tables in schema public to app_rw")
        conn.execute("grant usage, select on all sequences in schema public to app_rw")
        conn.execute("grant execute on all functions in schema public to app_rw")
    print("migrations OK")


async def _bootstrap(args) -> None:
    from agentplatform.api.auth import hash_password
    from agentplatform.core.config.registry import load_yaml, registry
    from agentplatform.core.db.pool import close_pool, platform_tx, tenant_tx
    from agentplatform.core.packages.contract import load_package
    from agentplatform.core.security.keys import crypto, encrypt_field, ensure_dek
    cfg = load_yaml(ROOT / "config" / "tenants" / f"{args.slug}.yaml")
    pkg = load_package(cfg.segment_package.name)
    async with platform_tx() as conn:
        t = await (await conn.execute("select id from tenants where slug=%s", (args.slug,))).fetchone()
        tid = t["id"] if t else uuid.uuid4()
        if not t:
            await conn.execute(
                "insert into tenants (id, slug, display_name, segment_package, segment_package_version) values (%s,%s,%s,%s,%s)",
                (tid, args.slug, args.name, pkg.manifest.name, pkg.manifest.version))
    async with tenant_tx(tid) as conn:
        await ensure_dek(conn, tid)
        acc = await (await conn.execute(
            "select id from channel_accounts where tenant_id=%s and channel='instagram' order by id limit 1", (tid,))).fetchone()
        if acc and args.page_id:
            # connect a real page to an existing (e.g. placeholder dev) account: IDs and, if given, the page token
            token = await encrypt_field(conn, tid, args.page_token, "channel_accounts.token") if args.page_token else None
            await conn.execute(
                """update channel_accounts set external_account_id=%s, external_page_id=%s,
                          token_enc=coalesce(%s, token_enc),
                          token_status=case when %s::bytea is not null then 'valid' else token_status end
                   where id=%s""", (args.ig_account, args.page_id, token, token, acc["id"]))
            print(f"[bootstrap] conta instagram atualizada: ig={args.ig_account} page={args.page_id}"
                  f"{' (token gravado)' if token else ''}")
        if not acc:
            await conn.execute(
                "insert into channel_accounts (tenant_id, channel, external_account_id, external_page_id, token_enc, scopes) values (%s,'instagram',%s,%s,%s,%s)",
                (tid, args.ig_account, args.page_id or args.ig_account,
                 await encrypt_field(conn, tid, args.page_token, "channel_accounts.token") if args.page_token else None,
                 ["instagram_basic", "instagram_manage_messages", "instagram_manage_comments", "pages_messaging"]))
        if args.seller_email:
            h = crypto().lookup_hash(args.seller_email, kind="email")
            if not await (await conn.execute("select 1 from tenant_users where email_hash=%s", (h,))).fetchone():
                if not args.seller_password:
                    args.seller_password = secrets.token_urlsafe(18)
                    print(f"[bootstrap] senha gerada para {args.seller_email} (mostrada só agora): {args.seller_password}", file=sys.stderr)
                await conn.execute(
                    """insert into tenant_users (tenant_id, email_hash, email_enc, display_name, role, password_hash,
                                                 notify_whatsapp_enc, notify_email_enc) values (%s,%s,%s,%s,'tenant_owner',%s,%s,%s)""",
                    (tid, h, await encrypt_field(conn, tid, args.seller_email, "tenant_users.email"), args.seller_name,
                     hash_password(args.seller_password), await encrypt_field(conn, tid, args.seller_whatsapp, "tenant_users.whatsapp")
                     if args.seller_whatsapp else None, await encrypt_field(conn, tid, args.seller_email, "tenant_users.email")))
        _ensure_kb_index(cfg)
        activate = cfg.environment != "prod"      # production only activates after the gate: see config-activate
        vid = await registry.publish(conn, tid, cfg, package_compliance=pkg.manifest.compliance, activate=activate)
    await close_pool()
    print(json.dumps({"tenant_id": str(tid), "config_version_id": str(vid), "active": activate}))
    if not activate:
        print("[bootstrap] config de produção publicada como rascunho. Rode `lab gate --level full` e depois `config-activate`.", file=sys.stderr)


def _ensure_kb_index(cfg) -> str:
    """The search index of this config. Needs the owner role (DDL)."""
    import psycopg

    from agentplatform.settings import get_settings
    emb = cfg.models.embedding
    with psycopg.connect(get_settings().migration_database_url, autocommit=True) as oc:
        row = oc.execute(
            "insert into kb_indexes (name, strategy, embedding_model, dims, status) values (%s,%s,%s,%s,'active') "
            "on conflict (name) do update set status='active' returning id, embedding_model, dims",
            (cfg.rag.active_index_id, json.dumps({"chunking": "headings_1400c_200o"}), emb.model, emb.dims)).fetchone()
        if (row[1], row[2]) != (emb.model, emb.dims):
            raise SystemExit(f"o índice {cfg.rag.active_index_id} foi criado com {row[1]}/{row[2]} e a config pede {emb.model}/{emb.dims}: "
                             "use outro rag.active_index_id ou troque o modelo pela Base de conhecimento")
        idx = str(row[0])
        oc.execute(f"create index if not exists kb_chunks_hnsw_{idx.replace('-', '')} on kb_chunks using hnsw "
                   f"((embedding::vector({emb.dims})) vector_cosine_ops) where index_id = '{idx}'")
    return idx


async def _ingest(args) -> None:
    """Loads the operator's folder for review in the console. Approves nothing and calls no provider: vectors come with a person's approval."""


    from agentplatform.core.config.registry import load_yaml
    from agentplatform.core.db.pool import close_pool, platform_tx, tenant_tx
    from agentplatform.core.knowledge.admin import load_file, material_to_load, seed_faqs
    from agentplatform.core.packages.contract import load_package
    cfg = load_yaml(ROOT / "config" / "tenants" / f"{args.slug}.yaml")
    pkg = load_package(cfg.segment_package.name)
    idx = _ensure_kb_index(cfg)
    async with platform_tx() as conn:
        t = await (await conn.execute("select id from tenants where slug=%s", (args.slug,))).fetchone()
    if not t:
        raise SystemExit(f"tenant {args.slug} não existe: rode `bootstrap` antes")
    tid = t["id"]
    results = []
    async with tenant_tx(tid) as conn:
        results.append({"faq_drafts": await seed_faqs(conn, tid, pkg.manifest.name, pkg.manifest.faqs_seed)})
    load, skipped = material_to_load(Path(args.dir), [*pkg.manifest.never_import_name_hashes, *cfg.operations.knowledge.never_import_name_hashes])
    results += skipped
    for path, meta in load:
        async with tenant_tx(tid) as conn:
            results.append(await load_file(conn, tid, path.read_bytes(), meta, idx, pkg.manifest.name, cfg.operations.knowledge))
    await close_pool()
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    print("[ingest] tudo fica Em revisão até alguém aprovar em Base de conhecimento e Perguntas frequentes.", file=sys.stderr)


async def _simulate(args) -> None:
    """Chat with the real agent through the full pipeline (fake channel). Spends real budget (fractions of a cent per turn)."""
    from agentplatform.core.channels.events import CanonicalEvent
    from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
    from agentplatform.core.db.pool import close_pool, platform_tx, tenant_tx
    from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier
    from agentplatform.core.orchestration.ingress import persist_events
    from agentplatform.jobs.pump import pump
    from agentplatform.runtime import Runtime, set_runtime
    adapter = FakeChannelAdapter()
    rt = Runtime(adapters={"instagram": adapter}, inbox=FallbackInbox([LogNotifier()]))
    set_runtime(rt)
    async with platform_tx() as conn:
        t = await (await conn.execute(
            "select t.id, ca.external_account_id from tenants t join channel_accounts ca on ca.tenant_id=t.id and ca.channel='instagram' where t.slug=%s",
            (args.slug,))).fetchone()
    igsid = args.contact or f"SIM-{uuid.uuid4().hex[:6]}"
    lines = args.say or []
    interactive = not lines
    print(f"[simulate] tenant={args.slug} contact={igsid} (Ctrl+C para sair)")
    while True:
        if interactive:
            try:
                text = input("cliente> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
        else:
            if not lines:
                break
            text = lines.pop(0)
            print(f"cliente> {text}")
        sent_before = len(adapter.sent)
        ev = CanonicalEvent(event_type="MessageReceived", channel="instagram", account_external_id=t["external_account_id"],
                            contact_external_id=igsid, idempotency_key=f"sim:{uuid.uuid4().hex}", mid=uuid.uuid4().hex,
                            provider_ts=datetime.now(timezone.utc), text=text)
        created = await persist_events([ev])
        await pump([("ingress", {"tenant_id": str(tt), "event_id": e}, {}) for tt, e in created], honor_delays=True)
        for r in adapter.sent[sent_before:]:
            print(f"bot> {r.msg.text}")
        async with tenant_tx(t["id"]) as conn:
            st = await (await conn.execute(
                """select l.stage, c.ownership, (select coalesce(sum(cost_usd),0) from turns where conversation_id=c.id) as cost
                   from conversations c join contact_identities ci on ci.contact_id=c.contact_id
                   join leads l on l.contact_id=c.contact_id where ci.external_user_id=%s""", (igsid,))).fetchone()
        print(f"   [estágio={st['stage']} posse={st['ownership']} custo_conversa=US${float(st['cost']):.5f}]")
    await close_pool()


async def _config_activate(args) -> None:
    """Production activation: only the exact document a passing full gate evaluated (the experiment records its hash)."""
    from agentplatform.core.config.registry import ConfigError, config_hash, load_yaml, registry, resolve_with_package
    from agentplatform.core.db.pool import close_pool, platform_tx, tenant_tx
    from agentplatform.core.packages.contract import load_package
    cfg = load_yaml(ROOT / "config" / "tenants" / f"{args.slug}.yaml")
    pkg = load_package(cfg.segment_package.name)
    resolved = resolve_with_package(cfg, pkg.manifest.compliance)
    async with platform_tx() as conn:
        exp = await (await conn.execute("select dataset, variants from experiments where id=%s", (args.experiment,))).fetchone()
        t = await (await conn.execute("select id from tenants where slug=%s", (args.slug,))).fetchone()
    if not t:
        raise ConfigError(f"tenant {args.slug} não existe: rode bootstrap antes")
    v = (exp or {}).get("variants") or {}
    if not exp or exp["dataset"] != "gate:full" or v.get("passed") is not True:
        raise ConfigError("este experimento não é um gate full aprovado")
    if v.get("config_hash") != config_hash(resolved):
        raise ConfigError("o arquivo mudou depois do gate: rode o gate de novo")
    async with tenant_tx(t["id"]) as conn:
        vid = await registry.publish(conn, t["id"], cfg, package_compliance=pkg.manifest.compliance, eval_run_id=args.experiment, activate=True)
    await close_pool()
    print(json.dumps({"tenant_id": str(t["id"]), "config_version_id": str(vid), "active": True}))


async def _lab(args) -> None:
    """Evaluation gate. fast = deterministic (no LLM, every PR). full = + live node and conversation levels. With --config-version
    it evaluates a draft made in the console and, when a full gate passes, records that on the draft so it can be approved."""
    from agentplatform.core.config.registry import load_yaml
    from agentplatform.core.config.schema import PlatformConfig
    from agentplatform.core.db.pool import close_pool, platform_tx
    from agentplatform.core.packages.contract import load_package
    from agentplatform.lab import gate as G
    if args.config_version:
        async with platform_tx() as conn:
            doc = await (await conn.execute("select document from config_versions where id=%s", (args.config_version,))).fetchone()
        cfg = PlatformConfig.model_validate(doc["document"])
    else:
        cfg = load_yaml(ROOT / "config" / "tenants" / f"{args.slug}.yaml")
    pkg = load_package(cfg.segment_package.name)
    gw = None
    if args.level == "full":
        from agentplatform.core.models.gateway import get_gateway
        gw = get_gateway()
    res = await G.run_gate(cfg, pkg, args.slug, args.level, gw, max_scenarios=args.max_scenarios, repeats=args.repeats, verbose=args.verbose, output=print)
    for s in res.scores:
        need = G.GATES.get(s.name, 1.0)
        print(f"{'PASS' if s.rate >= need else 'FAIL'}  {s.name:32s} {s.passed}/{s.total} ({s.rate:.0%}, gate {need:.0%})"
              + (f"  failures: {s.failures}" if s.failures else ""))
    if res.partial and args.level == "full":
        print("avaliação parcial: serve para medir, mas não libera a ativação")
    if args.config_version and args.level == "full" and res.passed and not res.partial:
        async with platform_tx() as conn:
            await conn.execute("update config_versions set eval_run_id=%s where id=%s and status='draft'", (res.experiment_id, args.config_version))
    await close_pool()
    print(f"experiment={res.experiment_id} cost=US${res.cost:.5f}")
    if res.failed:
        sys.exit(1)


async def _budget(_args) -> None:
    from agentplatform.core.db.pool import close_pool
    from agentplatform.core.models.gateway import get_gateway
    u = await get_gateway().budget.usage()
    await close_pool()
    for k, v in sorted(u.items()):
        if k.startswith("key:"):
            print(f"{k}: spent={v['spent']} reserved={v['reserved']} cap={v['cap']}")


async def _user_add(args) -> None:
    """Extra console user (e.g. the second person who approves config changes and top-ups)."""
    import getpass

    from agentplatform.api.auth import hash_password
    from agentplatform.core.db.pool import close_pool, platform_tx, tenant_tx
    from agentplatform.core.security.keys import crypto, encrypt_field
    password = args.password or getpass.getpass("senha: ")
    if len(password) < 10:
        raise SystemExit("a senha precisa ter pelo menos 10 caracteres")
    async with platform_tx() as conn:
        t = await (await conn.execute("select id from tenants where slug=%s", (args.slug,))).fetchone()
    if not t:
        raise SystemExit(f"tenant {args.slug!r} não existe (rode bootstrap)")
    tid = t["id"]
    async with tenant_tx(tid) as conn:
        h = crypto().lookup_hash(args.email, kind="email")
        if await (await conn.execute("select 1 from tenant_users where email_hash=%s", (h,))).fetchone():
            raise SystemExit("já existe um usuário com esse e-mail")
        email_enc = await encrypt_field(conn, tid, args.email, "tenant_users.email")
        await conn.execute(
            """insert into tenant_users (tenant_id, email_hash, email_enc, display_name, role, password_hash,
                                         notify_whatsapp_enc, notify_email_enc) values (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (tid, h, email_enc, args.name, args.role, hash_password(password),
             await encrypt_field(conn, tid, args.whatsapp, "tenant_users.whatsapp") if args.whatsapp else None, email_enc))
        await conn.execute(
            "insert into audit_log (tenant_id, actor_kind, action, target, after) values (%s,'cli','user.add',%s,%s)",
            (tid, h.hex() if isinstance(h, bytes) else str(h), json.dumps({"role": args.role})))
    await close_pool()
    print(json.dumps({"tenant_id": str(tid), "role": args.role}))


def main() -> None:
    p = argparse.ArgumentParser(prog="agentplatform")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    b = sub.add_parser("bootstrap")
    b.add_argument("--slug", default="dev-consortium")
    b.add_argument("--name", default="Dev tenant")
    b.add_argument("--ig-account", default="17841400000000000")
    b.add_argument("--page-id")
    b.add_argument("--page-token")
    b.add_argument("--seller-email", default="vendedor@example.com")
    b.add_argument("--seller-name", default="Vendedor")
    b.add_argument("--seller-password", help="omita para gerar uma senha aleatória, mostrada uma única vez")
    b.add_argument("--seller-whatsapp")
    u = sub.add_parser("user-add", help="cria outro usuário do console (ex.: aprovador)")
    u.add_argument("--slug", default="dev-consortium")
    u.add_argument("--email", required=True)
    u.add_argument("--name", required=True)
    u.add_argument("--role", choices=["tenant_owner", "seller", "reviewer"], default="reviewer")
    u.add_argument("--password", help="omita para digitar sem eco")
    u.add_argument("--whatsapp")
    i = sub.add_parser("ingest")
    i.add_argument("--slug", default="dev-consortium")
    i.add_argument("--dir", default=str(ROOT / "docs" / "materiais"))
    s = sub.add_parser("simulate")
    s.add_argument("--slug", default="dev-consortium")
    s.add_argument("--contact")
    s.add_argument("--say", nargs="*")
    sub.add_parser("budget")
    ca = sub.add_parser("config-activate", help="ativa em produção o arquivo que passou no gate full")
    ca.add_argument("--slug", required=True)
    ca.add_argument("--experiment", required=True)
    lab = sub.add_parser("lab")
    lab.add_argument("action", choices=["gate"])
    lab.add_argument("--level", choices=["fast", "full"], default="fast")
    lab.add_argument("--slug", default="dev-consortium")
    lab.add_argument("--config-version", help="avalia um rascunho salvo em vez do arquivo")
    lab.add_argument("--max-scenarios", type=int, default=None, help="só para medir: com limite, a avaliação não libera a ativação")
    lab.add_argument("--verbose", action="store_true")
    lab.add_argument("--repeats", type=int, default=3, help="cada cenário precisa passar todas as vezes (pass^k); com menos de 3, não libera a ativação")
    args = p.parse_args()
    if args.cmd == "migrate":
        cmd_migrate(args)
    else:
        asyncio.run({"bootstrap": _bootstrap, "user-add": _user_add, "ingest": _ingest, "simulate": _simulate, "budget": _budget,
                     "lab": _lab, "config-activate": _config_activate}[args.cmd](args))


if __name__ == "__main__":
    main()
