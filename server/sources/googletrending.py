"""Google Trends "Trending Now" source.

Discovery and snapshot both come from Google's `batchexecute` endpoint that
powers https://trends.google.com/trending. Each trending item already has a
`search_volume` integer (10000, 50000, 100000, ...) which we use directly as
the score — no per-item Trends queries needed.

The response is a Google "wrb.fr" envelope with the XSSI `)]}'` prefix; the
inner payload for rpc id `i0OFE` is a JSON string we have to parse twice.

Each trend carries a list of numeric category ids (e.g. `[17]` Sports). The
user picks which categories to track via the UI; items whose category list
doesn't intersect the selection are dropped. With no categories selected the
source produces nothing — there's no "all" mode (way too noisy).

source_id is a slug of the trending title (so the same trend across refetches
maps to the same row). Trends that disappear from the upstream list get
marked dead.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from urllib.parse import quote_plus, urlencode

import httpx

from .. import db
from .base import SourcePost

log = logging.getLogger("hn-monitor.sources.googletrending")

ENDPOINT = "https://trends.google.com/_/TrendsUi/data/batchexecute"
RPC_ID = "i0OFE"
GEO = "US"
HOURS = 24
HL = "en"
MIN_FETCH_INTERVAL = 5 * 60       # cap upstream calls to once per 5 min
RATE_LIMIT_BACKOFF = 10 * 60

# History backfill via pytrends interest_over_time. Runs once per trending
# item so the chart shows ~60 minutes of curve immediately instead of
# accumulating one point per snapshot. Series is scaled so the most recent
# interest sample maps to the current search_volume — i.e. the rightmost
# point on the chart matches the score on the card, and earlier points are
# proportional.
PYTRENDS_BATCH = 5                 # pytrends max keywords per build_payload
PYTRENDS_TIMEFRAME = "now 1-H"
PYTRENDS_RATE_LIMIT_BACKOFF = 10 * 60
PYTRENDS_MAX_BATCHES_PER_TICK = 1  # spread backfill across snapshot ticks
                                   # so a big selection doesn't burst-trip 429
                                   # — rest of items get filled on later ticks

# i0OFE args — observed working for Trending Now: [null, null, geo, 0, lang, hours, 1].
# The 4th arg appears to be a server-side category filter (0 = all); we leave
# it at 0 and post-filter on the per-item `cats` list so multi-category
# selection works in a single fetch.
def _build_payload(geo: str, hours: int, hl: str) -> dict[str, str]:
    inner = json.dumps([None, None, geo, 0, hl, hours, 1], separators=(",", ":"))
    f_req = json.dumps([[[RPC_ID, inner, None, "generic"]]], separators=(",", ":"))
    return {"f.req": f_req}


# Best-effort label catalog. The numeric ids come from Google's Trending Now
# API; labels are based on observed category assignments. Unknown ids that
# appear in the data still work for filtering — they just render as
# "Cat <n>" in the UI.
CATEGORY_CATALOG: list[tuple[int, str]] = [
    (1, "Autos & Vehicles"),
    (2, "Beauty & Fashion"),
    (3, "Business & Finance"),
    (4, "Entertainment"),
    (5, "Food & Drink"),
    (6, "Games"),
    (7, "Health"),
    (8, "Hobbies & Leisure"),
    (9, "Jobs & Education"),
    (10, "Law & Government"),
    (11, "News"),
    (12, "Other"),
    (13, "Pets & Animals"),
    (14, "Politics"),
    (15, "Science"),
    (16, "Shopping"),
    (17, "Sports"),
    (18, "Technology"),
    (19, "Travel & Transportation"),
    (20, "Climate"),
]
CATEGORY_LABELS: dict[int, str] = dict(CATEGORY_CATALOG)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(title: str) -> str:
    s = _SLUG_RE.sub("-", title.lower()).strip("-")
    return s[:100] or "untitled"


def _parse_response(body: str) -> list[dict]:
    """Parse the batchexecute envelope and return a list of trend dicts.

    Each dict has: title, search_volume, growth, related, cats, started_ts.
    """
    # Strip the XSSI prefix `)]}'` and surrounding whitespace.
    body = body.lstrip(")]}'\n ")
    outer = json.loads(body)
    inner_json: str | None = None
    for env in outer:
        if isinstance(env, list) and len(env) >= 3 and env[0] == "wrb.fr" and env[1] == RPC_ID:
            inner_json = env[2]
            break
    if not inner_json:
        return []
    data = json.loads(inner_json)
    # data shape observed: [null, [<trend>, <trend>, ...]]
    trends = data[1] if isinstance(data, list) and len(data) >= 2 else []
    out: list[dict] = []
    for t in trends:
        if not isinstance(t, list) or not t:
            continue
        try:
            title = t[0]
            if not isinstance(title, str) or not title.strip():
                continue
            started = (t[3] or [None])[0] if len(t) > 3 and isinstance(t[3], list) else None
            volume = t[6] if len(t) > 6 and isinstance(t[6], int) else None
            growth = t[8] if len(t) > 8 and isinstance(t[8], int) else None
            related = t[9] if len(t) > 9 and isinstance(t[9], list) else []
            cats = t[10] if len(t) > 10 and isinstance(t[10], list) else []
        except Exception:
            continue
        out.append({
            "title": title.strip(),
            "search_volume": volume,
            "growth_pct": growth,
            "related": [r for r in related if isinstance(r, str)],
            "cats": [int(c) for c in cats if isinstance(c, int)],
            "started_ts": int(started) if isinstance(started, int) else None,
        })
    return out


class GoogleTrendingSource:
    name = "googletrending"
    label = "Google Trending"
    description = (
        "Currently trending Google searches in selected categories (US, last 24h). "
        "Score is Google's reported search volume; cards refresh every ~5 min. "
        "Pick categories from the header — empty selection = source idle."
    )

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._cache: list[dict] = []         # last raw response
        self._cache_ts: float = 0.0
        self._cache_lock = asyncio.Lock()
        self._rate_limit_until: float = 0.0
        self._known_slugs: set[str] = set()  # slugs seen in most-recent fetch
        # pytrends-backed one-time history backfill state (per trending slug).
        self._pytrends = None
        self._pytrends_rate_limit_until: float = 0.0
        self._history_done: set[str] = set()

    @classmethod
    def from_env(cls) -> "GoogleTrendingSource | None":
        # Public endpoint, no auth needed; always enabled.
        log.info("Google Trending source enabled (categories loaded from DB on each tick)")
        return cls()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=5.0),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                headers={
                    "User-Agent": "Mozilla/5.0 (boost-topic-monitor)",
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                },
            )
        return self._client

    def _rate_limited(self) -> bool:
        return self._rate_limit_until and time.monotonic() < self._rate_limit_until

    async def _refresh_cache_if_due(self) -> None:
        if self._rate_limited():
            return
        async with self._cache_lock:
            if time.monotonic() - self._cache_ts < MIN_FETCH_INTERVAL and self._cache:
                return
            client = await self._get_client()
            params = {"rpcids": RPC_ID, "source-path": "/trending", "hl": HL}
            payload = _build_payload(GEO, HOURS, HL)
            try:
                r = await client.post(
                    f"{ENDPOINT}?{urlencode(params)}",
                    data=payload,
                )
            except Exception as e:
                log.warning("[googletrending] fetch failed: %s", e)
                return
            if r.status_code == 429:
                self._rate_limit_until = time.monotonic() + RATE_LIMIT_BACKOFF
                log.warning(
                    "[googletrending] rate-limited; backing off %.0fs",
                    RATE_LIMIT_BACKOFF,
                )
                return
            if r.status_code != 200:
                log.warning("[googletrending] HTTP %d: %s", r.status_code, r.text[:200])
                return
            try:
                trends = _parse_response(r.text)
            except Exception as e:
                log.warning("[googletrending] parse failed: %s", e)
                return
            self._cache = trends
            self._cache_ts = time.monotonic()
            log.info("[googletrending] fetched %d trending items", len(trends))

    def _selected_categories(self) -> set[int]:
        return set(db.list_google_trending_categories())

    def _filter_for_categories(self, trends: list[dict], cats: set[int]) -> list[dict]:
        if not cats:
            return []
        out = []
        for t in trends:
            if any(c in cats for c in t.get("cats", [])):
                out.append(t)
        return out

    def _to_post(self, t: dict) -> SourcePost:
        title = t["title"]
        slug = _slug(title)
        # Show a category hint in the title for quick scan.
        cat_labels = [CATEGORY_LABELS.get(c, f"Cat {c}") for c in t.get("cats", [])]
        author = ", ".join(cat_labels) if cat_labels else None
        return SourcePost(
            source_id=slug,
            title=title,
            url=f"https://www.google.com/search?q={quote_plus(title)}",
            author=author,
            posted_ts=t.get("started_ts"),
            score=t.get("search_volume"),
            dead=False,
        )

    async def fetch_new_post_ids(self) -> list[str]:
        await self._refresh_cache_if_due()
        cats = self._selected_categories()
        if not cats or not self._cache:
            return []
        filtered = self._filter_for_categories(self._cache, cats)
        slugs = [_slug(t["title"]) for t in filtered]
        self._known_slugs = set(slugs)
        return slugs

    async def fetch_post(self, source_id: str) -> SourcePost | None:
        out = await self.fetch_posts([source_id])
        return out.get(source_id)

    async def fetch_posts(self, source_ids: list[str]) -> dict[str, SourcePost]:
        if not source_ids:
            return {}
        await self._refresh_cache_if_due()
        cats = self._selected_categories()
        wanted = set(source_ids)
        out: dict[str, SourcePost] = {}
        seen_slugs: set[str] = set()
        slug_to_trend: dict[str, dict] = {}
        for t in self._cache:
            slug = _slug(t["title"])
            if slug not in wanted:
                continue
            if cats and not any(c in cats for c in t.get("cats", [])):
                continue
            seen_slugs.add(slug)
            out[slug] = self._to_post(t)
            slug_to_trend[slug] = t
        # Items the snapshot loop asked about but that have rotated out of
        # Trending Now: mark them dead so they stop getting refreshed.
        for sid in wanted - seen_slugs:
            out[sid] = SourcePost(
                source_id=sid,
                title=sid,
                url=None,
                author=None,
                posted_ts=None,
                score=None,
                dead=True,
            )
        await self._backfill_history(out, slug_to_trend)
        return out

    async def _backfill_history(
        self,
        out: dict[str, SourcePost],
        slug_to_trend: dict[str, dict],
    ) -> None:
        """One-time pytrends interest_over_time backfill for fresh trending
        items. Scales the 0–100 series so the most recent sample maps to the
        item's reported search_volume. Mutates `out` in place.
        """
        if (
            self._pytrends_rate_limit_until
            and time.monotonic() < self._pytrends_rate_limit_until
        ):
            return
        # Items we haven't backfilled yet, with a usable volume to scale to.
        pending: list[tuple[str, str, int]] = []
        for slug, sp in out.items():
            if sp.dead or slug in self._history_done:
                continue
            t = slug_to_trend.get(slug)
            if not t:
                continue
            volume = t.get("search_volume")
            if not isinstance(volume, int) or volume <= 0:
                continue
            pending.append((slug, t["title"], volume))
        if not pending:
            return
        # Skip items that already have lots of snapshots — they were
        # backfilled in a previous server lifetime, no need to spend pytrends
        # queries again. The (post_id, ts) PK would dedupe anyway, this just
        # avoids the upstream call.
        existing_counts = db.snapshot_counts_by_source_id(
            self.name, [slug for slug, _, _ in pending]
        )
        filtered: list[tuple[str, str, int]] = []
        for slug, title, volume in pending:
            if existing_counts.get(slug, 0) >= 30:
                self._history_done.add(slug)
                continue
            filtered.append((slug, title, volume))
        pending = filtered
        if not pending:
            return
        if self._pytrends is None:
            try:
                from pytrends.request import TrendReq  # type: ignore
                self._pytrends = TrendReq(hl="en-US", tz=0, retries=2, backoff_factor=0.2)
            except ImportError:
                log.info("[googletrending] pytrends not available; skipping history backfill")
                # Mark these done so we don't retry on every tick.
                for slug, _, _ in pending:
                    self._history_done.add(slug)
                return

        max_items = PYTRENDS_BATCH * PYTRENDS_MAX_BATCHES_PER_TICK
        for start in range(0, min(len(pending), max_items), PYTRENDS_BATCH):
            chunk = pending[start:start + PYTRENDS_BATCH]
            titles = [title for _, title, _ in chunk]
            try:
                series_by_title = await asyncio.to_thread(
                    self._pytrends_query, titles
                )
            except _PytrendsRateLimited:
                self._pytrends_rate_limit_until = (
                    time.monotonic() + PYTRENDS_RATE_LIMIT_BACKOFF
                )
                log.warning(
                    "[googletrending] pytrends rate-limited; backing off %.0fs",
                    PYTRENDS_RATE_LIMIT_BACKOFF,
                )
                return
            except Exception as e:
                log.warning("[googletrending] pytrends query %s failed: %s", titles, e)
                continue
            for slug, title, volume in chunk:
                series = series_by_title.get(title)
                if not series:
                    # No useful data — mark done to avoid re-querying every tick.
                    self._history_done.add(slug)
                    continue
                latest_interest = next(
                    (v for ts, v in reversed(series) if v > 0), 0
                )
                if latest_interest <= 0:
                    self._history_done.add(slug)
                    continue
                scale = volume / latest_interest
                scaled = [(ts, max(0, int(round(v * scale)))) for ts, v in series]
                out[slug].history = scaled
                self._history_done.add(slug)

    def _pytrends_query(self, titles: list[str]) -> dict[str, list[tuple[int, int]]]:
        """Sync pytrends call. Returns {title: [(ts, raw_interest), ...]}.

        Uses no anchor — each batch of up to 5 trending titles competes
        against itself, so each gets a 0–100 curve internally. Raises
        `_PytrendsRateLimited` on 429-equivalent.
        """
        from pytrends.exceptions import TooManyRequestsError  # type: ignore

        try:
            self._pytrends.build_payload(
                kw_list=titles, cat=0, timeframe=PYTRENDS_TIMEFRAME, geo=GEO, gprop=""
            )
            df = self._pytrends.interest_over_time()
        except TooManyRequestsError as e:
            raise _PytrendsRateLimited() from e
        except Exception as e:
            msg = str(e)
            if "429" in msg or "too many 429" in msg.lower():
                raise _PytrendsRateLimited() from e
            raise

        if df is None or df.empty:
            return {}
        if "isPartial" in df.columns:
            complete = df[df["isPartial"] == False]  # noqa: E712
            row_df = complete if not complete.empty else df
        else:
            row_df = df
        timestamps = [int(ts.timestamp()) for ts in row_df.index]
        out: dict[str, list[tuple[int, int]]] = {}
        for title in titles:
            if title not in row_df.columns:
                continue
            series: list[tuple[int, int]] = []
            for ts, v in zip(timestamps, row_df[title].tolist()):
                try:
                    series.append((ts, int(round(float(v)))))
                except (TypeError, ValueError):
                    continue
            if series:
                out[title] = series
        return out

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class _PytrendsRateLimited(Exception):
    """Internal signal that a pytrends call hit a 429."""
