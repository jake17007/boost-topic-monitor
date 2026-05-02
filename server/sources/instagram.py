"""Instagram source — recent posts/reels per configured creator handle.

Instagram has no free public API; we scrape via Apify's
`apify/instagram-post-scraper` actor (returns view counts, likes, comments
for posts/reels in one call). Auth: `APIFY_TOKEN` env var (Bearer header).

Both discovery and snapshots are served from a single ~5-minute cache —
one Apify run per cache miss covers all configured creators in one POST.
This keeps cost predictable (Apify charges per dataset item returned;
~$1 / 1000 results, so trim `RESULTS_LIMIT` to control spend).

Engagement score per post:
    score = videoPlayCount or videoViewCount or likesCount

Reels/videos win; photo posts fall back to like count.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime

import httpx

from .. import db
from .base import SourcePost

log = logging.getLogger("hn-monitor.sources.instagram")

ACTOR_SLUG = "apify~instagram-post-scraper"
ENDPOINT = (
    f"https://api.apify.com/v2/acts/{ACTOR_SLUG}/run-sync-get-dataset-items"
)
RESULTS_LIMIT = 3             # per creator, per poll — keep tight to control cost
ONLY_NEWER_THAN = "7 days"    # passed to Apify as a hint (not always honored —
                              # we re-check below in _to_sourcepost)
MAX_POST_AGE_SECONDS = 7 * 24 * 3600  # hard local filter on posted_ts; drops
                                      # pinned/sponsored ancient posts the
                                      # actor returns. Matches the longest
                                      # Window option you'd realistically
                                      # use to view trends.
# Cache TTL = main cost lever. Apify charges ~$1/1000 results, so:
#   N_creators * RESULTS_LIMIT * (86400 / MIN_FETCH_INTERVAL) results/day.
# At 5 handles × 3 results × 12 calls/day (2h cache) ≈ $5/month worst-case.
MIN_FETCH_INTERVAL = 2 * 3600
SYNC_TIMEOUT = 320.0          # Apify caps run-sync at 300s; pad a bit
RATE_LIMIT_BACKOFF_MIN = 60.0
RATE_LIMIT_BACKOFF_MAX = 10 * 60.0


def _parse_iso(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _engagement(item: dict) -> int:
    """videoPlayCount > videoViewCount > likesCount."""
    for k in ("videoPlayCount", "videoViewCount", "likesCount"):
        v = item.get(k)
        if isinstance(v, int) and v > 0:
            return v
    # Even zeros are a valid score (a freshly published photo with 0 likes).
    return int(item.get("likesCount") or 0)


class InstagramSource:
    name = "instagram"
    label = "Instagram"
    description = (
        "Recent posts/reels from configured creator handles via Apify. "
        "Score: videoPlayCount (videos/reels) or likesCount (photos). "
        "Cached ~5 min between Apify calls to control cost."
    )

    def __init__(self, token: str) -> None:
        self._token = token
        self._handles: list[str] = []
        self._client: httpx.AsyncClient | None = None
        # Cache: source_id (shortCode) -> SourcePost. Refreshed atomically per
        # Apify run, served by both discovery and snapshot calls.
        self._cache: dict[str, SourcePost] = {}
        self._cache_ts: float = 0.0
        self._cache_lock = asyncio.Lock()
        self._rate_limit_until: float = 0.0
        self._rate_limit_streak: int = 0  # exponential backoff factor

    @classmethod
    def from_env(cls) -> "InstagramSource | None":
        # Accept either name; APIFY_TOKEN is the Apify CLI default, but
        # APIFY_API_TOKEN is also widely used in their docs/examples.
        token = os.getenv("APIFY_TOKEN") or os.getenv("APIFY_API_TOKEN")
        if not token:
            log.info("APIFY_TOKEN / APIFY_API_TOKEN not set; Instagram source disabled")
            return None
        log.info("Instagram source enabled (handles loaded from DB on each tick)")
        return cls(token)

    def _refresh_handles(self) -> None:
        self._handles = db.list_instagram_handles()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(SYNC_TIMEOUT, connect=10.0),
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "User-Agent": "boost-topic-monitor/0.1",
                },
            )
        return self._client

    def _rate_limited(self) -> bool:
        return self._rate_limit_until and time.monotonic() < self._rate_limit_until

    def _set_backoff(self, retry_after: float | None) -> None:
        # Exponential, capped. retry_after (from header) wins if present.
        self._rate_limit_streak = min(self._rate_limit_streak + 1, 4)
        wait = (
            float(retry_after) if retry_after else
            min(
                RATE_LIMIT_BACKOFF_MIN * (2 ** (self._rate_limit_streak - 1)),
                RATE_LIMIT_BACKOFF_MAX,
            )
        )
        self._rate_limit_until = time.monotonic() + wait
        log.warning("[instagram] backing off %.0fs", wait)

    async def _refresh_cache_if_due(self) -> None:
        if self._rate_limited():
            return
        async with self._cache_lock:
            if (
                time.monotonic() - self._cache_ts < MIN_FETCH_INTERVAL
                and self._cache
            ):
                return
            self._refresh_handles()
            if not self._handles:
                # No creators configured → nothing to fetch. Clear stale cache.
                self._cache = {}
                self._cache_ts = time.monotonic()
                return

            client = await self._get_client()
            payload = {
                "username": self._handles,
                "resultsLimit": RESULTS_LIMIT,
                "onlyPostsNewerThan": ONLY_NEWER_THAN,
            }
            try:
                r = await client.post(ENDPOINT, json=payload)
            except Exception as e:
                log.warning("[instagram] apify request failed: %s", e)
                self._set_backoff(None)
                return

            if r.status_code == 429:
                self._set_backoff(r.headers.get("Retry-After"))
                return
            if r.status_code >= 500:
                log.warning("[instagram] apify HTTP %d: %s", r.status_code, r.text[:200])
                self._set_backoff(None)
                return
            # Apify's run-sync-get-dataset-items returns 201 on success.
            if r.status_code not in (200, 201):
                log.warning("[instagram] apify HTTP %d: %s", r.status_code, r.text[:200])
                return

            try:
                items = r.json()
            except Exception as e:
                log.warning("[instagram] apify json decode failed: %s", e)
                return
            if not isinstance(items, list):
                log.warning("[instagram] unexpected apify payload type: %s", type(items))
                return

            new_cache: dict[str, SourcePost] = {}
            for it in items:
                sp = self._to_sourcepost(it)
                if sp is not None:
                    new_cache[sp.source_id] = sp
            self._cache = new_cache
            self._cache_ts = time.monotonic()
            self._rate_limit_streak = 0
            log.info(
                "[instagram] fetched %d posts across %d creators",
                len(new_cache), len(self._handles),
            )

    @staticmethod
    def _to_sourcepost(item: dict) -> SourcePost | None:
        short = item.get("shortCode")
        if not isinstance(short, str) or not short:
            return None
        posted_ts = _parse_iso(item.get("timestamp"))
        # Drop posts older than the active window. Apify's
        # `onlyPostsNewerThan` doesn't catch pinned/sponsored posts.
        if posted_ts is None or (int(time.time()) - posted_ts) > MAX_POST_AGE_SECONDS:
            return None
        owner = item.get("ownerUsername") or None
        caption = (item.get("caption") or "").strip()
        title = caption[:200] if caption else None
        url = item.get("url") or f"https://www.instagram.com/p/{short}/"
        return SourcePost(
            source_id=short,
            title=title,
            url=url,
            author=owner,
            posted_ts=posted_ts,
            score=_engagement(item),
            dead=False,
        )

    async def fetch_new_post_ids(self) -> list[str]:
        await self._refresh_cache_if_due()
        return list(self._cache.keys())

    async def fetch_post(self, source_id: str) -> SourcePost | None:
        await self._refresh_cache_if_due()
        return self._cache.get(source_id)

    async def fetch_posts(self, source_ids: list[str]) -> dict[str, SourcePost]:
        if not source_ids:
            return {}
        await self._refresh_cache_if_due()
        return {sid: self._cache[sid] for sid in source_ids if sid in self._cache}

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
