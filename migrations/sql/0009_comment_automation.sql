-- Comment automation configured per post in the console (ADR-018). The seller changes these daily, so they are operational
-- data with automatic text validation and audit, not versioned config with two-person approval.

-- Posts, reels, stories, ads and lives of a channel account: synced from the provider or first seen in an event.
create table channel_media (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  channel_account_id uuid not null references channel_accounts(id),
  external_media_id text not null,
  media_kind text not null default 'post' check (media_kind in ('post','reel','story','ad','live')),
  media_type text,
  caption_masked text,
  permalink text,
  image_url text,
  posted_at timestamptz,
  comments_count int,
  comments_seen int,                       -- count at the last comment poll: unchanged posts are not polled again
  polled_at timestamptz,
  ad_id text, ad_title text, original_media_id text,
  synced_at timestamptz,
  created_at timestamptz not null default now(),
  unique (tenant_id, external_media_id)
);
create index channel_media_recent on channel_media (tenant_id, posted_at desc nulls last);

-- Account default for every post. Starts inactive: nothing is sent until someone turns it on.
create table comment_settings (
  channel_account_id uuid primary key references channel_accounts(id),
  tenant_id uuid not null,
  default_active boolean not null default false,
  default_mode text not null default 'both' check (default_mode in ('register','public','private','both')),
  trigger_kind text not null default 'keyword' check (trigger_kind in ('keyword','any','intent')),
  keywords text[] not null default '{}',
  public_variants text[] not null default '{}',
  private_variants jsonb not null default '{}',
  repeat_variants text[] not null default '{}',   -- public reply when this person was already contacted
  hide_spam boolean not null default false,
  active_since timestamptz,                -- comments older than this are never answered
  updated_by uuid, updated_at timestamptz not null default now()
);

-- Per-post choice. Empty text fields inherit the account's.
create table media_automations (
  media_id uuid primary key references channel_media(id),
  tenant_id uuid not null,
  mode text not null default 'default' check (mode in ('default','off','register','public','private','both')),
  trigger_kind text check (trigger_kind in ('keyword','any','intent')),
  keywords text[] not null default '{}',
  public_variants text[] not null default '{}',
  private_variants jsonb not null default '{}',
  context_note_masked text,                -- what this post is about, in the seller's words (goes to the agent)
  context_slots jsonb not null default '{}',   -- package slots this post already answers (e.g. the customer's goal)
  valid_from timestamptz, valid_to timestamptz,
  active_since timestamptz,
  backfill_since timestamptz,              -- also answer comments from this moment on (seller's explicit choice)
  updated_by uuid, updated_at timestamptz not null default now()
);

-- Every comment is listed for the seller, with what was done about it.
alter table comment_actions add column text_masked text;
alter table comment_actions add column author_username text;
alter table comment_actions add column comment_ts timestamptz;
alter table comment_actions add column conversation_id uuid;
alter table comment_actions add column attention boolean not null default false;
alter table comment_actions add column resolved_at timestamptz;
alter table comment_actions add column resolved_by uuid;
create index comment_actions_media on comment_actions (tenant_id, media_id, created_at desc);
create index comment_actions_attention on comment_actions (tenant_id, created_at desc) where attention and resolved_at is null;

-- A profile lookup that the provider refuses is not retried on every message.
alter table contact_identities add column username_checked_at timestamptz;

do $$
declare t text;
begin
  foreach t in array array['channel_media','comment_settings','media_automations'] loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
    execute format('create policy tenant_isolation on %I using (tenant_id = app_tenant() or app_is_platform()) with check (tenant_id = app_tenant() or app_is_platform())', t);
    execute format('grant select, insert, update, delete on %I to app_rw', t);
  end loop;
end $$;
