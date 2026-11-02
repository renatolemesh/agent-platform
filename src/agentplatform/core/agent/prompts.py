"""Versioned prompt registry. Seeds live in prompts/<name>@vN.j2; versions edited in the console (prompt lab) live in
prompt_templates with an immutable label <name>@t-xxxxxxxx. Config points to either kind the same way. Each template
has `{# role: system #}` and `{# role: user #}` sections. No prompt strings in node code (plan 08 §5).

Rendering is synchronous (it runs inside graph nodes), so database versions are preloaded into memory when a tenant's
config is resolved (runtime). Versions are immutable, so the cache never goes stale."""
from __future__ import annotations

import re
import secrets
from functools import lru_cache
from typing import Iterable

from jinja2 import Environment, StrictUndefined, TemplateSyntaxError, meta

from agentplatform.paths import PROMPTS as ROOT

_env = Environment(undefined=StrictUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=True)
_SPLIT = re.compile(r"\{#\s*role:\s*(system|user)\s*#\}")
_DB_BODIES: dict[str, str] = {}
DB_MARK = "@t-"
MAX_BODY_CHARS = 20_000


def is_db_version(name_version: str) -> bool:
    return DB_MARK in name_version


def name_of(name_version: str) -> str:
    return name_version.split("@", 1)[0]


def _parse(raw: str, label: str) -> list[tuple[str, str]]:
    parts = _SPLIT.split(raw)
    out = [(parts[i], parts[i + 1].strip("\n")) for i in range(1, len(parts), 2)]
    if not out:
        raise ValueError(f"prompt {label} has no role sections")
    return out


@lru_cache
def _load_file(name_version: str) -> list[tuple[str, str]]:
    return _parse((ROOT / f"{name_version}.j2").read_text(encoding="utf-8"), name_version)


def _load(name_version: str) -> list[tuple[str, str]]:
    if is_db_version(name_version):
        if name_version not in _DB_BODIES:
            raise KeyError(f"prompt {name_version} not preloaded (runtime.preload_prompts)")
        return _parse(_DB_BODIES[name_version], name_version)
    return _load_file(name_version)


def render(name_version: str, **variables) -> list[dict]:
    return [{"role": role, "content": _env.from_string(body).render(**variables).strip()}
            for role, body in _load(name_version)]


def raw_body(name_version: str) -> str:
    if is_db_version(name_version):
        return _DB_BODIES[name_version]
    return (ROOT / f"{name_version}.j2").read_text(encoding="utf-8")


def seed_versions(name: str) -> list[str]:
    def key(p):
        tail = p.stem.split("@v", 1)[1]
        return int(tail) if tail.isdigit() else 0
    return [p.stem for p in sorted(ROOT.glob(f"{name}@v*.j2"), key=key, reverse=True)]


def variables(name_version_or_body: str, *, is_body: bool = False) -> set[str]:
    body = name_version_or_body if is_body else raw_body(name_version_or_body)
    return meta.find_undeclared_variables(_env.parse(body))


def validate(body: str, base_version: str) -> list[str]:
    """Problems that would break a turn at runtime: missing role sections, Jinja syntax, or variables the node does
    not provide (the base version's variables are exactly what the node passes)."""
    errors = []
    if len(body) > MAX_BODY_CHARS:
        errors.append(f"prompt longo demais (máximo {MAX_BODY_CHARS} caracteres)")
    roles = [m.group(1) for m in _SPLIT.finditer(body)]
    if "system" not in roles or "user" not in roles:
        errors.append("faltam as seções {# role: system #} e {# role: user #}")
    try:
        unknown = variables(body, is_body=True) - variables(base_version)
        if unknown:
            errors.append("variáveis que esta etapa não fornece: " + ", ".join(sorted(unknown)))
    except TemplateSyntaxError as e:
        errors.append(f"erro de sintaxe na linha {e.lineno}: {e.message}")
    return errors


async def preload(conn, versions: Iterable[str]) -> None:
    missing = sorted({v for v in versions if v and is_db_version(v) and v not in _DB_BODIES})
    if not missing:
        return
    rows = await (await conn.execute("select label, body from prompt_templates where label = any(%s)", (missing,))).fetchall()
    for r in rows:
        _DB_BODIES[r["label"]] = r["body"]
    lost = set(missing) - {r["label"] for r in rows}
    if lost:
        raise KeyError(f"prompt versions not found: {sorted(lost)}")


async def save_version(conn, tenant_id, name: str, body: str, base_version: str, note: str | None, created_by) -> str:
    """Immutable new version for this tenant; returns its label (to be referenced by a config version)."""
    row = await (await conn.execute(
        "select coalesce(max(version), 0) + 1 as v from prompt_templates where tenant_id=%s and name=%s",
        (tenant_id, name))).fetchone()
    label = f"{name}{DB_MARK}{secrets.token_hex(4)}"
    await conn.execute(
        """insert into prompt_templates (tenant_id, name, version, body, label, base_version, note, created_by, status)
           values (%s,%s,%s,%s,%s,%s,%s,%s,'draft')""",
        (tenant_id, name, row["v"], body, label, base_version, note, created_by))
    _DB_BODIES[label] = body
    return label


def config_prompt_versions(cfg) -> list[str]:
    m = cfg.models
    nodes = [m.analyze_turn, m.respond, m.output_judge, m.summarize, m.input_classifier, m.comment_intent, m.image_describe]
    return [n.prompt_version for n in nodes if n is not None]
