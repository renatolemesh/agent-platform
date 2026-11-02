"""Reading a document (plan 06 §4, ADR-015, ADR-021): extraction, OCR of pages without text, PII scan and structure-aware chunking.
Nothing here calls a provider: the text waits without vectors until a person approves it (core/knowledge/admin.py)."""
from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass

import pdfplumber

from agentplatform.core.config.schema import KnowledgeConfig
from agentplatform.core.media.processor import ocr_image
from agentplatform.core.security.pii import mask

log = logging.getLogger(__name__)

_HEADING = re.compile(r"^(\d+(\.\d+)*[.)]?\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ].{2,80}|[A-ZÁÉÍÓÚÂÊÔÃÕÇ0-9 ,/–\-]{6,80})$")


class UnreadableDocument(Exception):
    pass


class TooManyPages(Exception):
    pass


@dataclass
class DocMeta:
    file: str
    title: str
    status: str
    visibility: str
    audience: str
    segment: str | None = None
    version_label: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    exclude_pages: tuple[int, ...] = ()


@dataclass
class Chunk:
    ordinal: int
    text: str
    heading_path: list[str]
    page: int
    tokens: int


def extract_pages(data: bytes, max_pages: int) -> list[str]:
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) > max_pages:
                raise TooManyPages(str(len(pdf.pages)))
            return [(p.extract_text() or "").replace("\x00", "") for p in pdf.pages]
    except TooManyPages:
        raise
    except Exception as e:      # the parsers raise many types on a broken or locked file
        raise UnreadableDocument(type(e).__name__) from e


def render_page(data: bytes, page_no: int, dpi: int) -> bytes:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        image = pdf.pages[page_no - 1].to_image(resolution=dpi).original
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


async def read_document(data: bytes, cfg: KnowledgeConfig) -> tuple[list[str], dict]:
    """Pages of text, and what happened on the pages that had none. One page at a time: this runs on a small machine (ADR-021)."""
    pages = await asyncio.to_thread(extract_pages, data, cfg.max_pages)
    empty = [i + 1 for i, t in enumerate(pages) if len(t.strip()) < cfg.page_min_chars]
    info: dict = {"empty_pages": empty, "ocr_pages": [], "skipped_pages": empty[cfg.max_ocr_pages:]}
    for page_no in empty[:cfg.max_ocr_pages]:
        text = await ocr_image(await asyncio.to_thread(render_page, data, page_no, cfg.ocr_dpi))
        if text is None:              # tesseract not installed
            info["ocr_unavailable"] = True
            break
        if text.strip():
            pages[page_no - 1] = text.replace("\x00", "")
            info["ocr_pages"].append(page_no)
    return pages, info


def scan_pii(pages: list[str]) -> dict:
    """Documents are blocked on hard identifiers (valid CPF/CNPJ, cards, e-mails, phones). Health vocabulary in
    policy documents is generic, so it is only counted, not blocking. Only counts are kept, never the values."""
    found: dict[str, int] = {}
    for t in pages:
        for k, n in mask(t).found.items():
            found[k] = found.get(k, 0) + n
    blocking = {k: v for k, v in found.items() if k in ("cpf", "cnpj", "card", "email", "phone")}
    return {"found": found, "blocking": blocking}


def chunk_pages(pages: list[str], target_chars: int = 1400, overlap_chars: int = 200,
                exclude: tuple[int, ...] = ()) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading: list[str] = []
    buf, buf_page = "", 1
    for pno, text in enumerate(pages, start=1):
        if pno in exclude:
            continue
        for line in (ln.strip() for ln in text.splitlines()):
            if not line:
                continue
            if _HEADING.match(line) and len(buf) > target_chars * 0.4:
                chunks.append(Chunk(len(chunks), buf.strip(), list(heading), buf_page, len(buf) // 4))
                buf, buf_page = "", pno
            if _HEADING.match(line):
                heading = [line[:80]]
            if not buf:
                buf_page = pno
            buf += (" " if buf else "") + line
            if len(buf) >= target_chars:
                chunks.append(Chunk(len(chunks), buf.strip(), list(heading), buf_page, len(buf) // 4))
                buf = buf[-overlap_chars:]
                buf_page = pno
    if buf.strip() and (not chunks or buf.strip() != chunks[-1].text[-len(buf.strip()):]):
        chunks.append(Chunk(len(chunks), buf.strip(), list(heading), buf_page, len(buf) // 4))
    return [c for c in chunks if len(c.text) > 80]
