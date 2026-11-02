-- Core schema (plan 03, after independent review). Identifiers in English.
create extension if not exists vector;
create extension if not exists unaccent;
create extension if not exists pgcrypto;

do $$ begin
  if not exists (select 1 from pg_ts_config where cfgname = 'pt_unaccent') then
    create text search configuration pt_unaccent (copy = portuguese);
    alter text search configuration pt_unaccent alter mapping for hword, hword_part, word with unaccent, portuguese_stem;
  end if;
end $$;

-- ===== Tenancy, users, config =====
create table tenants (
  id uuid primary key default gen_random_uuid(),
  slug text unique not null,
  display_name text not null,
  timezone text not null default 'America/Sao_Paulo',
  segment_package text not null,
  segment_package_version text not null,
  status text not null default 'active' check (status in ('active','paused','offboarded')),
  created_at timestamptz not null default now()
);
create table tenant_deks (
  tenant_id uuid primary key references tenants(id),
  wrapped_dek bytea not null,
  created_at timestamptz not null default now()
);
create table tenant_users (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid references tenants(id),
  email_hash bytea not null,
  email_enc bytea not null,
  display_name text not null,
  role text not null check (role in ('platform_admin','tenant_owner','seller','reviewer')),
  password_hash text,
  totp_secret_enc bytea,
  notify_whatsapp_enc bytea,
  notify_email_enc bytea,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  unique (tenant_id, email_hash)
);
create table config_versions (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid references tenants(id),
  env text not null check (env in ('dev','staging','prod')),
  version int not null,
  document jsonb not null,
  content_hash text not null,
  status text not null check (status in ('draft','approved','canary','active','superseded','rolled_back')),
  canary_percent int not null default 0 check (canary_percent between 0 and 100),
  eval_run_id uuid,
  created_by uuid, approved_by uuid[] not null default '{}',
  created_at timestamptz not null default now(),
  unique (tenant_id, env, version)
);
create table kill_switches (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid,
  scope text not null check (scope in ('global','channel','tenant','campaign','feature')),
  scope_ref text,
  is_on boolean not null,
  reason text not null, changed_by uuid, changed_at timestamptz not null default now()
);
create unique index kill_switches_scope on kill_switches (scope, coalesce(scope_ref, ''), coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'));
create table prompt_templates (
  id uuid primary key default gen_random_uuid(),
  name text not null, version int not null, body text not null,
  variables_schema jsonb not null default '{}', eval_run_id uuid,
  status text not null default 'draft' check (status in ('draft','approved','active','retired')),
  created_at timestamptz not null default now(),
  unique (name, version)
);

-- ===== Channels =====
create table channel_accounts (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  channel text not null,
  external_account_id text not null,
  external_page_id text,
  token_enc bytea,
  token_kind text not null default 'page',
  token_expires_at timestamptz,
  token_last_validated_at timestamptz,
  token_status text not null default 'valid' check (token_status in ('valid','expiring','invalid','revoked')),
  scopes text[] not null default '{}',
  access_level text,
  rate_limit_profile text not null default 'instagram_default',
  created_at timestamptz not null default now(),
  unique (channel, external_account_id)
);

-- ===== Inbound (idempotent) =====
create table inbound_events (
  id bigint generated always as identity primary key,
  tenant_id uuid not null,
  channel_account_id uuid not null references channel_accounts(id),
  idempotency_key text not null,
  event_type text not null,
  conversation_id uuid,
  provider_ts timestamptz not null,
  received_at timestamptz not null default now(),
  payload_raw jsonb,
  payload_canonical jsonb not null,
  status text not null default 'pending' check (status in ('pending','processing','done','failed','dead','ignored')),
  attempts int not null default 0,
  last_error text,
  unique (channel_account_id, idempotency_key)
);
create index inbound_events_pending on inbound_events (status, received_at) where status in ('pending','failed');

-- ===== Contacts and identity =====
create table contacts (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  display_name_enc bytea,
  first_name text,
  phone_hash bytea, email_hash bytea,
  phone_enc bytea, email_enc bytea,
  merged_into uuid references contacts(id),
  created_at timestamptz not null default now()
);
create unique index contacts_phone on contacts (tenant_id, phone_hash) where phone_hash is not null and merged_into is null;
create unique index contacts_email on contacts (tenant_id, email_hash) where email_hash is not null and merged_into is null;
create table contact_identities (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  contact_id uuid not null references contacts(id),
  channel text not null,
  external_user_id text not null,
  username text,
  unique (tenant_id, channel, external_user_id)
);
create table contact_merge_suggestions (
  id uuid primary key default gen_random_uuid(), tenant_id uuid not null,
  contact_a uuid not null references contacts(id), contact_b uuid not null references contacts(id),
  evidence jsonb not null, score numeric not null,
  status text not null default 'pending' check (status in ('pending','approved','rejected')),
  decided_by uuid, decided_at timestamptz
);

-- ===== Conversations, ownership, messages =====
create table conversations (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  contact_id uuid not null references contacts(id),
  channel_account_id uuid not null references channel_accounts(id),
  thread_external_id text,
  ownership text not null default 'BOT' check (ownership in ('BOT','AWAITING_HUMAN','HUMAN','PAUSED','CLOSED')),
  ownership_changed_at timestamptz not null default now(),
  run_epoch bigint not null default 0,
  send_epoch bigint not null default 0,
  handoff_pending boolean not null default false,
  last_inbound_at timestamptz,
  last_reply_at timestamptz,
  window_expires_at timestamptz,
  entrypoint text,
  summary text,
  summary_turn_id uuid,
  consecutive_restarts int not null default 0,
  created_at timestamptz not null default now(),
  unique (channel_account_id, contact_id)
);
create table ownership_events (
  id bigint generated always as identity primary key,
  tenant_id uuid not null, conversation_id uuid not null references conversations(id),
  from_state text not null, to_state text not null,
  cause text not null, actor uuid, evidence jsonb, at timestamptz not null default now()
);
create table messages (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null, conversation_id uuid not null references conversations(id),
  direction text not null check (direction in ('in','out_bot','out_human','out_system')),
  external_mid text,
  inbound_event_id bigint references inbound_events(id),
  kind text not null,
  text_masked text,
  text_enc bytea,
  media jsonb,
  provider_ts timestamptz not null,
  turn_id uuid,
  created_at timestamptz not null default now()
);
create unique index messages_mid on messages (conversation_id, external_mid) where external_mid is not null;
create index messages_conv_ts on messages (conversation_id, provider_ts);

-- ===== Turns, LLM, tools, guards =====
create table turns (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null, conversation_id uuid not null references conversations(id),
  run_epoch bigint not null,
  config_version_id uuid,
  input_message_ids uuid[] not null,
  status text not null check (status in ('running','committed','cancelled','failed','handoff')),
  lease_id uuid not null,
  lease_expires_at timestamptz not null,
  procrastinate_job_id bigint,
  resumes int not null default 0,
  cancel_reason text,
  stage_before text, stage_after text,
  llm_calls int not null default 0, cost_usd numeric(12,6) not null default 0,
  started_at timestamptz not null default now(), heartbeat_at timestamptz, finished_at timestamptz,
  trace jsonb
);
create index turns_running on turns (conversation_id) where status = 'running';
create table llm_calls (
  id bigint generated always as identity primary key,
  tenant_id uuid, turn_id uuid, node text not null,
  purpose text not null,
  provider text not null, model_requested text not null, model_used text,
  prompt_version text,
  input_tokens int, output_tokens int, cached_tokens int,
  cost_usd numeric(12,6), cost_source text,
  latency_ms int,
  status text not null,
  generation_id text,
  created_at timestamptz not null default now()
);
create table tool_calls (
  id bigint generated always as identity primary key,
  tenant_id uuid not null, turn_id uuid not null, tool text not null,
  args_masked jsonb not null, result_digest jsonb, effect_class text not null,
  status text not null, latency_ms int, idempotency_key text not null unique,
  created_at timestamptz not null default now()
);
create table guard_results (
  id bigint generated always as identity primary key,
  tenant_id uuid not null, turn_id uuid,
  guard text not null, rule_id text not null,
  verdict text not null check (verdict in ('pass','block','rewrite','handoff')),
  detail jsonb, created_at timestamptz not null default now()
);

-- ===== Outbox =====
create table outbox (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null, conversation_id uuid not null references conversations(id),
  turn_id uuid,
  effect_key text not null unique,
  kind text not null,
  is_proactive boolean not null default false,
  priority smallint not null,
  payload jsonb not null,
  content_hash text not null,
  send_epoch bigint not null,
  source_inbound_at timestamptz,
  not_before timestamptz not null default now(),
  max_staleness_s int not null,
  status text not null default 'pending' check (status in ('pending','sending','sent','failed','cancelled','unknown')),
  attempt_registered_at timestamptz,
  external_mid text,
  cancel_reason text,
  attempts int not null default 0, last_error text,
  sent_at timestamptz, created_at timestamptz not null default now()
);
create index outbox_due on outbox (status, priority, not_before) where status in ('pending','unknown');
create index outbox_conv on outbox (conversation_id, status);
create unique index outbox_mid on outbox (external_mid) where external_mid is not null;

-- ===== Leads =====
create table leads (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null, contact_id uuid not null unique references contacts(id),
  stage text not null,
  stage_changed_at timestamptz not null default now(),
  turns_in_stage int not null default 0,
  profile jsonb not null default '{}',
  is_qualified boolean not null default false,
  qualified_at timestamptz,
  opted_out_at timestamptz,
  created_at timestamptz not null default now()
);
create table lead_stage_history (
  id bigint generated always as identity primary key,
  tenant_id uuid not null, lead_id uuid not null references leads(id),
  from_stage text, to_stage text not null,
  proposed_by text not null, confidence numeric, evidence jsonb, turn_id uuid,
  applied boolean not null, rejected_reason text,
  at timestamptz not null default now()
);
create table lead_sources (
  id uuid primary key default gen_random_uuid(), tenant_id uuid not null, lead_id uuid not null references leads(id),
  channel text not null, entrypoint text not null,
  media_id text, ad_id text, campaign text, utm jsonb, comment_id text,
  is_first_touch boolean not null, at timestamptz not null default now()
);
create table lead_forms (
  id uuid primary key default gen_random_uuid(), tenant_id uuid not null, lead_id uuid not null references leads(id),
  token_hash bytea not null unique, expires_at timestamptz not null, used_at timestamptz,
  fields_enc bytea, consent_id uuid, submitted_at timestamptz, delivered_to_seller_at timestamptz
);
create table handoffs (
  id uuid primary key default gen_random_uuid(), tenant_id uuid not null,
  conversation_id uuid not null references conversations(id), lead_id uuid references leads(id),
  reason text not null, priority text not null default 'normal', summary jsonb not null,
  notify_at timestamptz not null default now(),
  notified_via text, notified_at timestamptz, realerts int not null default 0,
  taken_at timestamptz, taken_by uuid,
  sla_due_at timestamptz, sla_breached boolean not null default false,
  created_at timestamptz not null default now()
);

-- ===== Consent, suppression =====
create table consents (
  id uuid primary key default gen_random_uuid(), tenant_id uuid not null, contact_id uuid references contacts(id),
  subject_kind text not null default 'contact' check (subject_kind in ('contact','seller')),
  subject_ref text,
  purpose text not null, legal_basis text not null,
  status text not null check (status in ('granted','revoked')),
  evidence jsonb not null, channel text, at timestamptz not null default now()
);
create table suppression_list (
  tenant_id uuid not null, identifier_hash bytea not null,
  reason text not null, at timestamptz not null default now(),
  primary key (tenant_id, identifier_hash)
);

-- ===== Comments =====
create table comment_actions (
  id uuid primary key default gen_random_uuid(), tenant_id uuid not null,
  comment_id text not null, media_id text not null, author_external_id text not null,
  rule_id text, decision text not null,
  private_variant text, public_variant text,
  private_reply_outbox_id uuid, public_reply_outbox_id uuid,
  created_at timestamptz not null default now(),
  unique (tenant_id, comment_id)
);
create table private_reply_claims (
  tenant_id uuid not null, media_id text not null, author_external_id text not null,
  claimed_at timestamptz not null, comment_id text not null,
  primary key (tenant_id, media_id, author_external_id)
);

-- ===== Follow-ups =====
create table followup_runs (
  id uuid primary key default gen_random_uuid(), tenant_id uuid not null,
  conversation_id uuid not null references conversations(id),
  cadence_id text not null, step int not null,
  status text not null check (status in ('scheduled','queued','sent','skipped','stopped')),
  due_at timestamptz not null, stop_reason text, outbox_id uuid,
  created_at timestamptz not null default now(),
  unique (conversation_id, cadence_id, step)
);
create index followup_due on followup_runs (status, due_at) where status = 'scheduled';

-- ===== Knowledge =====
create table kb_sources (
  id uuid primary key default gen_random_uuid(), tenant_id uuid,
  scope text not null check (scope in ('package','tenant')),
  package text, title text not null, segment text, kind text not null
);
create table kb_documents (
  id uuid primary key default gen_random_uuid(), tenant_id uuid, source_id uuid not null references kb_sources(id),
  file_name text not null, file_sha256 text not null, version_label text, effective_from date, effective_to date,
  status text not null check (status in ('draft','in_review','approved','superseded','do_not_use','blocked_pii')),
  visibility text not null check (visibility in ('citable','guidance_only')),
  audience text not null check (audience in ('customer','internal')),
  approved_by uuid, approved_at timestamptz, pii_scan jsonb, ocr jsonb, pages int,
  unique (source_id, file_sha256)
);
create table kb_indexes (
  id uuid primary key default gen_random_uuid(), name text unique not null,
  strategy jsonb not null, embedding_model text not null, dims int not null,
  status text not null check (status in ('building','ready','active','retired')),
  created_at timestamptz not null default now()
);
create table kb_chunks (
  id uuid primary key default gen_random_uuid(), tenant_id uuid, index_id uuid not null references kb_indexes(id),
  document_id uuid not null references kb_documents(id),
  ordinal int not null, heading_path text[], text text not null, context_prefix text,
  tsv tsvector generated always as (to_tsvector('pt_unaccent', coalesce(context_prefix,'') || ' ' || text)) stored,
  embedding vector,
  token_count int not null, page int
);
create index kb_chunks_tsv on kb_chunks using gin (tsv);
create index kb_chunks_index on kb_chunks (index_id, document_id);
create table faqs (
  id uuid primary key default gen_random_uuid(), tenant_id uuid, package text, segment text,
  canonical_question text not null, answer text not null,
  status text not null check (status in ('draft','approved','retired')), valid_to date, owner uuid, approved_by uuid
);
create table faq_variants (
  id uuid primary key default gen_random_uuid(), tenant_id uuid, faq_id uuid not null references faqs(id),
  question text not null, embedding vector,
  tsv tsvector generated always as (to_tsvector('pt_unaccent', question)) stored
);
create index faq_variants_tsv on faq_variants using gin (tsv);
create table curation_queue (
  id uuid primary key default gen_random_uuid(), tenant_id uuid not null,
  question_masked text not null, turn_id uuid, cluster_id uuid,
  frequency int not null default 1, status text not null default 'open', resolution jsonb,
  created_at timestamptz not null default now()
);
create table domain_datasets (
  id uuid primary key default gen_random_uuid(), tenant_id uuid, package text not null, dataset text not null,
  version int not null, status text not null check (status in ('draft','approved','active','retired')),
  source_document_id uuid, row_count int not null, checksum text not null,
  approved_by uuid, created_at timestamptz not null default now(),
  unique (tenant_id, package, dataset, version)
);
create table domain_rows (
  tenant_id uuid,
  dataset_id uuid not null references domain_datasets(id), row_no int not null, data jsonb not null,
  primary key (dataset_id, row_no)
);

-- ===== Budget =====
create table budget_balances (
  tenant_id uuid,
  scope text not null,
  cap_usd numeric(12,6) not null,
  spent_usd numeric(12,6) not null default 0,
  reserved_usd numeric(12,6) not null default 0
);
create unique index budget_balances_pk on budget_balances (coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'), scope);
create table budget_reservations (
  id uuid primary key default gen_random_uuid(), tenant_id uuid, scopes text[] not null,
  amount_usd numeric(12,6) not null, expires_at timestamptz not null,
  settled_at timestamptz, settled_amount_usd numeric(12,6), reconciled boolean not null default false
);
create table budget_ledger (
  id bigint generated always as identity primary key,
  tenant_id uuid, scope text not null,
  kind text not null check (kind in ('reserve','settle','release','topup')),
  amount_usd numeric(12,6) not null, llm_call_id bigint, at timestamptz not null default now()
);

-- ===== Audit, DLQ =====
create table audit_log (
  id bigint generated always as identity primary key,
  tenant_id uuid, actor uuid, actor_kind text not null,
  action text not null, target text not null, before jsonb, after jsonb,
  at timestamptz not null default now()
);
create table dead_letters (
  id bigint generated always as identity primary key, tenant_id uuid,
  source text not null, ref text not null, error text not null, payload_masked jsonb,
  at timestamptz not null default now(), reprocessed_at timestamptz
);

-- ===== Evaluation / lab =====
create table eval_datasets (
  id uuid primary key default gen_random_uuid(), name text not null, version int not null,
  path text not null, sha256 text not null, unique (name, version)
);
create table experiments (
  id uuid primary key default gen_random_uuid(), dataset text not null, variants jsonb not null,
  metrics jsonb, status text not null, cost_estimate_usd numeric(12,6), cost_actual_usd numeric(12,6),
  git_commit text, created_at timestamptz not null default now()
);
create table eval_results (
  id bigint generated always as identity primary key, experiment_id uuid not null references experiments(id),
  variant text not null, case_id text not null, rep int not null,
  outputs jsonb, scores jsonb, cost_usd numeric(12,6), latency_ms int
);

-- ===== Phase 4 (import, campaigns) =====
create table import_batches (
  id uuid primary key default gen_random_uuid(), tenant_id uuid not null, file_sha256 text not null,
  mapping jsonb not null, status text not null, counts jsonb, created_by uuid,
  created_at timestamptz not null default now()
);
create table import_rows (
  batch_id uuid not null references import_batches(id), tenant_id uuid not null, row_no int not null,
  status text not null, reason text, contact_id uuid, primary key (batch_id, row_no)
);
create table campaigns (
  id uuid primary key default gen_random_uuid(), tenant_id uuid not null, name text not null,
  segment jsonb not null, template jsonb not null, schedule jsonb not null, limits jsonb not null,
  status text not null default 'draft', created_at timestamptz not null default now()
);
create table campaign_targets (
  campaign_id uuid not null references campaigns(id), tenant_id uuid not null, contact_id uuid not null,
  variant text, status text not null, outbox_id uuid, primary key (campaign_id, contact_id)
);
