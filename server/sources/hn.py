"""Hacker News source via the public Firebase API."""
from __future__ import annotations

import asyncio
import httpx

from .base import SourcePost

BASE = "https://hacker-news.firebaseio.com/v0"
NEW_STORIES_LIMIT = 200


class HNSource:
    name = "hn"
    label = "Hacker News"
    description = "Polls /newstories every 30s; refreshes each post's score every 60s. Metric: upvotes."

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._sem = asyncio.Semaphore(10)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=BASE,
                timeout=httpx.Timeout(10.0, connect=5.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                headers={"User-Agent": "boost-topic-monitor/0.1"},
            )
        return self._client

    async def fetch_new_post_ids(self) -> list[str]:
        client = await self._get_client()
        r = await client.get("/newstories.json")
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return []
        return [str(i) for i in data[:NEW_STORIES_LIMIT]]

    async def fetch_post(self, source_id: str) -> SourcePost | None:
        client = await self._get_client()
        async with self._sem:
            r = await client.get(f"/item/{source_id}.json")
        if r.status_code != 200:
            return None
        item = r.json()
        if not isinstance(item, dict):
            return None
        if item.get("type") and item["type"] != "story":
            return None
        return SourcePost(
            source_id=str(item.get("id", source_id)),
            title=item.get("title"),
            url=item.get("url"),
            author=item.get("by"),
            posted_ts=item.get("time"),
            score=item.get("score") if isinstance(item.get("score"), int) else None,
            dead=bool(item.get("dead") or item.get("deleted")),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
