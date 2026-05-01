"""Shared interface for engagement sources.

To add a new source:
  1. Implement a class with the `Source` protocol below.
  2. Register it in `server/sources/__init__.py:_build_sources()`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class SourcePost:
    """A normalized post snapshot returned by a source."""

    source_id: str          # source-native identifier (e.g. HN item id)
    title: str | None
    url: str | None         # canonical link to discuss/view (falls back to source-internal page)
    author: str | None
    posted_ts: int | None   # original posting time, unix seconds
    score: int | None       # current engagement score (upvotes / votes)
    dead: bool = False      # deleted/removed/dead


@runtime_checkable
class Source(Protocol):
    """Polled source of posts and per-post engagement scores."""

    name: str         # short stable id, e.g. "hn", "producthunt"
    label: str        # human-readable name shown in UI
    description: str  # one-paragraph explanation of how this source is collected

    async def fetch_new_post_ids(self) -> list[str]:
        """Return the most recent post ids (newest first)."""
        ...

    async def fetch_post(self, source_id: str) -> SourcePost | None:
        """Fetch the current state of a single post."""
        ...

    # Optional: sources can implement `fetch_posts` to batch many lookups into
    # one upstream call. `jobs._fetch_many` will use it if present. Signature:
    #     async def fetch_posts(self, source_ids: list[str]) -> dict[str, SourcePost]
    # The dict maps source_id -> SourcePost. Missing ids = post not found.

    async def close(self) -> None:
        """Release any open resources (HTTP clients etc.)."""
        ...
