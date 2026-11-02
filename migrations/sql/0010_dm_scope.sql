-- Who the assistant may talk to in the direct of an account that also holds the owner's personal life (ADR-019).
-- Nothing here stores conversations: contacts are kept only as a keyed hash of their channel id.
create table dm_settings (
  channel_account_id uuid primary key references channel_accounts(id),
  tenant_id uuid not null,
  scope text not null default 'origin_only' check (scope in ('origin_only','origin_and_new','all')),
  contacts_synced_at timestamptz,          -- list of who already talks to this account is complete; until then nobody new is served
  contacts_cursor text,
  contacts_count int not null default 0,
  updated_by uuid, updated_at timestamptz not null default now()
);

create table contact_flags (
  tenant_id uuid not null,
  channel_account_id uuid not null references channel_accounts(id),
  identifier_hash bytea not null,
  status text not null check (status in ('known','blocked','allowed')),
  source text not null,                    -- snapshot | seller_message | console
  updated_by uuid, updated_at timestamptz not null default now(),
  primary key (channel_account_id, identifier_hash)
);

do $$
declare t text;
begin
  foreach t in array array['dm_settings','contact_flags'] loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
    execute format('create policy tenant_isolation on %I using (tenant_id = app_tenant() or app_is_platform()) with check (tenant_id = app_tenant() or app_is_platform())', t);
    execute format('grant select, insert, update, delete on %I to app_rw', t);
  end loop;
end $$;
