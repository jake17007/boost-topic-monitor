# boost-topic-monitor

A small local app that monitors **Hacker News**, **Bluesky**, **Product Hunt**, and **X** for new posts, snapshots their engagement once a minute, and forecasts each post's score one hour ahead with **Google's TimesFM-2.5**. The web feed lets you rank everything by current score, hot, velocity, predicted growth, or acceleration.

![architecture](https://img.shields.io/badge/python-3.10%2B-blue) ![architecture](https://img.shields.io/badge/react-18-blue) ![architecture](https://img.shields.io/badge/timesfm-2.5--200m-orange)

## What it does

1. **Discovery** — for every registered source, ingest new post IDs (HN polls `/newstories`, Bluesky reads from the Jetstream WebSocket with a keyword filter, Product Hunt fetches the daily launch list, X polls each configured handle's recent timeline).
2. **Snapshot** — every 60s, refresh each tracked post's engagement score (HN upvotes, BSKY likes+reposts+replies+quotes, PH votes, X likes+reposts+replies+quotes) and write a `(post_id, ts, score)` row.
3. **Forecast** — immediately after each snapshot tick, run TimesFM-2.5 on every post that gained new data, predicting the next 60 minutes. Cached in-memory by `(post_id, last_snapshot_ts)`.
4. **Feed** — React UI shows each post as a card with its actual score line + dashed forecast line, ranked by your choice of metric.

## Sources

| Source | Discovery | Score | Auth |
|---|---|---|---|
| **Hacker News** | `/newstories` polled every 30s | upvotes | none |
| **Bluesky** | Jetstream WebSocket, regex-filtered locally | likes + reposts + replies + quotes | none |
| **Product Hunt** | One batched GraphQL query for today's launches every 30s | votes | `PRODUCTHUNT_TOKEN` |
| **X** | Configurable handle list, each user's recent timeline polled every 30s | likes + reposts + replies + quotes | `X_BEARER_TOKEN` + `X_HANDLES` |

Sources self-disable if their required env vars are missing.

## Rank metrics

Every post gets all five computed; the dropdown picks which one to sort by.

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

### Environment variables

```bash
# Optional — Bluesky keyword filter (defaults to AI/dev/productivity terms)
export BLUESKY_KEYWORDS="ai,llm,gpt,claude,startup,..."

# Optional — Product Hunt
# Create a developer token at https://www.producthunt.com/v2/oauth/applications
export PRODUCTHUNT_TOKEN="..."

# Optional — X (charges per read; ~$0.005 / unique read)
# Get a Bearer Token from a developer App attached to a Project at
# https://developer.x.com/en/portal/projects-and-apps
export X_BEARER_TOKEN="..."
export X_HANDLES="elonmusk,sama,satyanadella,..."
```

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

That's it — discovery, snapshot, forecast, ranking, and the UI badge/filter all wire up automatically.

## Layout

```
boost-topic-monitor/
├── server/                    # FastAPI app
│   ├── __main__.py            # `python -m server`
│   ├── app.py                 # routes + scheduler
│   ├── db.py                  # SQLite schema + queries (data.db)
│   ├── forecast.py            # TimesFM 2.5 loader + cache + job
│   ├── jobs.py                # discovery + snapshot loops
│   ├── ranking.py             # top/hot/velocity/rising/trending
│   └── sources/               # one file per source + base.py protocol
└── web/                       # Vite + React + TypeScript
    └── src/
        ├── App.tsx
        ├── components/PostCard.tsx
        ├── api.ts, types.ts, chartSetup.ts, styles.css
```

Built with React + Chart.js on the frontend, FastAPI + APScheduler + httpx + websockets on the backend, SQLite for storage, and TimesFM-2.5 (PyTorch) for forecasting.
