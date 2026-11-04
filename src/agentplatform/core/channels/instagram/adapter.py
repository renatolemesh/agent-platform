"""Instagram API with Facebook Login (graph.facebook.com, Page token). Facts verified 2026-10-09 (plan 05 §3, 18 §1)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Mapping

import httpx

from agentplatform.core.channels.events import Attachment, CanonicalEvent
from agentplatform.core.channels.ports import (
    AccountRef,
    AmbiguousSendError,
    ChannelCapabilities,
    MediaInfo,
    OutboundMessage,
    PartialParse,
    PostItem,
    RateLimitedError,
    RetryableSendError,
    SendError,
    SendResult,
    TokenInvalidError,
)

log = logging.getLogger(__name__)

CAPS = ChannelCapabilities(
    max_text_bytes=1000, quick_replies_max=13, quick_reply_title_max=20, typing_indicator=True,
    service_window=timedelta(hours=24), supports_templates=False, text_per_s=100, media_per_s=10,
    private_replies_per_h=750, mixed_inbox=True,
)
_RATE_CODES = {4, 17, 32, 613, 80002}
# What a refusal is about (Messenger Platform error reference, code and subcode). Everything else: scope unknown.
_ERROR_SCOPE = {
    (10, 2534077): "account", (100, 2534013): "account", (200, 2534041): "account", (100, 2534029): "account",
    (10, 1893063): "account", (613, 2018338): "account",
    (551, None): "recipient", (200, 1545041): "recipient", (10, 2018108): "recipient", (100, 2534014): "recipient",
    (100, 2018001): "recipient",
    (10, 2534022): "window", (10, 2018278): "window", (10, 2018065): "window",
    (100, 2534025): "object", (100, 33): "object",
    (100, 2534037): "thread_owner",
}
_MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,thumbnail_url,media_url,timestamp,comments_count"
_STORY_FIELDS = "id,caption,media_type,media_product_type,permalink,thumbnail_url,media_url,timestamp"
_COMMENT_FIELDS = "id,text,timestamp,from{id,username},parent_id"
_KIND = {"FEED": "post", "REELS": "reel", "STORY": "story", "AD": "ad"}


_CLOCK_MARGIN = timedelta(seconds=30)


def _graph_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z") if value[-5] in "+-" else datetime.fromisoformat(value)


def _body(r: httpx.Response) -> dict:
    """A gateway in front of the provider can answer anything: a body that is not a JSON object counts as empty."""
    try:
        data = r.json() if r.content else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _refusal(r: httpx.Response, data: dict) -> SendError:
    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    code, sub = err.get("code"), err.get("error_subcode")
    if code == 190:
        return TokenInvalidError(f"190/{sub}")
    scope = _ERROR_SCOPE.get((code, sub)) or _ERROR_SCOPE.get((code, None))
    if not scope and code in _RATE_CODES:
        return RateLimitedError(f"rate limited ({code}/{sub})")
    return SendError(f"HTTP {r.status_code} code={code} subcode={sub}", scope=scope)


def _ts(ms: int | float | str) -> datetime:
    v = float(ms)
    if v > 1e12:          # milliseconds
        v /= 1000
    return datetime.fromtimestamp(v, tz=timezone.utc)


class InstagramGraphAdapter:
    channel = "instagram"
    publish_scope = "instagram_content_publish"
    delete_scope = "instagram_manage_contents"

    def __init__(self, app_secret: str | None, graph_version: str = "v26.0",
                 transport: httpx.AsyncBaseTransport | None = None, timeout_s: float = 10):
        self._secret = app_secret.encode() if app_secret else None
        self._client = httpx.AsyncClient(base_url=f"https://graph.facebook.com/{graph_version}",
                                         transport=transport, timeout=timeout_s)

    def capabilities(self) -> ChannelCapabilities:
        return CAPS

    # ----------------------------------------------------------- inbound
    def verify_signature(self, headers: Mapping[str, str], body: bytes) -> bool:
        if not self._secret:
            return False
        sig = headers.get("x-hub-signature-256") or headers.get("X-Hub-Signature-256") or ""
        if not sig.startswith("sha256="):
            return False
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig[7:], expected)

    def parse(self, body: bytes) -> list[CanonicalEvent]:
        """One unreadable item never costs the rest of the delivery: the good ones go on and the error carries them."""
        data = json.loads(body)
        if data.get("object") not in ("instagram", "page"):
            return []
        events: list[CanonicalEvent] = []
        failures = 0
        for entry in data.get("entry", []):
            account = str(entry.get("id"))
            items = [(m, False) for m in entry.get("messaging", []) or []] + [(m, True) for m in entry.get("standby", []) or []]
            for m, standby in items:
                try:
                    ev = self._parse_messaging(account, m, standby)
                except Exception:
                    log.exception("unreadable messaging item")
                    failures += 1
                    continue
                if ev:
                    events.append(ev)
            for ch in entry.get("changes", []) or []:
                if ch.get("field") in ("comments", "live_comments"):
                    try:
                        ev = self._parse_comment_change(account, ch, entry.get("time", 0))
                    except Exception:
                        log.exception("unreadable comment item")
                        failures += 1
                        continue
                    if ev:
                        events.append(ev)
        if failures:
            raise PartialParse(events, failures)
        return events

    @staticmethod
    def _parse_comment_change(account: str, ch: dict, entry_time) -> CanonicalEvent | None:
        v = ch.get("value", {})
        comment_id = v.get("comment_id") or v.get("id")     # Facebook Login sends comment_id, Instagram Login sends id
        if not comment_id:
            return None
        frm = v.get("from") or {}
        media = v.get("media") or {}
        return CanonicalEvent(
            event_type="CommentReceived", channel="instagram", account_external_id=account,
            contact_external_id=str(frm.get("id")) if frm.get("id") else None,
            idempotency_key=f"cmt:{comment_id}", provider_ts=_ts(entry_time),
            text=v.get("text"), comment_id=str(comment_id), media_id=str(media.get("id")) if media.get("id") else None,
            media_product_type="LIVE" if ch["field"] == "live_comments" else media.get("media_product_type"),
            parent_comment_id=v.get("parent_id"), author_username=frm.get("username"),
            ad_id=media.get("ad_id"), ad_title=media.get("ad_title"),
            original_media_id=str(media["original_media_id"]) if media.get("original_media_id") else None)

    def _parse_messaging(self, account: str, m: dict, standby: bool = False) -> CanonicalEvent | None:
        sender = str((m.get("sender") or {}).get("id", ""))
        recipient = str((m.get("recipient") or {}).get("id", ""))
        ts = _ts(m.get("timestamp", 0))
        base = dict(channel="instagram", account_external_id=account, provider_ts=ts, standby=standby)
        if "message" in m:
            msg = m["message"]
            mid = msg.get("mid")
            if not mid:                    # e.g. handoff admin text: not a message of this conversation
                return None
            atts = [Attachment(type=a.get("type") if a.get("type") in Attachment.model_fields["type"].annotation.__args__ else "unsupported",
                               url=(a.get("payload") or {}).get("url")) for a in msg.get("attachments", []) or []]
            if msg.get("is_deleted"):      # the customer's or the owner's: comes before the echo check
                return CanonicalEvent(event_type="MessageDeleted", contact_external_id=recipient if msg.get("is_echo") else sender,
                                      idempotency_key=f"del:{mid}", mid=mid, **base)
            if msg.get("is_echo"):
                return CanonicalEvent(event_type="MessageEcho", contact_external_id=recipient, idempotency_key=f"echo:{mid}",
                                      mid=mid, text=msg.get("text"), attachments=atts, sender_app_id=msg.get("app_id"), **base)
            reply_to = msg.get("reply_to") or {}
            story = reply_to.get("story")
            etype = "StoryReply" if story else ("StoryMention" if any(a.type == "story_mention" for a in atts) else "MessageReceived")
            qr = (msg.get("quick_reply") or {}).get("payload")
            return CanonicalEvent(event_type=etype, contact_external_id=sender, idempotency_key=f"msg:{mid}", mid=mid,
                                  text=msg.get("text"), attachments=atts, story_id=(story or {}).get("id"),
                                  reply_to_mid=reply_to.get("mid"), postback_payload=qr, referral=msg.get("referral") or m.get("referral"),
                                  is_self=bool(msg.get("is_self")), **base)
        if "postback" in m:
            pb = m["postback"]
            return CanonicalEvent(event_type="Postback", contact_external_id=sender, idempotency_key=f"pb:{pb.get('mid')}",
                                  mid=pb.get("mid"), text=pb.get("title"), postback_payload=pb.get("payload"),
                                  referral=pb.get("referral"), **base)
        if "referral" in m:
            return CanonicalEvent(event_type="Referral", contact_external_id=sender,
                                  idempotency_key=f"ref:{sender}:{m.get('timestamp')}", referral=m["referral"], **base)
        if "reaction" in m:
            r = m["reaction"]
            return CanonicalEvent(event_type="Reaction", contact_external_id=sender,
                                  idempotency_key=f"react:{r.get('mid')}:{r.get('action')}:{m.get('timestamp')}",
                                  mid=r.get("mid"), text=r.get("emoji") or r.get("reaction"), **base)
        if "read" in m:
            return CanonicalEvent(event_type="Read", contact_external_id=sender, idempotency_key=f"read:{m['read'].get('mid')}",
                                  mid=m["read"].get("mid"), **base)
        for key in ("pass_thread_control", "take_thread_control", "messaging_handover"):
            if key in m:
                h = m[key]
                return CanonicalEvent(event_type="ConversationStatusChanged", contact_external_id=sender,
                                      idempotency_key=f"handover:{sender}:{m.get('timestamp')}:{key}",
                                      referral={"handover": key, "new_owner_app_id": str(h.get("new_owner_app_id") or ""),
                                                "previous_owner_app_id": str(h.get("previous_owner_app_id") or "")}, **base)
        if "message_edit" in m:
            e = m["message_edit"]
            return CanonicalEvent(event_type="MessageEdited", contact_external_id=sender,
                                  idempotency_key=f"edit:{e.get('mid')}:{e.get('num_edit', 0)}", mid=e.get("mid"),
                                  text=e.get("text"), **base)
        return None

    # ---------------------------------------------------------- outbound
    async def send(self, account: AccountRef, msg: OutboundMessage) -> SendResult:
        if not account.token:
            raise TokenInvalidError("no token")
        params = {"access_token": account.token}
        page = account.external_page_id or account.external_account_id
        if msg.kind == "public_comment_reply":
            return await self._post(f"/{msg.comment_id}/replies", params={**params, "message": msg.text or ""}, json_body=None,
                                    id_field="id")
        if msg.kind == "hide_comment":
            await self._post(f"/{msg.comment_id}", params={**params, "hide": "true"}, json_body=None, id_field=None)
            return SendResult(None)
        if msg.kind == "typing":
            await self._post(f"/{page}/messages", params=params,
                             json_body={"recipient": {"id": msg.recipient_external_id}, "sender_action": "typing_on"},
                             id_field=None)
            return SendResult(None)
        if msg.kind == "private_reply":
            recipient = {"comment_id": msg.comment_id}
        else:
            recipient = {"id": msg.recipient_external_id}
        message: dict = {"text": msg.text}
        if msg.quick_replies:
            message["quick_replies"] = [{"content_type": "text", "title": q[:20], "payload": f"qr:{i}:{q[:40]}"}
                                        for i, q in enumerate(msg.quick_replies[:13])]
        # NOTE: never sends a `tag` (HUMAN_AGENT is for humans only; plan 11 M3)
        body = {"recipient": recipient, "message": message}
        return await self._post(f"/{page}/messages", params=params, json_body=body, id_field="message_id")

    async def _post(self, path: str, *, params: dict, json_body: dict | None, id_field: str | None, method: str = "POST") -> SendResult:
        try:
            r = await self._client.request(method, path, params=params, json=json_body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:   # never reached the provider: safe to retry
            raise RetryableSendError(f"connect: {type(e).__name__}") from e
        except httpx.HTTPError as e:                  # may have reached it: reconcile, never resend
            raise AmbiguousSendError(f"ambiguous: {type(e).__name__}") from e
        if r.status_code >= 500:
            raise AmbiguousSendError(f"HTTP {r.status_code}")
        data = _body(r)
        if r.status_code >= 400:
            raise _refusal(r, data)
        if id_field and not data.get(id_field):       # accepted without saying what it created: only the echo can tell
            raise AmbiguousSendError(f"HTTP {r.status_code} without {id_field}")
        return SendResult(str(data[id_field]) if id_field else None)

    async def find_sent_message(self, account: AccountRef, recipient_external_id: str, text: str,
                                sent_after: datetime | None = None) -> str | None:
        """Only a message at or after the attempt confirms it: the same words sent earlier (a standard reply) prove nothing."""
        if not recipient_external_id:      # private reply: the thread is only known by the comment, so this cannot confirm it
            return None
        page = account.external_page_id or account.external_account_id
        try:
            r = await self._client.get(f"/{page}/conversations", params={
                "platform": "instagram", "user_id": recipient_external_id,
                "fields": "messages{id,message,from,created_time}", "access_token": account.token or ""})
            r.raise_for_status()
        except httpx.HTTPError:
            return None
        for conv in _body(r).get("data", []):
            for m in (conv.get("messages") or {}).get("data", []):
                if (m.get("message") or "").strip() != text.strip() or str((m.get("from") or {}).get("id")) == recipient_external_id:
                    continue
                if sent_after is not None and (not m.get("created_time") or _graph_time(m["created_time"]) < sent_after - _CLOCK_MARGIN):
                    continue
                return m.get("id")
        return None

    # ------------------------------------- questions offered in a new direct
    async def set_ice_breakers(self, account: AccountRef, questions: list[str]) -> None:
        """A tap arrives as a postback with the question as its title. An empty list takes them off the account."""
        if not account.token:
            raise TokenInvalidError("no token")
        params = {"platform": "instagram", "access_token": account.token}
        if not questions:
            await self._post("/me/messenger_profile", params=params, json_body={"fields": ["ice_breakers"]}, id_field=None, method="DELETE")
            return
        body = {"platform": "instagram", "ice_breakers": [{"locale": "default", "call_to_actions": [
            {"question": q, "payload": f"quick_question:{i}"} for i, q in enumerate(questions)]}]}
        await self._post("/me/messenger_profile", params=params, json_body=body, id_field=None)

    # ----------------------------------------------------------- publishing (ADR-024; facts verified 2026-11-03, plan 18)
    async def _create_container(self, account: AccountRef, params: dict) -> str:
        if not account.token:
            raise TokenInvalidError("no token")
        r = await self._post(f"/{account.external_account_id}/media", params={**params, "access_token": account.token}, json_body=None, id_field="id")
        return str(r.external_mid)

    async def create_post_item(self, account: AccountRef, item: PostItem) -> str:
        params: dict = {"video_url": item.url} if item.is_video else {"image_url": item.url}
        if item.carousel_item:
            params["is_carousel_item"] = "true"
            if item.is_video:
                params["media_type"] = "VIDEO"
        elif item.kind == "story":
            params["media_type"] = "STORIES"
        elif item.kind == "reel":
            params["media_type"] = "REELS"
        if item.caption and item.kind != "story":
            params["caption"] = item.caption
        return await self._create_container(account, params)

    async def create_carousel(self, account: AccountRef, children: list[str], caption: str) -> str:
        return await self._create_container(account, {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption})

    async def post_status(self, account: AccountRef, container_id: str) -> str:
        return str((await self._get(f"/{container_id}", account, {"fields": "status_code"})).get("status_code") or "")

    async def publish_post(self, account: AccountRef, container_id: str) -> str:
        if not account.token:
            raise TokenInvalidError("no token")
        r = await self._post(f"/{account.external_account_id}/media_publish", params={"creation_id": container_id, "access_token": account.token},
                             json_body=None, id_field="id")
        return str(r.external_mid)

    async def set_comments_enabled(self, account: AccountRef, media_id: str, enabled: bool) -> None:
        if not account.token:
            raise TokenInvalidError("no token")
        await self._post(f"/{media_id}", params={"comment_enabled": "true" if enabled else "false", "access_token": account.token}, json_body=None, id_field=None)

    async def delete_media(self, account: AccountRef, media_id: str) -> None:
        if not account.token:
            raise TokenInvalidError("no token")
        await self._post(f"/{media_id}", params={"access_token": account.token}, json_body=None, id_field=None, method="DELETE")

    # ----------------------------------------------------------- catalog
    async def _get(self, path: str, account: AccountRef, params: dict) -> dict:
        if not account.token:
            raise TokenInvalidError("no token")
        try:
            r = await self._client.get(path, params={**params, "access_token": account.token})
        except httpx.HTTPError as e:
            raise RetryableSendError(f"catalog: {type(e).__name__}") from e
        data = _body(r)
        if r.status_code >= 400:
            raise _refusal(r, data)
        return data

    @staticmethod
    def _media(d: dict, kind: str | None = None) -> MediaInfo:
        ts = d.get("timestamp")
        return MediaInfo(external_id=str(d["id"]), kind=kind or _KIND.get(d.get("media_product_type") or "", "post"),
                         media_type=d.get("media_type"), caption=d.get("caption"), permalink=d.get("permalink"),
                         image_url=d.get("thumbnail_url") or d.get("media_url"),
                         posted_at=datetime.fromisoformat(ts) if ts else None, comments_count=d.get("comments_count"))

    async def list_media(self, account: AccountRef, limit: int) -> list[MediaInfo]:
        data = await self._get(f"/{account.external_account_id}/media", account, {"fields": _MEDIA_FIELDS, "limit": limit})
        return [self._media(d) for d in data.get("data", [])]

    async def list_stories(self, account: AccountRef) -> list[MediaInfo]:
        data = await self._get(f"/{account.external_account_id}/stories", account, {"fields": _STORY_FIELDS})
        return [self._media(d, "story") for d in data.get("data", [])]

    async def get_media(self, account: AccountRef, media_id: str) -> MediaInfo | None:
        try:
            return self._media(await self._get(f"/{media_id}", account, {"fields": _MEDIA_FIELDS}))
        except (TokenInvalidError, RetryableSendError):
            raise
        except SendError:
            return None                # expired story or a post the provider no longer shows

    async def list_comments(self, account: AccountRef, media_id: str, kind: str, limit: int) -> list[CanonicalEvent]:
        """Same canonical event and idempotency key as the webhook, so a comment is processed once whichever arrives first."""
        data = await self._get(f"/{media_id}/comments", account, {
            "fields": f"{_COMMENT_FIELDS},replies{{{_COMMENT_FIELDS}}}", "limit": limit})
        out = []
        for c in data.get("data", []):
            out.append(self._comment_event(account, media_id, kind, c))
            out += [self._comment_event(account, media_id, kind, r) for r in (c.get("replies") or {}).get("data", [])]
        return out

    @staticmethod
    def _comment_event(account: AccountRef, media_id: str, kind: str, c: dict) -> CanonicalEvent:
        frm = c.get("from") or {}
        product = {v: k for k, v in _KIND.items()}.get(kind)
        return CanonicalEvent(event_type="CommentReceived", channel="instagram", account_external_id=account.external_account_id,
                              contact_external_id=str(frm["id"]) if frm.get("id") else None,
                              idempotency_key=f"cmt:{c['id']}", provider_ts=datetime.fromisoformat(c["timestamp"]),
                              text=c.get("text"), comment_id=str(c["id"]), media_id=media_id, media_product_type=product,
                              parent_comment_id=c.get("parent_id"), author_username=frm.get("username"))

    async def get_username(self, account: AccountRef, external_user_id: str) -> str | None:
        return (await self._get(f"/{external_user_id}", account, {"fields": "username"})).get("username")

    async def list_conversation_contacts(self, account: AccountRef, after: str | None, limit: int) -> tuple[list[str], str | None]:
        """Who already talks to this account: participants only, never message contents. Returns (ids, next cursor)."""
        page = account.external_page_id or account.external_account_id
        params: dict = {"platform": "instagram", "fields": "participants", "limit": limit}
        if after:
            params["after"] = after
        data = await self._get(f"/{page}/conversations", account, params)
        own = {account.external_account_id, account.external_page_id}
        ids = {str(p["id"]) for c in data.get("data", []) for p in (c.get("participants") or {}).get("data", [])
               if p.get("id") and str(p["id"]) not in own}
        paging = data.get("paging") or {}
        return sorted(ids), (paging.get("cursors") or {}).get("after") if paging.get("next") else None

    async def list_recent_threads(self, account: AccountRef, limit: int) -> list[dict]:
        """Who wrote lately and when: participants and time only, never contents."""
        page = account.external_page_id or account.external_account_id
        data = await self._get(f"/{page}/conversations", account, {"platform": "instagram", "fields": "participants,updated_time", "limit": limit})
        own = {account.external_account_id, account.external_page_id}
        out = []
        for c in data.get("data", []):
            others = [str(p["id"]) for p in (c.get("participants") or {}).get("data", []) if p.get("id") and str(p["id"]) not in own]
            if others and c.get("updated_time"):
                out.append({"id": c["id"], "contact": others[0], "updated_at": _graph_time(c["updated_time"])})
        return out

    async def list_thread_messages(self, account: AccountRef, thread_id: str, limit: int) -> list[dict]:
        """Newest first. Only called for threads the assistant may read (ADR-019)."""
        data = await self._get(f"/{thread_id}/messages", account, {"fields": "id,message,from,created_time", "limit": limit})
        return [{"mid": m["id"], "text": m.get("message"), "from": str((m.get("from") or {}).get("id", "")), "at": _graph_time(m["created_time"])}
                for m in data.get("data", []) if m.get("id") and m.get("created_time")]

    async def aclose(self) -> None:
        await self._client.aclose()
