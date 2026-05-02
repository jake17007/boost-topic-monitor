import { useCallback, useEffect, useState } from "react";
import {
  fetchTrendingCategories,
  fetchTrendingCategoryOptions,
  saveTrendingCategories,
} from "../api";

interface Option {
  id: number;
  label: string;
}

interface Props {
  onChange?: () => void;
  // Increments when the user saves the modal — tells us to fold in any
  // newly-added categories so they appear as chips here too.
  refreshKey?: number;
}

export function TrendingCategoryBar({ onChange, refreshKey }: Props) {
  const [options, setOptions] = useState<Option[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  // Which chips to render. Always a superset of `selected` so a user can
  // toggle a chip off and back on without losing it; only ever grows when
  // new categories are saved via the modal.
  const [displayIds, setDisplayIds] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    Promise.all([fetchTrendingCategoryOptions(), fetchTrendingCategories()])
      .then(([opts, ids]) => {
        setOptions(opts);
        const sel = new Set(ids);
        setSelected(sel);
        setDisplayIds((prev) => new Set([...prev, ...sel]));
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  const toggle = async (id: number) => {
    if (busy) return;
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
    setBusy(true);
    try {
      const saved = await saveTrendingCategories([...next]);
      setSelected(new Set(saved));
      onChange?.();
    } catch {
      load();
    } finally {
      setBusy(false);
    }
  };

  if (!options.length || displayIds.size === 0) return null;

  const labelById = new Map(options.map((o) => [o.id, o.label] as const));
  // Render chips in the catalog's natural order so the layout is stable.
  const chips = options.filter((o) => displayIds.has(o.id));

  return (
    <div className="trending-bar">
      <span className="trending-bar-label">Trending</span>
      {chips.map((o) => {
        const on = selected.has(o.id);
        return (
          <button
            key={o.id}
            type="button"
            className={`trending-chip${on ? " on" : ""}`}
            onClick={() => toggle(o.id)}
            disabled={busy}
            aria-pressed={on}
            title={labelById.get(o.id)}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
