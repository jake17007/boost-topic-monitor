import { useEffect, useState } from "react";

interface Props {
  title: string;
  hint: string;
  fetcher: () => Promise<string[]>;
  saver: (items: string[]) => Promise<string[]>;
  onClose: () => void;
}

export function ListEditorModal({ title, hint, fetcher, saver, onClose }: Props) {
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void fetcher()
      .then((items) => setText(items.join("\n")))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [fetcher]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const onSave = async () => {
    setSaving(true);
    setError(null);
    const items = text
      .split(/[\s,]+/)
      .map((s) => s.trim().replace(/^@/, ""))
      .filter(Boolean);
    try {
      const saved = await saver(items);
      setText(saved.join("\n"));
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
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-title">{title}</div>
        <div className="modal-hint">{hint}</div>
        {loading ? (
          <div className="modal-loading">loading…</div>
        ) : (
          <textarea
            className="modal-textarea"
            value={text}
            onChange={(e) => setText(e.target.value)}
            spellCheck={false}
            autoFocus
            rows={14}
          />
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
