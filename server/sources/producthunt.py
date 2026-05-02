"""Product Hunt source via the public GraphQL API.

Requires a developer API token. From your application page on Product Hunt,
click "Create Token" — that gives a long-lived bearer token. Then set:

    export PRODUCTHUNT_TOKEN="your-token"

If unset the source is not registered.

Implementation notes:
  * The PH GraphQL API has a 6250 complexity-point/15-min budget. Per-post
    queries blow through it instantly, so we fetch the whole featured-launch
    list in one call and serve `fetch_post()` from a short-lived in-memory
    cache.
  * If we still hit a 429, we honor the `reset_in` and stay quiet until the
    window opens.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime

import httpx

from .base import SourcePost

log = logging.getLogger("hn-monitor.sources.producthunt")

GRAPHQL_URL = "https://api.producthunt.com/v2/api/graphql"
LISTING_LIMIT = 50
# Re-fetch the listing if older than this; one tick = 60s, so 30s usually means
# one network call per tick.
LISTING_TTL = 30

LISTING_QUERY = """
query DailyLaunches($first: Int!) {
  posts(order: RANKING, featured: true, first: $first) {
    edges {
      node {
        id
        slug
        name
        tagline
        url
        createdAt
        votesCount
        commentsCount
        thumbnail { url }
        user { name username }
      }
    }
  }
}
"""


def _parse_iso(dt_str: str | None) -> int | None:
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def _node_to_post(node: dict) -> SourcePost:
    user = node.get("user") or {}
    slug = node.get("slug")
    url = node.get("url") or (f"https://www.producthunt.com/posts/{slug}" if slug else None)
    name = (node.get("name") or "").strip()
    tagline = (node.get("tagline") or "").strip()
    if name and tagline:
        title = f"{name} — {tagline}"
    else:
        title = name or tagline or None
    thumb = (node.get("thumbnail") or {}).get("url")
    return SourcePost(
        source_id=str(node["id"]),
        title=title,
        url=url,
        author=user.get("name") or user.get("username"),
        posted_ts=_parse_iso(node.get("createdAt")),
        score=node.get("votesCount") if isinstance(node.get("votesCount"), int) else None,
        dead=False,
        thumbnail_url=str(thumb) if thumb else None,
    )


class ProductHuntSource:
    name = "producthunt"
    label = "Product Hunt"
    description = "One batched GraphQL query for today's launches every 30s. Metric: votes."

    def __init__(self, token: str) -> None:
        self._token = token
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        # Cache of the whole daily-launch listing.
        self._cache: dict[str, SourcePost] = {}
        self._cache_at: float = 0
        # If non-zero, we're rate-limited until this monotonic time.
        self._rate_limit_until: float = 0

    @classmethod
    def from_env(cls) -> "ProductHuntSource | None":
        token = os.getenv("PRODUCTHUNT_TOKEN")
        if not token:
            log.info("PRODUCTHUNT_TOKEN not set; Product Hunt source disabled")
            return None
        return cls(token)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "User-Agent": "boost-topic-monitor/0.1",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def _rate_limited(self) -> bool:
        if self._rate_limit_until and time.monotonic() < self._rate_limit_until:
            return True
        return False

    async def _refresh_listing(self) -> None:
        """Refresh the cached launch listing. Single GraphQL call serving all posts."""
        async with self._lock:
            if (time.time() - self._cache_at) < LISTING_TTL and self._cache:
                return
            if self._rate_limited():
                return
            client = await self._get_client()
            r = await client.post(
                GRAPHQL_URL,
                json={"query": LISTING_QUERY, "variables": {"first": LISTING_LIMIT}},
            )
            if r.status_code == 429:
                self._handle_429(r)
                return
            if r.status_code != 200:
                log.warning("PH GraphQL HTTP %d: %s", r.status_code, r.text[:200])
                return
            body = r.json()
            if body.get("errors"):
                # 429 sometimes returned with a 200 status + errors envelope.
                if any("rate_limit" in str(e) for e in body["errors"]):
                    self._handle_429_body(body)
                    return
                log.warning("PH GraphQL errors: %s", body["errors"])
                return
            edges = (((body.get("data") or {}).get("posts") or {}).get("edges") or [])
            self._cache = {n["node"]["id"]: _node_to_post(n["node"]) for n in edges if n.get("node", {}).get("id")}
            self._cache_at = time.time()

    def _handle_429(self, r: httpx.Response) -> None:
        try:
            body = r.json()
        except Exception:
            body = {}
        self._handle_429_body(body)

    def _handle_429_body(self, body: dict) -> None:
        details = (((body.get("errors") or [{}])[0]).get("details") or {})
        reset_in = details.get("reset_in")
        if isinstance(reset_in, (int, float)) and reset_in > 0:
            self._rate_limit_until = time.monotonic() + float(reset_in)
            log.warning("PH rate-limited; backing off %.0fs", reset_in)
        else:
            self._rate_limit_until = time.monotonic() + 60
            log.warning("PH rate-limited; backing off 60s (no reset hint)")

    async def fetch_new_post_ids(self) -> list[str]:
        await self._refresh_listing()
        return list(self._cache.keys())

    async def fetch_post(self, source_id: str) -> SourcePost | None:
        # Always go through the cached listing — never hit per-post queries.
        await self._refresh_listing()
        return self._cache.get(source_id)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
