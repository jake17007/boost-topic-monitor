"""TimesFM 2.5-backed score forecasting.

Forecasts are persisted in SQLite (the `forecasts` table) so they survive
restarts. The model is loaded lazily in a background thread; `forecast_job`
runs on a scheduler tick (default hourly) and only re-runs the model on posts
whose latest snapshot ts changed since the last cached forecast.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Iterable

from . import db

log = logging.getLogger("hn-monitor.forecast")

RESAMPLE_INTERVAL = 60        # 1-minute samples
HORIZON_STEPS = 60            # predict 60 minutes ahead
CONTEXT_MAX = 1024            # TimesFM 2.5 max context
MIN_HISTORY_MINUTES = 5

_model = None
_model_lock = threading.Lock()
_model_state = "unloaded"  # unloaded | loading | ready | failed

# Live job state, surfaced to the UI for kicking off + tracking progress.
_job_state: dict = {
    "state": "idle",  # idle | running
    "total": 0,
    "processed": 0,
    "wrote": 0,
    "started_at": 0,
    "finished_at": 0,
}


def model_state() -> str:
    return _model_state


def get_job_state() -> dict:
    return dict(_job_state)


def _start_loader() -> None:
    global _model_state
    with _model_lock:
        if _model_state in ("loading", "ready", "failed"):
            return
        _model_state = "loading"

    def _load() -> None:
        global _model, _model_state
        try:
            import torch
            import timesfm

            torch.set_float32_matmul_precision("high")
            log.info("loading TimesFM 2.5 (first run downloads ~400MB)...")
            t0 = time.time()
            m = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
                "google/timesfm-2.5-200m-pytorch"
            )
            m.compile(
                timesfm.ForecastConfig(
                    max_context=CONTEXT_MAX,
                    max_horizon=HORIZON_STEPS,
                    normalize_inputs=True,
                    use_continuous_quantile_head=True,
                    force_flip_invariance=True,
                    infer_is_positive=True,
                    fix_quantile_crossing=True,
                )
            )
            with _model_lock:
                _model = m
                _model_state = "ready"
            log.info("TimesFM loaded in %.1fs", time.time() - t0)
        except Exception as e:
            with _model_lock:
                _model_state = "failed"
            log.exception("TimesFM load failed: %s", e)

    threading.Thread(target=_load, daemon=True, name="timesfm-loader").start()


def _resample(series: list[tuple[int, int]]) -> tuple[int, list[float]] | None:
    if len(series) < 2:
        return None
    start = series[0][0]
    end = series[-1][0]
    if end - start < RESAMPLE_INTERVAL * MIN_HISTORY_MINUTES:
        return None
    # Skip posts with no engagement variation — no signal to learn from.
    scores = [s[1] for s in series]
    if min(scores) == max(scores):
        return None
    times = list(range(start, end + 1, RESAMPLE_INTERVAL))
    if len(times) > CONTEXT_MAX:
        times = times[-CONTEXT_MAX:]
    values: list[float] = []
    j = 0
    n = len(series)
    for t in times:
        while j + 1 < n and series[j + 1][0] <= t:
            j += 1
        values.append(float(series[j][1]))
    return times[-1], values


def _predict_batch_sync(
    items: list[tuple[int, list[tuple[int, int]]]],
) -> dict[int, tuple[int, list[tuple[int, int]]]]:
    """Run the model on a batch. Returns {pid: (last_input_ts, points)}."""
    if not items:
        return {}
    with _model_lock:
        m = _model
    if m is None:
        return {}

    inputs = []
    metas = []  # (pid, last_input_ts, end_ts, last_actual)
    for pid, series in items:
        resampled = _resample(series)
        if not resampled:
            continue
        end_ts, values = resampled
        inputs.append(values)
        metas.append((pid, series[-1][0], end_ts, int(values[-1])))

    if not inputs:
        return {}

    try:
        import numpy as np
        arrays = [np.array(v, dtype=np.float32) for v in inputs]
        point_forecast, _ = m.forecast(horizon=HORIZON_STEPS, inputs=arrays)
    except Exception as e:
        log.warning("batched forecast failed (%d inputs): %s", len(inputs), e)
        return {}

    out: dict[int, tuple[int, list[tuple[int, int]]]] = {}
    for (pid, last_input_ts, end_ts, _last_actual), horizon in zip(metas, point_forecast):
        result: list[tuple[int, int]] = []
        for i, v in enumerate(horizon):
            ts = end_ts + RESAMPLE_INTERVAL * (i + 1)
            result.append((ts, int(round(float(v)))))
        out[pid] = (last_input_ts, result)
    return out


def cached_forecast(post_id: int) -> list[tuple[int, int]]:
    return db.get_forecast(post_id)


def _todo_for_refresh(ids: Iterable[int]) -> list[tuple[int, list[tuple[int, int]]]]:
    """Posts with enough history whose cached forecast is missing or stale."""
    ids = list(ids)
    series_map = db.series_for_ids(ids)
    cached_ts = db.get_forecast_input_ts(ids)
    min_span = RESAMPLE_INTERVAL * MIN_HISTORY_MINUTES
    todo: list[tuple[int, list[tuple[int, int]]]] = []
    for pid in ids:
        s = series_map.get(pid, [])
        if len(s) < 2:
            continue
        if s[-1][0] - s[0][0] < min_span:
            continue
        if cached_ts.get(pid) == s[-1][0]:
            continue  # up to date
        todo.append((pid, list(s)))
    return todo


async def forecast_job() -> None:
    """Refresh forecasts for active posts. Triggered by the scheduler or by
    POST /api/forecast/run. Concurrent calls are skipped (only one runs at a
    time)."""
    if _model_state == "unloaded":
        _start_loader()
        return
    if _model_state != "ready":
        return
    # Cooperative single-run guard. Coroutines aren't preempted between
    # statements (no await), so this check-and-set is atomic.
    if _job_state["state"] == "running":
        log.info("forecast: already running; skip")
        return

    now = int(time.time())
    ids = db.active_post_ids(now, 24 * 3600)
    if not ids:
        return
    todo = _todo_for_refresh(ids)
    if not todo:
        return

    BATCH = 64
    total = len(todo)
    n_batches = (total + BATCH - 1) // BATCH
    _job_state.update(
        state="running",
        total=total,
        processed=0,
        wrote=0,
        started_at=int(time.time()),
        finished_at=0,
    )
    log.info("forecasting %d posts (%d batches of %d)", total, n_batches, BATCH)
    t0 = time.time()
    written = 0
    try:
        for batch_i, chunk_start in enumerate(range(0, total, BATCH), start=1):
            chunk = todo[chunk_start:chunk_start + BATCH]
            b0 = time.time()
            results = await asyncio.to_thread(_predict_batch_sync, chunk)
            for pid, (last_input_ts, points) in results.items():
                db.save_forecast(pid, last_input_ts, points)
                written += 1
            _job_state["processed"] = chunk_start + len(chunk)
            _job_state["wrote"] = written
            log.info(
                "  forecast %d/%d (%d posts in %.1fs, total wrote %d)",
                batch_i, n_batches, len(chunk), time.time() - b0, written,
            )
        log.info("forecast batch done in %.1fs (wrote %d)", time.time() - t0, written)
    finally:
        _job_state.update(state="idle", finished_at=int(time.time()), wrote=written)
