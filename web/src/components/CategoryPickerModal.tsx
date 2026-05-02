import { useEffect, useState } from "react";
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
  onClose: () => void;
}

export function CategoryPickerModal({ onClose }: Props) {
  const [options, setOptions] = useState<Option[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([fetchTrendingCategoryOptions(), fetchTrendingCategories()])
      .then(([opts, ids]) => {
        setOptions(opts);
        setSelected(new Set(ids));
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const onSave = async () => {
    setSaving(true);
    setError(null);
    try {
      await saveTrendingCategories([...selected]);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-panel"
        role="dialog"
        aria-label="Edit Trending categories"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-title">Edit Trending categories</div>
        <div className="modal-hint">
          Pick which Google "Trending now" categories to track. Each trending term
          becomes a card with its current search volume; cards refresh every ~5 min.
        </div>
        {loading ? (
          <div className="modal-loading">loading…</div>
        ) : (
          <div className="cat-grid">
            {options.map((o) => {
              const on = selected.has(o.id);
              return (
                <label key={o.id} className={`cat-option${on ? " on" : ""}`}>
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={() => toggle(o.id)}
                  />
                  <span>{o.label}</span>
                </label>
              );
            })}
          </div>
        )}
        {error && <div className="modal-error">{error}</div>}
        <div className="modal-actions">
          <button type="button" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button
            type="button"
            className="primary"
            onClick={onSave}
            disabled={loading || saving}
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
