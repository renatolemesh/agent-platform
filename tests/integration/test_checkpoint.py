"""The turn runs with the encrypted Postgres checkpointer; stored checkpoints are not readable in clear text."""
import pytest

from agentplatform.core.agent.checkpoint import encrypted_checkpointer
from agentplatform.core.agent.turn import TurnDeps, TurnRunner
from agentplatform.core.db.pool import platform_tx
from agentplatform.core.models.fake import ScriptedGateway, analysis, draft
from tests.integration.test_turn import CFG, NOON, PKG, inbound

pytestmark = pytest.mark.db


async def test_turn_with_encrypted_checkpointer(tenant):
    conv = await inbound(tenant, ["quero saber sobre consórcio de imóvel SEGREDO123"])
    g = ScriptedGateway({"analyze_turn": [analysis(stage="engajado")], "respond": [draft("Oi! Você pensa em imóvel ou veículo?")]})
    async with encrypted_checkpointer() as saver:
        r = TurnRunner(TurnDeps(config=CFG, package=PKG, gateway=g, knowledge=None, now=NOON), checkpointer=saver)
        res = await r.run(tenant, conv["id"])
    assert res.status == "committed"
    async with platform_tx() as conn:
        rows = await (await conn.execute(
            "select checkpoint::text as c from checkpoints where thread_id like %s", (f"{tenant}:%",))).fetchall()
        blobs = await (await conn.execute(
            "select blob from checkpoint_blobs where thread_id like %s and blob is not null", (f"{tenant}:%",))).fetchall()
    assert rows
    raw = b"".join(bytes(b["blob"]) for b in blobs) + "".join(r["c"] for r in rows).encode()
    assert b"SEGREDO123" not in raw
