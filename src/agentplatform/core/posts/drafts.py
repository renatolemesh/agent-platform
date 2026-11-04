"""Posts written in the console (ADR-024): text and files until a person approves them. No provider call here."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Awaitable, Callable
from uuid import UUID

from psycopg import AsyncConnection

from agentplatform.core.comments.settings import TextProblem, text_problems
from agentplatform.core.config.schema import PlatformConfig, PostsConfig
from agentplatform.core.security.keys import decrypt_blob, encrypt_blob
from agentplatform.core.security.pii import mask

KINDS = ("feed", "reel", "story")
CHUNK_CONTEXT = "post_file_chunks.content"
# A person wrote it and a person approves it, so numbers and contact details are theirs to decide. What no post may say still applies.
_CHECKED = ("url_not_allowed", "forbidden_phrase", "required_context", "never_request")
_NEVER_IN_A_POST = ("cpf", "card")         # e-mail and phone can be the company's own
_HASHTAG = re.compile(r"(?<!\w)#\w+")
_MENTION = re.compile(r"(?<!\w)@[\w.]+")


class Refused(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


def sniff(head: bytes) -> tuple[str, bool] | None:
    """(content type, is video) from the bytes: what the browser claims is not evidence. The provider takes only these."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", False
    if head[4:8] == b"ftyp":
        return ("video/quicktime" if head[8:10] == b"qt" else "video/mp4"), True
    return None


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from the frame header. None when it is not within these bytes: the provider then has the last word."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


def content_hash(kind: str, caption: str, files: list[dict]) -> str:
    return hashlib.sha256(json.dumps([kind, caption, [f["sha256"] for f in files]]).encode()).hexdigest()


def caption_problems(cfg: PlatformConfig, caption: str, allowed_urls: list[str]) -> list[TextProblem]:
    """Published text: the same refusals as the automatic replies, except what only makes sense for text nobody reviews."""
    c = cfg.operations.posts
    if not caption:
        return []
    out = text_problems(cfg, "caption", caption, c.caption_max_chars, allowed_urls, checked=_CHECKED)
    if len(_HASHTAG.findall(caption)) > c.caption_max_hashtags:
        out.append(TextProblem("caption", "too_many_hashtags"))
    if len(_MENTION.findall(caption)) > c.caption_max_mentions:
        out.append(TextProblem("caption", "too_many_mentions"))
    if set(mask(caption).found) & set(_NEVER_IN_A_POST):
        out.append(TextProblem("caption", "pii_placeholder"))
    return out


def file_problems(kind: str, files: list[dict], c: PostsConfig) -> list[TextProblem]:
    out: list[TextProblem] = []
    videos = sum(1 for f in files if f["is_video"])
    if not files:
        out.append(TextProblem("files", "no_file"))
    elif kind == "reel" and (len(files) != 1 or videos != 1):
        out.append(TextProblem("files", "reel_one_video"))
    elif kind == "story" and len(files) != 1:
        out.append(TextProblem("files", "story_one_file"))
    elif kind == "feed" and len(files) == 1 and videos:
        out.append(TextProblem("files", "feed_single_video"))
    elif kind == "feed" and len(files) > c.carousel_max_items:
        out.append(TextProblem("files", "too_many_files"))
    if kind == "feed":
        for f in files:
            if f["is_video"] or not f.get("width") or not f.get("height"):
                continue
            if f["width"] < c.image_min_width or not c.image_min_ratio <= f["width"] / f["height"] <= c.image_max_ratio:
                out.append(TextProblem("files", "image_shape"))
                break
    return out


async def create(conn: AsyncConnection, tenant_id: UUID, account_id: UUID, user_id: UUID | None, kind: str, caption: str) -> UUID:
    row = await (await conn.execute(
        "insert into post_drafts (tenant_id, channel_account_id, kind, caption, created_by) values (%s,%s,%s,%s,%s) returning id",
        (tenant_id, account_id, kind, caption, user_id))).fetchone()
    return row["id"]


async def load(conn: AsyncConnection, post_id: UUID) -> dict | None:
    p = await (await conn.execute("select * from post_drafts where id=%s", (post_id,))).fetchone()
    if not p:
        return None
    files = await (await conn.execute("select * from post_draft_files where draft_id=%s order by position, created_at", (post_id,))).fetchall()
    return {**p, "files": [dict(f) for f in files]}


async def add_file(conn: AsyncConnection, tenant_id: UUID, post_id: UUID, read: Callable[[int], Awaitable[bytes]], c: PostsConfig) -> UUID:
    """Read, encrypted and stored piece by piece. Refused by raising, so the caller's transaction leaves nothing behind."""
    head = await read(c.file_chunk_bytes)
    kind = sniff(head)
    if kind is None:
        raise Refused("unsupported_file")
    content_type, is_video = kind
    limit = c.max_video_bytes if is_video else c.max_image_bytes
    size = jpeg_size(head) if not is_video else None
    next_pos = await (await conn.execute("select coalesce(max(position), -1) + 1 as p, count(*) as n from post_draft_files where draft_id=%s", (post_id,))).fetchone()
    if next_pos["n"] >= c.carousel_max_items:
        raise Refused("too_many_files", str(c.carousel_max_items))
    row = await (await conn.execute(
        """insert into post_draft_files (tenant_id, draft_id, position, content_type, is_video, width, height, size_bytes, sha256, chunks)
           values (%s,%s,%s,%s,%s,%s,%s,0,'',0) returning id""",
        (tenant_id, post_id, next_pos["p"], content_type, is_video, size[0] if size else None, size[1] if size else None))).fetchone()
    digest, total, seq, piece = hashlib.sha256(), 0, 0, head
    while piece:
        total += len(piece)
        if total > limit:
            raise Refused("file_too_big", str(limit // 1_000_000))
        digest.update(piece)
        await conn.execute("insert into post_file_chunks (file_id, tenant_id, seq, content_enc) values (%s,%s,%s,%s)",
                           (row["id"], tenant_id, seq, await encrypt_blob(conn, tenant_id, piece, CHUNK_CONTEXT)))
        seq += 1
        piece = await read(c.file_chunk_bytes)
    await conn.execute("update post_draft_files set size_bytes=%s, sha256=%s, chunks=%s where id=%s", (total, digest.hexdigest(), seq, row["id"]))
    await conn.execute("update post_drafts set updated_at=now() where id=%s", (post_id,))
    return row["id"]


async def read_chunk(conn: AsyncConnection, tenant_id: UUID, file_id: UUID, seq: int) -> bytes | None:
    row = await (await conn.execute("select content_enc from post_file_chunks where file_id=%s and seq=%s", (file_id, seq))).fetchone()
    return await decrypt_blob(conn, tenant_id, row["content_enc"], CHUNK_CONTEXT) if row else None


async def find_public_file(conn: AsyncConnection, token: str) -> dict | None:
    """A provider fetching a file by the link it was given: the link only works while that post is being published."""
    return await (await conn.execute(
        """select f.id, f.tenant_id, f.content_type, f.size_bytes, f.chunks from post_draft_files f join post_drafts p on p.id = f.draft_id
           where f.public_token_hash=%s and p.status='publishing'""", (hash_token(token),))).fetchone()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def approve(conn: AsyncConnection, post: dict, user_id: UUID | None, when) -> bool:
    """A person read this exact content. Any change afterwards needs another approval (the publisher compares the hash)."""
    row = await (await conn.execute(
        """update post_drafts set status='approved', approved_by=%s, approved_at=now(), approved_hash=%s, scheduled_at=%s, error=null, error_detail=null,
                  updated_at=now() where id=%s and status='draft' returning id""",
        (user_id, content_hash(post["kind"], post["caption"], post["files"]), when, post["id"]))).fetchone()
    return bool(row)


async def back_to_draft(conn: AsyncConnection, post_id: UUID, statuses: tuple[str, ...] = ("approved", "failed")) -> bool:
    """Everything of the last attempt goes: containers expire, and the next attempt needs its own approval."""
    row = await (await conn.execute(
        """update post_drafts set status='draft', approved_by=null, approved_at=null, approved_hash=null, scheduled_at=null, error=null, error_detail=null,
                  container_id=null, publishing_since=null, publish_started_at=null, updated_at=now()
           where id=%s and status = any(%s) returning id""", (post_id, list(statuses)))).fetchone()
    if row:
        await conn.execute("update post_draft_files set container_id=null, public_token_hash=null where draft_id=%s", (post_id,))
    return bool(row)
