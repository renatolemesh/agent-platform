-- Configuration a person can change in the console, and what keeps that safe (ADR-022).

-- A draft is a copy of the version it came from. Approving it after another change would silently undo that change, so it records its base.
alter table config_versions
  add column base_version_id uuid references config_versions(id),
  add column note text;
alter table config_versions drop constraint config_versions_status_check;
alter table config_versions add constraint config_versions_status_check
  check (status in ('draft','approved','canary','active','superseded','rolled_back','discarded'));

-- An evaluation asked for in the console. The worker runs it; the draft only counts as evaluated when it passes.
create table eval_requests (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  config_version_id uuid not null references config_versions(id),
  level text not null check (level in ('fast','full')),
  status text not null default 'queued' check (status in ('queued','running','passed','failed','error')),
  experiment_id uuid,
  scores jsonb,
  cost_usd numeric(10,4) not null default 0,
  error text,
  requested_by uuid,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz
);
create index eval_requests_version on eval_requests (tenant_id, config_version_id, created_at desc);
create unique index eval_requests_one_open on eval_requests (tenant_id) where status in ('queued','running');
alter table eval_requests enable row level security;
alter table eval_requests force row level security;
create policy tenant_isolation on eval_requests using (tenant_id = app_tenant() or app_is_platform()) with check (tenant_id = app_tenant() or app_is_platform());
grant select, insert, update, delete on eval_requests to app_rw;

-- People join through a link they open once. Only the hash is kept; the email is encrypted like every other one.
create table user_invites (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  token_hash text not null unique,
  email_hash text not null,
  email_enc bytea not null,
  display_name text not null,
  role text not null check (role in ('tenant_owner','seller','reviewer')),
  created_by uuid not null,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  used_at timestamptz,
  revoked_at timestamptz
);
create index user_invites_tenant on user_invites (tenant_id, created_at desc);
alter table user_invites enable row level security;
alter table user_invites force row level security;
create policy tenant_isolation on user_invites using (tenant_id = app_tenant() or app_is_platform()) with check (tenant_id = app_tenant() or app_is_platform());
grant select, insert, update, delete on user_invites to app_rw;

-- FAQ vectors belong to a search index, like passages do: two models can coexist while one is replaced, a canary or a rollback keeps working,
-- and a vector is never compared with one from another model.
create table faq_variant_vectors (
  variant_id uuid not null references faq_variants(id) on delete cascade,
  index_id uuid not null references kb_indexes(id),
  tenant_id uuid,
  embedding vector not null,
  primary key (variant_id, index_id)
);
create index faq_variant_vectors_index on faq_variant_vectors (index_id);
alter table faq_variant_vectors enable row level security;
alter table faq_variant_vectors force row level security;
create policy tenant_read on faq_variant_vectors for select using (tenant_id is null or tenant_id = app_tenant() or app_is_platform());
create policy tenant_write on faq_variant_vectors for all
  using (tenant_id = app_tenant() or app_is_platform())
  with check (tenant_id = app_tenant() or (tenant_id is null and app_is_platform()) or app_is_platform());
grant select, insert, update, delete on faq_variant_vectors to app_rw;

insert into faq_variant_vectors (variant_id, index_id, tenant_id, embedding)
select v.id,
       (select k.id from kb_indexes k where k.dims = vector_dims(v.embedding) order by (k.status = 'active') desc, k.created_at desc limit 1),
       v.tenant_id, v.embedding
from faq_variants v
where v.embedding is not null
  and exists (select 1 from kb_indexes k where k.dims = vector_dims(v.embedding));
alter table faq_variants drop column embedding;

-- Replacing the embedding model (ADR-022): a new index is built next to the current one, and a person activates it through a normal draft.
create table kb_reindex_runs (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  from_index uuid not null references kb_indexes(id),
  to_index uuid not null references kb_indexes(id),
  model text not null,
  dims int not null,
  status text not null default 'running' check (status in ('running','ready','failed','cancelled')),
  total int not null default 0,
  done int not null default 0,
  error text,
  draft_version_id uuid,
  created_by uuid,
  created_at timestamptz not null default now(),
  finished_at timestamptz,
  attempts int not null default 0,
  heartbeat_at timestamptz not null default now()
);
create unique index kb_reindex_one_running on kb_reindex_runs (tenant_id) where status = 'running';
alter table kb_reindex_runs enable row level security;
alter table kb_reindex_runs force row level security;
create policy tenant_isolation on kb_reindex_runs using (tenant_id = app_tenant() or app_is_platform()) with check (tenant_id = app_tenant() or app_is_platform());
grant select, insert, update, delete on kb_reindex_runs to app_rw;

-- Two passages about the same thing with different numbers. Found with the vectors already there: nothing is sent anywhere.
create table kb_conflicts (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  document_id uuid not null references kb_documents(id),
  chunk_id uuid not null references kb_chunks(id) on delete cascade,
  other_document_id uuid not null references kb_documents(id),
  other_chunk_id uuid not null references kb_chunks(id) on delete cascade,
  similarity real not null,
  status text not null default 'open' check (status in ('open','dismissed')),
  dismissed_by uuid,
  created_at timestamptz not null default now(),
  unique (chunk_id, other_chunk_id)
);
create index kb_conflicts_open on kb_conflicts (tenant_id, status);
alter table kb_conflicts enable row level security;
alter table kb_conflicts force row level security;
create policy tenant_isolation on kb_conflicts using (tenant_id = app_tenant() or app_is_platform()) with check (tenant_id = app_tenant() or app_is_platform());
grant select, insert, update, delete on kb_conflicts to app_rw;
