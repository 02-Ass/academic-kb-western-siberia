import { useState } from "react";
import { api } from "../api";

const FORMATS = [
  ["csv", "CSV (Excel)"],
  ["bibtex", "BibTeX"],
  ["ris", "RIS (Zotero, Mendeley)"],
  ["json", "JSON"],
];

// Кнопка «Экспорт» для списка публикаций из локальной базы: ids — id публикаций в порядке выдачи.
export default function ExportMenu({ ids }) {
  const [format, setFormat] = useState("csv");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const list = [...new Set((ids || []).filter((x) => x != null))];
  if (list.length === 0) return null;

  async function run() {
    setBusy(true);
    setError(null);
    try {
      await api.exportPublications(list, format);
    } catch (e) {
      setError(e.status === 401 ? "Для выгрузки нужно войти в систему." : e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="export-menu">
      <select value={format} onChange={(e) => setFormat(e.target.value)} aria-label="Формат выгрузки">
        {FORMATS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select>
      <button className="btn-secondary export-btn" onClick={run} disabled={busy}>
        {busy ? "Готовлю…" : `Экспорт (${list.length})`}
      </button>
      {error && <span className="export-error">{error}</span>}
    </div>
  );
}
