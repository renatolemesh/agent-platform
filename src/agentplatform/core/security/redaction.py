"""Logging filter that removes secrets and PII from every log record (no raw PII in logs/traces)."""
from __future__ import annotations

import logging
import re

from agentplatform.core.security.pii import mask

_SECRET_PATTERNS = [
    re.compile(r"sk-(?:proj-|or-v1-|or-)?[A-Za-z0-9_\-]{16,}"),   # OpenAI / OpenRouter keys
    re.compile(r"EAA[A-Za-z0-9]{20,}"),                           # Meta access tokens
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s\"']+"),
    # secrets in URLs (uvicorn access log, httpx request log): webhook verify token, OAuth code/state, Graph tokens
    re.compile(r"(?i)((?:hub[._]verify_token|verify_token|access_token|input_token|fb_exchange_token|client_secret"
               r"|appsecret_proof|code|state)=)[^&\s\"']+"),
]


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return mask(text).text


def _redact_arg(v):
    if isinstance(v, str):
        return redact(v)
    if v is None or isinstance(v, (int, float)):
        return v
    s = str(v)              # objects rendered with %s, e.g. the httpx.URL of httpx's request log
    r = redact(s)
    return v if r == s else r


def scrub(record: logging.LogRecord) -> None:
    """Redacts the message, each argument and the traceback in place. The argument structure is kept: some formatters
    (uvicorn's access log) unpack record.args, so collapsing it into the message would break them."""
    try:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact_arg(v) for k, v in record.args.items()}
        if record.exc_info and not record.exc_text:     # e.g. httpx errors quote the request URL
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
    except Exception:  # pragma: no cover - never lose a log line because of redaction
        pass


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        scrub(record)
        return True


def install() -> None:
    # every record is redacted when created, whatever logger or handler emits it: handlers added later (the worker CLI
    # configures logging after importing the app) and child loggers (filters on root don't see them) are covered
    base = logging.getLogRecordFactory()
    if not getattr(base, "redacting", False):
        def factory(*args, **kwargs) -> logging.LogRecord:
            record = base(*args, **kwargs)
            scrub(record)
            return record
        factory.redacting = True  # type: ignore[attr-defined]
        logging.setLogRecordFactory(factory)
    # uvicorn's loggers have their own handlers and do not propagate to root
    for logger in (logging.getLogger(), logging.getLogger("uvicorn.access"), logging.getLogger("uvicorn.error")):
        if not any(isinstance(f, RedactingFilter) for f in logger.filters):
            logger.addFilter(RedactingFilter())
        for h in logger.handlers:
            if not any(isinstance(f, RedactingFilter) for f in h.filters):
                h.addFilter(RedactingFilter())
