-- Roles and row-level security (plan 03 §7). The app connects as app_rw (no BYPASSRLS).
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'app_rw') then
    create role app_rw login password 'app_rw' nosuperuser nocreatedb nocreaterole nobypassrls;
  end if;
end $$;
grant usage on schema public to app_rw;
grant select, insert, update, delete on all tables in schema public to app_rw;
grant usage, select on all sequences in schema public to app_rw;
alter default privileges in schema public grant select, insert, update, delete on tables to app_rw;
alter default privileges in schema public grant usage, select on sequences to app_rw;

create or replace function app_tenant() returns uuid language sql stable as
  $$ select nullif(current_setting('app.tenant_id', true), '')::uuid $$;
create or replace function app_is_platform() returns boolean language sql stable as
  $$ select coalesce(current_setting('app.platform', true), '') = 'on' $$;

-- Strict tables: tenant_id must match (or platform job)
do $$
declare t text;
begin
  foreach t in array array[
    'tenant_deks','channel_accounts','inbound_events','contacts','contact_identities','contact_merge_suggestions',
    'conversations','ownership_events','messages','turns','tool_calls','guard_results','outbox','leads',
    'lead_stage_history','lead_sources','lead_forms','handoffs','consents','suppression_list','comment_actions',
    'private_reply_claims','followup_runs','curation_queue','import_batches','import_rows','campaigns','campaign_targets']
  loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
    execute format($p$create policy tenant_isolation on %I
      using (tenant_id = app_tenant() or app_is_platform())
      with check (tenant_id = app_tenant() or app_is_platform())$p$, t);
  end loop;
end $$;

-- Shared tables: tenant rows + package/platform rows (tenant_id null) are readable; writes strict
do $$
declare t text;
begin
  foreach t in array array[
    'tenant_users','config_versions','kill_switches','kb_sources','kb_documents','kb_chunks','faqs','faq_variants',
    'domain_datasets','domain_rows','budget_balances','budget_reservations','budget_ledger','llm_calls',
    'audit_log','dead_letters']
  loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
    execute format($p$create policy tenant_read on %I for select
      using (tenant_id is null or tenant_id = app_tenant() or app_is_platform())$p$, t);
    execute format($p$create policy tenant_write on %I for all
      using (tenant_id = app_tenant() or app_is_platform())
      with check (tenant_id = app_tenant() or (tenant_id is null and app_is_platform()) or app_is_platform())$p$, t);
  end loop;
end $$;

-- tenants: a tenant sees itself; platform sees all
alter table tenants enable row level security;
alter table tenants force row level security;
create policy tenant_self on tenants using (id = app_tenant() or app_is_platform()) with check (app_is_platform());

-- Budget rows of the process-wide key (tenant_id null) must be writable by any tenant transaction,
-- because every tenant's LLM call spends from the same provider key.
create policy key_budget_write on budget_balances for update using (tenant_id is null) with check (tenant_id is null);
create policy key_budget_resv on budget_reservations for all using (tenant_id is null) with check (tenant_id is null);
create policy key_budget_ledger on budget_ledger for insert with check (tenant_id is null);
create policy key_llm_calls on llm_calls for insert with check (tenant_id is null or tenant_id = app_tenant());
