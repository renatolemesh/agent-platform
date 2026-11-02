-- Independent review fixes: seller notices that keep the conversation with the assistant, owner escalation, and removal
-- of message text kept in clear in the inbound event log.
alter table handoffs add column is_notice boolean not null default false;   -- tells the seller; ownership does not change
alter table handoffs add column escalated_at timestamptz;                   -- the owner was told nobody answered

-- Event payloads now carry the text encrypted (text_enc) and only until processing ends. Older rows lose the clear text.
-- Comment events keep theirs until the retention sweep: the seller can still choose to answer recent comments.
update inbound_events set payload_raw = null, payload_canonical = payload_canonical - 'text'
 where event_type <> 'CommentReceived' and status in ('done', 'ignored', 'dead');
update inbound_events set payload_raw = null where event_type = 'CommentReceived';

-- A notice is not a request someone has to take: it stays out of the handoff numbers.
create or replace view v_handoff_sla with (security_invoker = true) as
  select tenant_id, reason, count(*) as handoffs,
         percentile_cont(0.5) within group (order by extract(epoch from (taken_at - notified_at)) / 60)
           filter (where taken_at is not null and notified_at is not null) as p50_minutes_to_take,
         count(*) filter (where sla_breached) as sla_breached
  from handoffs where not is_notice group by 1, 2;
