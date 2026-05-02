import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import "./chartSetup";
import {
  fetchBlueskyKeywords,
  fetchFeed,
  fetchForecastStatus,
  fetchGoogleTrendsKeywords,
  fetchInstagramHandles,
  fetchRedditSubreddits,
  fetchRssFeeds,
  fetchSources,
  fetchXHandles,
  saveBlueskyKeywords,
  saveGoogleTrendsKeywords,
  saveInstagramHandles,
  saveRedditSubreddits,
  saveRssFeeds,
  saveXHandles,
  triggerForecastRun,
} from "./api";
import type {
  FeedItem,
  ForecastStatus,
  ModelState,
  Sort,
  SourceInfo,
  Window,
} from "./types";
import { PostCard } from "./components/PostCard";
import { ListEditorModal } from "./components/ListEditorModal";
import { CategoryPickerModal } from "./components/CategoryPickerModal";
import { TrendingCategoryBar } from "./components/TrendingCategoryBar";

const REFRESH_MS = 30_000;

const LS_KEY = "btm.controls.v1";

interface PersistedControls {
  window?: Window;
  sources?: string[];
  sort?: Sort;
}

function loadControls(): PersistedControls {
  try {
    const raw = localStorage.getItem(LS_KEY);
    return raw ? (JSON.parse(raw) as PersistedControls) : {};
  } catch {
    return {};
  }
}

function saveControls(c: PersistedControls): void {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(c));
  } catch {
    /* ignore quota / disabled storage */
  }
}

const VALID_WINDOWS: Window[] = ["1h", "3h", "6h", "12h", "24h", "2d", "3d", "7d", "30d"];
const VALID_SORTS: Sort[] = ["top", "hot", "velocity", "rising", "trending"];

const MODEL_LABEL: Record<ModelState, string> = {
  unloaded: "TimesFM: not started",
  loading: "TimesFM: loading…",
  ready: "TimesFM: ready",
  failed: "TimesFM: failed",
};

const SORT_OPTIONS: { value: Sort; label: string; tip: string }[] = [
  { value: "top", label: "Top", tip: "Highest current score." },
  { value: "hot", label: "Hot", tip: "HN formula: score discounted by post age. Favors fresh + popular." },
  { value: "velocity", label: "Velocity", tip: "Score gained per minute over the last 10 minutes." },
  { value: "rising", label: "Rising", tip: "TimesFM-predicted absolute gain over the next 60 minutes." },
  { value: "trending", label: "Trending", tip: "Acceleration: recent (5 min) velocity minus prior (5–10 min) velocity. Detects posts going viral." },
];

export default function App() {
  const [window, setWindow] = useState<Window>(() => {
    const saved = loadControls().window;
    return saved && VALID_WINDOWS.includes(saved) ? saved : "6h";
  });
  const [activeSources, setActiveSources] = useState<Set<string>>(() => {
    const saved = loadControls().sources;
    return new Set(Array.isArray(saved) ? saved : []);
  }); // empty = all
  const [sortBy, setSortBy] = useState<Sort>(() => {
    const saved = loadControls().sort;
    return saved && VALID_SORTS.includes(saved) ? saved : "top";
  });
  const [sources, setSources] = useState<SourceInfo[]>([]);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [modelState, setModelState] = useState<ModelState>("unloaded");
  const [status, setStatus] = useState("loading…");
  const [handlesOpen, setHandlesOpen] = useState(false);
  const [igHandlesOpen, setIgHandlesOpen] = useState(false);
  const [redditSubsOpen, setRedditSubsOpen] = useState(false);
  const [rssFeedsOpen, setRssFeedsOpen] = useState(false);
  const [keywordsOpen, setKeywordsOpen] = useState(false);
  const [trendsOpen, setTrendsOpen] = useState(false);
  const [trendingCatsOpen, setTrendingCatsOpen] = useState(false);
  const [trendingBarKey, setTrendingBarKey] = useState(0);
  const [forecastJob, setForecastJob] = useState<ForecastStatus | null>(null);
  const [infoOpen, setInfoOpen] = useState(false);
  const [infoPos, setInfoPos] = useState<{ top: number; left: number } | null>(null);
  const infoRef = useRef<HTMLSpanElement | null>(null);
  const [srcOpen, setSrcOpen] = useState(false);
  const [srcPos, setSrcPos] = useState<{ top: number; left: number } | null>(null);
  const srcRef = useRef<HTMLDivElement | null>(null);

  const windowRef = useRef(window);
  const sourcesRef = useRef(activeSources);
  const sortRef = useRef(sortBy);
  windowRef.current = window;
  sourcesRef.current = activeSources;
  sortRef.current = sortBy;

  const tick = useCallback(async () => {
    try {
      const res = await fetchFeed(
        windowRef.current,
        [...sourcesRef.current],
        sortRef.current,
      );
      setFeed(res.items);
      setModelState(res.model_state);
      setStatus(`updated ${new Date().toLocaleTimeString()} · ${res.items.length} posts`);
    } catch (e) {
      setStatus(`error: ${e instanceof Error ? e.message : String(e)}`);
    }
  }, []);

  // One-time: load source list.
  useEffect(() => {
    void fetchSources().then(setSources).catch(() => {});
  }, []);

  // Forecast job status polling. Fast while running, slow while idle.
  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const s = await fetchForecastStatus();
        if (stopped) return;
        setForecastJob(s);
        const delay = s.state === "running" ? 1500 : 5000;
        timer = globalThis.setTimeout(poll, delay);
      } catch {
        if (!stopped) timer = globalThis.setTimeout(poll, 5000);
      }
    };
    void poll();
    return () => {
      stopped = true;
      if (timer != null) globalThis.clearTimeout(timer);
    };
  }, []);

  const onRunForecast = async () => {
    try {
      await triggerForecastRun();
      // Kick the next status poll to update immediately.
      const s = await fetchForecastStatus();
      setForecastJob(s);
    } catch {
      /* ignore */
    }
  };

  useEffect(() => {
    void tick();
    const id = setInterval(() => void tick(), REFRESH_MS);
    return () => clearInterval(id);
  }, [tick]);

  // Re-fetch immediately when a control changes.
  useEffect(() => {
    void tick();
  }, [window, activeSources, sortBy, tick]);

  // Persist controls so a reload reopens the same view.
  useEffect(() => {
    saveControls({ window, sources: [...activeSources], sort: sortBy });
  }, [window, activeSources, sortBy]);

  const sourceMap = useMemo(() => {
    const m: Record<string, SourceInfo> = {};
    for (const s of sources) m[s.name] = s;
    return m;
  }, [sources]);

  const popoverSources =
    activeSources.size > 0
      ? sources.filter((s) => activeSources.has(s.name))
      : sources;
  const hoverTip = popoverSources
    .map((s) => `${s.label}: ${s.description}`)
    .join("\n\n");

  // Position the popover relative to the info dot. Recompute on open and on
  // resize/scroll so it stays anchored.
  useLayoutEffect(() => {
    if (!infoOpen) return;
    const place = () => {
      if (!infoRef.current) return;
      const r = infoRef.current.getBoundingClientRect();
      const vw = globalThis.innerWidth;
      const POPOVER_W = Math.min(420, vw - 32);
      let left = r.left;
      if (left + POPOVER_W > vw - 16) left = vw - POPOVER_W - 16;
      if (left < 16) left = 16;
      setInfoPos({ top: r.bottom + 6, left });
    };
    place();
    globalThis.addEventListener("resize", place);
    globalThis.addEventListener("scroll", place, true);
    return () => {
      globalThis.removeEventListener("resize", place);
      globalThis.removeEventListener("scroll", place, true);
    };
  }, [infoOpen]);

  // Close info popover on outside click or Escape.
  useEffect(() => {
    if (!infoOpen) return;
    const onClick = (e: MouseEvent) => {
      const target = e.target as Node;
      if (infoRef.current && !infoRef.current.contains(target)) {
        if (!(target instanceof Element && target.closest(".info-popover"))) {
          setInfoOpen(false);
        }
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setInfoOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [infoOpen]);

  // Position the source dropdown.
  useLayoutEffect(() => {
    if (!srcOpen) return;
    const place = () => {
      if (!srcRef.current) return;
      const r = srcRef.current.getBoundingClientRect();
      const vw = globalThis.innerWidth;
      const W = Math.min(260, vw - 32);
      let left = r.left;
      if (left + W > vw - 16) left = vw - W - 16;
      if (left < 16) left = 16;
      setSrcPos({ top: r.bottom + 6, left });
    };
    place();
    globalThis.addEventListener("resize", place);
    globalThis.addEventListener("scroll", place, true);
    return () => {
      globalThis.removeEventListener("resize", place);
      globalThis.removeEventListener("scroll", place, true);
    };
  }, [srcOpen]);

  useEffect(() => {
    if (!srcOpen) return;
    const onClick = (e: MouseEvent) => {
      const target = e.target as Node;
      if (srcRef.current && !srcRef.current.contains(target)) {
        if (!(target instanceof Element && target.closest(".src-dropdown"))) {
          setSrcOpen(false);
        }
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSrcOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [srcOpen]);

  return (
    <div className="app">
      <header>
        <h1>Engagement Monitor</h1>
        <div className="src-wrap" ref={srcRef}>
          <button
            type="button"
            className="src-trigger"
            aria-haspopup="listbox"
            aria-expanded={srcOpen}
            onClick={() => setSrcOpen((o) => !o)}
          >
            Sources:{" "}
            <strong>
              {activeSources.size === 0
                ? "All"
                : sources
                    .filter((s) => activeSources.has(s.name))
                    .map((s) => s.label)
                    .join(", ") || "None"}
            </strong>
            <span className="caret">▾</span>
          </button>
          <span className="info-wrap" ref={infoRef}>
            <button
              type="button"
              className="info-dot"
              title={hoverTip}
              aria-label="How sources are collected"
              aria-expanded={infoOpen}
              onClick={() => setInfoOpen((o) => !o)}
            >
              ⓘ
            </button>
          </span>
          {infoOpen && infoPos && createPortal(
            <div
              className="info-popover"
              role="dialog"
              style={{ top: infoPos.top, left: infoPos.left }}
            >
              {popoverSources.map((s) => (
                <div key={s.name} className="info-row">
                  <span className={`source-badge source-${s.name}`}>
                    {s.name === "hn"
                      ? "HN"
                      : s.name === "producthunt"
                        ? "PH"
                        : s.name === "bluesky"
                          ? "BSKY"
                          : s.name === "x"
                            ? "X"
                            : s.name === "instagram"
                              ? "IG"
                              : s.name === "huggingfacepapers"
                                ? "PAPERS"
                                : s.name === "huggingfaceposts"
                                  ? "HF POSTS"
                                  : s.name === "rss"
                                    ? "RSS"
                                    : s.name === "googletrends"
                                      ? "TRENDS"
                                      : s.name === "googletrending"
                                        ? "TRENDING"
                                        : s.name.toUpperCase()}
                  </span>
                  <span className="info-desc">{s.description}</span>
                </div>
              ))}
            </div>,
            document.body,
          )}
          {srcOpen && srcPos && createPortal(
            <div
              className="src-dropdown"
              role="listbox"
              style={{ top: srcPos.top, left: srcPos.left }}
            >
              <button
                type="button"
                className="src-option"
                onClick={() => setActiveSources(new Set())}
              >
                <span className="src-checkbox">
                  {activeSources.size === 0 ? "✓" : ""}
                </span>
                <span>All sources</span>
              </button>
              <div className="src-divider" />
              {sources.map((s) => {
                const on = activeSources.has(s.name);
                return (
                  <button
                    key={s.name}
                    type="button"
                    className="src-option"
                    onClick={() =>
                      setActiveSources((prev) => {
                        const next = new Set(prev);
                        if (next.has(s.name)) next.delete(s.name);
                        else next.add(s.name);
                        return next;
                      })
                    }
                  >
                    <span className="src-checkbox">{on ? "✓" : ""}</span>
                    <span>{s.label}</span>
                  </button>
                );
              })}
            </div>,
            document.body,
          )}
        </div>
        <label className="window-label">
          Sort
          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value as Sort)}
            title={SORT_OPTIONS.find((o) => o.value === sortBy)?.tip}
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value} title={o.tip}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <label className="window-label">
          Window
          <select value={window} onChange={(e) => setWindow(e.target.value as Window)}>
            <option value="1h">1h</option>
            <option value="3h">3h</option>
            <option value="6h">6h</option>
            <option value="12h">12h</option>
            <option value="24h">24h</option>
            <option value="2d">2d</option>
            <option value="3d">3d</option>
            <option value="7d">7d</option>
            <option value="30d">30d</option>
          </select>
        </label>
        <button
          type="button"
          className="header-btn"
          onClick={() => setHandlesOpen(true)}
          title="Edit the X handles being monitored"
        >
          Edit X handles
        </button>
        <button
          type="button"
          className="header-btn"
          onClick={() => setIgHandlesOpen(true)}
          title="Edit the Instagram creators being monitored (via Apify)"
        >
          Edit Instagram handles
        </button>
        <button
          type="button"
          className="header-btn"
          onClick={() => setRedditSubsOpen(true)}
          title="Edit the subreddits being monitored"
        >
          Edit Reddit subreddits
        </button>
        <button
          type="button"
          className="header-btn"
          onClick={() => setRssFeedsOpen(true)}
          title="Edit the RSS/Atom feeds being polled (frontier-AI lab blogs by default)"
        >
          Edit RSS feeds
        </button>
        <button
          type="button"
          className="header-btn"
          onClick={() => setKeywordsOpen(true)}
          title="Edit the Bluesky keywords used to filter the firehose"
        >
          Edit Bluesky keywords
        </button>
        <button
          type="button"
          className="header-btn"
          onClick={() => setTrendsOpen(true)}
          title="Edit the Google Trends keywords being monitored"
        >
          Edit Trends keywords
        </button>
        <button
          type="button"
          className="header-btn"
          onClick={() => setTrendingCatsOpen(true)}
          title="Pick which Google Trending Now categories to track"
        >
          Edit Trending categories
        </button>
        <ForecastJobControl
          job={forecastJob}
          modelState={modelState}
          onRun={onRunForecast}
        />
        <span className={`model-state ${modelState}`}>{MODEL_LABEL[modelState]}</span>
        <span className="status">{status}</span>
      </header>
      {activeSources.size === 1 && activeSources.has("googletrending") && (
        <TrendingCategoryBar
          onChange={() => void tick()}
          refreshKey={trendingBarKey}
        />
      )}
      <main className="feed">
        {feed.length === 0 ? (
          <div className="empty">No posts yet — discovery runs every 30s.</div>
        ) : (
          feed.map((p) => (
            <PostCard
              key={p.id}
              post={p}
              sourceDescription={sourceMap[p.source]?.description}
              sort={sortBy}
            />
          ))
        )}
      </main>
      {handlesOpen && (
        <ListEditorModal
          title="Edit X handles"
          hint="One handle per line (or comma-separated). No @ needed."
          fetcher={fetchXHandles}
          saver={saveXHandles}
          onClose={() => {
            setHandlesOpen(false);
            void fetchSources().then(setSources).catch(() => {});
            void tick();
          }}
        />
      )}
      {igHandlesOpen && (
        <ListEditorModal
          title="Edit Instagram handles"
          hint="One creator per line (or comma-separated). No @ needed. Posts/reels refresh every ~5 min via Apify."
          fetcher={fetchInstagramHandles}
          saver={saveInstagramHandles}
          onClose={() => {
            setIgHandlesOpen(false);
            void fetchSources().then(setSources).catch(() => {});
            void tick();
          }}
        />
      )}
      {redditSubsOpen && (
        <ListEditorModal
          title="Edit Reddit subreddits"
          hint="One subreddit per line (or comma-separated). No r/ prefix needed."
          fetcher={fetchRedditSubreddits}
          saver={saveRedditSubreddits}
          onClose={() => {
            setRedditSubsOpen(false);
            void fetchSources().then(setSources).catch(() => {});
            void tick();
          }}
        />
      )}
      {rssFeedsOpen && (
        <ListEditorModal
          title="Edit RSS feeds"
          hint="One feed URL per line. RSS or Atom — feedparser handles both. Score is a recency decay (no engagement metric)."
          fetcher={fetchRssFeeds}
          saver={saveRssFeeds}
          onClose={() => {
            setRssFeedsOpen(false);
            void fetchSources().then(setSources).catch(() => {});
            void tick();
          }}
        />
      )}
      {keywordsOpen && (
        <ListEditorModal
          title="Edit Bluesky keywords"
          hint="One keyword per line (or comma-separated). Posts whose text matches any keyword will be tracked."
          fetcher={fetchBlueskyKeywords}
          saver={saveBlueskyKeywords}
          onClose={() => {
            setKeywordsOpen(false);
            void tick();
          }}
        />
      )}
      {trendsOpen && (
        <ListEditorModal
          title="Edit Google Trends keywords"
          hint="One search term per line (or comma-separated). Each becomes a tracked card with a 0–100 interest score (US, ~1-min resolution)."
          fetcher={fetchGoogleTrendsKeywords}
          saver={saveGoogleTrendsKeywords}
          onClose={() => {
            setTrendsOpen(false);
            void tick();
          }}
        />
      )}
      {trendingCatsOpen && (
        <CategoryPickerModal
          onClose={() => {
            setTrendingCatsOpen(false);
            setTrendingBarKey((k) => k + 1);
            void tick();
          }}
        />
      )}
    </div>
  );
}

interface ForecastControlProps {
  job: ForecastStatus | null;
  modelState: ModelState;
  onRun: () => void;
}

function ForecastJobControl({ job, modelState, onRun }: ForecastControlProps) {
  const running = job?.state === "running";
  const total = job?.total ?? 0;
  const processed = job?.processed ?? 0;
  const wrote = job?.wrote ?? 0;
  const pct = running && total > 0 ? Math.round((processed / total) * 100) : 0;
  const ageStr = (() => {
    if (!job?.finished_at) return "";
    const s = Math.max(0, Math.floor(Date.now() / 1000) - job.finished_at);
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    return `${Math.floor(s / 3600)}h ago`;
  })();
  const disabled = running || modelState !== "ready";

  return (
    <div className={`forecast-ctl${running ? " running" : ""}`}>
      <button
        type="button"
        className="header-btn"
        onClick={onRun}
        disabled={disabled}
        title={
          modelState !== "ready"
            ? "TimesFM not ready"
            : "Refresh predictions for posts with new snapshot data"
        }
      >
        {running ? `Predicting ${processed}/${total}` : "Run predictions"}
      </button>
      {running && (
        <div className="forecast-progress">
          <div className="forecast-bar" style={{ width: `${pct}%` }} />
        </div>
      )}
      {!running && job && job.finished_at > 0 && (
        <span className="forecast-meta">last: wrote {wrote} · {ageStr}</span>
      )}
    </div>
  );
}
