"""Posts written in the console (ADR-024): nothing goes out without approval, and an answer lost from the provider never becomes a second post.
The provider here is a fake and all text is invented."""
import io
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
from agentplatform.core.channels.instagram.adapter import InstagramGraphAdapter
from agentplatform.core.channels.ports import (
    AccountRef,
    AmbiguousSendError,
    MediaInfo,
    PostItem,
    RateLimitedError,
    RetryableSendError,
    SendError,
    TokenInvalidError,
)
from agentplatform.core.config.registry import load_yaml, registry, resolve_with_package
from agentplatform.core.config.schema import PlatformConfig
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.lgpd.service import retention_sweep
from agentplatform.core.ops.observability import evaluate_alerts
from agentplatform.core.packages.contract import load_package
from agentplatform.core.posts import drafts as D
from agentplatform.core.posts.publisher import advance, due_posts
from agentplatform.runtime import Runtime, reset_scoped_runtime, scoped_runtime
from tests.integration.test_console_admin import PW, _client, _user

pytestmark = pytest.mark.db
ROOT = Path(__file__).resolve().parents[2]
PKG = load_package("consortium")
RAW = load_yaml(ROOT / "config/tenants/dev-consortium.yaml")
JPEG = b"\xff\xd8\xff\xc0\x00\x11\x08" + (1080).to_bytes(2, "big") + (1080).to_bytes(2, "big") + b"\x00" * 200
TALL = b"\xff\xd8\xff\xc0\x00\x11\x08" + (3000).to_bytes(2, "big") + (1080).to_bytes(2, "big") + b"\x00" * 200
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def now():
    return datetime.now(timezone.utc)


def config(publishing: bool = True, **posts) -> PlatformConfig:
    doc = RAW.model_dump(mode="json")
    doc["operations"]["posts"] = {"publishing_enabled": publishing, **posts}
    return resolve_with_package(PlatformConfig.model_validate(doc), PKG.manifest.compliance)


ON = config()


def reader(data: bytes):
    stream = io.BytesIO(data)

    async def read(n):
        return stream.read(n)
    return read


async def draft(tenant, *files, kind="feed", caption="Legenda de teste"):
    async with tenant_tx(tenant) as conn:
        account = await (await conn.execute("select id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        post_id = await D.create(conn, tenant, account["id"], None, kind, caption)
    for data in files:
        async with tenant_tx(tenant) as conn:
            await D.add_file(conn, tenant, post_id, reader(data), ON.operations.posts)
    return post_id, account["id"]


async def approved(tenant, *files, when=None, **kw):
    post_id, account = await draft(tenant, *files, **kw)
    async with tenant_tx(tenant) as conn:
        assert await D.approve(conn, await D.load(conn, post_id), None, when or now())
    return post_id, account


async def step(tenant, post_id, account, fake, cfg=ON, at=None):
    return await advance(tenant, post_id, cfg, {"instagram": fake}, {account: AccountRef(tenant, account, "ig", "page", "tok")}, "https://base.test", [], at or now())


async def row(tenant, post_id):
    async with tenant_tx(tenant) as conn:
        return await D.load(conn, post_id)


async def chunks(tenant, post_id):
    async with tenant_tx(tenant) as conn:
        r = await (await conn.execute("select count(*) as n from post_file_chunks c join post_draft_files f on f.id=c.file_id where f.draft_id=%s", (post_id,))).fetchone()
    return r["n"]


# ---------------------------------------------------------------- publishing
async def test_an_approved_image_goes_out_once_joins_the_list_and_its_file_is_gone(tenant):
    fake = FakeChannelAdapter()
    post_id, account = await approved(tenant, JPEG)
    assert await chunks(tenant, post_id) == 1
    jobs = await step(tenant, post_id, account, fake)
    p = await row(tenant, post_id)
    assert p["status"] == "published" and p["external_media_id"] and fake.published_posts == ["container-1"]
    assert fake.post_items[0].caption == "Legenda de teste" and not fake.post_items[0].is_video
    assert await chunks(tenant, post_id) == 0 and p["files"][0]["public_token_hash"] is None
    assert [j[0] for j in jobs] == ["hydrate_media"]
    async with tenant_tx(tenant) as conn:
        m = await (await conn.execute("select media_kind from channel_media where external_media_id=%s", (p["external_media_id"],))).fetchone()
    assert m["media_kind"] == "post"
    await step(tenant, post_id, account, fake)
    assert len(fake.published_posts) == 1


async def test_a_carousel_is_built_from_its_items_and_only_the_album_has_the_text(tenant):
    fake = FakeChannelAdapter()
    post_id, account = await approved(tenant, JPEG, VIDEO, caption="Três fotos")
    await step(tenant, post_id, account, fake)
    assert all(i.carousel_item and i.caption is None for i in fake.post_items) and [i.is_video for i in fake.post_items] == [False, True]
    assert fake.carousels == [(["container-1", "container-2"], "Três fotos")]
    assert fake.published_posts == ["carousel-3"]


async def test_a_video_still_processing_waits_and_the_wait_has_an_end(tenant):
    fake = FakeChannelAdapter(post_statuses={"container-1": "IN_PROGRESS"})
    post_id, account = await approved(tenant, VIDEO, kind="reel")
    await step(tenant, post_id, account, fake)
    assert (await row(tenant, post_id))["status"] == "publishing" and not fake.published_posts
    fake.post_statuses["container-1"] = "FINISHED"
    await step(tenant, post_id, account, fake)
    assert (await row(tenant, post_id))["status"] == "published" and len(fake.post_items) == 1

    fake = FakeChannelAdapter(post_statuses={"container-1": "IN_PROGRESS"})
    other, account = await approved(tenant, VIDEO, kind="reel")
    await step(tenant, other, account, fake)
    await step(tenant, other, account, fake, at=now() + timedelta(minutes=30))
    p = await row(tenant, other)
    assert (p["status"], p["error"]) == ("failed", "container_timeout") and not fake.published_posts and await chunks(tenant, other) == 1


async def test_a_file_the_provider_rejects_ends_the_attempt(tenant):
    fake = FakeChannelAdapter(post_statuses={"container-1": "ERROR"})
    post_id, account = await approved(tenant, JPEG)
    await step(tenant, post_id, account, fake)
    p = await row(tenant, post_id)
    assert (p["status"], p["error"]) == ("failed", "container_error") and p["files"][0]["public_token_hash"] is None


async def test_containers_can_be_retried_because_they_publish_nothing(tenant):
    fake = FakeChannelAdapter(post_failures={"create": [AmbiguousSendError("timeout"), RetryableSendError("connect")]})
    post_id, account = await approved(tenant, JPEG)
    await step(tenant, post_id, account, fake)
    await step(tenant, post_id, account, fake)
    assert (await row(tenant, post_id))["status"] == "publishing" and not fake.published_posts
    await step(tenant, post_id, account, fake)
    assert (await row(tenant, post_id))["status"] == "published" and len(fake.published_posts) == 1


# ---------------------------------------------------------------- a lost answer is never a second post
async def test_publish_answer_lost_but_the_container_says_published(tenant):
    fake = FakeChannelAdapter(publish_then_fail=[AmbiguousSendError("timeout")],
                              media=[MediaInfo("found-1", "post", caption="Legenda de teste", posted_at=now())])
    post_id, account = await approved(tenant, JPEG)
    await step(tenant, post_id, account, fake)
    p = await row(tenant, post_id)
    assert (p["status"], p["external_media_id"]) == ("published", "found-1") and len(fake.published_posts) == 1


async def test_published_but_not_found_in_the_list_is_still_published(tenant):
    fake = FakeChannelAdapter(publish_then_fail=[AmbiguousSendError("timeout")])
    post_id, account = await approved(tenant, JPEG)
    await step(tenant, post_id, account, fake)
    p = await row(tenant, post_id)
    assert (p["status"], p["external_media_id"]) == ("published", None) and await chunks(tenant, post_id) == 0


async def test_publish_answer_lost_and_no_confirmation_goes_to_a_person_and_is_never_retried(tenant):
    fake = FakeChannelAdapter(post_failures={"publish": [AmbiguousSendError("timeout")]})
    post_id, account = await approved(tenant, JPEG)
    await step(tenant, post_id, account, fake)
    p = await row(tenant, post_id)
    assert p["status"] == "publishing" and p["publish_started_at"]
    fake.post_failures["publish"] = [AssertionError("published twice")]    # any further publish call fails this test
    await step(tenant, post_id, account, fake)
    assert (await row(tenant, post_id))["status"] == "publishing"
    await step(tenant, post_id, account, fake, at=now() + timedelta(minutes=30))
    p = await row(tenant, post_id)
    assert (p["status"], p["error"]) == ("unknown", "unconfirmed") and not fake.published_posts
    await step(tenant, post_id, account, fake, at=now() + timedelta(minutes=60))
    async with tenant_tx(tenant) as conn:
        assert await due_posts(conn, tenant, ON.operations.posts) == []


async def test_a_worker_lost_right_after_the_publish_call_only_reads(tenant):
    """The crash leaves the mark and no answer: whoever picks it up must not call publish again."""
    fake = FakeChannelAdapter(post_failures={"publish": [AssertionError("published again after a crash")]})
    post_id, account = await approved(tenant, JPEG)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update post_drafts set status='publishing', publishing_since=now(), publish_started_at=now(), container_id='container-9' where id=%s", (post_id,))
    await step(tenant, post_id, account, fake)
    await step(tenant, post_id, account, fake, at=now() + timedelta(minutes=30))
    assert (await row(tenant, post_id))["status"] == "unknown" and not fake.published_posts


async def test_a_container_already_published_is_not_published_again(tenant):
    """The mark was lost or cleared, and the provider knows better."""
    fake = FakeChannelAdapter(post_statuses={"container-1": "PUBLISHED"}, post_failures={"publish": [AssertionError("published twice")]})
    post_id, account = await approved(tenant, JPEG)
    await step(tenant, post_id, account, fake)
    assert (await row(tenant, post_id))["status"] == "published" and not fake.published_posts


async def test_a_story_whose_answer_was_lost_is_published_without_guessing_which_one(tenant):
    fake = FakeChannelAdapter(publish_then_fail=[AmbiguousSendError("timeout")], media=[MediaInfo("someone-else", "story", posted_at=now())])
    post_id, account = await approved(tenant, JPEG, kind="story")
    await step(tenant, post_id, account, fake)
    p = await row(tenant, post_id)
    assert (p["status"], p["external_media_id"]) == ("published", None)


async def test_the_adapter_is_the_one_of_the_post_channel(tenant):
    """A tenant with more than one channel: another connected channel must not decide this."""
    fake = FakeChannelAdapter()
    post_id, account = await approved(tenant, JPEG)
    await advance(tenant, post_id, ON, {"whatsapp": object(), "instagram": fake}, {account: AccountRef(tenant, account, "ig", "page", "tok")}, "https://base.test", [], now())
    assert (await row(tenant, post_id))["status"] == "published" and len(fake.published_posts) == 1


async def test_what_may_have_been_sent_is_still_looked_at_with_publishing_off(tenant):
    post_id, _ = await approved(tenant, JPEG)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update post_drafts set status='publishing', publishing_since=now(), publish_started_at=now(), container_id='c' where id=%s", (post_id,))
        assert [j[1]["post_id"] for j in await due_posts(conn, tenant, config(False).operations.posts)] == [str(post_id)]


async def test_publish_that_never_left_is_tried_again(tenant):
    for error in (RetryableSendError("connect"), RateLimitedError("limit")):
        fake = FakeChannelAdapter(post_failures={"publish": [error]})
        post_id, account = await approved(tenant, JPEG)
        await step(tenant, post_id, account, fake)
        p = await row(tenant, post_id)
        assert p["status"] == "publishing" and p["publish_started_at"] is None
        await step(tenant, post_id, account, fake)
        assert (await row(tenant, post_id))["status"] == "published" and len(fake.published_posts) == 1


@pytest.mark.parametrize("error,code", [(SendError("HTTP 400 code=100"), "refused"), (TokenInvalidError("190"), "token")])
async def test_a_clear_refusal_ends_the_attempt_and_a_person_can_start_over(tenant, error, code):
    fake = FakeChannelAdapter(post_failures={"publish": [error]})
    post_id, account = await approved(tenant, JPEG)
    await step(tenant, post_id, account, fake)
    p = await row(tenant, post_id)
    assert (p["status"], p["error"]) == ("failed", code) and await chunks(tenant, post_id) == 1
    async with tenant_tx(tenant) as conn:
        assert await D.back_to_draft(conn, post_id)
    p = await row(tenant, post_id)
    assert (p["status"], p["approved_hash"], p["container_id"], p["publish_started_at"], p["error"]) == ("draft", None, None, None, None)
    assert p["files"][0]["container_id"] is None


# ---------------------------------------------------------------- what stops it
async def test_publishing_off_sends_nothing_and_nothing_is_due(tenant):
    fake = FakeChannelAdapter()
    post_id, account = await approved(tenant, JPEG)
    off = config(False)
    await step(tenant, post_id, account, fake, cfg=off)
    assert (await row(tenant, post_id))["status"] == "approved" and not fake.post_items
    async with tenant_tx(tenant) as conn:
        assert await due_posts(conn, tenant, off.operations.posts) == []
        assert len(await due_posts(conn, tenant, ON.operations.posts)) == 1


async def test_the_kill_switch_stops_it_before_every_step(tenant):
    fake = FakeChannelAdapter(post_statuses={"container-1": "IN_PROGRESS"})
    post_id, account = await approved(tenant, JPEG)
    await step(tenant, post_id, account, fake)
    fake.post_statuses["container-1"] = "FINISHED"
    async with platform_tx() as conn:
        await conn.execute("insert into kill_switches (tenant_id, scope, scope_ref, is_on, reason) values (%s,'feature','posts',true,'test')", (tenant,))
    registry.invalidate_kill_switches()
    try:
        await step(tenant, post_id, account, fake)
        assert not fake.published_posts and (await row(tenant, post_id))["status"] == "publishing"
    finally:
        async with platform_tx() as conn:
            await conn.execute("update kill_switches set is_on=false where tenant_id=%s and scope_ref='posts'", (tenant,))
        registry.invalidate_kill_switches()


async def test_a_change_after_the_approval_is_not_published(tenant):
    fake = FakeChannelAdapter()
    post_id, account = await approved(tenant, JPEG)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update post_drafts set caption='Outro texto' where id=%s", (post_id,))
    await step(tenant, post_id, account, fake)
    p = await row(tenant, post_id)
    assert (p["status"], p["error"]) == ("failed", "changed_after_approval") and not fake.post_items


async def test_a_text_that_no_longer_passes_is_not_published(tenant):
    fake = FakeChannelAdapter()
    post_id, account = await approved(tenant, JPEG, caption="Aprovação garantida para todos")
    await step(tenant, post_id, account, fake)
    p = await row(tenant, post_id)
    assert (p["status"], p["error"]) == ("failed", "caption_refused") and not fake.post_items


async def test_an_approved_post_that_is_very_late_waits_for_a_person(tenant):
    fake = FakeChannelAdapter()
    post_id, account = await approved(tenant, JPEG, when=now() - timedelta(days=2))
    await step(tenant, post_id, account, fake)
    p = await row(tenant, post_id)
    assert (p["status"], p["error"]) == ("failed", "late") and not fake.post_items


async def test_the_daily_limit_holds(tenant):
    fake = FakeChannelAdapter()
    cfg = config(max_per_day=1)
    first, account = await approved(tenant, JPEG)
    await step(tenant, first, account, fake, cfg=cfg)
    second, _ = await approved(tenant, JPEG)
    await step(tenant, second, account, fake, cfg=cfg)
    p = await row(tenant, second)
    assert (p["status"], p["error"]) == ("failed", "daily_limit") and len(fake.published_posts) == 1


async def test_without_the_permission_or_the_account_nothing_goes_out(tenant):
    fake = FakeChannelAdapter(publish_scope="permission_to_publish")
    post_id, account = await approved(tenant, JPEG)
    await step(tenant, post_id, account, fake)
    p = await row(tenant, post_id)
    assert (p["status"], p["error"]) == ("failed", "missing_permission") and not fake.post_items

    other, _ = await approved(tenant, JPEG)
    await advance(tenant, other, ON, {"instagram": FakeChannelAdapter()}, {}, "https://base.test", [], now())
    assert (await row(tenant, other))["error"] == "no_account"


async def test_a_second_tenant_cannot_see_a_post_or_its_files(tenant):
    post_id, _ = await approved(tenant, JPEG)
    other = uuid.uuid4()
    async with platform_tx() as conn:
        await conn.execute("insert into tenants (id, slug, display_name, segment_package, segment_package_version) values (%s,%s,'Other','consortium','0.1.0')",
                           (other, f"t-{other.hex[:8]}"))
    async with tenant_tx(other) as conn:
        assert await D.load(conn, post_id) is None
        assert (await (await conn.execute("select count(*) as n from post_file_chunks")).fetchone())["n"] == 0


# ---------------------------------------------------------------- the link the provider fetches
async def test_the_file_link_works_only_while_that_post_is_being_published(tenant):
    from agentplatform.api.main import create_app
    fake = FakeChannelAdapter(post_statuses={"container-1": "IN_PROGRESS"})
    rt = Runtime(adapters={"instagram": fake}, gateway=None, inbox=object())
    token = scoped_runtime(rt)
    try:
        big = JPEG + b"\x07" * 2_500_000       # more than one stored piece
        post_id, account = await approved(tenant, big)
        await step(tenant, post_id, account, fake)
        url = fake.post_items[0].url
        assert url.startswith("https://base.test/pm/")
        path = url.removeprefix("https://base.test")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://t") as c:
            r = await c.get(path)
            assert r.status_code == 200 and r.content == big and r.headers["content-type"] == "image/jpeg" and r.headers["cache-control"] == "no-store"
            assert (await c.head(path)).status_code == 200
            assert (await c.get("/pm/invented-token")).status_code == 404
            async with tenant_tx(tenant) as conn:
                await conn.execute("update post_drafts set status='approved' where id=%s", (post_id,))
            assert (await c.get(path)).status_code == 404
            async with tenant_tx(tenant) as conn:
                await conn.execute("update post_drafts set status='publishing' where id=%s", (post_id,))
            fake.post_statuses["container-1"] = "FINISHED"
            await step(tenant, post_id, account, fake)
            assert (await row(tenant, post_id))["status"] == "published"
            assert (await c.get(path)).status_code == 404
    finally:
        reset_scoped_runtime(token)


# ---------------------------------------------------------------- retention and alerts
async def test_old_drafts_go_and_what_went_out_stays(tenant):
    old, _ = await draft(tenant, JPEG)
    recent, _ = await draft(tenant, JPEG)
    done, _ = await approved(tenant, JPEG)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update post_drafts set updated_at=now() - interval '200 days' where id = any(%s)", ([old, done],))
        await conn.execute("update post_drafts set status='published' where id=%s", (done,))
        await retention_sweep(conn, ON, tenant)
    assert await row(tenant, old) is None and await row(tenant, recent) and await row(tenant, done)


async def test_a_post_that_did_not_go_out_raises_an_alert(tenant):
    post_id, account = await approved(tenant, JPEG, when=now() - timedelta(days=2))
    await step(tenant, post_id, account, FakeChannelAdapter())
    async with tenant_tx(tenant) as conn:
        alerts = await evaluate_alerts(conn, tenant, ON.operations.alerts, [])
    assert any(a.key.startswith("posts_attention:") for a in alerts)


# ---------------------------------------------------------------- the provider's requests
def adapter(body=None, status=200):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(status, json=body if body is not None else {"id": "c1"})
    return InstagramGraphAdapter("s", transport=httpx.MockTransport(handler)), seen


ACCOUNT = AccountRef(uuid.uuid4(), uuid.uuid4(), "ig1", "page1", "tok")


@pytest.mark.parametrize("item,expected,absent", [
    (PostItem("feed", "https://x/a", False, caption="Oi"), {"image_url": "https://x/a", "caption": "Oi"}, ["media_type", "is_carousel_item"]),
    (PostItem("reel", "https://x/a", True, caption="Oi"), {"video_url": "https://x/a", "media_type": "REELS", "caption": "Oi"}, []),
    (PostItem("story", "https://x/a", False, caption="Oi"), {"image_url": "https://x/a", "media_type": "STORIES"}, ["caption"]),
    (PostItem("story", "https://x/a", True), {"video_url": "https://x/a", "media_type": "STORIES"}, []),
    (PostItem("feed", "https://x/a", False, carousel_item=True), {"image_url": "https://x/a", "is_carousel_item": "true"}, ["media_type", "caption"]),
    (PostItem("feed", "https://x/a", True, carousel_item=True), {"video_url": "https://x/a", "is_carousel_item": "true", "media_type": "VIDEO"}, []),
])
async def test_each_kind_of_file_asks_for_the_right_container(item, expected, absent):
    a, seen = adapter()
    assert await a.create_post_item(ACCOUNT, item) == "c1"
    q = seen[0].url.params
    assert seen[0].method == "POST" and seen[0].url.path.endswith("/ig1/media")
    assert all(q.get(k) == v for k, v in expected.items()) and not any(k in q for k in absent)


async def test_album_status_and_publish_requests():
    a, seen = adapter()
    await a.create_carousel(ACCOUNT, ["c1", "c2"], "Oi")
    assert seen[-1].url.params["media_type"] == "CAROUSEL" and seen[-1].url.params["children"] == "c1,c2" and seen[-1].url.params["caption"] == "Oi"
    assert await a.publish_post(ACCOUNT, "c9") == "c1"
    assert seen[-1].url.path.endswith("/ig1/media_publish") and seen[-1].url.params["creation_id"] == "c9"
    a, seen = adapter({"status_code": "FINISHED"})
    assert await a.post_status(ACCOUNT, "c9") == "FINISHED"
    assert seen[-1].url.path.endswith("/c9") and seen[-1].url.params["fields"] == "status_code"


async def test_comments_and_removal_requests():
    a, seen = adapter({"success": True})
    await a.set_comments_enabled(ACCOUNT, "m1", False)
    assert seen[-1].method == "POST" and seen[-1].url.path.endswith("/m1") and seen[-1].url.params["comment_enabled"] == "false"
    await a.delete_media(ACCOUNT, "m1")
    assert seen[-1].method == "DELETE" and seen[-1].url.path.endswith("/m1")


async def test_a_server_error_on_publish_counts_as_unknown_and_a_refusal_as_refused():
    a, _ = adapter(status=500)
    with pytest.raises(AmbiguousSendError):
        await a.publish_post(ACCOUNT, "c9")
    a, _ = adapter({"error": {"code": 100}}, status=400)
    with pytest.raises(SendError) as e:
        await a.publish_post(ACCOUNT, "c9")
    assert not isinstance(e.value, (AmbiguousSendError, RetryableSendError))


# ---------------------------------------------------------------- what is checked before saving
def test_files_are_recognised_by_their_bytes():
    assert D.sniff(JPEG) == ("image/jpeg", False) and D.sniff(VIDEO) == ("video/mp4", True)
    assert D.sniff(b"\x00\x00\x00\x14ftypqt  ") == ("video/quicktime", True)
    assert D.sniff(PNG) is None and D.sniff(b"") is None
    assert D.jpeg_size(JPEG) == (1080, 1080) and D.jpeg_size(TALL) == (1080, 3000) and D.jpeg_size(b"\xff\xd8\xff") is None
    padded = b"\xff\xd8\xff\xe1\x00\x04ab" + JPEG[2:]
    assert D.jpeg_size(padded) == (1080, 1080)


def _f(is_video=False, width=1080, height=1080):
    return {"is_video": is_video, "width": width, "height": height}


@pytest.mark.parametrize("kind,files,rule", [
    ("feed", [], "no_file"), ("feed", [_f(True, None, None)], "feed_single_video"), ("feed", [_f()] * 11, "too_many_files"),
    ("reel", [_f()], "reel_one_video"), ("reel", [_f(True), _f(True)], "reel_one_video"), ("story", [_f(), _f()], "story_one_file"),
    ("feed", [_f(False, 1080, 3000)], "image_shape"), ("feed", [_f(False, 3000, 1080)], "image_shape"), ("feed", [_f(False, 200, 200)], "image_shape"),
])
def test_files_that_do_not_fit_the_kind(kind, files, rule):
    assert [p.rule for p in D.file_problems(kind, files, ON.operations.posts)] == [rule]


@pytest.mark.parametrize("kind,files", [
    ("feed", [_f()]), ("feed", [_f(), _f(True, None, None)]), ("reel", [_f(True, None, None)]), ("story", [_f()]), ("story", [_f(True, None, None)]),
    ("story", [_f(False, 1080, 1920)]), ("feed", [_f(False, 1080, 1350)]), ("feed", [_f(False, None, None)]),
])
def test_files_that_fit(kind, files):
    assert D.file_problems(kind, files, ON.operations.posts) == []


@pytest.mark.parametrize("caption,rule", [
    ("Aprovação garantida para todos", "forbidden_phrase"), ("x" * 2201, "too_long"), (" ".join(f"#t{i}" for i in range(31)), "too_many_hashtags"),
    (" ".join(f"@p{i}" for i in range(21)), "too_many_mentions"), ("Veja em https://outro-site.example/oferta", "url_not_allowed"),
    ("CPF do ganhador 529.982.247-25", "pii_placeholder"),
])
def test_a_caption_that_cannot_go_out(caption, rule):
    assert rule in [p.rule for p in D.caption_problems(ON, caption, [])]


@pytest.mark.parametrize("caption", ["", "Carta de 100 mil, parcelas a partir de R$ 900.", "Fale com a gente: contato@empresa.example ou (11) 99999-0000.", "#carro #sonho @empresa"])
def test_numbers_and_the_company_contact_are_a_person_decision(caption):
    assert D.caption_problems(ON, caption, []) == []


# ---------------------------------------------------------------- the screens
@contextmanager
def channel(fake):
    token = scoped_runtime(Runtime(adapters={"instagram": fake}, gateway=None, inbox=object()))
    try:
        yield fake
    finally:
        reset_scoped_runtime(token)


async def signed_in(tenant, c, *, publishing=True, role="seller", **posts):
    async with tenant_tx(tenant) as conn:
        await registry.publish(conn, tenant, config(publishing, **posts), package_compliance=PKG.manifest.compliance, activate=True)
    email, _ = await _user(tenant, role=role, name="Pessoa")
    await c.post("/console/login", data={"email": email, "password": PW})


async def csrf(c, path="/console/posts/drafts/new"):
    r = await c.get(path)
    assert r.status_code == 200, r.text[:300]
    return re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)


async def new_post(c, *files, kind="feed", caption="Legenda de teste"):
    r = await c.post("/console/posts/drafts", data={"csrf": await csrf(c), "kind": kind, "caption": caption},
                     files=[("files", (f"f{i}.bin", data, "application/octet-stream")) for i, data in enumerate(files)])
    assert r.status_code == 303, r.text[:300]
    return r.headers["location"].split("?")[0], uuid.UUID(r.headers["location"].split("?")[0].rsplit("/", 1)[-1])


async def test_writing_seeing_and_changing_a_draft(tenant):
    with channel(FakeChannelAdapter()):
        async with _client() as c:
            await signed_in(tenant, c, publishing=False)
            page = await c.get("/console/posts")
            assert "Novo post" in page.text
            assert "A publicação está desligada" in (await c.get("/console/posts/drafts/new")).text
            url, post_id = await new_post(c, JPEG, JPEG)
            r = await c.get(url)
            assert r.status_code == 200 and "Legenda de teste" in r.text and "Rascunho" in r.text
            p = await row(tenant, post_id)
            assert len(p["files"]) == 2 and p["files"][0]["width"] == 1080
            preview = await c.get(f"{url}/files/{p['files'][0]['id']}")
            assert preview.content == JPEG and preview.headers["content-type"] == "image/jpeg"
            assert "Escritos aqui" in (await c.get("/console/posts")).text

            token = await csrf(c, url)
            await c.post(url, data={"csrf": token, "kind": "story", "caption": "Novo texto"})
            p = await row(tenant, post_id)
            assert (p["kind"], p["caption"]) == ("story", "Novo texto")
            assert "Um Story é um único arquivo" in (await c.get(url)).text

            first, second = [f["id"] for f in p["files"]]
            await c.post(f"{url}/files/{second}/move", data={"csrf": token, "to": "up"})
            assert [f["id"] for f in (await row(tenant, post_id))["files"]] == [second, first]
            await c.post(f"{url}/files/{first}/remove", data={"csrf": token})
            assert [f["id"] for f in (await row(tenant, post_id))["files"]] == [second]
            assert await chunks(tenant, post_id) == 1

            r = await c.post(f"{url}/delete", data={"csrf": token})
            assert r.status_code == 303 and await row(tenant, post_id) is None
            assert (await c.get(url)).status_code == 404


async def test_files_that_are_refused_say_why_and_leave_nothing_behind(tenant):
    with channel(FakeChannelAdapter()):
        async with _client() as c:
            await signed_in(tenant, c, max_image_bytes=100)
            url, post_id = await new_post(c)
            token = await csrf(c, url)
            r = await c.post(url, data={"csrf": token, "kind": "feed", "caption": ""}, files=[("files", ("a.png", PNG, "image/jpeg"))], follow_redirects=True)
            assert "Use JPEG para imagem" in r.text
            r = await c.post(url, data={"csrf": token, "kind": "feed", "caption": ""}, files=[("files", ("a.jpg", JPEG, "image/jpeg"))], follow_redirects=True)
            assert "Arquivo grande demais" in r.text
            assert (await row(tenant, post_id))["files"] == [] and await chunks(tenant, post_id) == 0


async def test_a_draft_stops_taking_files_at_the_limit(tenant):
    with channel(FakeChannelAdapter()):
        async with _client() as c:
            await signed_in(tenant, c, carousel_max_items=2)
            url, post_id = await new_post(c, JPEG, JPEG)
            r = await c.post(url, data={"csrf": await csrf(c, url), "kind": "feed", "caption": ""}, files=[("files", ("a.jpg", JPEG, "image/jpeg"))], follow_redirects=True)
            assert "o máximo de 2 arquivos" in r.text and len((await row(tenant, post_id))["files"]) == 2


async def test_approving_is_refused_with_publishing_off(tenant, monkeypatch):
    deferred = []

    async def defer(jobs):
        deferred.extend(jobs)
    monkeypatch.setattr("agentplatform.jobs.app.defer_jobs", defer)
    with channel(FakeChannelAdapter()):
        async with _client() as c:
            await signed_in(tenant, c, publishing=False)
            url, post_id = await new_post(c, JPEG)
            page = await c.get(url)
            assert page.text.count("disabled") >= 2
            r = await c.post(f"{url}/approve", data={"csrf": await csrf(c, url), "action": "now"}, follow_redirects=True)
            assert "A publicação está desligada" in r.text
    assert (await row(tenant, post_id))["status"] == "draft" and not deferred


async def test_approving_a_draft_with_a_problem_is_refused(tenant):
    with channel(FakeChannelAdapter()):
        async with _client() as c:
            await signed_in(tenant, c)
            url, post_id = await new_post(c, JPEG, caption="Aprovação garantida para todos")
            page = await c.get(url)
            assert "promessa ou termo proibido" in page.text
            r = await c.post(f"{url}/approve", data={"csrf": await csrf(c, url), "action": "now"}, follow_redirects=True)
            assert "Corrija o que está apontado" in r.text
    assert (await row(tenant, post_id))["status"] == "draft"


async def test_approve_now_schedule_cancel_and_edit_only_as_a_draft(tenant, monkeypatch):
    from agentplatform.jobs import tasks
    deferred = []

    async def defer(jobs):
        deferred.extend(jobs)
    monkeypatch.setattr("agentplatform.jobs.app.defer_jobs", defer)
    fake = FakeChannelAdapter()
    with channel(fake):
        async with _client() as c:
            await signed_in(tenant, c)
            url, post_id = await new_post(c, JPEG)
            token = await csrf(c, url)

            r = await c.post(f"{url}/approve", data={"csrf": token, "action": "schedule", "when": "2001-01-01T10:00"}, follow_redirects=True)
            assert "data e hora no futuro" in r.text
            r = await c.post(f"{url}/approve", data={"csrf": token, "action": "schedule", "when": "bad"}, follow_redirects=True)
            assert "data e hora no futuro" in r.text
            assert (await row(tenant, post_id))["status"] == "draft"

            future = (now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
            r = await c.post(f"{url}/approve", data={"csrf": token, "action": "schedule", "when": future}, follow_redirects=True)
            assert "Aprovado e agendado" in r.text and not deferred
            p = await row(tenant, post_id)
            assert p["status"] == "approved" and p["approved_by"] and p["scheduled_at"] > now()
            async with tenant_tx(tenant) as conn:
                assert await due_posts(conn, tenant, ON.operations.posts) == []
            r = await c.post(url, data={"csrf": token, "kind": "feed", "caption": "Mudei"}, follow_redirects=True)
            assert "Só um rascunho" in r.text and (await row(tenant, post_id))["caption"] == "Legenda de teste"
            r = await c.post(f"{url}/delete", data={"csrf": token}, follow_redirects=True)
            assert "Só um rascunho" in r.text

            r = await c.post(f"{url}/back", data={"csrf": token}, follow_redirects=True)
            assert "Voltou para rascunho" in r.text
            p = await row(tenant, post_id)
            assert (p["status"], p["approved_hash"], p["scheduled_at"]) == ("draft", None, None)

            r = await c.post(f"{url}/approve", data={"csrf": token, "action": "now"}, follow_redirects=True)
            assert "A publicação começou" in r.text
            assert [(j[0], j[1]["post_id"]) for j in deferred] == [("post_publish", str(post_id))]
            await tasks.post_publish(**deferred[0][1])         # this account has no token: the job does not get to the provider
            p = await row(tenant, post_id)
            assert (p["status"], p["error"]) == ("failed", "no_account") and not fake.post_items


async def test_the_job_publishes_with_the_account_of_the_post(tenant):
    from agentplatform.core.security.keys import encrypt_field
    from agentplatform.jobs import tasks
    fake = FakeChannelAdapter()
    post_id, account = await approved(tenant, JPEG)
    async with tenant_tx(tenant) as conn:
        await conn.execute("update channel_accounts set token_enc=%s where id=%s", (await encrypt_field(conn, tenant, "tok", "channel_accounts.token"), account))
        await registry.publish(conn, tenant, ON, package_compliance=PKG.manifest.compliance, activate=True)
    with channel(fake):
        jobs = await tasks.post_publish(str(tenant), str(post_id))
    assert (await row(tenant, post_id))["status"] == "published" and len(fake.published_posts) == 1 and jobs[0][0] == "hydrate_media"


async def test_a_person_answers_when_the_provider_did_not(tenant):
    with channel(FakeChannelAdapter()):
        async with _client() as c:
            await signed_in(tenant, c)
            url, post_id = await new_post(c, JPEG)
            token = await csrf(c, url)

            async def unconfirmed():
                async with tenant_tx(tenant) as conn:
                    p = await D.load(conn, post_id)
                    await D.approve(conn, p, None, now())
                    await conn.execute("update post_drafts set status='unknown', error='unconfirmed', publish_started_at=now(), container_id='c' where id=%s", (post_id,))
            await unconfirmed()
            r = await c.get(url)
            assert "Confira no Instagram" in r.text and "O post está lá" in r.text
            await c.post(f"{url}/back", data={"csrf": token})
            assert (await row(tenant, post_id))["status"] == "unknown"     # cancelling cannot hide a post that may exist

            await c.post(f"{url}/resolve", data={"csrf": token, "action": "not_published"})
            p = await row(tenant, post_id)
            assert p["status"] == "draft" and p["publish_started_at"] is None and await chunks(tenant, post_id) == 1

            await unconfirmed()
            await c.post(f"{url}/resolve", data={"csrf": token, "action": "published"})
            assert (await row(tenant, post_id))["status"] == "published" and await chunks(tenant, post_id) == 0
            r = await c.get(url)
            assert "A Meta não permite editar" in r.text


async def _media(tenant, kind="post", ext=None, ad=None):
    ext = ext or f"m-{uuid.uuid4().hex[:8]}"
    from agentplatform.core.security.keys import encrypt_field
    async with tenant_tx(tenant) as conn:
        account = await (await conn.execute("select id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        await conn.execute("update channel_accounts set token_enc=%s, scopes=%s where id=%s",
                           (await encrypt_field(conn, tenant, "tok", "channel_accounts.token"), ["permission_to_delete"], account["id"]))
        m = await (await conn.execute(
            "insert into channel_media (tenant_id, channel_account_id, external_media_id, media_kind, ad_id, caption_masked, posted_at) values (%s,%s,%s,%s,%s,'Post antigo',now()) returning id",
            (tenant, account["id"], ext, kind, ad))).fetchone()
    return m["id"], ext


async def test_turning_comments_off_and_on(tenant):
    fake = FakeChannelAdapter()
    with channel(fake):
        async with _client() as c:
            await signed_in(tenant, c, publishing=False)
            media_id, ext = await _media(tenant)
            url = f"/console/posts/{media_id}"
            page = await c.get(url)
            assert "Desligar comentários" in page.text and "Ligar comentários" in page.text and "A Meta não permite editar" in page.text
            token = await csrf(c, url)
            r = await c.post(f"{url}/comments", data={"csrf": token, "enabled": "0"}, follow_redirects=True)
            assert "Comentários desligados" in r.text and fake.comments_set == [(ext, False)] and "Desligar comentários" not in r.text
            fake.post_failures["comments"] = [AmbiguousSendError("timeout")]
            r = await c.post(f"{url}/comments", data={"csrf": token, "enabled": "1"}, follow_redirects=True)
            assert "A Meta não confirmou" in r.text and "Ligar comentários" in r.text
            fake.post_failures["comments"] = [SendError("HTTP 400")]
            r = await c.post(f"{url}/comments", data={"csrf": token, "enabled": "1"}, follow_redirects=True)
            assert "Nada mudou no Instagram" in r.text
            story, _ = await _media(tenant, kind="story")
            assert "Ligar comentários" not in (await c.get(f"/console/posts/{story}")).text
            r = await c.post(f"/console/posts/{story}/comments", data={"csrf": token, "enabled": "0"}, follow_redirects=True)
            assert "não pode ser mudado" in r.text and len(fake.comments_set) == 1


async def test_removing_a_post_only_the_owner_only_with_publishing_on_and_never_an_ad(tenant):
    fake = FakeChannelAdapter(delete_scope="permission_to_delete")
    with channel(fake):                # each block is a new process: the runtime keeps a config for 30 seconds
        async with _client() as c:
            await signed_in(tenant, c, publishing=False, role="tenant_owner")
            media_id, ext = await _media(tenant)
            url = f"/console/posts/{media_id}"
            assert "Apagar do Instagram" not in (await c.get(url)).text
            token = await csrf(c, url)
            r = await c.post(f"{url}/remove", data={"csrf": token}, follow_redirects=True)
            assert "não pode ser apagado" in r.text and not fake.deleted_media

    with channel(fake):
        async with _client() as c:
            await signed_in(tenant, c, publishing=True, role="seller")
            assert "Apagar do Instagram" not in (await c.get(url)).text
            assert (await c.post(f"{url}/remove", data={"csrf": await csrf(c, url)})).status_code == 403

    with channel(fake):
        async with _client() as c:
            await signed_in(tenant, c, publishing=True, role="tenant_owner")
            ad, _ = await _media(tenant, ad="ad-1")
            assert "Apagar do Instagram" not in (await c.get(f"/console/posts/{ad}")).text
            page = await c.get(url)
            assert "Apagar do Instagram" in page.text
            token = await csrf(c, url)
            fake.post_failures["delete"] = [AmbiguousSendError("timeout")]
            r = await c.post(f"{url}/remove", data={"csrf": token}, follow_redirects=True)
            assert "A Meta não confirmou" in r.text
            r = await c.post(f"{url}/remove", data={"csrf": token}, follow_redirects=True)
            assert "Post apagado do Instagram" in r.text and fake.deleted_media == [ext]
            assert ext not in (await c.get("/console/posts")).text and "Post antigo</a>" in (await c.get("/console/posts")).text.replace(ext, "")

    # the provider still lists it: it exists, whatever was recorded here
    from agentplatform.core.comments.catalog import _upsert
    async with tenant_tx(tenant) as conn:
        account = await (await conn.execute("select id from channel_accounts where tenant_id=%s", (tenant,))).fetchone()
        await _upsert(conn, tenant, account["id"], MediaInfo(ext, "post", caption="Post antigo"))
        m = await (await conn.execute("select removed_at from channel_media where id=%s", (media_id,))).fetchone()
    assert m["removed_at"] is None


async def test_without_the_provider_permission_the_owner_cannot_remove(tenant):
    fake = FakeChannelAdapter(delete_scope="another_permission")
    with channel(fake):
        async with _client() as c:
            await signed_in(tenant, c, role="tenant_owner")
            media_id, _ = await _media(tenant)
            url = f"/console/posts/{media_id}"
            assert "Apagar do Instagram" not in (await c.get(url)).text
            r = await c.post(f"{url}/remove", data={"csrf": await csrf(c, url)}, follow_redirects=True)
            assert "não pode ser apagado" in r.text and not fake.deleted_media


async def test_someone_from_another_tenant_cannot_reach_a_draft(tenant):
    other = uuid.uuid4()
    async with platform_tx() as conn:
        await conn.execute("insert into tenants (id, slug, display_name, segment_package, segment_package_version) values (%s,%s,'Other','consortium','0.1.0')",
                           (other, f"t-{other.hex[:8]}"))
        await conn.execute("insert into channel_accounts (tenant_id, channel, external_account_id, external_page_id) values (%s,'instagram',%s,%s)",
                           (other, f"ig-{other.hex[:10]}", f"page-{other.hex[:10]}"))
    post_id, _ = await draft(tenant, JPEG)
    p = await row(tenant, post_id)
    with channel(FakeChannelAdapter()):
        async with _client() as c:
            await signed_in(other, c)
            url = f"/console/posts/drafts/{post_id}"
            assert (await c.get(url)).status_code == 404
            assert (await c.get(f"{url}/files/{p['files'][0]['id']}")).status_code == 404
            r = await c.post(f"{url}/delete", data={"csrf": await csrf(c)})
            assert r.status_code == 303
    assert await row(tenant, post_id)
