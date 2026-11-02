-- Console hardening and admin features (F2-CON-01/04, F1-META-03 reconnection, F4-IMP-01 upload).

-- TOTP 2FA and login lockout on console users
alter table tenant_users add column if not exists totp_enabled_at timestamptz;
alter table tenant_users add column if not exists totp_last_step bigint;          -- replay protection (one use per 30 s step)
alter table tenant_users add column if not exists failed_logins int not null default 0;
alter table tenant_users add column if not exists locked_until timestamptz;

-- Credit top-ups recorded by the tenant owner (the provider key cap is a process setting and is never raised here)
create table if not exists budget_topups (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id),
  gross_usd numeric(12,6) not null check (gross_usd > 0),
  fee_usd numeric(12,6) not null default 0 check (fee_usd >= 0),
  net_usd numeric(12,6) not null check (net_usd >= 0),
  note text,
  config_version_id uuid,          -- the proposed config version that raises the tenant caps (two-person approval)
  created_by uuid,
  created_at timestamptz not null default now()
);

-- Single-use OAuth state nonces (Facebook Login reconnection)
create table if not exists oauth_states (
  nonce text primary key,
  tenant_id uuid not null references tenants(id),
  channel_account_id uuid not null references channel_accounts(id),
  created_by uuid,
  created_at timestamptz not null default now(),
  used_at timestamptz
);

do $$
declare t text;
begin
  foreach t in array array['budget_topups', 'oauth_states']
  loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
    execute format('drop policy if exists tenant_isolation on %I', t);
    execute format($p$create policy tenant_isolation on %I
      using (tenant_id = app_tenant() or app_is_platform())
      with check (tenant_id = app_tenant() or app_is_platform())$p$, t);
  end loop;
end $$;
grant select, insert, update, delete on budget_topups, oauth_states to app_rw;
