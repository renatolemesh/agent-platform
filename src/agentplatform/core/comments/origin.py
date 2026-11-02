"""Where a conversation came from, for the agent to know what it is about (ADR-018): the customer's comment and the post
(caption plus the seller's note), a story, or an ad. Everything here is masked data; the prompt wraps it as data, and the
output guard never treats a caption or a note as a source for product numbers (only the customer's own comment counts)."""
from __future__ import annotations

from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.config.schema import CommentsConfig


async def load_origin(conn: AsyncConnection, conversation_id: UUID, lead_id: UUID, cfg: CommentsConfig) -> list[dict]:
    limit, size = cfg.origin_max_items, cfg.origin_max_chars

    def cut(v: str | None) -> str | None:
        return v[:size] if v else None

    comments = await (await conn.execute(
        """select a.text_masked, m.media_kind, m.caption_masked, m.ad_title, coalesce(au.context_note_masked, oa.context_note_masked) as note
           from comment_actions a
           left join channel_media m on m.tenant_id = a.tenant_id and m.external_media_id = a.media_id
           left join media_automations au on au.media_id = m.id
           left join channel_media om on om.tenant_id = m.tenant_id and om.external_media_id = m.original_media_id
           left join media_automations oa on oa.media_id = om.id
           where a.conversation_id = %s and a.decision <> 'spam' order by a.created_at desc limit %s""", (conversation_id, limit))).fetchall()
    items = [{"kind": "comment", "comment": cut(r["text_masked"]), "media_kind": r["media_kind"], "caption": cut(r["caption_masked"]),
              "note": cut(r["note"]), "ad_title": cut(r["ad_title"])} for r in comments]
    sources = await (await conn.execute(
        """select s.entrypoint, s.utm, m.caption_masked, a.context_note_masked as note from lead_sources s
           left join channel_media m on m.tenant_id = s.tenant_id and m.external_media_id = s.media_id
           left join media_automations a on a.media_id = m.id
           where s.lead_id = %s and s.entrypoint in ('story_reply', 'story_mention', 'ad_referral') order by s.at desc limit %s""",
        (lead_id, limit))).fetchall()
    for r in sources:
        ads = (r["utm"] or {}).get("ads_context_data") or {}
        items.append({"kind": r["entrypoint"], "caption": cut(r["caption_masked"]), "note": cut(r["note"]), "ad_title": cut(ads.get("ad_title"))})
    return items[:limit]


def origin_comments(origin: list[dict] | None) -> list[str]:
    return [o["comment"] for o in origin or [] if o.get("comment")]
