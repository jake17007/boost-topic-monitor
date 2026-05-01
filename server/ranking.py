"""Sort orders for the feed.

Each ranker is a function feed_item -> float. Higher = better. The feed
endpoint applies one of these and sorts descending; ties break on
latest_score then posted_ts.

The feed_item is the dict returned by db.feed() enriched with `forecast`:
  - series:   [[ts, score], ...] (oldest first)
  - forecast: [[ts, score], ...] (future, may be empty)
  - latest_score, posted_ts, first_seen, source, ...
"""
from __future__ import annotations

import time
from typing import Callable

Ranker = Callable[[dict], float]


def _score_at(series: list, ts: int) -> float | None:
    """Forward-fill: return the score at time `ts` from a sorted series."""
    if not series:
        return None
    # Series is oldest first.
    if ts < series[0][0]:
        return None
    last = float(series[0][1])
    for s_ts, s_score in series:
        if s_ts <= ts:
            last = float(s_score)
        else:
            break
    return last


def _velocity_over(item: dict, t0: int, t1: int) -> float:
    """Score change rate (score/min) over [t0, t1]."""
    series = item.get("series") or []
    s0 = _score_at(series, t0)
    s1 = _score_at(series, t1)
    if s0 is None or s1 is None:
        return 0.0
    span_min = max(1.0, (t1 - t0) / 60.0)
    return (s1 - s0) / span_min


def rank_top(item: dict) -> float:
    s = item.get("latest_score")
    return float(s) if s is not None else float("-inf")


def rank_hot(item: dict) -> float:
    """HN's classic formula: (score - 1) / (age_hours + 2)^1.8."""
    score = item.get("latest_score") or 0
    posted = item.get("posted_ts") or item.get("first_seen") or int(time.time())
    age_h = max(0.0, (int(time.time()) - posted) / 3600.0)
    return (max(score, 0) - 1) / (age_h + 2) ** 1.8


def rank_velocity(item: dict) -> float:
    """Score change per minute over the last 10 minutes."""
    now = int(time.time())
    return _velocity_over(item, now - 600, now)


def rank_rising(item: dict) -> float:
    """TimesFM-predicted absolute gain over the next horizon."""
    forecast = item.get("forecast") or []
    score = item.get("latest_score")
    if not forecast or score is None:
        return 0.0
    return float(forecast[-1][1]) - float(score)


def rank_trending(item: dict) -> float:
    """Acceleration: velocity(last 5 min) - velocity(5-10 min ago).

    Positive => growth is accelerating (possibly going viral). Posts younger
    than ~10 min lack enough history for a meaningful prior window and rank
    near zero.
    """
    now = int(time.time())
    recent = _velocity_over(item, now - 300, now)
    prior = _velocity_over(item, now - 600, now - 300)
    return recent - prior


RANKERS: dict[str, Ranker] = {
    "top": rank_top,
    "hot": rank_hot,
    "velocity": rank_velocity,
    "rising": rank_rising,
    "trending": rank_trending,
}


def get(name: str | None) -> Ranker:
    return RANKERS.get(name or "top", rank_top)


def attach_ranks(items: list[dict]) -> None:
    """Compute every ranker and attach as `ranks: {name: value}` on each item."""
    for it in items:
        ranks: dict[str, float] = {}
        for name, fn in RANKERS.items():
            try:
                ranks[name] = float(fn(it))
            except Exception:
                ranks[name] = 0.0
        it["ranks"] = ranks


def sort_items(items: list[dict], sort: str | None) -> list[dict]:
    """Compute all ranks (so the UI can show them) and sort by the chosen one."""
    attach_ranks(items)
    name = sort if sort in RANKERS else "top"
    return sorted(
        items,
        key=lambda it: (
            it["ranks"].get(name, 0.0),
            it.get("latest_score") or 0,
            it.get("posted_ts") or it.get("first_seen") or 0,
        ),
        reverse=True,
    )
