"""X (formerly Twitter) source — follows a list of handles stored in SQLite.

Setup:

    export X_BEARER_TOKEN="..."          # OAuth 2.0 Bearer token (required)

Handles live in the `x_handles` table and are editable from the UI.

Discovery: for each handle, fetch the latest posts from GET /2/users/{id}/tweets
(handle → user_id resolved once and cached in memory).
Snapshot: batched GET /2/tweets?ids=... up to 100 ids/call.
Engagement metric: likes + reposts + replies + quotes.
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

log = logging.getLogger("hn-monitor.sources.x")

API_BASE = "https://api.x.com/2"
POSTS_PER_HANDLE = 10
GETPOSTS_BATCH = 100

POST_FIELDS = "public_metrics,created_at,author_id"
USER_FIELDS = "name,username"


def _parse_iso(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _engagement(metrics: dict | None) -> int:
    if not metrics:
        return 0
    return int(
        (metrics.get("like_count") or 0)
        + (metrics.get("retweet_count") or 0)
        + (metrics.get("reply_count") or 0)
        + (metrics.get("quote_count") or 0)
    )


class XSource:
    name = "x"
    label = "X"
    description = (
        "Follows a fixed list of handles (X_HANDLES). "
        "Discovery polls each user's recent posts every 30s; snapshot batches "
        "post lookups every 60s. Metric: likes + reposts + replies + quotes. "
        "Note: X charges per read."
    )

    def __init__(self, token: str) -> None:
        self._token = token
        # In-memory cache of the handle list — refreshed from DB on every
        # discovery tick so UI edits take effect immediately.
        self._handles: list[str] = []
        self._handle_to_uid: dict[str, str] = {}  # lowercase handle -> user_id
        self._uid_to_handle: dict[str, str] = {}
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(4)
        self._rate_limit_until: float = 0.0

    @classmethod
    def from_env(cls) -> "XSource | None":
        token = os.getenv("X_BEARER_TOKEN")
        if not token:
            log.info("X_BEARER_TOKEN not set; X source disabled")
            return None
        log.info("X source enabled (handles loaded from DB on each tick)")
        return cls(token)

    def _refresh_handles(self) -> None:
        self._handles = db.list_x_handles()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=API_BASE,
                timeout=httpx.Timeout(15.0, connect=5.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "User-Agent": "boost-topic-monitor/0.1",
                },
            )
        return self._client

    def _rate_limited(self) -> bool:
        return self._rate_limit_until and time.monotonic() < self._rate_limit_until

    def _handle_429(self, r: httpx.Response) -> None:
        # X returns x-rate-limit-reset (epoch seconds) on 429.
        reset = r.headers.get("x-rate-limit-reset")
        if reset and reset.isdigit():
            wait = max(5.0, int(reset) - int(time.time()))
        else:
            wait = 60.0
        self._rate_limit_until = time.monotonic() + wait
        log.warning("[x] rate-limited; backing off %.0fs", wait)

    async def _resolve_uids(self) -> None:
        """Look up user IDs for any handles we haven't resolved yet."""
        missing = [h for h in self._handles if h.lower() not in self._handle_to_uid]
        if not missing:
            return
        client = await self._get_client()
        for handle in missing:
            if self._rate_limited():
                return
            try:
                async with self._sem:
                    r = await client.get(f"/users/by/username/{handle}")
            except Exception as e:
                log.warning("[x] resolve %s failed: %s", handle, e)
                continue
            if r.status_code == 429:
                self._handle_429(r)
                return
            if r.status_code != 200:
                log.warning("[x] resolve %s HTTP %d: %s", handle, r.status_code, r.text[:200])
                continue
            data = (r.json() or {}).get("data") or {}
            uid = data.get("id")
            if not uid:
                log.warning("[x] resolve %s: no id in response", handle)
                continue
            self._handle_to_uid[handle.lower()] = uid
            self._uid_to_handle[uid] = data.get("username") or handle

    async def fetch_new_post_ids(self) -> list[str]:
        if self._rate_limited():
            return []
        self._refresh_handles()
        if not self._handles:
            return []
        await self._resolve_uids()
        ids: list[str] = []
        client = await self._get_client()
        for handle in self._handles:
            uid = self._handle_to_uid.get(handle.lower())
            if not uid or self._rate_limited():
                continue
            try:
                async with self._sem:
                    r = await client.get(
                        f"/users/{uid}/tweets",
                        params={
                            "max_results": POSTS_PER_HANDLE,
                            "tweet.fields": "id",
                            "exclude": "retweets,replies",
                        },
                    )
            except Exception as e:
                log.warning("[x] timeline %s failed: %s", handle, e)
                continue
            if r.status_code == 429:
                self._handle_429(r)
                return ids
            if r.status_code != 200:
                log.warning("[x] timeline %s HTTP %d: %s", handle, r.status_code, r.text[:200])
                continue
            data = (r.json() or {}).get("data") or []
            for t in data:
                if t.get("id"):
                    ids.append(str(t["id"]))
        return ids

    async def fetch_post(self, source_id: str) -> SourcePost | None:
        out = await self.fetch_posts([source_id])
        return out.get(source_id)

    async def fetch_posts(self, source_ids: list[str]) -> dict[str, SourcePost]:
        if not source_ids or self._rate_limited():
            return {}
        client = await self._get_client()
        out: dict[str, SourcePost] = {}
        for start in range(0, len(source_ids), GETPOSTS_BATCH):
            chunk = source_ids[start:start + GETPOSTS_BATCH]
            try:
                async with self._sem:
                    r = await client.get(
                        "/tweets",
                        params={
                            "ids": ",".join(chunk),
                            "tweet.fields": POST_FIELDS,
                            "user.fields": USER_FIELDS,
                            "expansions": "author_id",
                        },
                    )
            except Exception as e:
                log.warning("[x] posts lookup failed: %s", e)
                continue
            if r.status_code == 429:
                self._handle_429(r)
                return out
            if r.status_code != 200:
                log.warning("[x] posts HTTP %d: %s", r.status_code, r.text[:200])
                continue
            body = r.json() or {}
            users = {u["id"]: u for u in (body.get("includes") or {}).get("users", [])}
            for t in body.get("data") or []:
                tid = t.get("id")
                if not tid:
                    continue
                user = users.get(t.get("author_id")) or {}
                username = user.get("username")
                url = f"https://x.com/{username}/status/{tid}" if username else None
                text = (t.get("text") or "").strip()
                out[str(tid)] = SourcePost(
                    source_id=str(tid),
                    title=text[:200] if text else None,
                    url=url,
                    author=user.get("name") or username,
                    posted_ts=_parse_iso(t.get("created_at")),
                    score=_engagement(t.get("public_metrics")),
                    dead=False,
                )
        return out

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
