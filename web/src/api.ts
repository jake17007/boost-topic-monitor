import type { FeedResponse, Sort, SourceInfo, Window } from "./types";

export async function fetchFeed(
  window: Window,
  sources: string[],
  sort: Sort,
  limit = 60,
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
