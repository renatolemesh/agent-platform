"""Inbound media -> masked text (F2-MEDIA-01): audio limits, PDF extraction, OCR availability, WhatsApp download."""
import io

import httpx
import pytest

from agentplatform.core.config.schema import PlatformConfig
from agentplatform.core.media import processor
from agentplatform.core.media.processor import download, process_attachment, render_for_turn
from tests.integration.test_pipeline import fast_cfg


def cfg(**media) -> PlatformConfig:
    c = fast_cfg()
    return c.model_copy(update={"media": c.media.model_copy(update=media)})


def _pdf(text: str) -> bytes:
    # minimal one-page PDF with a text object (pypdf can extract it)
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>stream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = io.BytesIO(b"%PDF-1.4\n"), []
    out.seek(0, 2)
    for i, o in enumerate(objs, 1):
        offs.append(out.tell())
        out.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % x for x in offs))
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref))
    return out.getvalue()


async def _fetch_const(data):
    async def f(url):
        return data
    return f


async def test_audio_transcribed_masked_and_bounded():
    seen = {}

    async def transcribe(audio, seconds):
        seen["seconds"] = seconds
        return "meu email é ana@ex.com quero um carro"
    res = await process_attachment({"type": "audio", "url": "https://cdn/a"}, cfg(), fetch=await _fetch_const(b"x" * 40000),
                                   transcribe=transcribe)
    assert "ana@ex.com" not in res.transcript and "carro" in res.transcript
    assert seen["seconds"] == 20.0                         # 40 kB at the low-bitrate bound: reservation is an upper bound


async def test_audio_too_long_is_not_sent_to_the_model():
    async def transcribe(audio, seconds):
        raise AssertionError("must not be called")
    res = await process_attachment({"type": "audio", "url": "u"}, cfg(max_audio_seconds=10), fetch=await _fetch_const(b"x" * 80000),
                                   transcribe=transcribe)
    assert res.note == "audio_too_long" and render_for_turn("audio", {"note": res.note}) == "[áudio longo não transcrito]"


async def test_pdf_text_extracted_and_image_without_tesseract(monkeypatch):
    res = await process_attachment({"type": "file", "url": "u"}, cfg(), fetch=await _fetch_const(_pdf("Contrato do carro 80 mil")),
                                   transcribe=None)
    assert "Contrato do carro" in res.transcript
    monkeypatch.setattr(processor.shutil, "which", lambda _: None)
    img = await process_attachment({"type": "image", "url": "u"}, cfg(), fetch=await _fetch_const(b"\x89PNG"), transcribe=None)
    assert img.note == "ocr_unavailable" and render_for_turn("image", {}) == "[imagem recebida, sem texto legível]"
    refused = await process_attachment({"type": "image", "url": "u"}, cfg(images="refuse_and_handoff"),
                                       fetch=await _fetch_const(b""), transcribe=None)
    assert refused.note == "image_refused"


async def test_whatsapp_media_download_uses_token_and_size_limit():
    def handler(req: httpx.Request):
        assert req.headers["authorization"] == "Bearer tok"
        if req.url.host == "graph.facebook.com":
            return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/whatsapp/x"})
        return httpx.Response(200, content=b"audio-bytes")
    t = httpx.MockTransport(handler)
    assert await download("whatsapp-media://123", "tok", "v26.0", 1024, transport=t) == b"audio-bytes"
    with pytest.raises(ValueError):
        await download("whatsapp-media://123", "tok", "v26.0", 4, transport=t)
