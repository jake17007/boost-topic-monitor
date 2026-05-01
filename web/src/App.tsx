import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import "./chartSetup";
import { fetchFeed, fetchSources } from "./api";
import type { FeedItem, ModelState, Sort, SourceInfo, Window } from "./types";
import { PostCard } from "./components/PostCard";

const REFRESH_MS = 30_000;

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
  const [window, setWindow] = useState<Window>("6h");
  const [activeSources, setActiveSources] = useState<Set<string>>(new Set()); // empty = all
  const [sortBy, setSortBy] = useState<Sort>("top");
  const [sources, setSources] = useState<SourceInfo[]>([]);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [modelState, setModelState] = useState<ModelState>("unloaded");
  const [status, setStatus] = useState("loading…");
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

  useEffect(() => {
    void tick();
    const id = setInterval(() => void tick(), REFRESH_MS);
    return () => clearInterval(id);
  }, [tick]);

  // Re-fetch immediately when a control changes.
  useEffect(() => {
    void tick();
  }, [window, activeSources, sortBy, tick]);

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
          </select>
        </label>
        <span className={`model-state ${modelState}`}>{MODEL_LABEL[modelState]}</span>
        <span className="status">{status}</span>
      </header>
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
    </div>
  );
}
