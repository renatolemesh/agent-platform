-- Prompt versions edited in the console (prompt lab). Seeds stay as files (prompts/<name>@vN.j2); a tenant's edited
-- version lives here with an immutable, globally unique label (<name>@t-xxxxxxxx) and is referenced by config like a
-- file version. Rows are immutable: editing creates a new row.
alter table prompt_templates add column tenant_id uuid;
alter table prompt_templates add column label text;
alter table prompt_templates add column base_version text;
alter table prompt_templates add column note text;
alter table prompt_templates add column created_by uuid;
alter table prompt_templates drop constraint prompt_templates_name_version_key;
create unique index prompt_templates_label on prompt_templates (label);
create unique index prompt_templates_tenant_name_version
  on prompt_templates (coalesce(tenant_id, '00000000-0000-0000-0000-000000000000'), name, version);

alter table prompt_templates enable row level security;
alter table prompt_templates force row level security;
create policy tenant_read on prompt_templates for select
  using (tenant_id is null or tenant_id = app_tenant() or app_is_platform());
create policy tenant_write on prompt_templates for all
  using (tenant_id = app_tenant() or app_is_platform())
  with check (tenant_id = app_tenant() or app_is_platform());
grant select, insert on prompt_templates to app_rw;
