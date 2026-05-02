import type { FeedResponse, ForecastStatus, Sort, SourceInfo, Window } from "./types";

export async function fetchFeed(
  window: Window,
  sources: string[],
  sort: Sort,
  limit = 100,
): Promise<FeedResponse> {
  const params = new URLSearchParams({ window, sort, limit: String(limit) });
  if (sources.length) params.set("sources", sources.join(","));
  const r = await fetch(`/api/feed?${params}`);
  if (!r.ok) throw new Error(`/api/feed ${r.status}`);
  return r.json();
}

export async function fetchSources(): Promise<SourceInfo[]> {
  const r = await fetch("/api/sources");
  if (!r.ok) throw new Error(`/api/sources ${r.status}`);
  return r.json();
}

export async function fetchXHandles(): Promise<string[]> {
  const r = await fetch("/api/x/handles");
  if (!r.ok) throw new Error(`/api/x/handles ${r.status}`);
  const d = await r.json();
  return d.handles ?? [];
}

export async function saveXHandles(handles: string[]): Promise<string[]> {
  const r = await fetch("/api/x/handles", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ handles }),
  });
  if (!r.ok) throw new Error(`PUT /api/x/handles ${r.status}`);
  const d = await r.json();
  return d.handles ?? [];
}

export async function fetchInstagramHandles(): Promise<string[]> {
  const r = await fetch("/api/instagram/handles");
  if (!r.ok) throw new Error(`/api/instagram/handles ${r.status}`);
  const d = await r.json();
  return d.handles ?? [];
}

export async function saveInstagramHandles(handles: string[]): Promise<string[]> {
  const r = await fetch("/api/instagram/handles", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ handles }),
  });
  if (!r.ok) throw new Error(`PUT /api/instagram/handles ${r.status}`);
  const d = await r.json();
  return d.handles ?? [];
}

export async function fetchRedditSubreddits(): Promise<string[]> {
  const r = await fetch("/api/reddit/subreddits");
  if (!r.ok) throw new Error(`/api/reddit/subreddits ${r.status}`);
  const d = await r.json();
  return d.subreddits ?? [];
}

export async function saveRedditSubreddits(subreddits: string[]): Promise<string[]> {
  const r = await fetch("/api/reddit/subreddits", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ subreddits }),
  });
  if (!r.ok) throw new Error(`PUT /api/reddit/subreddits ${r.status}`);
  const d = await r.json();
  return d.subreddits ?? [];
}

export async function fetchRssFeeds(): Promise<string[]> {
  const r = await fetch("/api/rss/feeds");
  if (!r.ok) throw new Error(`/api/rss/feeds ${r.status}`);
  const d = await r.json();
  return d.feeds ?? [];
}

export async function saveRssFeeds(feeds: string[]): Promise<string[]> {
  const r = await fetch("/api/rss/feeds", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ feeds }),
  });
  if (!r.ok) throw new Error(`PUT /api/rss/feeds ${r.status}`);
  const d = await r.json();
  return d.feeds ?? [];
}

export async function fetchBlueskyKeywords(): Promise<string[]> {
  const r = await fetch("/api/bluesky/keywords");
  if (!r.ok) throw new Error(`/api/bluesky/keywords ${r.status}`);
  const d = await r.json();
  return d.keywords ?? [];
}

export async function saveBlueskyKeywords(keywords: string[]): Promise<string[]> {
  const r = await fetch("/api/bluesky/keywords", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keywords }),
  });
  if (!r.ok) throw new Error(`PUT /api/bluesky/keywords ${r.status}`);
  const d = await r.json();
  return d.keywords ?? [];
}

export async function fetchGoogleTrendsKeywords(): Promise<string[]> {
  const r = await fetch("/api/google_trends/keywords");
  if (!r.ok) throw new Error(`/api/google_trends/keywords ${r.status}`);
  const d = await r.json();
  return d.keywords ?? [];
}

export async function saveGoogleTrendsKeywords(keywords: string[]): Promise<string[]> {
  const r = await fetch("/api/google_trends/keywords", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keywords }),
  });
  if (!r.ok) throw new Error(`PUT /api/google_trends/keywords ${r.status}`);
  const d = await r.json();
  return d.keywords ?? [];
}

export async function fetchTrendingCategoryOptions(): Promise<{ id: number; label: string }[]> {
  const r = await fetch("/api/google_trending/category_options");
  if (!r.ok) throw new Error(`/api/google_trending/category_options ${r.status}`);
  const d = await r.json();
  return d.options ?? [];
}

export async function fetchTrendingCategories(): Promise<number[]> {
  const r = await fetch("/api/google_trending/categories");
  if (!r.ok) throw new Error(`/api/google_trending/categories ${r.status}`);
  const d = await r.json();
  return d.category_ids ?? [];
}

export async function saveTrendingCategories(category_ids: number[]): Promise<number[]> {
  const r = await fetch("/api/google_trending/categories", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ category_ids }),
  });
  if (!r.ok) throw new Error(`PUT /api/google_trending/categories ${r.status}`);
  const d = await r.json();
  return d.category_ids ?? [];
}

export async function fetchForecastStatus(): Promise<ForecastStatus> {
  const r = await fetch("/api/forecast/status");
  if (!r.ok) throw new Error(`/api/forecast/status ${r.status}`);
  return r.json();
}

export async function triggerForecastRun(): Promise<{ started: boolean; reason?: string }> {
  const r = await fetch("/api/forecast/run", { method: "POST" });
  return r.json();
}
