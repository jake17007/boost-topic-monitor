"""RSS/Atom source — polls a configurable list of feeds.

RSS has no native engagement metric, so we use a recency-decay score: each
entry starts at 1000 and falls off over a few days. This keeps fresh lab
announcements competitive with HN top stories in the "Top" sort instead
of pinned to the bottom with a constant score.

Feeds live in the `rss_feeds` table and are editable from the UI. Each
entry's source_id is `<feed_url>::<entry_guid>` so the same article appearing
in two feeds is tracked twice (rare in practice).

Defaults seed the seven frontier-AI labs:
  OpenAI, Anthropic, Google DeepMind, Meta AI, Mistral, xAI, Apple ML.
Where the lab doesn't publish official RSS, we use Olshansk/rss-feeds —
a community project that scrapes blog pages into RSS hourly.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit

import feedparser
import httpx

from .. import db
from .base import SourcePost

log = logging.getLogger("hn-monitor.sources.rss")

# Per-feed cache TTL: discovery and snapshot run on overlapping schedules,
# so cache the parsed feed briefly to avoid double-fetches each minute.
FEED_CACHE_TTL = 25.0

# Score decay: 1000 fresh, falls 10 per hour, clamped at 1.
# Tuned so a 1h-old entry (~990) and a 24h-old entry (~760) both surface in
# "Top" alongside HN scores.
SCORE_BASE = 1000
SCORE_DECAY_PER_HOUR = 10

DEFAULT_FEEDS = [
    "https://openai.com/news/rss.xml",
    "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml",
    "https://deepmind.google/blog/rss.xml",
    "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_meta_ai.xml",
    "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_mistral.xml",
    "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_xainews.xml",
    "https://machinelearning.apple.com/rss.xml",
]


def _decay_score(posted_ts: int | None, now: int) -> int:
    if not posted_ts:
        return 1
    age_hours = max(0.0, (now - int(posted_ts)) / 3600.0)
    return max(1, int(SCORE_BASE - SCORE_DECAY_PER_HOUR * age_hours))


def _entry_id(entry) -> str | None:
    # feedparser exposes id/guid via .id; fall back to link.
    eid = getattr(entry, "id", None) or entry.get("id") if isinstance(entry, dict) else None
    if not eid:
        eid = getattr(entry, "link", None) or (entry.get("link") if isinstance(entry, dict) else None)
    return str(eid) if eid else None


def _entry_posted_ts(entry) -> int | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        # parsed is a struct_time in UTC
        return int(time.mktime(parsed))
    except Exception:
        return None


def _entry_author(entry, feed_meta) -> str | None:
    a = entry.get("author")
    if a:
        return str(a)
    feed_title = (feed_meta or {}).get("title")
    return str(feed_title) if feed_title else None


def _hostname(url: str) -> str:
    try:
        return urlsplit(url).hostname or url
    except Exception:
        return url


class RSSSource:
    name = "rss"
    label = "RSS"
    description = (
        "Polls configured RSS/Atom feeds (frontier-AI lab blogs by default; "
        "editable from the UI). Score is a recency decay (1000 at publish, "
        "falls 10/hour) since RSS exposes no engagement metric."
    )

    def __init__(self) -> None:
        self._feeds: list[str] = []
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(4)
        # url -> (fetched_at_monotonic, parsed_feed)
        self._cache: dict[str, tuple[float, object]] = {}

    def _refresh_feeds(self) -> None:
        self._feeds = db.list_rss_feeds()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=5.0),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                headers={"User-Agent": "boost-topic-monitor/0.1"},
            )
        return self._client

    async def _fetch_and_parse(self, url: str):
        cached = self._cache.get(url)
        if cached and (time.monotonic() - cached[0]) < FEED_CACHE_TTL:
            return cached[1]
        client = await self._get_client()
        try:
            async with self._sem:
                r = await client.get(url)
        except Exception as e:
            log.warning("[rss] fetch %s failed: %s", url, e)
            return None
        if r.status_code != 200:
            log.warning("[rss] %s HTTP %d", url, r.status_code)
            return None
        # feedparser is sync and CPU-bound; offload to a thread.
        try:
            parsed = await asyncio.to_thread(feedparser.parse, r.content)
        except Exception as e:
            log.warning("[rss] parse %s failed: %s", url, e)
            return None
        self._cache[url] = (time.monotonic(), parsed)
        return parsed

    def _to_post(self, feed_url: str, entry, feed_meta, now: int) -> SourcePost | None:
        eid = _entry_id(entry)
        if not eid:
            return None
        link = entry.get("link")
        title = entry.get("title")
        posted_ts = _entry_posted_ts(entry)
        return SourcePost(
            source_id=f"{feed_url}::{eid}",
            title=str(title) if title else None,
            url=str(link) if link else None,
            author=_entry_author(entry, feed_meta),
            posted_ts=posted_ts,
            score=_decay_score(posted_ts, now),
            dead=False,
        )

    async def fetch_new_post_ids(self) -> list[str]:
        self._refresh_feeds()
        if not self._feeds:
            return []
        now = int(time.time())
        results = await asyncio.gather(
            *(self._fetch_and_parse(u) for u in self._feeds),
            return_exceptions=False,
        )
        ids: list[str] = []
        for url, parsed in zip(self._feeds, results):
            if not parsed:
                continue
            for entry in (parsed.entries or [])[:50]:
                eid = _entry_id(entry)
                if eid:
                    ids.append(f"{url}::{eid}")
        return ids

    async def fetch_post(self, source_id: str) -> SourcePost | None:
        out = await self.fetch_posts([source_id])
        return out.get(source_id)

    async def fetch_posts(self, source_ids: list[str]) -> dict[str, SourcePost]:
        if not source_ids:
            return {}
        # Group requested ids by feed url so we fetch each feed at most once.
        wanted: dict[str, set[str]] = {}
        for sid in source_ids:
            if "::" not in sid:
                continue
            url, eid = sid.split("::", 1)
            wanted.setdefault(url, set()).add(eid)
        if not wanted:
            return {}
        urls = list(wanted.keys())
        parsed_list = await asyncio.gather(
            *(self._fetch_and_parse(u) for u in urls),
            return_exceptions=False,
        )
        now = int(time.time())
        out: dict[str, SourcePost] = {}
        for url, parsed in zip(urls, parsed_list):
            if not parsed:
                continue
            feed_meta = parsed.get("feed") or {}
            wanted_for_url = wanted[url]
            for entry in parsed.entries or []:
                eid = _entry_id(entry)
                if not eid or eid not in wanted_for_url:
                    continue
                post = self._to_post(url, entry, feed_meta, now)
                if post is not None:
                    out[post.source_id] = post
        return out

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
