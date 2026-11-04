-- Posts written in the console and published by the platform (ADR-024).
create table post_drafts (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  channel_account_id uuid not null references channel_accounts(id),
  kind text not null check (kind in ('feed','reel','story')),
  caption text not null default '',
  scheduled_at timestamptz,
  status text not null default 'draft' check (status in ('draft','approved','publishing','published','failed','unknown')),
  error text,
  error_detail text,
  approved_by uuid,
  approved_at timestamptz,
  approved_hash text,
  container_id text,                   -- what the provider publishes
  publishing_since timestamptz,
  publish_started_at timestamptz,      -- from here on the post may exist: it is never sent again
  external_media_id text,
  published_at timestamptz,
  created_by uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index post_drafts_due on post_drafts (tenant_id, scheduled_at) where status in ('approved','publishing');
alter table post_drafts enable row level security;
alter table post_drafts force row level security;
create policy tenant_isolation on post_drafts using (tenant_id = app_tenant() or app_is_platform()) with check (tenant_id = app_tenant() or app_is_platform());
grant select, insert, update, delete on post_drafts to app_rw;

create table post_draft_files (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  draft_id uuid not null references post_drafts(id) on delete cascade,
  position int not null,
  content_type text not null,
  is_video boolean not null,
  width int,
  height int,
  size_bytes bigint not null,
  sha256 text not null,
  chunks int not null,
  public_token_hash text unique,       -- only while the provider fetches the file
  container_id text,
  created_at timestamptz not null default now()
);
create index post_draft_files_draft on post_draft_files (draft_id, position);
alter table post_draft_files enable row level security;
alter table post_draft_files force row level security;
create policy tenant_isolation on post_draft_files using (tenant_id = app_tenant() or app_is_platform()) with check (tenant_id = app_tenant() or app_is_platform());
grant select, insert, update, delete on post_draft_files to app_rw;

-- The file only until it is published. Encrypted, and in pieces: a video must not have to fit in the memory of the small API process.
create table post_file_chunks (
  file_id uuid not null references post_draft_files(id) on delete cascade,
  tenant_id uuid not null,
  seq int not null,
  content_enc bytea not null,
  primary key (file_id, seq)
);
alter table post_file_chunks enable row level security;
alter table post_file_chunks force row level security;
create policy tenant_isolation on post_file_chunks using (tenant_id = app_tenant() or app_is_platform()) with check (tenant_id = app_tenant() or app_is_platform());
grant select, insert, update, delete on post_file_chunks to app_rw;

alter table channel_media
  add column comments_enabled boolean,  -- known only after someone changes it here: reading it is one call per post
  add column removed_at timestamptz;
