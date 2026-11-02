-- external_mid is unique per provider account, not globally: replace the global unique index
drop index if exists outbox_mid;
create index outbox_mid on outbox (tenant_id, external_mid) where external_mid is not null;
