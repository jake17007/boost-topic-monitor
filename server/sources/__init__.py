"""Source registry. Add new sources to `_build_sources()` below."""
from __future__ import annotations

import logging

from .base import Source, SourcePost
from .bluesky import BlueskySource
from .googletrending import GoogleTrendingSource
from .googletrends import GoogleTrendsSource
from .hn import HNSource
from .huggingfacepapers import HuggingFacePapersSource
from .huggingfaceposts import HuggingFacePostsSource
from .instagram import InstagramSource
from .producthunt import ProductHuntSource
from .reddit import RedditSource
from .rss import RSSSource
from .x import XSource

log = logging.getLogger("hn-monitor.sources")

_sources: list[Source] | None = None


def _build_sources() -> list[Source]:
    sources: list[Source] = [
        HNSource(),
        BlueskySource(),
        RedditSource(),
        HuggingFacePapersSource(),
        HuggingFacePostsSource(),
        RSSSource(),
    ]
    ph = ProductHuntSource.from_env()
    if ph is not None:
        sources.append(ph)
    x = XSource.from_env()
    if x is not None:
        sources.append(x)
    ig = InstagramSource.from_env()
    if ig is not None:
        sources.append(ig)
    gt = GoogleTrendsSource.from_env()
    if gt is not None:
        sources.append(gt)
    gtr = GoogleTrendingSource.from_env()
    if gtr is not None:
        sources.append(gtr)
    log.info("active sources: %s", [s.name for s in sources])
    return sources


def get_sources() -> list[Source]:
    global _sources
    if _sources is None:
        _sources = _build_sources()
    return _sources


def get_source(name: str) -> Source | None:
    for s in get_sources():
        if s.name == name:
            return s
    return None


async def close_sources() -> None:
    if _sources:
        for s in _sources:
            try:
                await s.close()
            except Exception:
                log.exception("error closing source %s", s.name)


__all__ = ["Source", "SourcePost", "get_sources", "get_source", "close_sources"]
