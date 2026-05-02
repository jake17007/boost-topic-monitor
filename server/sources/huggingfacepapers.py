"""Hugging Face Daily Papers — community-curated papers feed.

Public no-auth JSON API. One endpoint serves both discovery and snapshot:
GET /api/daily_papers?limit=N returns the recent papers with current upvote
and comment counts, so we can compute scores in a single call regardless of
how many papers we're tracking.

Engagement metric: upvotes + numComments.
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from .base import SourcePost

log = logging.getLogger("hn-monitor.sources.huggingfacepapers")

BASE = "https://huggingface.co"
LIMIT = 100  # roughly 7 days of curated daily papers


def _parse_iso(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


class HuggingFacePapersSource:
    name = "huggingfacepapers"
    label = "HF Papers"
    description = (
        "Hugging Face's community-curated Daily Papers feed. Discovery polls "
        "/api/daily_papers every 30s; snapshot batches the same endpoint every "
        "60s. Metric: upvotes + comments."
    )

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=BASE,
                timeout=httpx.Timeout(15.0, connect=5.0),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                headers={"User-Agent": "boost-topic-monitor/0.1"},
            )
        return self._client

    async def _list(self) -> list[dict]:
        client = await self._get_client()
        try:
            r = await client.get("/api/daily_papers", params={"limit": LIMIT})
        except Exception as e:
            log.warning("[hfpapers] daily_papers fetch failed: %s", e)
            return []
        if r.status_code != 200:
            log.warning("[hfpapers] daily_papers HTTP %d: %s", r.status_code, r.text[:200])
            return []
        data = r.json()
        return data if isinstance(data, list) else []

    def _to_post(self, item: dict) -> SourcePost | None:
        paper = item.get("paper") or {}
        arxiv_id = paper.get("id")
        if not arxiv_id:
            return None
        upvotes = int(paper.get("upvotes") or 0)
        num_comments = int(item.get("numComments") or 0)
        submitted_by = item.get("submittedBy") or {}
        author_name = submitted_by.get("fullname") or submitted_by.get("name")
        if not author_name:
            authors = paper.get("authors") or []
            if authors:
                author_name = authors[0].get("name")
        return SourcePost(
            source_id=str(arxiv_id),
            title=item.get("title") or paper.get("title"),
            url=f"https://arxiv.org/abs/{arxiv_id}",
            author=author_name,
            posted_ts=_parse_iso(
                item.get("publishedAt") or paper.get("publishedAt")
            ),
            score=upvotes + num_comments,
            dead=False,
        )

    async def fetch_new_post_ids(self) -> list[str]:
        items = await self._list()
        ids: list[str] = []
        for item in items:
            paper = item.get("paper") or {}
            pid = paper.get("id")
            if pid:
                ids.append(str(pid))
        return ids

    async def fetch_post(self, source_id: str) -> SourcePost | None:
        out = await self.fetch_posts([source_id])
        return out.get(source_id)

    async def fetch_posts(self, source_ids: list[str]) -> dict[str, SourcePost]:
        if not source_ids:
            return {}
        wanted = set(source_ids)
        items = await self._list()
        out: dict[str, SourcePost] = {}
        for item in items:
            post = self._to_post(item)
            if post is None or post.source_id not in wanted:
                continue
            out[post.source_id] = post
        return out

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
