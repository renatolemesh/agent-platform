-- Business dashboards (plan 10 §1). Views respect RLS of the underlying tables (security_invoker).
create or replace view v_funnel with (security_invoker = true) as
  select tenant_id, stage, count(*) as leads, count(*) filter (where is_qualified) as qualified,
         count(*) filter (where opted_out_at is not null) as opted_out
  from leads group by tenant_id, stage;

create or replace view v_attribution with (security_invoker = true) as
  select s.tenant_id, s.entrypoint, coalesce(s.campaign, s.ad_id, s.media_id, '-') as source_ref,
         count(distinct s.lead_id) as leads,
         count(distinct s.lead_id) filter (where l.is_qualified) as qualified,
         count(distinct f.lead_id) filter (where f.submitted_at is not null) as pre_registrations
  from lead_sources s join leads l on l.id = s.lead_id
  left join lead_forms f on f.lead_id = s.lead_id
  where s.is_first_touch group by 1, 2, 3;

create or replace view v_private_reply_variants with (security_invoker = true) as
  select a.tenant_id, a.private_variant, count(*) as sent,
         count(*) filter (where exists (select 1 from messages m join conversations c on c.id = m.conversation_id
                                         join contact_identities ci on ci.contact_id = c.contact_id
                                         where ci.external_user_id = a.author_external_id and m.direction = 'in'
                                           and m.provider_ts between a.created_at and a.created_at + interval '7 days')) as replied
  from comment_actions a where a.private_variant is not null group by 1, 2;

create or replace view v_cost_daily with (security_invoker = true) as
  select tenant_id, date_trunc('day', created_at) as day, node, model_used, count(*) as calls, sum(cost_usd) as cost_usd
  from llm_calls group by 1, 2, 3, 4;

create or replace view v_handoff_sla with (security_invoker = true) as
  select tenant_id, reason, count(*) as handoffs,
         percentile_cont(0.5) within group (order by extract(epoch from (taken_at - notified_at)) / 60)
           filter (where taken_at is not null and notified_at is not null) as p50_minutes_to_take,
         count(*) filter (where sla_breached) as sla_breached
  from handoffs group by 1, 2;
grant select on v_funnel, v_attribution, v_private_reply_variants, v_cost_daily, v_handoff_sla to app_rw;
