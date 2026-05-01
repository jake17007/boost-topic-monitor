"""SQLite storage. Internal `posts.id` is an autoincrement integer; per-source
identity is `(source, source_id)`. Snapshots reference internal ids only.
"""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from .sources.base import SourcePost

DB_PATH = Path(__file__).resolve().parent.parent / "data.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL,
  source_id   TEXT NOT NULL,
  title       TEXT,
  url         TEXT,
  author      TEXT,
  posted_ts   INTEGER,
  first_seen  INTEGER NOT NULL,
  dead        INTEGER NOT NULL DEFAULT 0,
  UNIQUE(source, source_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
  post_id  INTEGER NOT NULL REFERENCES posts(id),
  ts       INTEGER NOT NULL,
  score    INTEGER NOT NULL,
  PRIMARY KEY (post_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_post_ts ON snapshots(post_id, ts);
CREATE INDEX IF NOT EXISTS idx_posts_posted_ts ON posts(posted_ts);
CREATE INDEX IF NOT EXISTS idx_posts_source ON posts(source);
"""


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def known_source_ids(source: str) -> set[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT source_id FROM posts WHERE source = ?", (source,)
        ).fetchall()
    return {r["source_id"] for r in rows}


def upsert_post(source: str, post: SourcePost) -> int:
    """Insert or update a post and return its internal id."""
    now = int(time.time())
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO posts (source, source_id, title, url, author, posted_ts, first_seen, dead)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_id) DO UPDATE SET
              title = excluded.title,
              url = excluded.url,
              author = excluded.author,
              posted_ts = excluded.posted_ts,
              dead = excluded.dead
            """,
            (
                source,
                post.source_id,
                post.title,
                post.url,
                post.author,
                post.posted_ts,
                now,
                1 if post.dead else 0,
            ),
        )
        row = conn.execute(
            "SELECT id FROM posts WHERE source = ? AND source_id = ?",
            (source, post.source_id),
        ).fetchone()
    return int(row["id"])


def mark_dead(post_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE posts SET dead = 1 WHERE id = ?", (post_id,))


def latest_score(post_id: int) -> int | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT score FROM snapshots WHERE post_id = ? ORDER BY ts DESC LIMIT 1",
            (post_id,),
        ).fetchone()
    return row["score"] if row else None


def latest_snapshot_ts(post_id: int) -> int | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT ts FROM snapshots WHERE post_id = ? ORDER BY ts DESC LIMIT 1",
            (post_id,),
        ).fetchone()
    return row["ts"] if row else None


def insert_snapshot(post_id: int, ts: int, score: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO snapshots (post_id, ts, score) VALUES (?, ?, ?)",
            (post_id, ts, score),
        )


def active_posts(source: str | None, now: int, max_age_seconds: int) -> list[dict]:
    """Return active (alive, in-window) posts, optionally filtered by source.

    Each row includes both the internal id and the source-specific id, since
    callers will typically need both (DB writes vs. external fetch).
    """
    cutoff = now - max_age_seconds
    sql = """
        SELECT id, source, source_id, title
        FROM posts
        WHERE dead = 0
          AND COALESCE(posted_ts, first_seen) >= ?
    """
    params: list[object] = [cutoff]
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY COALESCE(posted_ts, first_seen) DESC"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def active_post_ids(now: int, max_age_seconds: int) -> list[int]:
    """Internal ids of all active posts across sources (used by forecasting)."""
    return [r["id"] for r in active_posts(None, now, max_age_seconds)]


def recent_posts(window_seconds: int) -> list[dict]:
    """Posts we've been tracking in the last `window_seconds`.

    Filters by `first_seen` so PH's day-boundary timestamps don't get clipped
    by short windows; `posted_ts` is preserved for display.
    """
    cutoff = int(time.time()) - window_seconds
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
              p.id, p.source, p.source_id, p.title, p.url, p.author,
              p.posted_ts, p.first_seen, p.dead,
              (SELECT score FROM snapshots s
                 WHERE s.post_id = p.id ORDER BY s.ts DESC LIMIT 1) AS latest_score,
              (SELECT COUNT(*) FROM snapshots s WHERE s.post_id = p.id) AS snapshot_count
            FROM posts p
            WHERE p.first_seen >= ?
            ORDER BY p.first_seen DESC
            """,
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def series_for_ids(ids: Iterable[int]) -> dict[int, list[tuple[int, int]]]:
    ids = list(ids)
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT post_id, ts, score FROM snapshots
            WHERE post_id IN ({placeholders})
            ORDER BY post_id, ts
            """,
            ids,
        ).fetchall()
    out: dict[int, list[tuple[int, int]]] = {i: [] for i in ids}
    for r in rows:
        out[r["post_id"]].append((r["ts"], r["score"]))
    return out


def feed(
    window_seconds: int,
    sources: list[str] | None = None,
    candidates_max: int = 300,
) -> list[dict]:
    """Posts in the window with snapshot series attached. Pass `sources=None`
    or an empty list for all sources; otherwise filter to those names.
    """
    posts = recent_posts(window_seconds)
    if sources:
        allowed = set(sources)
        posts = [p for p in posts if p["source"] in allowed]
    posts = posts[:candidates_max]
    if not posts:
        return []
    series = series_for_ids(p["id"] for p in posts)
    for p in posts:
        p["series"] = series.get(p["id"], [])
    return posts
