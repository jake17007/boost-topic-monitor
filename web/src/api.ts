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

export async function fetchForecastStatus(): Promise<ForecastStatus> {
  const r = await fetch("/api/forecast/status");
  if (!r.ok) throw new Error(`/api/forecast/status ${r.status}`);
  return r.json();
}

export async function triggerForecastRun(): Promise<{ started: boolean; reason?: string }> {
  const r = await fetch("/api/forecast/run", { method: "POST" });
  return r.json();
}
