"""Bluesky source.

Discovery uses the public Jetstream WebSocket
(https://github.com/bluesky-social/jetstream) — every post on the network
streams in as JSON; we keyword-filter `record.text` and stage matches in an
in-memory buffer. Snapshots use the public batched `app.bsky.feed.getPosts`
endpoint to refresh likes/reposts/replies in groups of up to 25.

No auth, no rate limit. Keywords live in the `bluesky_keywords` table and are
editable from the UI; the regex rebuilds on the next discovery tick after a
change.

Engagement score = likes + reposts + replies + quotes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import OrderedDict
from datetime import datetime

import httpx
import websockets

from .. import db
from .base import SourcePost

log = logging.getLogger("hn-monitor.sources.bluesky")

JETSTREAM_URL = (
    "wss://jetstream2.us-east.bsky.network/subscribe"
    "?wantedCollections=app.bsky.feed.post"
)
GETPOSTS_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts"
GETPOSTS_BATCH = 25
BUFFER_MAX = 1000

DEFAULT_KEYWORDS = [
    # AI
    "ai", "llm", "llms", "gpt", "claude", "anthropic", "openai", "gemini",
    "mistral", "agentic", "agent", "rag", "embeddings", "fine-tuning",
    "fine tuning", "machine learning",
    # Tech / dev
    "developer", "engineering", "software", "tech", "startup", "saas",
    "devtool", "devtools", "open source", "indie hacker", "react", "rust",
    "typescript", "python",
    # Productivity
    "productivity", "pkm", "second brain", "deep work", "focus",
]


def _parse_iso(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _build_regex(keywords: list[str]) -> re.Pattern[str] | None:
    if not keywords:
        return None
    alts = "|".join(re.escape(k) for k in keywords)
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.IGNORECASE)


class BlueskySource:
    name = "bluesky"
    label = "Bluesky"
    description = "Live WebSocket firehose, keyword-filtered. Engagement refreshed every 60s. Metric: likes + reposts + replies."

    def __init__(self) -> None:
        self._keywords: list[str] = []
        self._regex: re.Pattern[str] | None = None
        # Insertion-ordered dict: source_id -> created_ts (for staging only).
        self._buffer: "OrderedDict[str, int]" = OrderedDict()
        self._buffer_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(4)
        self._ws_task: asyncio.Task | None = None
        self._closing = False

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                headers={"User-Agent": "boost-topic-monitor/0.1"},
            )
        return self._client

    def _ensure_ws(self) -> None:
        if self._closing:
            return
        if self._ws_task is None or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._ws_loop(), name="bsky-jetstream")

    def _refresh_keywords(self) -> None:
        kw = db.list_bluesky_keywords()
        if kw != self._keywords:
            self._keywords = kw
            self._regex = _build_regex(kw)
            log.info(
                "[bluesky] keywords reloaded (%d): %s",
                len(kw), ", ".join(kw) if kw else "(none — no posts will match)",
            )

    async def _ws_loop(self) -> None:
        self._refresh_keywords()
        while not self._closing:
            try:
                async with websockets.connect(
                    JETSTREAM_URL,
                    ping_interval=30,
                    ping_timeout=10,
                    max_size=2 ** 20,
                ) as ws:
                    log.info("[bluesky] connected to Jetstream")
                    async for raw in ws:
                        if self._closing:
                            break
                        await self._handle_msg(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._closing:
                    return
                log.warning("[bluesky] ws error: %s; reconnecting in 5s", e)
                await asyncio.sleep(5)

    async def _handle_msg(self, raw: str | bytes) -> None:
        try:
            data = json.loads(raw)
        except Exception:
            return
        if data.get("kind") != "commit":
            return
        commit = data.get("commit") or {}
        if commit.get("operation") != "create":
            return
        if commit.get("collection") != "app.bsky.feed.post":
            return
        record = commit.get("record") or {}
        # Skip replies — we want top-level posts on the topic.
        if record.get("reply"):
            return
        text = record.get("text") or ""
        if self._regex is None or not self._regex.search(text):
            return
        did = data.get("did")
        rkey = commit.get("rkey")
        if not did or not rkey:
            return
        uri = f"at://{did}/app.bsky.feed.post/{rkey}"
        ts = _parse_iso(record.get("createdAt"))
        async with self._buffer_lock:
            self._buffer[uri] = ts or 0
            while len(self._buffer) > BUFFER_MAX:
                self._buffer.popitem(last=False)

    async def fetch_new_post_ids(self) -> list[str]:
        self._refresh_keywords()
        self._ensure_ws()
        async with self._buffer_lock:
            ids = list(self._buffer.keys())
            self._buffer.clear()
        return ids

    async def fetch_post(self, source_id: str) -> SourcePost | None:
        out = await self.fetch_posts([source_id])
        return out.get(source_id)

    async def fetch_posts(self, source_ids: list[str]) -> dict[str, SourcePost]:
        if not source_ids:
            return {}
        client = await self._get_client()
        out: dict[str, SourcePost] = {}
        for start in range(0, len(source_ids), GETPOSTS_BATCH):
            chunk = source_ids[start:start + GETPOSTS_BATCH]
            try:
                async with self._sem:
                    r = await client.get(
                        GETPOSTS_URL,
                        params=[("uris", u) for u in chunk],
                    )
            except Exception as e:
                log.warning("[bluesky] getPosts request failed: %s", e)
                continue
            if r.status_code != 200:
                log.warning("[bluesky] getPosts HTTP %d: %s", r.status_code, r.text[:200])
                continue
            try:
                body = r.json()
            except Exception:
                continue
            for p in body.get("posts", []):
                sp = self._post_to_sourcepost(p)
                if sp is not None:
                    out[sp.source_id] = sp
        return out

    @staticmethod
    def _post_to_sourcepost(p: dict) -> SourcePost | None:
        uri = p.get("uri")
        if not isinstance(uri, str):
            return None
        record = p.get("record") or {}
        author = p.get("author") or {}
        likes = int(p.get("likeCount") or 0)
        reposts = int(p.get("repostCount") or 0)
        replies = int(p.get("replyCount") or 0)
        quotes = int(p.get("quoteCount") or 0)
        score = likes + reposts + replies + quotes
        text = (record.get("text") or "").strip()
        title = text[:200] if text else None
        handle = author.get("handle")
        rkey = uri.rsplit("/", 1)[-1] if "/" in uri else None
        url = (
            f"https://bsky.app/profile/{handle}/post/{rkey}"
            if handle and rkey else None
        )
        return SourcePost(
            source_id=uri,
            title=title,
            url=url,
            author=author.get("displayName") or handle,
            posted_ts=_parse_iso(record.get("createdAt")),
            score=score,
            dead=False,
        )

    async def close(self) -> None:
        self._closing = True
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client is not None:
            await self._client.aclose()
            self._client = None
