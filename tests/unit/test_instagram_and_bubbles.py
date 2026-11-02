import hashlib
import hmac
import json
import uuid

import httpx
import pytest

from agentplatform.core.channels.instagram.adapter import InstagramGraphAdapter
from agentplatform.core.channels.ports import (
    AccountRef,
    AmbiguousSendError,
    OutboundMessage,
    RateLimitedError,
    TokenInvalidError,
)
from agentplatform.core.outbox.bubbles import split_bubbles

PAYLOAD = {
    "object": "instagram",
    "entry": [{
        "id": "17841400000000000", "time": 1760040000,
        "messaging": [
            {"sender": {"id": "IGSID1"}, "recipient": {"id": "17841400000000000"}, "timestamp": 1760040000123,
             "message": {"mid": "m1", "text": "quero saber de consórcio"}},
            {"sender": {"id": "17841400000000000"}, "recipient": {"id": "IGSID1"}, "timestamp": 1760040001000,
             "message": {"mid": "m2", "text": "Oi!", "is_echo": True}},
            {"sender": {"id": "IGSID2"}, "recipient": {"id": "17841400000000000"}, "timestamp": 1760040002000,
             "message": {"mid": "m3", "text": "lindo", "reply_to": {"story": {"id": "st1", "url": "https://x"}}}},
            {"sender": {"id": "IGSID3"}, "recipient": {"id": "17841400000000000"}, "timestamp": 1760040003000,
             "message": {"mid": "m4", "attachments": [{"type": "audio", "payload": {"url": "https://cdn/a.mp4"}}]}},
            {"sender": {"id": "IGSID4"}, "recipient": {"id": "17841400000000000"}, "timestamp": 1760040004000,
             "referral": {"ref": "camp1", "ad_id": "ad9", "source": "ADS", "type": "OPEN_THREAD"}},
            {"sender": {"id": "IGSID5"}, "recipient": {"id": "17841400000000000"}, "timestamp": 1760040005000,
             "postback": {"mid": "m5", "title": "Como funciona?", "payload": "icebreaker_1"}},
        ],
        "changes": [{"field": "comments", "value": {"id": "c1", "text": "quero", "from": {"id": "IGSID6", "username": "ana"},
                                                    "media": {"id": "media1", "media_product_type": "FEED"}}}],
    }],
}


def test_parse_all_event_kinds():
    evs = InstagramGraphAdapter(None).parse(json.dumps(PAYLOAD).encode())
    kinds = [e.event_type for e in evs]
    assert kinds == ["MessageReceived", "MessageEcho", "StoryReply", "MessageReceived", "Referral", "Postback", "CommentReceived"]
    echo = evs[1]
    assert echo.contact_external_id == "IGSID1" and echo.idempotency_key == "echo:m2"
    assert evs[3].attachments[0].type == "audio"
    assert evs[4].referral["ad_id"] == "ad9"
    assert evs[6].comment_id == "c1" and evs[6].media_id == "media1" and evs[6].idempotency_key == "cmt:c1"
    assert evs[0].provider_ts.year == 2025 or evs[0].provider_ts.year >= 2025


def test_signature():
    a = InstagramGraphAdapter("secret")
    body = b'{"object":"instagram","entry":[]}'
    good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert a.verify_signature({"x-hub-signature-256": good}, body)
    assert not a.verify_signature({"x-hub-signature-256": "sha256=deadbeef"}, body)
    assert not a.verify_signature({}, body)


ACC = AccountRef(uuid.uuid4(), uuid.uuid4(), "ig1", "page1", "EAAtoken")


@pytest.mark.parametrize("status,body,exc", [
    (400, {"error": {"code": 190, "error_subcode": 460}}, TokenInvalidError),
    (400, {"error": {"code": 80002}}, RateLimitedError),
    (502, {}, AmbiguousSendError),
])
async def test_send_error_mapping(status, body, exc):
    t = httpx.MockTransport(lambda req: httpx.Response(status, json=body))
    a = InstagramGraphAdapter("s", transport=t)
    with pytest.raises(exc):
        await a.send(ACC, OutboundMessage("dm_text", "IGSID1", text="oi"))


async def test_send_never_sets_tag_and_private_reply_shape():
    seen = []
    def h(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"recipient_id": "x", "message_id": "mid.1"})
    a = InstagramGraphAdapter("s", transport=httpx.MockTransport(h))
    r = await a.send(ACC, OutboundMessage("private_reply", None, text="Oi! É para imóvel ou veículo?", comment_id="c1"))
    assert r.external_mid == "mid.1"
    assert seen[0]["recipient"] == {"comment_id": "c1"} and "tag" not in seen[0]


def test_bubbles_respect_bytes_and_accents():
    text = ("Consórcio é uma modalidade de compra em grupo, sem juros, com taxa de administração. " * 40).strip()
    bubbles = split_bubbles(text, 1000)
    assert all(len(b.encode("utf-8")) <= 1000 for b in bubbles)
    assert len(bubbles) <= 4
    assert split_bubbles("Olá! Tudo bem?") == ["Olá! Tudo bem?"]
    huge = "á" * 1200
    assert all(len(b.encode()) <= 1000 for b in split_bubbles(huge, 1000))
