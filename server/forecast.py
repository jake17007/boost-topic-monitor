"""TimesFM 2.5-backed score forecasting.

The model is loaded lazily in a background thread so server startup stays fast.
Forecasts are computed by a scheduler-driven background job after each snapshot
tick and cached in-memory; the API just reads the cache so request latency is
unaffected by model inference.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Iterable

from . import db

log = logging.getLogger("hn-monitor.forecast")

# Resample raw snapshots to evenly-spaced 1-minute samples before forecasting.
RESAMPLE_INTERVAL = 60
# Predict the next 60 minutes.
HORIZON_STEPS = 60
# TimesFM 2.5 supports up to 1024 context.
CONTEXT_MAX = 1024
# Require at least N minutes of post history before producing a forecast.
MIN_HISTORY_MINUTES = 5
# Forecasts older than this many seconds are dropped from cache (cheap GC).
CACHE_MAX_AGE = 6 * 3600

# (post_id) -> (last_input_ts, computed_at, [(ts, score), ...])
_forecasts: dict[int, tuple[int, int, list[tuple[int, int]]]] = {}
_cache_lock = threading.Lock()

_model = None
_model_lock = threading.Lock()
_model_state = "unloaded"  # unloaded | loading | ready | failed


def model_state() -> str:
    return _model_state


def _start_loader() -> None:
    """Load TimesFM on a background thread. Idempotent."""
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
    """Forward-fill snapshots to evenly-spaced 1-minute samples.

    Returns (last_sample_ts, values) or None if we don't have enough history.
    """
    if len(series) < 2:
        return None
    start = series[0][0]
    end = series[-1][0]
    if end - start < RESAMPLE_INTERVAL * MIN_HISTORY_MINUTES:
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


def _predict_sync(post_id: int, series: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Run the model. Caller is responsible for off-loading from the event loop."""
    with _model_lock:
        m = _model
    if m is None:
        return []
    resampled = _resample(series)
    if not resampled:
        return []
    end_ts, values = resampled
    try:
        import numpy as np
        point_forecast, _ = m.forecast(
            horizon=HORIZON_STEPS,
            inputs=[np.array(values, dtype=np.float32)],
        )
    except Exception as e:
        log.warning("forecast failed for post %d: %s", post_id, e)
        return []
    horizon = point_forecast[0]
    last_actual = int(values[-1])
    result: list[tuple[int, int]] = []
    for i, v in enumerate(horizon):
        ts = end_ts + RESAMPLE_INTERVAL * (i + 1)
        # HN scores are monotonically non-decreasing on alive posts; clip to that.
        score = max(last_actual, int(round(float(v))))
        last_actual = score
        result.append((ts, score))
    return result


def cached_forecast(post_id: int) -> list[tuple[int, int]]:
    with _cache_lock:
        cached = _forecasts.get(post_id)
    return list(cached[2]) if cached else []


def _gc_cache(now: int) -> None:
    cutoff = now - CACHE_MAX_AGE
    with _cache_lock:
        stale = [pid for pid, (_, computed_at, _) in _forecasts.items() if computed_at < cutoff]
        for pid in stale:
            _forecasts.pop(pid, None)


def _todo_for_refresh(ids: Iterable[int]) -> list[tuple[int, list[tuple[int, int]]]]:
    """Return (post_id, series) pairs whose cached forecast is stale or missing."""
    ids = list(ids)
    series_map = db.series_for_ids(ids)
    todo: list[tuple[int, list[tuple[int, int]]]] = []
    with _cache_lock:
        for pid in ids:
            s = series_map.get(pid, [])
            if not s:
                continue
            cached = _forecasts.get(pid)
            if cached and cached[0] == s[-1][0]:
                continue
            todo.append((pid, list(s)))
    return todo


async def forecast_job() -> None:
    """Refresh forecasts for active posts. Runs on a scheduler tick."""
    if _model_state == "unloaded":
        _start_loader()
        return
    if _model_state != "ready":
        return  # still loading or failed; will retry next tick

    now = int(time.time())
    ids = db.active_post_ids(now, 24 * 3600)
    if not ids:
        return

    todo = _todo_for_refresh(ids)
    if not todo:
        return
    todo = todo[:60]

    log.info("forecasting %d posts", len(todo))
    t0 = time.time()
    for pid, s in todo:
        result = await asyncio.to_thread(_predict_sync, pid, s)
        if result:
            with _cache_lock:
                _forecasts[pid] = (s[-1][0], int(time.time()), result)
    log.info("forecast batch done in %.1fs", time.time() - t0)
    _gc_cache(now)
