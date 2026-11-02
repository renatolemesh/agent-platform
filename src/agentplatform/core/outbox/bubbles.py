"""Split text into bubbles that fit the channel's byte limit (Instagram: 1000 bytes UTF-8)."""
from __future__ import annotations

import re

_SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n{2,}")


def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _hard_split(text: str, limit: int) -> list[str]:
    out, cur = [], ""
    for word in text.split(" "):
        cand = f"{cur} {word}".strip()
        if _bytes(cand) <= limit:
            cur = cand
            continue
        if cur:
            out.append(cur)
        while _bytes(word) > limit:            # a single huge token: split by characters, never mid-codepoint
            cut = ""
            for ch in word:
                if _bytes(cut + ch) > limit:
                    break
                cut += ch
            out.append(cut)
            word = word[len(cut):]
        cur = word
    if cur:
        out.append(cur)
    return out


def split_bubbles(text: str, max_bytes: int = 1000, max_bubbles: int = 4) -> list[str]:
    text = text.strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENTENCE.split(text) if p.strip()]
    bubbles: list[str] = []
    cur = ""
    for p in parts:
        cand = f"{cur} {p}".strip() if cur else p
        if _bytes(cand) <= max_bytes:
            cur = cand
        else:
            if cur:
                bubbles.append(cur)
            if _bytes(p) <= max_bytes:
                cur = p
            else:
                chunks = _hard_split(p, max_bytes)
                bubbles.extend(chunks[:-1])
                cur = chunks[-1]
    if cur:
        bubbles.append(cur)
    if len(bubbles) > max_bubbles:            # never flood: merge the tail into the last allowed bubble if it fits
        head, tail = bubbles[:max_bubbles - 1], " ".join(bubbles[max_bubbles - 1:])
        bubbles = head + _hard_split(tail, max_bytes)
    return bubbles
