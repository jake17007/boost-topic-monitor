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
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source        TEXT NOT NULL,
  source_id     TEXT NOT NULL,
  title         TEXT,
  url           TEXT,
  author        TEXT,
  posted_ts     INTEGER,
  first_seen    INTEGER NOT NULL,
  dead          INTEGER NOT NULL DEFAULT 0,
  thumbnail_url TEXT,
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

CREATE TABLE IF NOT EXISTS forecasts (
  post_id        INTEGER PRIMARY KEY REFERENCES posts(id),
  computed_at    INTEGER NOT NULL,
  last_input_ts  INTEGER NOT NULL,
  points         TEXT NOT NULL    -- JSON array of [ts, score]
);

CREATE TABLE IF NOT EXISTS x_handles (
  handle    TEXT PRIMARY KEY,
  added_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bluesky_keywords (
  keyword   TEXT PRIMARY KEY,
  added_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS google_trends_keywords (
  keyword   TEXT PRIMARY KEY,
  added_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS google_trending_categories (
  category_id  INTEGER PRIMARY KEY,
  added_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS instagram_handles (
  handle    TEXT PRIMARY KEY,
  added_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reddit_subreddits (
  subreddit TEXT PRIMARY KEY,
  added_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rss_feeds (
  url       TEXT PRIMARY KEY,
  added_at  INTEGER NOT NULL
);
"""


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # Idempotent column adds for pre-existing databases.
        try:
            conn.execute("ALTER TABLE posts ADD COLUMN thumbnail_url TEXT")
        except sqlite3.OperationalError:
            pass  # already exists


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
    """Source_ids we've seen for this source AND haven't marked dead.

    Dead items are excluded so sources can revive them — relevant for
    googletrending, where a slug rotates off the trending list (marked
    dead) and later returns. Other sources rarely re-emit dead ids in
    discovery, so the change is a no-op for them.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT source_id FROM posts WHERE source = ? AND dead = 0", (source,)
        ).fetchall()
    return {r["source_id"] for r in rows}


def upsert_post(source: str, post: SourcePost) -> int:
    """Insert or update a post and return its internal id."""
    now = int(time.time())
    # google_trends (keyword) and googletrending (category) are long-lived
    # monitoring targets, not discrete posts — refresh first_seen on
    # re-discovery so they stay inside the 24h active-window used by
    # active_posts() / recent_posts().
    refresh_first_seen = (
        "first_seen = excluded.first_seen,"
        if source in ("googletrends", "googletrending") else ""
    )
    with connect() as conn:
        conn.execute(
            f"""
            INSERT INTO posts (source, source_id, title, url, author, posted_ts, first_seen, dead, thumbnail_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_id) DO UPDATE SET
              title = excluded.title,
              url = excluded.url,
              author = excluded.author,
              posted_ts = excluded.posted_ts,
              {refresh_first_seen}
              dead = excluded.dead,
              thumbnail_url = COALESCE(excluded.thumbnail_url, posts.thumbnail_url)
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
                post.thumbnail_url,
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


def update_thumbnail_if_missing(post_id: int, thumbnail_url: str) -> None:
    """Backfill thumbnail_url on existing rows that don't have one yet."""
    with connect() as conn:
        conn.execute(
            "UPDATE posts SET thumbnail_url = ? WHERE id = ? AND thumbnail_url IS NULL",
            (thumbnail_url, post_id),
        )


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


def insert_snapshots(post_id: int, points: list[tuple[int, int]]) -> None:
    """Bulk insert; deduped by the (post_id, ts) primary key."""
    if not points:
        return
    with connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO snapshots (post_id, ts, score) VALUES (?, ?, ?)",
            [(post_id, int(ts), int(score)) for ts, score in points],
        )


def snapshot_counts_by_source_id(source: str, source_ids: list[str]) -> dict[str, int]:
    """For each (source, source_id), return the snapshot count. Missing
    source_ids map to 0. Used by backfill paths to skip items that already
    have substantial history.
    """
    if not source_ids:
        return {}
    placeholders = ",".join("?" * len(source_ids))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT p.source_id, COUNT(s.ts) AS n
            FROM posts p LEFT JOIN snapshots s ON s.post_id = p.id
            WHERE p.source = ? AND p.source_id IN ({placeholders})
            GROUP BY p.source_id
            """,
            [source, *source_ids],
        ).fetchall()
    out = {sid: 0 for sid in source_ids}
    for r in rows:
        out[r["source_id"]] = int(r["n"])
    return out


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
    """Posts whose creation time falls inside `window_seconds`.

    Uses the post's actual `posted_ts` (when it was published on the source),
    falling back to `first_seen` for sources that don't have a meaningful
    posted_ts (e.g. Google Trends keywords). Dead posts are excluded — for
    HN/Bluesky/X/PH/Instagram that means deleted/removed content, for
    googletrending it means terms that rotated off the trending list or no
    longer match the user's selected categories.
    """
    cutoff = int(time.time()) - window_seconds
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
              p.id, p.source, p.source_id, p.title, p.url, p.author,
              p.posted_ts, p.first_seen, p.dead, p.thumbnail_url,
              (SELECT score FROM snapshots s
                 WHERE s.post_id = p.id ORDER BY s.ts DESC LIMIT 1) AS latest_score,
              (SELECT COUNT(*) FROM snapshots s WHERE s.post_id = p.id) AS snapshot_count
            FROM posts p
            WHERE COALESCE(p.posted_ts, p.first_seen) >= ? AND p.dead = 0
            ORDER BY COALESCE(p.posted_ts, p.first_seen) DESC
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


# --- x handles ---

def list_x_handles() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT handle FROM x_handles ORDER BY added_at ASC"
        ).fetchall()
    return [r["handle"] for r in rows]


def set_x_handles(handles: list[str]) -> None:
    """Replace the entire list of handles. Lower-cases and de-dupes."""
    cleaned = []
    seen = set()
    now = int(time.time())
    for h in handles:
        h = (h or "").strip().lstrip("@").lower()
        if not h or h in seen:
            continue
        seen.add(h)
        cleaned.append(h)
    with connect() as conn:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM x_handles")
        for h in cleaned:
            conn.execute(
                "INSERT INTO x_handles (handle, added_at) VALUES (?, ?)",
                (h, now),
            )
        conn.execute("COMMIT")


# --- instagram handles ---

def list_instagram_handles() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT handle FROM instagram_handles ORDER BY added_at ASC"
        ).fetchall()
    return [r["handle"] for r in rows]


def set_instagram_handles(handles: list[str]) -> None:
    """Replace the entire list of handles. Lower-cases, strips @, de-dupes."""
    cleaned = []
    seen = set()
    now = int(time.time())
    for h in handles:
        h = (h or "").strip().lstrip("@").lower()
        if not h or h in seen:
            continue
        seen.add(h)
        cleaned.append(h)
    with connect() as conn:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM instagram_handles")
        for h in cleaned:
            conn.execute(
                "INSERT INTO instagram_handles (handle, added_at) VALUES (?, ?)",
                (h, now),
            )
        conn.execute("COMMIT")


# --- reddit subreddits ---

def list_reddit_subreddits() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT subreddit FROM reddit_subreddits ORDER BY added_at ASC"
        ).fetchall()
    return [r["subreddit"] for r in rows]


def set_reddit_subreddits(subreddits: list[str]) -> None:
    """Replace the entire list. Strips r/ prefix and de-dupes (case-insensitive)."""
    cleaned = []
    seen = set()
    now = int(time.time())
    for s in subreddits:
        s = (s or "").strip()
        if s.startswith("/"):
            s = s.lstrip("/")
        if s.lower().startswith("r/"):
            s = s[2:]
        s = s.strip("/")
        key = s.lower()
        if not s or key in seen:
            continue
        seen.add(key)
        cleaned.append(s)
    with connect() as conn:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM reddit_subreddits")
        for s in cleaned:
            conn.execute(
                "INSERT INTO reddit_subreddits (subreddit, added_at) VALUES (?, ?)",
                (s, now),
            )
        conn.execute("COMMIT")


def seed_reddit_subreddits_if_empty(defaults: list[str]) -> None:
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM reddit_subreddits").fetchone()[0]
    if n == 0 and defaults:
        set_reddit_subreddits(defaults)


# --- rss feeds ---

def list_rss_feeds() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT url FROM rss_feeds ORDER BY added_at ASC"
        ).fetchall()
    return [r["url"] for r in rows]


def set_rss_feeds(urls: list[str]) -> None:
    """Replace the entire list. Trims and de-dupes."""
    cleaned: list[str] = []
    seen: set[str] = set()
    now = int(time.time())
    for u in urls:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        cleaned.append(u)
    with connect() as conn:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM rss_feeds")
        for u in cleaned:
            conn.execute(
                "INSERT INTO rss_feeds (url, added_at) VALUES (?, ?)",
                (u, now),
            )
        conn.execute("COMMIT")


def seed_rss_feeds_if_empty(defaults: list[str]) -> None:
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM rss_feeds").fetchone()[0]
    if n == 0 and defaults:
        set_rss_feeds(defaults)


# --- bluesky keywords ---

def list_bluesky_keywords() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT keyword FROM bluesky_keywords ORDER BY added_at ASC"
        ).fetchall()
    return [r["keyword"] for r in rows]


def set_bluesky_keywords(keywords: list[str]) -> None:
    """Replace the entire list. Lower-cases and de-dupes."""
    cleaned = []
    seen = set()
    now = int(time.time())
    for k in keywords:
        k = (k or "").strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        cleaned.append(k)
    with connect() as conn:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM bluesky_keywords")
        for k in cleaned:
            conn.execute(
                "INSERT INTO bluesky_keywords (keyword, added_at) VALUES (?, ?)",
                (k, now),
            )
        conn.execute("COMMIT")


def seed_bluesky_keywords_if_empty(defaults: list[str]) -> None:
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM bluesky_keywords").fetchone()[0]
    if n == 0 and defaults:
        set_bluesky_keywords(defaults)


# --- google trends keywords ---

def list_google_trends_keywords() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT keyword FROM google_trends_keywords ORDER BY added_at ASC"
        ).fetchall()
    return [r["keyword"] for r in rows]


def set_google_trends_keywords(keywords: list[str]) -> None:
    """Replace the entire list. Trims and de-dupes (case-preserving)."""
    cleaned = []
    seen = set()
    now = int(time.time())
    for k in keywords:
        k = (k or "").strip()
        key = k.lower()
        if not k or key in seen:
            continue
        seen.add(key)
        cleaned.append(k)
    with connect() as conn:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM google_trends_keywords")
        for k in cleaned:
            conn.execute(
                "INSERT INTO google_trends_keywords (keyword, added_at) VALUES (?, ?)",
                (k, now),
            )
        conn.execute("COMMIT")


# --- google trending categories (numeric ids) ---

def list_google_trending_categories() -> list[int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT category_id FROM google_trending_categories ORDER BY added_at ASC"
        ).fetchall()
    return [int(r["category_id"]) for r in rows]


def set_google_trending_categories(category_ids: list[int]) -> None:
    """Replace the entire list of selected category ids.

    Also marks all existing googletrending posts dead so the feed shows only
    items that match the new selection — the next discovery tick un-deads
    any matching items via upsert_post.
    """
    cleaned: list[int] = []
    seen: set[int] = set()
    now = int(time.time())
    for cid in category_ids:
        try:
            i = int(cid)
        except (TypeError, ValueError):
            continue
        if i in seen:
            continue
        seen.add(i)
        cleaned.append(i)
    with connect() as conn:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM google_trending_categories")
        for cid in cleaned:
            conn.execute(
                "INSERT INTO google_trending_categories (category_id, added_at) VALUES (?, ?)",
                (cid, now),
            )
        conn.execute(
            "UPDATE posts SET dead = 1 WHERE source = 'googletrending'"
        )
        conn.execute("COMMIT")


# --- forecasts ---

def get_forecast(post_id: int) -> list[tuple[int, int]]:
    import json
    with connect() as conn:
        row = conn.execute(
            "SELECT points FROM forecasts WHERE post_id = ?", (post_id,)
        ).fetchone()
    if not row:
        return []
    try:
        return [(int(ts), int(s)) for ts, s in json.loads(row["points"])]
    except Exception:
        return []


def get_forecast_input_ts(ids: Iterable[int]) -> dict[int, int]:
    """Return {post_id: last_input_ts} for posts that have a cached forecast."""
    ids = list(ids)
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT post_id, last_input_ts FROM forecasts WHERE post_id IN ({placeholders})",
            ids,
        ).fetchall()
    return {r["post_id"]: r["last_input_ts"] for r in rows}


def save_forecast(post_id: int, last_input_ts: int, points: list[tuple[int, int]]) -> None:
    import json
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO forecasts (post_id, computed_at, last_input_ts, points)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
              computed_at = excluded.computed_at,
              last_input_ts = excluded.last_input_ts,
              points = excluded.points
            """,
            (post_id, int(time.time()), int(last_input_ts), json.dumps(points)),
        )


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
