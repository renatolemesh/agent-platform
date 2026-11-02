"""An option that nothing reads misleads whoever changes it: a tone that changes nothing, a limit that limits nothing. Every config field must be read
somewhere outside the schema. A field with one possible value is a declared invariant, not an option."""
from __future__ import annotations

import inspect
import re
import typing
from pathlib import Path

from pydantic import BaseModel

from agentplatform.core.config import schema as S

ROOT = Path(__file__).resolve().parents[2]
# Read as a whole, or kept as a record: not options anyone could expect to change behaviour on their own.
RECORD_ONLY = {
    "ProviderPolicy.require_parameters", "ProviderPolicy.allow_fallbacks", "ProviderPolicy.only", "ProviderPolicy.order",   # sent whole to the provider
    "Subprocessor.purpose", "Subprocessor.data_categories", "Subprocessor.country", "Subprocessor.zdr",     # the LGPD inventory, read by the jurídico
    "BudgetConfig.openrouter_key_limit_usd",       # what the operator set at the provider: only checked against the daily cap
}


def _sources() -> str:
    parts = []
    for root, globs in (("src", ("*.py", "*.html")), ("packages", ("*.py",)), ("prompts", ("*.j2",))):
        for g in globs:
            for p in (ROOT / root).rglob(g):
                if p.as_posix().endswith("core/config/schema.py"):
                    continue
                parts.append(p.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(parts)


def _single_value(annotation) -> bool:
    return typing.get_origin(annotation) is typing.Literal and len(typing.get_args(annotation)) == 1


def test_every_option_does_something():
    blob = _sources()
    unread = []
    for name, cls in inspect.getmembers(S, inspect.isclass):
        if not issubclass(cls, BaseModel) or cls.__module__ != S.__name__ or name == "Strict":
            continue
        for field, info in cls.model_fields.items():
            if f"{name}.{field}" in RECORD_ONLY or _single_value(info.annotation):
                continue
            used = re.search(rf"\.{re.escape(field)}\b|[\"']{re.escape(field)}[\"']|\.get\([\"']{re.escape(field)}", blob)
            validated = re.search(rf"self\.{re.escape(field)}\b", inspect.getsource(S))
            if not used and not validated:
                unread.append(f"{name}.{field}")
    assert not unread, "config options nothing reads (implement them, or remove them through `_removed`):\n" + "\n".join(sorted(unread))


def test_removed_options_still_load():
    """Versions stored before an option was removed must keep working, or a rollback would break."""
    from agentplatform.core.config.registry import load_yaml
    cfg = load_yaml(ROOT / "config" / "tenants" / "dev-consortium.yaml")
    import json
    doc = json.loads(cfg.model_dump_json())
    doc["meta"] = {"graph_api_version": "v26.0", "login_kind": "facebook_login"}
    doc["models"]["rerank"] = None
    doc["rag"] |= {"pre_rerank_k": 20, "rerank": False}
    doc["agent"]["analyze_mode"] = "merged"
    doc["handoff"] |= {"triggers_enabled": ["explicit_request"], "notifier": "email", "notifier_fallback": "none", "return_to_bot_after_inactivity_min": None}
    doc["sending"] |= {"holidays_local_calendar": "x", "channel_rate_safety_margin": 0.2}
    doc["comments"] |= {"hide_spam": True, "rules": []}
    doc["operations"] |= {"trace_sampling": 1.0, "canary_percent": 0}
    assert S.PlatformConfig.model_validate(doc).rag.top_k == cfg.rag.top_k
