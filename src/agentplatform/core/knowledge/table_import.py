"""A file the tenant already has (a PDF, a spreadsheet in its own layout) -> rows for the table editor, read by the model.

Nothing is saved here: the person checks the rows in the editor, saves a draft and activates it, like any other change.
The file is data, never instructions, and what the model returns is only a proposal: a number that is not literally in
the file is dropped before anyone sees it, and the person is told which cell it was."""
from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, create_model

from agentplatform.core.agent.prompts import render
from agentplatform.core.knowledge import datasets as D
from agentplatform.core.knowledge.ingest import TooManyPages, UnreadableDocument, read_document
from agentplatform.core.security.pii import mask

_PLAIN = re.compile(r"\d[\d.,]*\d|\d")
_SPACED = re.compile(r"\d{1,3}(?:[  ]\d{3})+(?:,\d+)?")        # "300 000,00", common in scanned tables


class FileRefused(Exception):
    """`reason` is a key of ui.TABLE_FILE_ERROR."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Dropped:
    row: int
    label: str
    value: str


@dataclass
class Proposal:
    rows: list[dict[str, str]]
    dropped: list[Dropped] = field(default_factory=list)
    doubtful: list[Dropped] = field(default_factory=list)       # in the file, but not on the line of this item: left blank
    text_cut: bool = False
    rows_cut: bool = False


def _sheet_text(data: bytes, limit: int) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines: list[str] = []
    size = 0
    for ws in wb.worksheets:
        lines.append(f"# {ws.title}")
        for row in ws.iter_rows(values_only=True):
            if any(v not in (None, "") for v in row):
                lines.append("\t".join("" if v is None else str(v) for v in row))
                size += len(lines[-1])
            if size > limit:
                return "\n".join(lines)
    return "\n".join(lines)


async def source_text(data: bytes, filename: str, knowledge_cfg, max_chars: int) -> str:
    """The file as masked text: identifiers never reach the model, and the file is not stored."""
    name = filename.lower()
    try:
        if data.startswith(b"%PDF-"):
            pages, _ = await read_document(data, knowledge_cfg)
            text = "\n".join(pages)
        elif name.endswith((".xlsx", ".xlsm")):
            text = await asyncio.to_thread(_sheet_text, data, max_chars)
        elif name.endswith((".csv", ".txt")):
            text = data.decode("utf-8-sig", errors="replace")
        else:
            raise FileRefused("unsupported")
    except TooManyPages as e:
        raise FileRefused("too_many_pages") from e
    except UnreadableDocument as e:
        raise FileRefused("unreadable") from e
    except FileRefused:
        raise
    except Exception as e:      # the parsers raise many types on a broken or locked file
        raise FileRefused("unreadable") from e
    if not text.strip():
        raise FileRefused("no_text")
    return mask(text).text


def _decimals(token: str) -> set[Decimal]:
    """Every reading of a number as written: '1.500' is 1500 here and 1.5 elsewhere, and the token alone cannot tell which."""
    out = set()
    for c in (D.parse_number(token), token.replace(",", ""), token.replace(".", "").replace(",", "."), token):
        try:
            out.add(Decimal(c))
        except (InvalidOperation, TypeError):
            pass
    return out


def source_numbers(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for rx in (_PLAIN, _SPACED):
        for m in rx.finditer(text):
            found |= _decimals(m.group())
    return found


def _row_model(cols: list[D.Column]) -> type[BaseModel]:
    row = create_model("TableRow", **{c.name: ((Literal[tuple(c.choices)] if c.choices else str) | None, ...) for c in cols})
    return create_model("TableRows", rows=(list[row], ...))


def _off_line(row: dict[str, str], cols: list[D.Column], lines: list[set[Decimal]]) -> list[D.Column]:
    """A number can be in the file and still belong to another item. Where the file has one item per line (at least two of the
    row's numbers share a line), a number outside that line is left blank for the person to type: it may be a value stated once
    for all items, but a wrong price costs more than retyping one. In other layouts the lines say nothing, so nothing is flagged."""
    values = {c.name: Decimal(row[c.name]) for c in cols if c.kind in ("number", "integer") and c.name in row}
    best = max(lines, key=lambda line: sum(v in line for v in values.values()), default=set())
    if sum(v in best for v in values.values()) < 2:
        return []
    return [c for c in cols if c.name in values and values[c.name] not in best]


def check_rows(raw_rows: list[dict[str, Any]], cols: list[D.Column], text: str, max_rows: int) -> Proposal:
    """Keep what the file backs. A number must be in it, a code or name carries no digit that is not in it, a choice must exist."""
    numbers, digits = source_numbers(text), set(re.findall(r"\d+", text))
    lines = [source_numbers(line) for line in text.splitlines()]
    out = Proposal(rows=[])
    for raw in raw_rows:
        given = {k: v for k, v in raw.items() if str(v or "").strip()}
        norm, _ = D.normalize_row(given, cols)
        row: dict[str, str] = {}
        dropped: list[Dropped] = []
        for c in cols:
            value = norm.get(c.name)
            if value is None:
                continue
            value = str(value)
            if c.kind in ("number", "integer"):
                try:
                    ok = Decimal(value) in numbers
                except InvalidOperation:
                    ok = False
            elif c.kind == "choice":
                ok = value in c.choices
            else:
                ok = all(d in digits for d in re.findall(r"\d+", value))
            if ok:
                row[c.name] = value
            else:
                dropped.append(Dropped(len(out.rows) + 1, c.label, str(given.get(c.name, value))[:60]))
        if row:
            out.rows.append(row)
            out.dropped += dropped
            for c in _off_line(row, cols, lines):
                out.doubtful.append(Dropped(len(out.rows), c.label, str(given.get(c.name, row[c.name]))[:60]))
                del row[c.name]
    if len(out.rows) > max_rows:
        out.rows, out.rows_cut = out.rows[:max_rows], True
        out.dropped = [d for d in out.dropped if d.row <= max_rows]
        out.doubtful = [d for d in out.doubtful if d.row <= max_rows]
    return out


async def propose(gateway, node, ctx, *, label: str, description: str, cols: list[D.Column], text: str,
                  max_chars: int, max_rows: int) -> Proposal:
    cut = len(text) > max_chars
    sent = text[:max_chars].replace("</data>", "")
    columns = [{"name": c.name, "label": c.label, "help": c.help.rstrip("."),
                "choices": [f"{value} ({text_label})" for value, text_label in c.choices.items()]} for c in cols]
    r = await gateway.chat(node, render(node.prompt_version, table=label, description=description, columns=columns, text=sent),
                           ctx, _row_model(cols))
    proposal = check_rows([row.model_dump() for row in r.parsed.rows], cols, sent, max_rows)
    proposal.text_cut = cut
    return proposal
