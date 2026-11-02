"""Instagram adapter: comment webhook of an ad post, post catalog, polled comments shaped like webhook events, hiding a comment."""
import json
from uuid import uuid4

import httpx

from agentplatform.core.channels.instagram.adapter import InstagramGraphAdapter
from agentplatform.core.channels.ports import AccountRef, OutboundMessage

ACC = AccountRef(uuid4(), uuid4(), "IG1", "PAGE1", "tok")


def adapter(handler) -> InstagramGraphAdapter:
    return InstagramGraphAdapter("secret", transport=httpx.MockTransport(handler))


def test_comment_on_an_ad_keeps_the_ad_and_the_original_post():
    body = {"object": "instagram", "entry": [{"id": "IG1", "time": 1790000000, "changes": [{"field": "comments", "value": {
        "from": {"id": "U1", "username": "fulano"}, "id": "C1", "text": "quero",
        "media": {"id": "AD-M", "media_product_type": "FEED", "ad_id": "A1", "ad_title": "Oferta", "original_media_id": 777}}}]}]}
    e = adapter(lambda r: httpx.Response(200)).parse(json.dumps(body).encode())[0]
    assert (e.ad_id, e.ad_title, e.original_media_id, e.media_id, e.author_username) == ("A1", "Oferta", "777", "AD-M", "fulano")


def test_live_comment_is_marked_live():
    body = {"object": "instagram", "entry": [{"id": "IG1", "time": 1790000000, "changes": [{"field": "live_comments", "value": {
        "from": {"id": "U1"}, "id": "C2", "text": "quero", "media": {"id": "L1"}}}]}]}
    assert adapter(lambda r: httpx.Response(200)).parse(json.dumps(body).encode())[0].media_product_type == "LIVE"


async def test_list_media_maps_kinds_and_prefers_the_video_thumbnail():
    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.path.endswith("/IG1/media") and r.url.params["access_token"] == "tok"
        return httpx.Response(200, json={"data": [
            {"id": "1", "caption": "c", "media_type": "IMAGE", "media_product_type": "FEED", "media_url": "https://x/i.jpg",
             "permalink": "https://p/1", "timestamp": "2026-11-01T16:24:35+0000", "comments_count": 2},
            {"id": "2", "media_type": "VIDEO", "media_product_type": "REELS", "media_url": "https://x/v.mp4",
             "thumbnail_url": "https://x/t.jpg", "timestamp": "2026-11-01T16:24:35+0000"}]})
    feed, reel = await adapter(handler).list_media(ACC, 10)
    assert (feed.kind, feed.image_url, feed.comments_count) == ("post", "https://x/i.jpg", 2) and feed.posted_at.year == 2026
    assert (reel.kind, reel.image_url) == ("reel", "https://x/t.jpg")


async def test_polled_comment_has_the_same_key_as_the_webhook_and_includes_replies():
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{
            "id": "C1", "text": "quero", "timestamp": "2026-11-01T16:24:35+0000", "from": {"id": "U1", "username": "fulano"},
            "replies": {"data": [{"id": "C2", "text": "eu também", "timestamp": "2026-11-01T16:25:35+0000", "from": {"id": "U2"}, "parent_id": "C1"}]}}]})
    top, reply = await adapter(handler).list_comments(ACC, "M1", "reel", 50)
    assert (top.idempotency_key, top.comment_id, top.media_id, top.contact_external_id) == ("cmt:C1", "C1", "M1", "U1")
    assert top.media_product_type == "REELS" and top.author_username == "fulano" and top.account_external_id == "IG1"
    assert (reply.idempotency_key, reply.parent_comment_id) == ("cmt:C2", "C1")


async def test_hide_comment_calls_the_moderation_endpoint():
    seen = {}

    def handler(r: httpx.Request) -> httpx.Response:
        seen.update(path=r.url.path, hide=r.url.params.get("hide"))
        return httpx.Response(200, json={"success": True})
    await adapter(handler).send(ACC, OutboundMessage(kind="hide_comment", recipient_external_id=None, comment_id="C9"))
    assert seen["path"].endswith("/C9") and seen["hide"] == "true"


async def test_a_post_the_provider_no_longer_shows_is_not_an_error():
    a = adapter(lambda r: httpx.Response(400, json={"error": {"code": 100}}))
    assert await a.get_media(ACC, "gone") is None


async def test_username_lookup():
    a = adapter(lambda r: httpx.Response(200, json={"username": "fulano", "id": "U1"}))
    assert await a.get_username(ACC, "U1") == "fulano"


async def test_contact_list_reads_participants_only_and_leaves_out_the_account():
    seen = {}

    def handler(r: httpx.Request) -> httpx.Response:
        seen.update(r.url.params)
        seen["path"] = r.url.path
        return httpx.Response(200, json={"data": [
            {"id": "t1", "participants": {"data": [{"id": "IG1", "username": "conta"}, {"id": "U1", "username": "amigo"}]}},
            {"id": "t2", "participants": {"data": [{"id": "PAGE1"}, {"id": "U2"}, {"id": "U3"}]}}],
            "paging": {"cursors": {"after": "CUR"}, "next": "https://x"}})
    ids, cursor = await adapter(handler).list_conversation_contacts(ACC, None, 50)
    assert ids == ["U1", "U2", "U3"] and cursor == "CUR"
    assert seen["path"].endswith("/PAGE1/conversations") and seen["platform"] == "instagram" and seen["fields"] == "participants"


async def test_contact_list_ends_when_there_is_no_next_page():
    a = adapter(lambda r: httpx.Response(200, json={"data": [], "paging": {"cursors": {"after": "CUR"}}}))
    assert await a.list_conversation_contacts(ACC, "PREV", 50) == ([], None)
