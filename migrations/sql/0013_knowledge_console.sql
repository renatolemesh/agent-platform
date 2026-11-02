-- Material managed by a person in the console (ADR-021). Text waits in chunks without vectors until someone approves it:
-- search only ever reads approved documents, so nothing here reaches a customer or leaves the server before that.
alter table kb_documents
  add column created_at timestamptz not null default now(),
  add column created_by uuid,
  add column supersedes uuid references kb_documents(id),
  add column mask_identifiers boolean not null default false,   -- a person said the identifiers in this file are the company's own
  add column processing text not null default 'done' check (processing in ('queued','indexing','done','failed')),
  add column processing_error text,
  add column processing_at timestamptz,
  add column processing_attempts int not null default 0;
create index kb_documents_processing on kb_documents (tenant_id, processing) where processing in ('queued','indexing');

-- A person can take a passage out (a promise, a commission slide) without losing the rest of the document.
alter table kb_chunks
  add column excluded_at timestamptz,
  add column excluded_by uuid;

alter table faqs
  add column created_at timestamptz not null default now(),
  add column approved_at timestamptz,
  add column processing text not null default 'done' check (processing in ('indexing','done','failed')),
  add column processing_error text,
  add column processing_at timestamptz,
  add column processing_attempts int not null default 0;
create index faqs_processing on faqs (tenant_id, processing) where processing = 'indexing';

-- The file only until it is read. Encrypted, like every other thing a tenant hands us.
create table kb_uploads (
  document_id uuid primary key references kb_documents(id),
  tenant_id uuid not null,
  content_enc bytea not null,
  created_at timestamptz not null default now()
);
alter table kb_uploads enable row level security;
alter table kb_uploads force row level security;
create policy tenant_isolation on kb_uploads using (tenant_id = app_tenant() or app_is_platform()) with check (tenant_id = app_tenant() or app_is_platform());
grant select, insert, update, delete on kb_uploads to app_rw;
