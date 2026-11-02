-- What a person found reviewing conversations by hand (plan 01 section 7: violations come from guards AND from review).
create table conversation_reviews (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  conversation_id uuid not null references conversations(id),
  verdict text not null check (verdict in ('ok','problem')),
  category text,                 -- operations.review.categories; only for a problem
  note_masked text,              -- masked on the way in, like every other free text here
  reviewer uuid not null,
  created_at timestamptz not null default now()
);
create index conversation_reviews_recent on conversation_reviews (tenant_id, created_at desc);
create index conversation_reviews_conversation on conversation_reviews (conversation_id, created_at desc);

alter table conversation_reviews enable row level security;
alter table conversation_reviews force row level security;
create policy tenant_isolation on conversation_reviews using (tenant_id = app_tenant() or app_is_platform()) with check (tenant_id = app_tenant() or app_is_platform());
grant select, insert, update, delete on conversation_reviews to app_rw;

-- Restore tests run on the host by cron; this is what lets the platform notice they stopped. Platform data: no tenant, counts only.
create table restore_tests (
  id bigserial primary key,
  status text not null check (status in ('ok','failed')),
  summary text not null,
  finished_at timestamptz not null default now()
);
grant select on restore_tests to app_rw;

-- Questions offered when someone opens a new direct with the account (ice breakers).
alter table dm_settings
  add column quick_questions jsonb not null default '[]'::jsonb,
  add column quick_questions_published_at timestamptz;
