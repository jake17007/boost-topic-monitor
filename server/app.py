import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, forecast, jobs, ranking, sources
from .sources.bluesky import DEFAULT_KEYWORDS as BLUESKY_DEFAULTS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIST = REPO_ROOT / "web" / "dist"


def _load_dotenv() -> None:
    """Tiny .env loader — sets vars in os.environ if not already present."""
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv()

WINDOW_RE = re.compile(r"^(\d+)([smhd])$")
WINDOW_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_window(s: str, default: int = 6 * 3600) -> int:
    if not s:
        return default
    m = WINDOW_RE.match(s.strip().lower())
    if not m:
        return default
    n, unit = int(m.group(1)), m.group(2)
    return min(n * WINDOW_UNITS[unit], 7 * 86400)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db.seed_bluesky_keywords_if_empty(BLUESKY_DEFAULTS)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        jobs.discovery_job, "interval", seconds=30,
        id="discovery", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        jobs.snapshot_job, "interval", seconds=60,
        id="snapshot", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        forecast.forecast_job, "interval", seconds=3600,
        id="forecast", max_instances=1, coalesce=True,
    )
    scheduler.start()
    scheduler.add_job(jobs.discovery_job, id="discovery_initial")
    scheduler.add_job(forecast.forecast_job, id="forecast_initial")
    # Trigger the (slow) TimesFM load on a background thread.
    forecast._start_loader()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await sources.close_sources()


app = FastAPI(lifespan=lifespan)


@app.get("/api/posts")
async def api_posts(window: str = Query("6h")):
    seconds = parse_window(window)
    return JSONResponse(db.recent_posts(seconds))


@app.get("/api/snapshots")
async def api_snapshots(ids: str = Query(...)):
    raw = [p for p in ids.split(",") if p]
    try:
        id_list = [int(x) for x in raw][:20]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids must be comma-separated integers")
    series = db.series_for_ids(id_list)
    return JSONResponse({str(k): v for k, v in series.items()})


@app.get("/api/feed")
async def api_feed(
    window: str = Query("6h"),
    limit: int = Query(60, ge=1, le=200),
    sources: str | None = Query(None),  # comma-separated; empty/None = all
    sort: str = Query("top"),
):
    seconds = parse_window(window)
    src_list = [s.strip() for s in (sources or "").split(",") if s.strip()] or None
    items = db.feed(seconds, sources=src_list)
    for item in items:
        item["forecast"] = forecast.cached_forecast(item["id"])
    items = ranking.sort_items(items, sort)[:limit]
    return JSONResponse(
        {
            "model_state": forecast.model_state(),
            "sort": sort if sort in ranking.RANKERS else "top",
            "items": items,
        }
    )


class HandlesPayload(BaseModel):
    handles: list[str]


@app.get("/api/x/handles")
async def api_x_handles_get():
    return JSONResponse({"handles": db.list_x_handles()})


@app.put("/api/x/handles")
async def api_x_handles_put(payload: HandlesPayload):
    db.set_x_handles(payload.handles)
    return JSONResponse({"handles": db.list_x_handles()})


class KeywordsPayload(BaseModel):
    keywords: list[str]


@app.get("/api/bluesky/keywords")
async def api_bluesky_keywords_get():
    return JSONResponse({"keywords": db.list_bluesky_keywords()})


@app.put("/api/bluesky/keywords")
async def api_bluesky_keywords_put(payload: KeywordsPayload):
    db.set_bluesky_keywords(payload.keywords)
    return JSONResponse({"keywords": db.list_bluesky_keywords()})


@app.get("/api/forecast/status")
async def api_forecast_status():
    return JSONResponse(
        {**forecast.get_job_state(), "model_state": forecast.model_state()}
    )


@app.post("/api/forecast/run")
async def api_forecast_run():
    state = forecast.get_job_state()
    if state["state"] == "running":
        return JSONResponse(
            {"started": False, "reason": "already running", **state},
            status_code=409,
        )
    if forecast.model_state() != "ready":
        return JSONResponse(
            {"started": False, "reason": f"model {forecast.model_state()}"},
            status_code=503,
        )
    asyncio.create_task(forecast.forecast_job())
    return JSONResponse({"started": True})


@app.get("/api/sources")
async def api_sources():
    return JSONResponse(
        [
            {"name": s.name, "label": s.label, "description": s.description}
            for s in sources.get_sources()
        ]
    )


# Mount the built React app at "/" if it exists. API routes above are matched
# first because they are registered first. In dev, run Vite separately and use
# its proxy to forward /api/* to this server.
if WEB_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")
else:
    @app.get("/", response_class=PlainTextResponse)
    async def dev_hint() -> str:
        return (
            "API is up. The React app is not built.\n"
            "Run `cd web && npm install && npm run dev` for hot-reload at "
            "http://127.0.0.1:5173, or `npm run build` to serve the bundle "
            "from this server.\n"
        )
