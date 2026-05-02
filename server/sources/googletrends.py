"""Google Trends source.

Each tracked keyword becomes a synthetic "post": source_id = keyword,
title = keyword, url = a `trends.google.com/trends/explore` link. The score
is the latest 0–100 interest-over-time value (geo=US, timeframe='now 1-H',
~1-minute resolution).

Keywords live in the `google_trends_keywords` table and are editable from
the UI. The list starts empty; the source self-disables if `pytrends` isn't
installed.

Implementation notes:
- pytrends is sync (`requests`-based); calls run via `asyncio.to_thread`.
- Anchored scaling: every batch is `[ANCHOR] + up to 4 user keywords`. Google
  rescales 0–100 inside each batch, so anchoring against a fixed term keeps
  scores comparable across batches within this source.
- Per-keyword cadence guard: at most one upstream query per keyword every
  `MIN_QUERY_INTERVAL` seconds — both the 30s discovery and 60s snapshot
  loops call through this guard so traffic to Google stays bounded.
- 429 / rate-limit: log + back off for `RATE_LIMIT_BACKOFF` seconds.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote_plus

from .. import db
from .base import SourcePost

log = logging.getLogger("hn-monitor.sources.googletrends")

ANCHOR = "news"
BATCH_USER_KEYWORDS = 4               # 5 total per pytrends batch (4 + anchor)
TIMEFRAME = "now 1-H"
GEO = "US"
MIN_QUERY_INTERVAL = 5 * 60           # per-keyword cadence guard
RATE_LIMIT_BACKOFF = 10 * 60          # back off this long on 429


class GoogleTrendsSource:
    name = "googletrends"
    label = "Google Trends"
    description = (
        "Per-keyword Google search interest (0–100, US, ~1-min resolution) "
        "via pytrends. Each keyword is a synthetic post; scores are anchored "
        "against a fixed term so they're comparable across keywords."
    )

    def __init__(self) -> None:
        # Lazy import — only construct the client once.
        from pytrends.request import TrendReq  # type: ignore

        self._pytrends = TrendReq(hl="en-US", tz=0, retries=2, backoff_factor=0.2)
        self._last_query_ts: dict[str, float] = {}
        self._rate_limit_until: float = 0.0

    @classmethod
    def from_env(cls) -> "GoogleTrendsSource | None":
        try:
            import pytrends  # noqa: F401
        except ImportError:
            log.info("pytrends not installed; Google Trends source disabled")
            return None
        log.info("Google Trends source enabled (keywords loaded from DB on each tick)")
        return cls()

    def _rate_limited(self) -> bool:
        return self._rate_limit_until and time.monotonic() < self._rate_limit_until

    async def fetch_new_post_ids(self) -> list[str]:
        # IDs ARE the keywords; upsert is idempotent so re-discovering existing
        # ones is fine. Snapshots happen via fetch_posts on the snapshot loop.
        return db.list_google_trends_keywords()

    async def fetch_post(self, source_id: str) -> SourcePost | None:
        out = await self.fetch_posts([source_id])
        return out.get(source_id)

    async def fetch_posts(self, source_ids: list[str]) -> dict[str, SourcePost]:
        if not source_ids:
            return {}

        # During backoff, return stubs (score=None) so discovery still creates
        # post rows and the snapshot loop can refresh them later. The
        # snapshot/discovery jobs both skip inserts when score isn't an int.
        if self._rate_limited():
            return {kw: self._stub(kw) for kw in source_ids}

        now_mono = time.monotonic()
        due = [
            kw for kw in source_ids
            if (now_mono - self._last_query_ts.get(kw, 0.0)) >= MIN_QUERY_INTERVAL
        ]
        if not due:
            # All keywords queried recently — let snapshot_job's heartbeat
            # decide whether to write a row from the existing latest score.
            return {}

        out: dict[str, SourcePost] = {}
        for start in range(0, len(due), BATCH_USER_KEYWORDS):
            chunk = due[start:start + BATCH_USER_KEYWORDS]
            try:
                values = await asyncio.to_thread(self._query_batch, chunk)
            except _RateLimited:
                self._rate_limit_until = time.monotonic() + RATE_LIMIT_BACKOFF
                log.warning(
                    "[googletrends] rate-limited; backing off %.0fs",
                    RATE_LIMIT_BACKOFF,
                )
                # Backfill stubs for all requested ids so callers (esp. discovery)
                # still create rows for fresh keywords.
                for kw in source_ids:
                    out.setdefault(kw, self._stub(kw))
                return out
            except Exception as e:
                log.warning("[googletrends] query %s failed: %s", chunk, e)
                continue

            now_mono = time.monotonic()
            for kw in chunk:
                self._last_query_ts[kw] = now_mono
                series = values.get(kw)
                if not series:
                    continue
                latest_value = series[-1][1]
                out[kw] = SourcePost(
                    source_id=kw,
                    title=kw,
                    url=f"https://trends.google.com/trends/explore?q={quote_plus(kw)}&geo={GEO}",
                    author=None,
                    posted_ts=None,  # active-window stays open via first_seen refresh
                    score=int(round(latest_value)),
                    dead=False,
                    history=series,
                )
        return out

    def _stub(self, kw: str) -> SourcePost:
        return SourcePost(
            source_id=kw,
            title=kw,
            url=f"https://trends.google.com/trends/explore?q={quote_plus(kw)}&geo={GEO}",
            author=None,
            posted_ts=None,
            score=None,
            dead=False,
        )

    def _query_batch(self, keywords: list[str]) -> dict[str, list[tuple[int, int]]]:
        """Sync pytrends call. Returns {keyword: [(unix_ts, score), ...]} —
        the full sliding-window series pytrends gives us (typically ~60
        per-minute samples for `now 1-H`). Empty if no data for that keyword.

        Raises `_RateLimited` on 429-equivalent so the caller can back off.
        """
        from pytrends.exceptions import TooManyRequestsError  # type: ignore

        kw_list = [ANCHOR] + keywords
        try:
            self._pytrends.build_payload(
                kw_list=kw_list, cat=0, timeframe=TIMEFRAME, geo=GEO, gprop=""
            )
            df = self._pytrends.interest_over_time()
        except TooManyRequestsError as e:
            raise _RateLimited() from e
        except Exception as e:
            # urllib3 hits its retry ceiling on 429s and surfaces a
            # MaxRetryError("too many 429 error responses") wrapped in
            # requests.exceptions.RetryError — pytrends doesn't translate
            # that, so we sniff it out of the message ourselves.
            msg = str(e)
            if "429" in msg or "too many 429" in msg.lower():
                raise _RateLimited() from e
            raise

        if df is None or df.empty:
            return {}

        # Drop the partial (in-progress) sample if present so the latest
        # value reflects a complete bucket.
        if "isPartial" in df.columns:
            complete = df[df["isPartial"] == False]  # noqa: E712
            row_df = complete if not complete.empty else df
        else:
            row_df = df

        # Normalize the timestamp index to unix-second integers.
        timestamps = [int(ts.timestamp()) for ts in row_df.index]
        out: dict[str, list[tuple[int, int]]] = {}
        for kw in keywords:
            if kw not in row_df.columns:
                continue
            series: list[tuple[int, int]] = []
            for ts, v in zip(timestamps, row_df[kw].tolist()):
                try:
                    series.append((ts, int(round(float(v)))))
                except (TypeError, ValueError):
                    continue
            if series:
                out[kw] = series
        return out

    async def close(self) -> None:
        # pytrends has no persistent connection to close.
        return None


class _RateLimited(Exception):
    """Internal signal that a pytrends call hit a 429."""
