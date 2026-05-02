"""Reddit source — follows a list of subreddits stored in SQLite.

Uses Reddit's public unauthenticated JSON endpoints (no API key, no app
registration). Rate budget is ~10 req/min per IP; with 4 subs at 30s
discovery + 1 batched snapshot/min that's ~9/min — fine. Adding more subs
or shrinking intervals will eventually 429.

Subreddits live in the `reddit_subreddits` table and are editable from the UI.

Discovery: for each configured subreddit, GET /r/<sub>/new.json?limit=25.
Snapshot:  batched GET /api/info.json?id=t3_<id1>,t3_<id2>,... up to 100 ids/call.
Engagement metric: score + num_comments.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .. import db
from .base import SourcePost

log = logging.getLogger("hn-monitor.sources.reddit")

BASE = "https://www.reddit.com"
POSTS_PER_SUB = 25
INFO_BATCH = 100

DEFAULT_SUBREDDITS = [
    "singularity",
    "LocalLLaMA",
    "StableDiffusion",
    "MachineLearning",
]


class RedditSource:
    name = "reddit"
    label = "Reddit"
    description = (
        "Follows a list of subreddits (editable from the UI) via Reddit's "
        "public JSON endpoints — no API key. Discovery polls /r/<sub>/new "
        "every 30s; snapshot batches /api/info every 60s. Metric: score + comments."
    )

    def __init__(self) -> None:
        self._subreddits: list[str] = []
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(4)
        self._rate_limit_until: float = 0.0

    def _refresh_subreddits(self) -> None:
        self._subreddits = db.list_reddit_subreddits()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=BASE,
                timeout=httpx.Timeout(15.0, connect=5.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                headers={"User-Agent": "boost-topic-monitor/0.1"},
            )
        return self._client

    def _rate_limited(self) -> bool:
        return self._rate_limit_until and time.monotonic() < self._rate_limit_until

    def _handle_429(self, r: httpx.Response) -> None:
        # Reddit returns x-ratelimit-reset as seconds-until-reset (float).
        reset = r.headers.get("x-ratelimit-reset")
        try:
            wait = max(5.0, float(reset)) if reset else 60.0
        except ValueError:
            wait = 60.0
        self._rate_limit_until = time.monotonic() + wait
        log.warning("[reddit] rate-limited; backing off %.0fs", wait)

    async def fetch_new_post_ids(self) -> list[str]:
        if self._rate_limited():
            return []
        self._refresh_subreddits()
        if not self._subreddits:
            return []
        client = await self._get_client()
        ids: list[str] = []
        for sub in self._subreddits:
            if self._rate_limited():
                break
            try:
                async with self._sem:
                    r = await client.get(
                        f"/r/{sub}/new.json",
                        params={"limit": POSTS_PER_SUB, "raw_json": 1},
                    )
            except Exception as e:
                log.warning("[reddit] /r/%s/new failed: %s", sub, e)
                continue
            if r.status_code == 429:
                self._handle_429(r)
                return ids
            if r.status_code != 200:
                log.warning("[reddit] /r/%s/new HTTP %d: %s", sub, r.status_code, r.text[:200])
                continue
            children = ((r.json() or {}).get("data") or {}).get("children") or []
            for child in children:
                data = child.get("data") or {}
                pid = data.get("id")
                if pid:
                    ids.append(str(pid))
        return ids

    async def fetch_post(self, source_id: str) -> SourcePost | None:
        out = await self.fetch_posts([source_id])
        return out.get(source_id)

    async def fetch_posts(self, source_ids: list[str]) -> dict[str, SourcePost]:
        if not source_ids or self._rate_limited():
            return {}
        client = await self._get_client()
        out: dict[str, SourcePost] = {}
        for start in range(0, len(source_ids), INFO_BATCH):
            chunk = source_ids[start:start + INFO_BATCH]
            fullnames = ",".join(f"t3_{sid}" for sid in chunk)
            try:
                async with self._sem:
                    r = await client.get(
                        "/api/info.json",
                        params={"id": fullnames, "raw_json": 1},
                    )
            except Exception as e:
                log.warning("[reddit] /api/info failed: %s", e)
                continue
            if r.status_code == 429:
                self._handle_429(r)
                return out
            if r.status_code != 200:
                log.warning("[reddit] /api/info HTTP %d: %s", r.status_code, r.text[:200])
                continue
            children = ((r.json() or {}).get("data") or {}).get("children") or []
            for child in children:
                data = child.get("data") or {}
                pid = data.get("id")
                if not pid:
                    continue
                author = data.get("author")
                selftext = data.get("selftext") or ""
                dead = bool(data.get("removed_by_category")) or (
                    author == "[deleted]" and selftext == "[deleted]"
                )
                score = int(data.get("score") or 0) + int(data.get("num_comments") or 0)
                created = data.get("created_utc")
                posted_ts = int(created) if isinstance(created, (int, float)) else None
                out[str(pid)] = SourcePost(
                    source_id=str(pid),
                    title=data.get("title"),
                    url=data.get("url"),
                    author=author if author and author != "[deleted]" else None,
                    posted_ts=posted_ts,
                    score=score,
                    dead=dead,
                )
        return out

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
