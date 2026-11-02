-- Console simulator: simulated leads (channel 'simulator') stay out of the business dashboards.
-- AI cost (v_cost_daily) keeps them: the simulator spends real budget.
create or replace view v_funnel with (security_invoker = true) as
  select tenant_id, stage, count(*) as leads, count(*) filter (where is_qualified) as qualified,
         count(*) filter (where opted_out_at is not null) as opted_out
  from leads l
  where not exists (select 1 from contact_identities ci where ci.contact_id = l.contact_id and ci.channel = 'simulator')
  group by tenant_id, stage;

create or replace view v_attribution with (security_invoker = true) as
  select s.tenant_id, s.entrypoint, coalesce(s.campaign, s.ad_id, s.media_id, '-') as source_ref,
         count(distinct s.lead_id) as leads,
         count(distinct s.lead_id) filter (where l.is_qualified) as qualified,
         count(distinct f.lead_id) filter (where f.submitted_at is not null) as pre_registrations
  from lead_sources s join leads l on l.id = s.lead_id
  left join lead_forms f on f.lead_id = s.lead_id
  where s.is_first_touch and s.channel <> 'simulator' group by 1, 2, 3;

create or replace view v_handoff_sla with (security_invoker = true) as
  select h.tenant_id, h.reason, count(*) as handoffs,
         percentile_cont(0.5) within group (order by extract(epoch from (h.taken_at - h.notified_at)) / 60)
           filter (where h.taken_at is not null and h.notified_at is not null) as p50_minutes_to_take,
         count(*) filter (where h.sla_breached) as sla_breached
  from handoffs h join conversations c on c.id = h.conversation_id
  join channel_accounts ca on ca.id = c.channel_account_id
  where ca.channel <> 'simulator'
  group by 1, 2;
grant select on v_funnel, v_attribution, v_handoff_sla to app_rw;
