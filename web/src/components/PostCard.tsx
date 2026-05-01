import { useMemo } from "react";
import { Line } from "react-chartjs-2";
import { DateTime } from "luxon";
import type { ChartData, ChartOptions, TooltipItem } from "chart.js";
import type { FeedItem, Sort } from "../types";

const ACCENT = "#ff6600";
const FORECAST = "#5ac8fa";
const MUTED = "#8a94a6";
const GRID = "#1f2530";

function fmtAge(unix: number | null): string {
  if (!unix) return "";
  const s = Math.max(0, Math.floor(Date.now() / 1000) - unix);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function fmtPostedAt(unix: number | null): string {
  if (!unix) return "";
  return DateTime.fromSeconds(unix).toFormat("MMM d, h:mm a");
}

function sourceDiscussHref(source: string, sourceId: string): string {
  if (source === "hn") return `https://news.ycombinator.com/item?id=${sourceId}`;
  if (source === "producthunt") return `https://www.producthunt.com/posts/${sourceId}`;
  if (source === "bluesky") {
    // sourceId is an at:// URI: at://did:plc:xxx/app.bsky.feed.post/<rkey>
    const m = sourceId.match(/^at:\/\/([^/]+)\/app\.bsky\.feed\.post\/(.+)$/);
    if (m) return `https://bsky.app/profile/${m[1]}/post/${m[2]}`;
  }
  if (source === "x") return `https://x.com/i/status/${sourceId}`;
  return "#";
}

function sourceShortLabel(source: string): string {
  if (source === "hn") return "HN";
  if (source === "producthunt") return "PH";
  if (source === "bluesky") return "BSKY";
  if (source === "x") return "X";
  return source.slice(0, 3).toUpperCase();
}

interface Props {
  post: FeedItem;
  sourceDescription?: string;
  sort: Sort;
}

interface RankInfo {
  key: Sort;
  label: string;
  format: (v: number) => string;
  tip: string;
}

const RANK_INFOS: RankInfo[] = [
  {
    key: "hot",
    label: "Hot",
    format: (v) => v.toFixed(2),
    tip: "HN formula: score discounted by post age.",
  },
  {
    key: "velocity",
    label: "Velocity",
    format: (v) => `${v > 0 ? "+" : ""}${v.toFixed(2)}/min`,
    tip: "Score gained per minute over the last 10 minutes.",
  },
  {
    key: "rising",
    label: "Rising",
    format: (v) => `${v >= 0 ? "+" : ""}${Math.round(v)}`,
    tip: "TimesFM-predicted absolute gain over the next 60 minutes.",
  },
  {
    key: "trending",
    label: "Accel.",
    format: (v) => `${v > 0 ? "+" : ""}${v.toFixed(2)}/min²`,
    tip: "Acceleration: recent (5 min) velocity minus prior (5–10 min) velocity.",
  },
];

export function PostCard({ post, sourceDescription, sort }: Props) {
  const data = useMemo<ChartData<"line">>(() => {
    const actual = post.series.map(([ts, score]) => ({ x: ts * 1000, y: score }));
    const forecastLine = post.forecast.map(([ts, score]) => ({ x: ts * 1000, y: score }));

    return {
      datasets: [
        {
          label: "HN score",
          data: actual,
          borderColor: ACCENT,
          backgroundColor: "rgba(255, 102, 0, 0.12)",
          fill: true,
          pointRadius: 0,
          pointHoverRadius: 3,
          borderWidth: 1.75,
          tension: 0.25,
          spanGaps: true,
        },
        {
          label: "Predicted (1h)",
          data: forecastLine,
          borderColor: FORECAST,
          backgroundColor: "rgba(90, 200, 250, 0.06)",
          fill: false,
          pointRadius: 0,
          pointHoverRadius: 3,
          borderWidth: 1.5,
          borderDash: [4, 4],
          tension: 0.25,
          spanGaps: true,
        },
      ],
    };
  }, [post.series, post.forecast]);

  const options = useMemo<ChartOptions<"line">>(
    () => ({
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: {
          type: "time",
          time: { tooltipFormat: "MMM d HH:mm:ss" },
          ticks: { color: MUTED, maxTicksLimit: 5, font: { size: 10 } },
          grid: { color: GRID, display: false },
          border: { color: GRID },
        },
        y: {
          beginAtZero: true,
          ticks: { color: MUTED, precision: 0, maxTicksLimit: 4, font: { size: 10 } },
          grid: { color: GRID },
          border: { color: GRID },
        },
      },
      plugins: {
        legend: {
          display: true,
          position: "top",
          align: "end",
          labels: {
            color: MUTED,
            boxWidth: 12,
            boxHeight: 2,
            font: { size: 11 },
          },
        },
        tooltip: {
          callbacks: {
            title: (items: TooltipItem<"line">[]) => {
              const x = items[0]?.parsed.x;
              return typeof x === "number"
                ? DateTime.fromMillis(x).toFormat("MMM d HH:mm:ss")
                : "";
            },
            label: (item: TooltipItem<"line">) =>
              ` ${item.dataset.label}: ${item.parsed.y}`,
          },
        },
      },
    }),
    [],
  );

  const hasSeries = post.series.length > 0;
  const discussHref = sourceDiscussHref(post.source, post.source_id);
  const titleHref = post.url ?? discussHref;
  const predictedFinal = post.forecast.length ? post.forecast[post.forecast.length - 1][1] : null;
  const showDelta = predictedFinal != null && post.latest_score != null;
  const delta = showDelta ? predictedFinal - (post.latest_score ?? 0) : null;
  const scoreLabel =
    post.source === "producthunt"
      ? "Votes"
      : post.source === "bluesky"
        ? "Engagement"
        : post.source === "x"
          ? "Engagement"
          : "HN score";

  const ranks = post.ranks;

  return (
    <article className="card">
      <header className="card-head">
        <div className="card-title-block">
          <span
            className={`source-badge source-${post.source}`}
            title={sourceDescription}
          >
            {sourceShortLabel(post.source)}
          </span>
          <a className="card-title" href={titleHref} target="_blank" rel="noopener noreferrer">
            {post.title ?? "(no title)"}
          </a>
        </div>
        <div className="card-scores">
          <div className="score-block">
            <div className="score-label">{scoreLabel}</div>
            <div className="card-score">{post.latest_score ?? "–"}</div>
          </div>
          {predictedFinal != null && (
            <div className="score-block" title="TimesFM forecast in 1h">
              <div className="score-label predicted">Predicted (1h)</div>
              <div className="card-predicted">
                {predictedFinal}
                {delta != null && delta !== 0 && (
                  <span className={`delta ${delta > 0 ? "up" : "down"}`}>
                    {" "}
                    ({delta > 0 ? "+" : ""}
                    {delta})
                  </span>
                )}
              </div>
            </div>
          )}
        </div>
      </header>
      <div className="card-meta">
        {post.author && (
          <>
            {post.source === "x" ? (
              <strong className="card-author">{post.author}</strong>
            ) : (
              post.author
            )}
            {" · "}
          </>
        )}
        Posted: {fmtPostedAt(post.posted_ts ?? post.first_seen)} (
        {fmtAge(post.posted_ts ?? post.first_seen)} ago) ·{" "}
        <a href={discussHref} target="_blank" rel="noopener noreferrer">
          discuss
        </a>
        {" · "}
        {post.snapshot_count} snapshot{post.snapshot_count === 1 ? "" : "s"}
      </div>
      {ranks && (
        <div className="rank-row">
          {RANK_INFOS.map((info) => {
            const v = ranks[info.key];
            if (v === undefined) return null;
            return (
              <span
                key={info.key}
                className={`rank-chip${sort === info.key ? " active" : ""}`}
                title={info.tip}
              >
                <span className="rank-label">{info.label}</span>
                <strong>{info.format(v)}</strong>
              </span>
            );
          })}
        </div>
      )}
      <div className="card-chart">
        {hasSeries ? (
          <Line data={data} options={options} />
        ) : (
          <div className="card-empty">no snapshots yet</div>
        )}
      </div>
    </article>
  );
}
