"""Background jobs: discovery, snapshot. Iterate over all registered sources."""
from __future__ import annotations

import asyncio
import logging
import time

from . import db, forecast, sources
from .sources.base import Source, SourcePost

log = logging.getLogger("hn-monitor")

ACTIVE_WINDOW_SECONDS = 24 * 3600
HEARTBEAT_SECONDS = 5 * 60


async def _fetch_many(source: Source, ids: list[str]) -> dict[str, SourcePost]:
    """Fetch many posts at once. Uses source.fetch_posts() if present, else
    falls back to parallel per-id fetch_post() calls."""
    if not ids:
        return {}
    fetch_posts = getattr(source, "fetch_posts", None)
    if callable(fetch_posts):
        try:
            out = await fetch_posts(ids)
        except Exception as e:
            log.warning("[%s] fetch_posts batch failed: %s", source.name, e)
            return {}
        return {k: v for k, v in (out or {}).items() if isinstance(v, SourcePost)}
    items = await asyncio.gather(
        *(source.fetch_post(i) for i in ids), return_exceptions=True
    )
    return {
        ids[i]: it for i, it in enumerate(items) if isinstance(it, SourcePost)
    }


async def discovery_job() -> None:
    await asyncio.gather(
        *(_discover_one(s) for s in sources.get_sources()),
        return_exceptions=False,
    )


async def _discover_one(source: Source) -> None:
    try:
        ids = await source.fetch_new_post_ids()
    except Exception as e:
        log.warning("[%s] discovery: failed to fetch new ids: %s", source.name, e)
        return
    if not ids:
        return

    known = db.known_source_ids(source.name)
    new_ids = [i for i in ids if i not in known]
    if not new_ids:
        return

    log.info("[%s] discovery: %d new posts", source.name, len(new_ids))
    items_by_id = await _fetch_many(source, new_ids)
    now = int(time.time())
    for item in items_by_id.values():
        post_id = db.upsert_post(source.name, item)
        if item.history:
            db.insert_snapshots(post_id, item.history)
        if isinstance(item.score, int):
            db.insert_snapshot(post_id, now, item.score)


async def snapshot_job() -> None:
    now = int(time.time())
    for source in sources.get_sources():
        await _snapshot_one(source, now)


async def _snapshot_one(source: Source, now: int) -> None:
    posts = db.active_posts(source.name, now, ACTIVE_WINDOW_SECONDS)
    if not posts:
        return

    ids = [p["source_id"] for p in posts]
    items_by_id = await _fetch_many(source, ids)
    written = 0
    for p in posts:
        item = items_by_id.get(p["source_id"])
        if item is None:
            continue
        if item.dead:
            db.mark_dead(p["id"])
            continue
        if item.history:
            db.insert_snapshots(p["id"], item.history)
        if not isinstance(item.score, int):
            continue
        prev = db.latest_score(p["id"])
        prev_ts = db.latest_snapshot_ts(p["id"]) or 0
        if prev == item.score and (now - prev_ts) < HEARTBEAT_SECONDS:
            continue
        db.insert_snapshot(p["id"], now, item.score)
        written += 1
    if written:
        log.info("[%s] snapshot: wrote %d rows across %d active posts",
                 source.name, written, len(posts))
