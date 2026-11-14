"""Split text into bubbles that fit the channel's byte limit (Instagram: 1000 bytes UTF-8)."""
from __future__ import annotations

import re

_SENTENCE = re.compile(r"(?<=[.!?…])\s+")
_PARAGRAPH = re.compile(r"\n\s*\n")


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
    """Sentences share a bubble up to the byte limit; a blank line always starts a new bubble (a paragraph is a message,
    as people write in a chat)."""
    text = text.strip()
    if not text:
        return []
    bubbles: list[str] = []
    for para in (p.strip() for p in _PARAGRAPH.split(text)):
        if para:
            bubbles += _pack([s.strip() for s in _SENTENCE.split(para) if s.strip()], max_bytes)
    if len(bubbles) > max_bubbles:            # never flood: merge the tail into the last allowed bubble if it fits
        head, tail = bubbles[:max_bubbles - 1], " ".join(bubbles[max_bubbles - 1:])
        bubbles = head + _hard_split(tail, max_bytes)
    return bubbles


def question_apart(text: str) -> str:
    """The closing question after a blank line, so it is sent as its own bubble. Text with no answer before the question
    stays as it is."""
    text = text.strip()
    if "\n" in text or not text.endswith("?"):
        return text
    sents = _SENTENCE.split(text)
    if len(sents) < 2:
        return text
    return " ".join(sents[:-1]) + "\n\n" + sents[-1]


def _pack(parts: list[str], max_bytes: int) -> list[str]:
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
    return bubbles
