"""Live smoke test against the configured provider. Spends real budget (fractions of a cent), counted against
the real key cap. Run explicitly: pytest -m live tests/integration/test_live_smoke.py"""
import pytest
from pydantic import BaseModel

from agentplatform.core.config.schema import NodeModel
from agentplatform.core.models.gateway import CallContext, get_gateway

pytestmark = [pytest.mark.live, pytest.mark.db]


@pytest.fixture(autouse=True)
def fresh_gateway():
    """The process-wide gateway's HTTP client is bound to one event loop; each test has its own loop."""
    from agentplatform.core.models import gateway
    gateway._gateway = None
    yield
    gateway._gateway = None


class Pong(BaseModel):
    reply: str


async def test_live_structured_call(db):
    gw = get_gateway()
    spec = NodeModel(primary="openai/gpt-4.1-nano", prompt_version="smoke@v1", max_output_tokens=20, temperature=0)
    r = await gw.chat(spec, [{"role": "system", "content": "Responda em JSON."},
                             {"role": "user", "content": "Diga apenas 'pong'."}],
                      CallContext(None, "smoke", purpose="ci"), Pong)
    assert "pong" in r.parsed.reply.lower()
    assert r.cost_usd < 0.001
    usage = await gw.budget.usage()
    print("cost", r.cost_usd, "key usage", usage.get("key:total"))


async def test_live_transcription(db):
    """Synthetic voice note (Windows TTS, English) through the real speech-to-text model and the budget."""
    from pathlib import Path

    from agentplatform.core.config.schema import ModelRef
    from agentplatform.core.media.processor import BYTES_PER_S_UPPER_BOUND
    audio = (Path(__file__).resolve().parents[1] / "fixtures" / "voice_en.ogg").read_bytes()
    gw = get_gateway()
    text = await gw.transcribe(ModelRef(model="openai/gpt-4o-mini-transcribe"), audio, "voice.ogg",
                               max(1.0, len(audio) / BYTES_PER_S_UPPER_BOUND), CallContext(None, "smoke:stt", purpose="ci"),
                               language="en")
    print("transcript:", text)
    assert "car" in text.lower() and ("eighty" in text.lower() or "80" in text)
