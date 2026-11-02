"""Structured domain datasets (plan 06 §2, F3-DATA-01): validated import -> draft version with diff/preview ->
approval/activation -> rollback. Rows are validated by the package's schema; the core stays segment-agnostic.

The tenant maintains them in the console (register, edit, import a spreadsheet): columns, labels and choices come from the
package's row model (contract.DatasetSpec), numbers are accepted in Brazilian format ("R$ 1.234,56", "24%")."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import types
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Union, get_args, get_origin
from uuid import UUID

from psycopg import AsyncConnection
from pydantic import BaseModel, ValidationError

# The simulator tests a table before it goes live: inside its scope a draft newer than the active version is used.
PREFER_DRAFTS: ContextVar[bool] = ContextVar("datasets_prefer_drafts", default=False)
MAX_ERRORS = 50


@dataclass
class ImportPreview:
    dataset_id: UUID | None
    version: int
    valid: int
    errors: list[str]
    added: int
    removed: int
    checksum: str
    ignored_columns: list[str] = field(default_factory=list)


@dataclass
class Column:
    name: str
    label: str
    help: str
    required: bool
    kind: Literal["number", "integer", "text", "choice"]
    choices: dict[str, str]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", s)


def _inner(ann: Any) -> Any:
    if get_origin(ann) in (Union, types.UnionType):
        args = [a for a in get_args(ann) if a is not type(None)]
        return args[0] if len(args) == 1 else str
    return ann


def columns(model: type[BaseModel]) -> list[Column]:
    out = []
    for name, f in model.model_fields.items():
        extra = f.json_schema_extra if isinstance(f.json_schema_extra, dict) else {}
        ann = _inner(f.annotation)
        choices = dict(extra.get("choices") or {})
        if get_origin(ann) is Literal:
            kind = "choice"
            choices = {str(v): choices.get(str(v), str(v)) for v in get_args(ann)}
        elif ann in (Decimal, float):
            kind = "number"
        elif ann is int:
            kind = "integer"
        else:
            kind = "choice" if choices else "text"
        out.append(Column(name, f.title or name, f.description or "", f.is_required(), kind, choices))
    return out


def parse_number(v: Any) -> str | None:
    """'R$ 1.234,56' -> '1234.56'; '24%' -> '24'; '0,05' -> '0.05'; '1.500' -> '1500' (Brazilian thousands)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float, Decimal)):
        return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)
    s = re.sub(r"[\sR$% ]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if s.count(",") == 1 else s.replace(",", "")
    elif s.count(".") > 1 or re.fullmatch(r"-?[1-9]\d{0,2}(\.\d{3})+", s):
        s = s.replace(".", "")
    return s


def normalize_row(raw: dict, cols: list[Column]) -> tuple[dict, set[str]]:
    """Map headers by name or label (accent/case-insensitive), convert numbers and choice labels. Empty cells are dropped
    so the model's defaults apply. Returns (row, unknown headers)."""
    by = {}
    for c in cols:
        by[_norm(c.name)] = c
        by[_norm(c.label)] = c
    out, unknown = {}, set()
    for k, v in raw.items():
        if k is None:
            continue
        c = by.get(_norm(k))
        if c is None:
            if str(k).strip():
                unknown.add(str(k).strip())
            continue
        if v is None or str(v).strip() == "":
            continue
        if c.kind in ("number", "integer"):
            v = parse_number(v)
            if c.kind == "integer" and v is not None:
                try:
                    d = Decimal(v)
                    v = str(int(d)) if d == d.to_integral_value() else v
                except InvalidOperation:
                    pass
        elif c.kind == "choice":
            sv = str(v).strip()
            v = next((val for val, lab in c.choices.items() if _norm(sv) in (_norm(val), _norm(lab))), sv)
        else:
            v = str(int(v)) if isinstance(v, float) and v.is_integer() else str(v).strip()
        out[c.name] = v
    return out, unknown


def _error_text(err: dict, col: Column | None) -> str:
    t = err.get("type", "")
    if t == "missing":
        return "obrigatório"
    if t.endswith("_parsing") or t in ("decimal_type", "int_from_float", "int_type", "float_type"):
        return f"número inválido ({err.get('input')!r})"
    ctx = err.get("ctx") or {}
    limits = {"greater_than": "deve ser maior que {gt}", "greater_than_equal": "deve ser no mínimo {ge}",
              "less_than": "deve ser menor que {lt}", "less_than_equal": "deve ser no máximo {le}",
              "string_too_long": "texto longo demais (máximo {max_length} caracteres)",
              "string_too_short": "texto curto demais"}
    if t in limits:
        try:
            return limits[t].format(**{k: str(v).replace(".", ",") for k, v in ctx.items()})
        except (KeyError, IndexError):
            return limits[t].split(" {")[0]
    if t == "value_error":
        return str(err.get("msg", "inválido")).removeprefix("Value error, ")
    if t == "literal_error" and col is not None:
        return "use uma das opções: " + ", ".join(col.choices.values())
    return str(err.get("msg", "inválido"))


def validate_rows(rows: list[dict], schema: type[BaseModel], *, key: str | None = None,
                  first_row: int = 2) -> tuple[list[dict], list[str], list[str]]:
    """-> (valid rows as JSON-ready dicts, errors in Portuguese with column labels, ignored columns)."""
    cols = columns(schema)
    by_name = {c.name: c for c in cols}
    valid, errors, ignored, seen = [], [], set(), {}
    for i, raw in enumerate(rows, start=first_row):
        row, unknown = normalize_row(raw, cols)
        ignored |= unknown
        if not row:
            continue                                     # blank line
        try:
            data = json.loads(schema.model_validate(row).model_dump_json())
        except ValidationError as e:
            fields = {}
            for err in e.errors():
                name = str(err["loc"][0]) if err.get("loc") else ""
                fields.setdefault(name, _error_text(err, by_name.get(name)))
            errors.append(f"linha {i}: " + "; ".join(
                f"{by_name[n].label}: {m}" if n in by_name else m for n, m in fields.items()))
            continue
        if key:
            k = str(data.get(key))
            if k in seen:
                errors.append(f"linha {i}: {by_name[key].label} \"{k}\" repetido (já usado na linha {seen[k]})")
                continue
            seen[k] = i
        valid.append(data)
    return valid, errors[:MAX_ERRORS], sorted(ignored)


def parse_table(content: bytes, filename: str) -> list[dict]:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        header = [str(h).strip() if h is not None else "" for h in rows[0]]
        return [{header[i]: (v if v is not None else "") for i, v in enumerate(r) if i < len(header) and header[i]}
                for r in rows[1:] if any(v not in (None, "") for v in r)]
    text = content.decode("utf-8-sig")
    if not text.strip():
        return []
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
    return [dict(r) for r in csv.DictReader(io.StringIO(text), dialect=dialect)]


def to_csv(rows: list[dict], schema: type[BaseModel]) -> bytes:
    """Spreadsheet-friendly export (pt-BR Excel): ';' separator, column labels as headers, decimal comma."""
    cols = columns(schema)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([c.label for c in cols])
    for r in rows:
        line = []
        for c in cols:
            v = r.get(c.name)
            if v is None:
                line.append("")
            elif c.kind == "number":
                line.append(str(v).replace(".", ","))
            elif c.kind == "choice":
                line.append(c.choices.get(str(v), str(v)))
            else:
                line.append(str(v))
        w.writerow(line)
    return ("﻿" + buf.getvalue()).encode("utf-8")


async def import_dataset(conn: AsyncConnection, *, tenant_id: UUID | None, package: str, dataset: str,
                         rows: list[dict], schema: type[BaseModel], source_document_id: UUID | None = None,
                         dry_run: bool = False, key: str | None = None, first_row: int = 2,
                         created_by: UUID | None = None, note: str | None = None) -> ImportPreview:
    valid, errors, ignored = validate_rows(rows, schema, key=key, first_row=first_row)
    checksum = hashlib.sha256(json.dumps(valid, sort_keys=True).encode()).hexdigest()
    current = await (await conn.execute(
        """select d.id from domain_datasets d where d.package=%s and d.dataset=%s and d.status='active'
           and d.tenant_id is not distinct from %s""", (package, dataset, tenant_id))).fetchone()
    old = set()
    if current:
        old = {json.dumps(r["data"], sort_keys=True) for r in await (await conn.execute(
            "select data from domain_rows where dataset_id=%s", (current["id"],))).fetchall()}
    new = {json.dumps(r, sort_keys=True) for r in valid}
    ver = (await (await conn.execute(
        "select coalesce(max(version),0)+1 as v from domain_datasets where package=%s and dataset=%s and tenant_id is not distinct from %s",
        (package, dataset, tenant_id))).fetchone())["v"]
    preview = ImportPreview(None, ver, len(valid), errors, len(new - old), len(old - new), checksum, ignored)
    if dry_run or errors:
        return preview
    ds = await (await conn.execute(
        """insert into domain_datasets (tenant_id, package, dataset, version, status, source_document_id, row_count, checksum,
                                        created_by, note)
           values (%s,%s,%s,%s,'draft',%s,%s,%s,%s,%s) returning id""",
        (tenant_id, package, dataset, ver, source_document_id, len(valid), checksum, created_by, note))).fetchone()
    for i, r in enumerate(valid):
        await conn.execute("insert into domain_rows (tenant_id, dataset_id, row_no, data) values (%s,%s,%s,%s)",
                           (tenant_id, ds["id"], i, json.dumps(r)))
    preview.dataset_id = ds["id"]
    return preview


async def activate(conn: AsyncConnection, dataset_id: UUID, approved_by: UUID | None) -> None:
    d = await (await conn.execute("select package, dataset, tenant_id from domain_datasets where id=%s", (dataset_id,))).fetchone()
    await conn.execute(
        "update domain_datasets set status='retired' where package=%s and dataset=%s and tenant_id is not distinct from %s and status='active'",
        (d["package"], d["dataset"], d["tenant_id"]))
    await conn.execute("update domain_datasets set status='active', approved_by=%s, activated_at=now() where id=%s",
                       (approved_by, dataset_id))


async def rollback(conn: AsyncConnection, package: str, dataset: str, tenant_id: UUID | None) -> UUID:
    rows = await (await conn.execute(
        """select id, status from domain_datasets where package=%s and dataset=%s and tenant_id is not distinct from %s
           and status in ('active','retired') order by version desc limit 2""", (package, dataset, tenant_id))).fetchall()
    if len(rows) < 2 or rows[0]["status"] != "active":
        raise ValueError("nothing to roll back to")
    await conn.execute("update domain_datasets set status='retired' where id=%s", (rows[0]["id"],))
    await conn.execute("update domain_datasets set status='active', activated_at=now() where id=%s", (rows[1]["id"],))
    return rows[1]["id"]


async def in_use(conn: AsyncConnection, package: str, dataset: str, *, prefer_drafts: bool | None = None) -> dict | None:
    """Version the agent reads: the tenant's own before the platform's (RLS scopes the tenant); inside the simulator a
    draft newer than the active version wins."""
    drafts = PREFER_DRAFTS.get() if prefer_drafts is None else prefer_drafts
    return await (await conn.execute(
        """select id, version, status, tenant_id, row_count, created_at from domain_datasets
           where package=%s and dataset=%s and status = any(%s)
           order by (tenant_id is null), version desc limit 1""",
        (package, dataset, ["active", "draft"] if drafts else ["active"]))).fetchone()


async def rows_of(conn: AsyncConnection, dataset_id: UUID) -> list[dict]:
    rows = await (await conn.execute("select data from domain_rows where dataset_id=%s order by row_no", (dataset_id,))).fetchall()
    return [r["data"] for r in rows]


async def versions(conn: AsyncConnection, tenant_id: UUID, package: str, dataset: str, limit: int = 30) -> list[dict]:
    return await (await conn.execute(
        """select d.id, d.version, d.status, d.row_count, d.created_at, d.created_by, d.note, d.activated_at,
                  d.approved_by, a.display_name as approved_by_name, c.display_name as created_by_name
           from domain_datasets d left join tenant_users a on a.id = d.approved_by
           left join tenant_users c on c.id = d.created_by
           where d.tenant_id=%s and d.package=%s and d.dataset=%s order by d.version desc limit %s""",
        (tenant_id, package, dataset, limit))).fetchall()


async def discard_drafts(conn: AsyncConnection, tenant_id: UUID, package: str, dataset: str) -> int:
    ids = [r["id"] for r in await (await conn.execute(
        "select id from domain_datasets where tenant_id=%s and package=%s and dataset=%s and status='draft'",
        (tenant_id, package, dataset))).fetchall()]
    if ids:
        await conn.execute("delete from domain_rows where dataset_id = any(%s)", (ids,))
        await conn.execute("delete from domain_datasets where id = any(%s)", (ids,))
    return len(ids)
