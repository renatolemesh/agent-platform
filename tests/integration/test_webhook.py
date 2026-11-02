import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentplatform.api.webhooks import Spool, build_router, replay_spool
from agentplatform.core.channels.instagram.adapter import InstagramGraphAdapter
from agentplatform.core.db.pool import tenant_tx

pytestmark = pytest.mark.db


def body_for(account_id: str, mid: str) -> bytes:
    return json.dumps({"object": "instagram", "entry": [{"id": account_id, "time": 1760040000, "messaging": [
        {"sender": {"id": "IGSIDW"}, "recipient": {"id": account_id}, "timestamp": 1760040000000,
         "message": {"mid": mid, "text": "oi, meu cpf é 529.982.247-25"}}]}]}).encode()


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()


@pytest.fixture
def app_and_spool(tmp_path):
    spool = Spool(str(tmp_path))
    app = FastAPI()
    app.include_router(build_router({"instagram": InstagramGraphAdapter("s3cret")}, spool))
    return app, spool


async def _account(tenant):
    async with tenant_tx(tenant) as conn:
        return (await (await conn.execute("select external_account_id from channel_accounts where tenant_id=%s", (tenant,))).fetchone())["external_account_id"]


async def test_fast_ack_idempotent_and_masked(tenant, app_and_spool):
    app, _ = app_and_spool
    acc = await _account(tenant)
    b = body_for(acc, f"mid-{tenant.hex[:6]}")
    with TestClient(app) as c:
        r1 = c.post("/webhooks/meta", content=b, headers={"X-Hub-Signature-256": sign(b)})
        r2 = c.post("/webhooks/meta", content=b, headers={"X-Hub-Signature-256": sign(b)})
        bad = c.post("/webhooks/meta", content=b, headers={"X-Hub-Signature-256": "sha256=00"})
    assert r1.json()["accepted"] == 1 and r2.json()["accepted"] == 0 and bad.status_code == 401
    async with tenant_tx(tenant) as conn:
        row = await (await conn.execute("select payload_canonical from inbound_events where tenant_id=%s", (tenant,))).fetchone()
    assert "[CPF]" in row["payload_canonical"]["text_masked"]


async def test_db_down_spools_and_replays(tenant, app_and_spool):
    app, spool = app_and_spool
    acc = await _account(tenant)
    b = body_for(acc, f"mid-spool-{tenant.hex[:6]}")
    with patch("agentplatform.api.webhooks.persist_events", side_effect=OSError("db down")):
        with TestClient(app) as c:
            r = c.post("/webhooks/meta", content=b, headers={"X-Hub-Signature-256": sign(b)})
    assert r.status_code == 200 and r.json()["status"] == "spooled" and spool.size() > 0
    n = await replay_spool(spool, {"instagram": InstagramGraphAdapter("s3cret")})
    assert n == 1 and spool.size() == 0


async def test_replay_failure_keeps_batch_and_a_later_replay_finishes(tenant, app_and_spool):
    """A crash/failure during replay must never lose spooled webhooks (the batch stays on disk until persisted)."""
    _, spool = app_and_spool
    acc = await _account(tenant)
    b = body_for(acc, f"mid-crash-{tenant.hex[:6]}")
    spool.append("instagram", b)
    with patch("agentplatform.api.webhooks.persist_events", side_effect=OSError("db still down")):
        assert await replay_spool(spool, {"instagram": InstagramGraphAdapter("s3cret")}) == 0
    assert spool.size() > 0 and list(spool.dir.glob("webhooks.*.replay"))
    spool.append("instagram", body_for(acc, f"mid-crash2-{tenant.hex[:6]}"))       # new traffic while down
    assert await replay_spool(spool, {"instagram": InstagramGraphAdapter("s3cret")}) == 2
    assert spool.size() == 0
