"""Inbound media (plan 04 §4, F2-MEDIA-01; config MediaConfig). Media is downloaded right away (CDN URLs expire),
turned into text and masked; raw bytes are never stored. Audio goes through the ModelGateway (budget); images use local
OCR (tesseract, when installed); PDFs use local text extraction. Nothing here decides what the agent says."""
from __future__ import annotations

import asyncio
import io
import logging
import shutil
from dataclasses import dataclass

import httpx

from agentplatform.core.config.schema import PlatformConfig
from agentplatform.core.security.pii import mask

log = logging.getLogger(__name__)

WHATSAPP_MEDIA = "whatsapp-media://"      # Attachment.url for WhatsApp: the media id (download needs the token)
# Duration estimates from size (no decoding): voice notes are 16–64 kbit/s. The budget reservation uses the
# low-bitrate bound (upper bound on minutes); the length rule uses a typical bitrate.
BYTES_PER_S_UPPER_BOUND = 2000
BYTES_PER_S_TYPICAL = 4000


@dataclass
class MediaResult:
    transcript: str | None = None        # masked text merged into the turn input
    note: str | None = None              # machine note: audio_too_long, ocr_unavailable, download_failed, ...


async def download(url: str, token: str | None, graph_version: str, max_bytes: int,
                   transport: httpx.AsyncBaseTransport | None = None) -> bytes:
    async with httpx.AsyncClient(timeout=20, transport=transport, follow_redirects=True) as c:
        if url.startswith(WHATSAPP_MEDIA):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            meta = await c.get(f"https://graph.facebook.com/{graph_version}/{url[len(WHATSAPP_MEDIA):]}", headers=headers)
            meta.raise_for_status()
            r = await c.get(meta.json()["url"], headers=headers)
        else:
            r = await c.get(url)
        r.raise_for_status()
        if len(r.content) > max_bytes:
            raise ValueError("media too large")
        return r.content


async def ocr_image(data: bytes) -> str | None:
    """Local OCR with the tesseract CLI (Portuguese). Returns None when tesseract is not installed."""
    exe = shutil.which("tesseract")
    if not exe:
        return None
    proc = await asyncio.create_subprocess_exec(exe, "stdin", "stdout", "-l", "por", "--psm", "3",
                                                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(data), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    return out.decode("utf-8", "ignore") if proc.returncode == 0 else None


def extract_pdf_text(data: bytes) -> str | None:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:5])
    except Exception as e:
        log.warning("pdf extraction failed: %s", type(e).__name__)
        return None


def _clean(text: str | None, limit: int) -> str | None:
    if not text or not text.strip():
        return None
    return mask(" ".join(text.split())[:limit]).text


async def process_attachment(att: dict, cfg: PlatformConfig, *, fetch, transcribe) -> MediaResult:
    """`fetch(url) -> bytes`; `transcribe(audio, seconds_upper_bound) -> str` (goes through the gateway)."""
    kind, url = att.get("type"), att.get("url")
    m = cfg.media
    if kind == "audio" and not m.transcribe_audio:
        return MediaResult(note="audio_disabled")
    if kind == "image" and m.images == "refuse_and_handoff":
        return MediaResult(note="image_refused")
    if kind == "file" and m.documents == "refuse_and_handoff":
        return MediaResult(note="document_refused")
    if kind not in ("audio", "image", "file") or not url:
        return MediaResult(note="unsupported")
    try:
        data = await fetch(url)
    except Exception as e:
        log.warning("media download failed: %s", type(e).__name__)
        return MediaResult(note="download_failed")
    if kind == "audio":
        if len(data) / BYTES_PER_S_TYPICAL > m.max_audio_seconds:
            return MediaResult(note="audio_too_long")
        try:
            text = await transcribe(data, max(1.0, len(data) / BYTES_PER_S_UPPER_BOUND))
            return MediaResult(transcript=_clean(text, m.max_extracted_chars))
        except Exception as e:                   # budget denied, provider error: never block the batch
            log.warning("transcription failed: %s", type(e).__name__)
            return MediaResult(note="transcription_failed")
    if kind == "image":
        if m.images == "llm_describe":
            log.warning("media.images=llm_describe is not implemented; using local OCR")
        text = await ocr_image(data)
        return MediaResult(transcript=_clean(text, m.max_extracted_chars),
                           note=None if text is not None else "ocr_unavailable")
    if not data.startswith(b"%PDF"):
        return MediaResult(note="unsupported_document")
    return MediaResult(transcript=_clean(extract_pdf_text(data), m.max_extracted_chars))


def render_for_turn(kind: str, media: dict) -> str:
    """How a media item appears in the (already masked) turn input."""
    text, note = media.get("transcript"), media.get("note")
    if kind == "audio" or note in ("audio_too_long", "transcription_failed", "audio_disabled"):
        if text:
            return text
        return "[áudio longo não transcrito]" if note == "audio_too_long" else "[áudio não transcrito]"
    if kind == "video":
        return "[vídeo recebido; o assistente não consegue assistir]"
    label, received = {"image": ("imagem", "recebida"), "file": ("documento", "recebido")}.get(kind, ("anexo", "recebido"))
    if note in ("image_refused", "document_refused"):
        return f"[{label} {received}; será vista pelo consultor]"
    return f"[{label}: {text}]" if text else f"[{label} {received}, sem texto legível]"
