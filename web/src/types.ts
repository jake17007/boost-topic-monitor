export interface Post {
  id: number;
  source: string;
  source_id: string;
  title: string | null;
  url: string | null;
  author: string | null;
  posted_ts: number | null;
  first_seen: number;
  dead: 0 | 1;
  latest_score: number | null;
  snapshot_count: number;
}

// [unix_seconds, score]
export type SnapshotPoint = [number, number];

export interface FeedItem extends Post {
  series: SnapshotPoint[];
  forecast: SnapshotPoint[];
  ranks?: Partial<Record<Sort, number>>;
}

export type ModelState = "unloaded" | "loading" | "ready" | "failed";

export interface FeedResponse {
  model_state: ModelState;
  items: FeedItem[];
}

export interface SourceInfo {
  name: string;
  label: string;
  description: string;
}

export type Window = "1h" | "3h" | "6h" | "12h" | "24h";

export type Sort = "top" | "hot" | "velocity" | "rising" | "trending";

export interface ForecastStatus {
  state: "idle" | "running";
  total: number;
  processed: number;
  wrote: number;
  started_at: number;
  finished_at: number;
  model_state: ModelState;
}
