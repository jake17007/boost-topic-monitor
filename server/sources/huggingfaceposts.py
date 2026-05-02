"""Hugging Face community Posts feed — Twitter-style updates from AI practitioners.

Public no-auth JSON API. One endpoint serves both discovery and snapshot:
GET /api/posts?limit=N returns the most recent posts with current reaction
and comment counts.

source_id is `<author_name>/<slug>` since constructing the post URL needs both,
and frontend code only receives (source, source_id).

Engagement metric: total reactions + numComments.
"""
from __future__ import annotations

import logging
from datetime import datetime

import httpx

from .base import SourcePost

log = logging.getLogger("hn-monitor.sources.huggingfaceposts")

BASE = "https://huggingface.co"
LIMIT = 100


def _parse_iso(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _excerpt(post: dict, max_len: int = 200) -> str | None:
    raw = post.get("rawContent")
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
    else:
        chunks: list[str] = []
        for c in post.get("content") or []:
            if c.get("type") == "text":
                v = (c.get("value") or "").strip()
                if v:
                    chunks.append(v)
        text = " ".join(chunks).strip()
    if not text:
        return None
    text = text.replace("\n", " ").strip()
    return text[:max_len]


def _total_reactions(post: dict) -> int:
    total = 0
    for r in post.get("reactions") or []:
        try:
            total += int(r.get("count") or 0)
        except (TypeError, ValueError):
            continue
    return total


class HuggingFacePostsSource:
    name = "huggingfaceposts"
    label = "HF Posts"
    description = (
        "Hugging Face's community Posts feed — short Twitter-style updates from "
        "AI practitioners. Polled every 30s; engagement refreshed every 60s. "
        "Metric: total reactions + comments."
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
            r = await client.get("/api/posts", params={"limit": LIMIT})
        except Exception as e:
            log.warning("[hfposts] fetch failed: %s", e)
            return []
        if r.status_code != 200:
            log.warning("[hfposts] HTTP %d: %s", r.status_code, r.text[:200])
            return []
        body = r.json() or {}
        posts = body.get("socialPosts")
        return posts if isinstance(posts, list) else []

    def _to_post(self, item: dict) -> SourcePost | None:
        slug = item.get("slug")
        author = item.get("author") or {}
        author_handle = author.get("name")  # the @-style handle used in the URL
        if not slug or not author_handle:
            return None
        source_id = f"{author_handle}/{slug}"
        url = f"https://huggingface.co/posts/{author_handle}/{slug}"
        # Prefer an image attachment; fall back to the author's avatar so the
        # card always has *some* thumbnail.
        thumb: str | None = None
        for att in item.get("attachments") or []:
            if att.get("type") == "image" and att.get("url"):
                thumb = str(att["url"])
                break
        if not thumb:
            thumb = author.get("avatarUrl")
        return SourcePost(
            source_id=source_id,
            title=_excerpt(item),
            url=url,
            author=author.get("fullname") or author_handle,
            posted_ts=_parse_iso(item.get("publishedAt")),
            score=_total_reactions(item) + int(item.get("numComments") or 0),
            dead=False,
            thumbnail_url=str(thumb) if thumb else None,
        )

    async def fetch_new_post_ids(self) -> list[str]:
        items = await self._list()
        ids: list[str] = []
        for item in items:
            slug = item.get("slug")
            handle = (item.get("author") or {}).get("name")
            if slug and handle:
                ids.append(f"{handle}/{slug}")
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
