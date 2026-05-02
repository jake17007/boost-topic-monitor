# boost-topic-monitor

A small local app that monitors **Hacker News**, **Bluesky**, **Product Hunt**, **X**, **Instagram** (creators you pick, via Apify), **Reddit** (subreddits you pick), **Hugging Face Daily Papers**, **Hugging Face Posts**, **RSS feeds** (frontier-AI lab blogs by default), **Google Trends** (your keywords), and **Google Trending Now** (auto-discovered trending searches by category) for new posts/topics, snapshots engagement once a minute, and forecasts each item's score one hour ahead with **Google's TimesFM-2.5**. The web feed lets you rank everything by current score, hot, velocity, predicted growth, or acceleration.

![python](https://img.shields.io/badge/python-3.10%2B-blue) ![react](https://img.shields.io/badge/react-18-blue) ![timesfm](https://img.shields.io/badge/timesfm-2.5--200m-orange)

## What it does

1. **Discovery** — for every registered source, ingest new post IDs (HN polls `/newstories`, Bluesky reads from the Jetstream WebSocket with a keyword filter, Product Hunt fetches the daily launch list, X polls each configured handle's recent timeline, Google Trends pulls the configured keyword list — each keyword becomes a synthetic "post").
2. **Snapshot** — every 60s, refresh each tracked post's engagement score (HN upvotes, BSKY likes+reposts+replies+quotes, PH votes, X likes+reposts+replies+quotes, Trends 0–100 search interest) and write a `(post_id, ts, score)` row.
3. **Forecast** — TimesFM-2.5 predicts each post's score 60 minutes out. Runs once an hour automatically, plus on-demand via the **"Run predictions"** header button (live progress: `Predicting 384/2779 …`). Forecasts are persisted in `data.db` and survive restarts; posts whose history shows no engagement variation are skipped.
4. **Feed** — React UI shows each post as a card with its actual score line + a separate dashed forecast line, ranked by your choice of metric. Default view: 100 posts.

## Sources

| Source | Discovery | Score | Auth |
|---|---|---|---|
| **Hacker News** | `/newstories` polled every 30s | upvotes | none |
| **Bluesky** | Jetstream WebSocket, regex-filtered locally | likes + reposts + replies + quotes | none |
| **Product Hunt** | One batched GraphQL query for today's launches every 30s | votes | `PRODUCTHUNT_TOKEN` |
| **X** | Each handle in the `x_handles` table polled every 30s; engagement batched every 60s | likes + reposts + replies + quotes | `X_BEARER_TOKEN` |
| **Instagram** | Each creator in the `instagram_handles` table; one Apify `instagram-post-scraper` run every ~5 min covers all of them | `videoPlayCount` (videos/reels) → `videoViewCount` → `likesCount` (photos) | `APIFY_TOKEN` |
| **Reddit** | Each subreddit in the `reddit_subreddits` table polled every 30s (`/r/<sub>/new.json`); engagement batched every 60s via `/api/info.json` | score + comments | none (unauthenticated public JSON, ~10 req/min cap) |
| **HF Papers** | `/api/daily_papers?limit=100` polled every 30s; same endpoint serves the snapshot (one call covers all tracked papers) | upvotes + comments | none |
| **HF Posts** | `/api/posts?limit=100` polled every 30s; same endpoint serves the snapshot | total reactions + comments | none |
| **RSS** | Each feed in the `rss_feeds` table polled every 30s via `feedparser`; defaults seed the seven frontier-AI labs (OpenAI, Anthropic, DeepMind, Meta AI, Mistral, xAI, Apple ML) | recency decay (1000 at publish, falls 10/hour) — RSS exposes no engagement metric | none |
| **Google Trends** | Keywords in the `google_trends_keywords` table, queried every ~5 min via `pytrends` (`now 1-H`, US, anchored to a fixed term for cross-keyword comparability) | search interest 0–100 | none |
| **Google Trending** | Auto-discovered: hits Google's "Trending Now" `batchexecute` endpoint every ~5 min, filters by the `google_trending_categories` you select (Tech, Sports, Politics, …) | reported search volume (10K, 50K, 100K …) | none |

Sources whose required token isn't set self-disable on startup. Google Trends self-disables if `pytrends` isn't installed.

### Editing what gets monitored

Bluesky's keyword filter, X's handle list, the Instagram creator list, and the Google Trends keyword list all live in SQLite and are editable from the header:

- **Edit X handles** — comma- or newline-separated. Changes take effect on the next discovery tick (≤30 s).
- **Edit Instagram handles** — same. New creators get scraped on the next Apify cache miss (≤5 min).
- **Edit Reddit subreddits** — comma- or newline-separated; `r/` prefix stripped. New subs get polled on the next discovery tick (≤30 s).
- **Edit Bluesky keywords** — same. The Jetstream regex rebuilds automatically.
- **Edit Trends keywords** — same. New keywords get an interest score on the next snapshot tick; pytrends queries are throttled to once every 5 min per keyword.
- **Edit Trending categories** — checkbox list of Google's "Trending Now" categories (Tech, Sports, Politics, etc). Each currently-trending search in the picked categories becomes a card; the upstream list refreshes every ~5 min and trends that rotate out are marked dead.

On first run the Bluesky keyword table is seeded with sane AI / dev / productivity defaults, and the Reddit subreddit table is seeded with `singularity`, `LocalLLaMA`, `StableDiffusion`, `MachineLearning`; the X handles, Instagram handles, Trends keywords, and Trending categories tables start empty.

## Rank metrics

Every post gets all five computed; the **Sort** dropdown picks which one to sort by, and each card shows all four (the active one highlighted).

- **Top** — `latest_score` (highest first).
- **Hot** — HN's `(score − 1) / (age_h + 2)^1.8`. Score discounted by post age.
- **Velocity** — score gained per minute over the last 10 minutes.
- **Rising** — TimesFM-predicted absolute gain over the next 60 minutes.
- **Trending** — acceleration: velocity(last 5 min) − velocity(prior 5 min). Detects posts going viral.

## Setup

```bash
# backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# frontend
cd web && npm install && npm run build && cd ..
```

First run downloads TimesFM-2.5 (~400 MB) from Hugging Face.

### Configuration

Copy `.env.example` to `.env` and fill in your tokens (the server auto-loads `.env` on startup):

```bash
PRODUCTHUNT_TOKEN=...    # https://www.producthunt.com/v2/oauth/applications
X_BEARER_TOKEN=...       # from a Project-attached app at https://developer.x.com/en/portal/projects-and-apps
APIFY_TOKEN=...          # https://console.apify.com/account/integrations
```

- X charges per read (~$0.005 / unique read).
- Apify charges ~$1 per 1000 returned posts. Default cadence (5-min cache, 10 creators, ~3 posts/creator/poll) ≈ **$8/day**; 60-second cadence with full feeds runs into the hundreds. Tune in `server/sources/instagram.py` (`MIN_FETCH_INTERVAL`, `RESULTS_LIMIT`).
- Empty `.env` is fine — HN and Bluesky still work without any auth.

### Running

```bash
python -m server
# open http://127.0.0.1:8000
```

Dev frontend with HMR (separately, while backend is running):

```bash
cd web && npm run dev   # http://localhost:5173, /api/* proxied to :8000
```

## Adding a new source

1. Create `server/sources/<name>.py` implementing the `Source` protocol from `server/sources/base.py`:
   ```python
   class MySource:
       name = "myname"
       label = "My Display Name"
       description = "One-line tooltip about how it works."

       async def fetch_new_post_ids(self) -> list[str]: ...
       async def fetch_post(self, source_id: str) -> SourcePost | None: ...
       async def close(self) -> None: ...
   ```
   Optionally implement `fetch_posts(ids: list[str]) -> dict[str, SourcePost]` for batch lookups (used by Bluesky / X / PH).
2. Append it to `_build_sources()` in `server/sources/__init__.py`.
3. (Optional) Add a frontend short-label and color in `web/src/components/PostCard.tsx` and `web/src/styles.css`.

Discovery, snapshot, forecast, ranking, and the UI badge/filter wire up automatically.

## Layout

```
boost-topic-monitor/
├── server/                          # FastAPI app
│   ├── __main__.py                  # `python -m server`
│   ├── app.py                       # routes + scheduler + .env loader
│   ├── db.py                        # SQLite schema + queries (data.db)
│   ├── forecast.py                  # TimesFM 2.5 loader + job + state
│   ├── jobs.py                      # discovery + snapshot loops
│   ├── ranking.py                   # top/hot/velocity/rising/trending
│   └── sources/                     # one file per source + base.py protocol
└── web/                             # Vite + React + TypeScript
    └── src/
        ├── App.tsx
        ├── api.ts, types.ts
        └── components/
            ├── PostCard.tsx
            └── ListEditorModal.tsx  # used for both X handles & Bluesky keywords
```

### Database tables (`data.db`)

- `posts` — one row per discovered post (`source`, `source_id`, title, etc.)
- `snapshots` — `(post_id, ts, score)` time series
- `forecasts` — latest TimesFM forecast per post (`points` is JSON)
- `x_handles`, `instagram_handles`, `reddit_subreddits`, `bluesky_keywords`, `google_trends_keywords`, `google_trending_categories` — UI-editable monitoring config

Built with React + Chart.js on the frontend; FastAPI + APScheduler + httpx + websockets on the backend; SQLite for storage; TimesFM-2.5 (PyTorch) for forecasting.
