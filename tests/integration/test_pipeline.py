"""Full pipeline with real job bodies: webhook payload -> inbound_events -> ingress -> debounce -> turn -> dispatch."""
import json
import uuid
from datetime import datetime, timezone

import pytest

from agentplatform.core.channels.events import CanonicalEvent
from agentplatform.core.channels.fake.adapter import FakeChannelAdapter
from agentplatform.core.db.pool import platform_tx, tenant_tx
from agentplatform.core.handoff.notifiers import FallbackInbox, LogNotifier
from agentplatform.core.models.fake import ScriptedGateway, analysis, draft
from agentplatform.core.orchestration.ingress import persist_events
from agentplatform.jobs.pump import pump
from agentplatform.runtime import Runtime, set_runtime

pytestmark = pytest.mark.db


def fast_cfg():
    from pathlib import Path

    import yaml

    from agentplatform.core.config.schema import PlatformConfig
    d = yaml.safe_load((Path(__file__).resolve().parents[2] / "config/tenants/dev-consortium.yaml").read_text(encoding="utf-8"))
    d["concurrency"] = {"debounce_window_s": 0.2, "debounce_max_wait_s": 1.0, "echo_grace_s": 0.1}
    return PlatformConfig.model_validate(d)


@pytest.fixture
async def rt_tenant(db):
    tid = uuid.uuid4()
    acc = f"ig-{tid.hex[:10]}"
    async with platform_tx() as conn:
        await conn.execute("insert into tenants (id, slug, display_name, segment_package, segment_package_version) values (%s,'dev-consortium-'||%s,'t','consortium','0.1.0')",
                           (tid, tid.hex[:6]))
        await conn.execute("insert into channel_accounts (tenant_id, channel, external_account_id, external_page_id) values (%s,'instagram',%s,%s)",
                           (tid, acc, acc))
    return tid, acc


def ev(acc, text, igsid, etype="MessageReceived", mid=None):
    return CanonicalEvent(event_type=etype, channel="instagram", account_external_id=acc, contact_external_id=igsid,
                          idempotency_key=f"msg:{mid or uuid.uuid4().hex}", mid=mid or uuid.uuid4().hex,
                          provider_ts=datetime.now(timezone.utc), text=text)


async def test_message_to_reply_end_to_end(rt_tenant, monkeypatch):
    tid, acc = rt_tenant
    adapter = FakeChannelAdapter()
    gw = ScriptedGateway({"analyze_turn": [analysis(intents=["greeting"], stage="engajado", evidence=["oi"])],
                          "respond": [draft("Oi! Sou o assistente virtual do consultor. Você pensa em imóvel ou veículo?")]})
    rt = Runtime(adapters={"instagram": adapter}, gateway=gw, inbox=FallbackInbox([LogNotifier()]))
    # tenant config from the dev YAML (slug prefix match)
    cfg = fast_cfg()
    from agentplatform.core.packages.contract import load_package
    from agentplatform.runtime import TenantRuntime
    rt._tenants[str(tid)] = (1e18, TenantRuntime(tid, cfg, None, load_package("consortium"), None))
    rt.use_checkpointer = True
    set_runtime(rt)
    igsid = f"IG-{uuid.uuid4().hex[:6]}"
    created = await persist_events([ev(acc, "oi", igsid), ev(acc, "tudo bem?", igsid)])
    ran = await pump([("ingress", {"tenant_id": str(t), "event_id": e}, {}) for t, e in created], honor_delays=True)
    assert "run_turn" in ran and "dispatch" in ran
    assert ran.count("run_turn") == 1                       # the burst became ONE turn
    assert adapter.texts() == ["Oi! Sou o assistente virtual do consultor. Você pensa em imóvel ou veículo?"]
    async with tenant_tx(tid) as conn:
        lead = await (await conn.execute("select l.stage from leads l join contact_identities ci on ci.contact_id=l.contact_id where ci.external_user_id=%s", (igsid,))).fetchone()
    assert lead["stage"] == "engajado"


async def test_seller_echo_pauses_bot_end_to_end(rt_tenant):
    tid, acc = rt_tenant
    adapter = FakeChannelAdapter()
    rt = Runtime(adapters={"instagram": adapter}, gateway=ScriptedGateway({}), inbox=FallbackInbox([LogNotifier()]))
    from agentplatform.core.packages.contract import load_package
    from agentplatform.runtime import TenantRuntime
    rt._tenants[str(tid)] = (1e18, TenantRuntime(tid, fast_cfg(), None, load_package("consortium"), None))
    rt.use_checkpointer = True
    set_runtime(rt)
    igsid = f"IG-{uuid.uuid4().hex[:6]}"
    created = await persist_events([ev(acc, "Oi, aqui é o João, vendedor!", igsid, etype="MessageEcho")])
    await pump([("ingress", {"tenant_id": str(t), "event_id": e}, {}) for t, e in created], honor_delays=True)
    async with tenant_tx(tid) as conn:
        c = await (await conn.execute("select c.ownership from conversations c join contact_identities ci on ci.contact_id=c.contact_id where ci.external_user_id=%s", (igsid,))).fetchone()
    assert c["ownership"] == "HUMAN"


async def test_audio_burst_is_transcribed_into_one_turn(rt_tenant, monkeypatch):
    from agentplatform.core.channels.events import Attachment
    from agentplatform.core.media import processor
    tid, acc = rt_tenant
    adapter = FakeChannelAdapter()
    gw = ScriptedGateway({"transcribe": ["quero um carro de oitenta mil"],
                          "analyze_turn": [analysis(intents=["greeting"], stage="engajado", evidence=["carro"])],
                          "respond": [draft("Legal! Para quando você pensa no carro?")]})
    rt = Runtime(adapters={"instagram": adapter}, gateway=gw, inbox=FallbackInbox([LogNotifier()]))
    from agentplatform.core.packages.contract import load_package
    from agentplatform.runtime import TenantRuntime
    rt._tenants[str(tid)] = (1e18, TenantRuntime(tid, fast_cfg(), None, load_package("consortium"), None))
    set_runtime(rt)

    async def fake_download(url, token, graph_version, max_bytes, transport=None):
        return b"o" * 20000
    monkeypatch.setattr(processor, "download", fake_download)
    igsid = f"IG-{uuid.uuid4().hex[:6]}"
    audio = ev(acc, None, igsid)
    audio = audio.model_copy(update={"attachments": [Attachment(type="audio", url="https://cdn/a.mp4")]})
    created = await persist_events([ev(acc, "oi", igsid), audio])
    ran = await pump([("ingress", {"tenant_id": str(t), "event_id": e}, {}) for t, e in created], honor_delays=True)
    # two queued turns are fine since turns have no queueing lock: they run one at a time and the second finds nothing to do
    assert ran.count("run_turn") >= 1 and "transcribe_pending" in ran
    analyze_prompt = json.dumps([m for node, m in gw.calls if node == "analyze_turn"], ensure_ascii=False)
    assert "oitenta mil" in analyze_prompt
    assert len(adapter.texts()) == 1 and adapter.texts()[0].endswith("Legal! Para quando você pensa no carro?")


async def test_refused_image_goes_to_a_human(rt_tenant):
    from agentplatform.core.channels.events import Attachment
    tid, acc = rt_tenant
    adapter = FakeChannelAdapter()
    gw = ScriptedGateway({"analyze_turn": [analysis(intents=["greeting"], stage="novo", evidence=["oi"])],
                          "respond": [draft("Recebi! Vou passar para o consultor dar uma olhada.")]})
    rt = Runtime(adapters={"instagram": adapter}, gateway=gw, inbox=FallbackInbox([LogNotifier()]))
    from agentplatform.core.packages.contract import load_package
    from agentplatform.runtime import TenantRuntime
    cfg = fast_cfg()
    cfg = cfg.model_copy(update={"media": cfg.media.model_copy(update={"images": "refuse_and_handoff"})})
    rt._tenants[str(tid)] = (1e18, TenantRuntime(tid, cfg, None, load_package("consortium"), None))
    set_runtime(rt)
    igsid = f"IG-{uuid.uuid4().hex[:6]}"
    img = ev(acc, "olha isso", igsid).model_copy(update={"attachments": [Attachment(type="image", url="https://cdn/i.jpg")]})
    created = await persist_events([img])
    await pump([("ingress", {"tenant_id": str(t), "event_id": e}, {}) for t, e in created], honor_delays=True)
    async with tenant_tx(tid) as conn:
        h = await (await conn.execute(
            "select h.reason from handoffs h join conversations c on c.id=h.conversation_id join contact_identities ci on ci.contact_id=c.contact_id where ci.external_user_id=%s",
            (igsid,))).fetchone()
    assert h and h["reason"] == "media_review"
    prompt = json.dumps([m for node, m in gw.calls if node == "analyze_turn"], ensure_ascii=False)
    assert "será vista pelo consultor" in prompt


async def test_audio_the_assistant_could_not_hear_goes_to_a_person_and_stays_readable(rt_tenant, monkeypatch):
    from agentplatform.core.channels.events import Attachment
    from agentplatform.core.db.pool import tenant_tx
    from agentplatform.core.media import processor
    tid, acc = rt_tenant
    gw = ScriptedGateway({"analyze_turn": [analysis(intents=["greeting"], stage="novo", evidence=["oi"])], "transcribe": [RuntimeError("provider down")]})
    rt = Runtime(adapters={"instagram": FakeChannelAdapter()}, gateway=gw, inbox=FallbackInbox([LogNotifier()]))
    from agentplatform.core.packages.contract import load_package
    from agentplatform.runtime import TenantRuntime
    rt._tenants[str(tid)] = (1e18, TenantRuntime(tid, fast_cfg(), None, load_package("consortium"), None))
    set_runtime(rt)

    async def fake_download(url, token, graph_version, max_bytes, transport=None):
        return b"o" * 20000
    monkeypatch.setattr(processor, "download", fake_download)
    audio = ev(acc, None, f"IG-{uuid.uuid4().hex[:6]}").model_copy(update={"attachments": [Attachment(type="audio", url="https://cdn/a.mp4")]})
    created = await persist_events([audio])
    await pump([("ingress", {"tenant_id": str(t), "event_id": e}, {}) for t, e in created], honor_delays=True)
    async with tenant_tx(tid) as conn:
        h = await (await conn.execute("select reason from handoffs where tenant_id=%s", (tid,))).fetchone()
    assert h["reason"] == "media_review"


async def test_a_transcript_is_the_message_for_later_turns_and_for_the_seller(rt_tenant, monkeypatch):
    from agentplatform.core.channels.events import Attachment
    from agentplatform.core.db.pool import tenant_tx
    from agentplatform.core.media import processor
    tid, acc = rt_tenant
    gw = ScriptedGateway({"transcribe": ["quero um carro de oitenta mil"],
                          "analyze_turn": [analysis(intents=["greeting"], stage="engajado", evidence=["carro"])],
                          "respond": [draft("Legal!")]})
    rt = Runtime(adapters={"instagram": FakeChannelAdapter()}, gateway=gw, inbox=FallbackInbox([LogNotifier()]))
    from agentplatform.core.packages.contract import load_package
    from agentplatform.runtime import TenantRuntime
    rt._tenants[str(tid)] = (1e18, TenantRuntime(tid, fast_cfg(), None, load_package("consortium"), None))
    set_runtime(rt)

    async def fake_download(url, token, graph_version, max_bytes, transport=None):
        return b"o" * 20000
    monkeypatch.setattr(processor, "download", fake_download)
    audio = ev(acc, None, f"IG-{uuid.uuid4().hex[:6]}").model_copy(update={"attachments": [Attachment(type="audio", url="https://cdn/a.mp4")]})
    created = await persist_events([audio])
    await pump([("ingress", {"tenant_id": str(t), "event_id": e}, {}) for t, e in created], honor_delays=True)
    async with tenant_tx(tid) as conn:
        m = await (await conn.execute("select text_masked from messages where tenant_id=%s and direction='in'", (tid,))).fetchone()
    assert m["text_masked"] == "quero um carro de oitenta mil"
    prompt = json.dumps([x for node, x in gw.calls if node == "analyze_turn"], ensure_ascii=False)
    assert prompt.count("oitenta mil") == 1          # not said twice to the model


async def test_a_video_gets_a_note_instead_of_an_empty_turn(rt_tenant):
    from agentplatform.core.channels.events import Attachment
    tid, acc = rt_tenant
    gw = ScriptedGateway({"analyze_turn": [analysis(intents=["other"], stage="novo", evidence=[])], "respond": [draft("Não consigo ver vídeos por aqui, me conta por escrito?")]})
    rt = Runtime(adapters={"instagram": FakeChannelAdapter()}, gateway=gw, inbox=FallbackInbox([LogNotifier()]))
    from agentplatform.core.packages.contract import load_package
    from agentplatform.runtime import TenantRuntime
    rt._tenants[str(tid)] = (1e18, TenantRuntime(tid, fast_cfg(), None, load_package("consortium"), None))
    set_runtime(rt)
    video = ev(acc, None, f"IG-{uuid.uuid4().hex[:6]}").model_copy(update={"attachments": [Attachment(type="video", url="https://cdn/v.mp4")]})
    created = await persist_events([video])
    await pump([("ingress", {"tenant_id": str(t), "event_id": e}, {}) for t, e in created], honor_delays=True)
    assert "vídeo recebido" in json.dumps([x for node, x in gw.calls if node == "analyze_turn"], ensure_ascii=False)
