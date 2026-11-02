-- Tables maintained in the console (F3-DATA-01): who created a version, a note, when it went live.
alter table domain_datasets add column if not exists created_by uuid;
alter table domain_datasets add column if not exists note text;
alter table domain_datasets add column if not exists activated_at timestamptz;
