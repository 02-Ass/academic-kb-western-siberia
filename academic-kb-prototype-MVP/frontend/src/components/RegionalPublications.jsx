import { useEffect, useState } from "react";
import { api } from "../api";
import PubCard from "./PubCard";

const LEVEL_LABEL = { rayon: "Район", oblast: "Область", province: "Провинция" };
const PAGE = 20;

// Один уровень: заголовок со счётчиком; список загружается при раскрытии, порциями по PAGE.
function Level({ item }) {
  const [open, setOpen] = useState(false);
  const [pubs, setPubs] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const loadMore = async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await api.getRegionalPublications(item.level, item.object_id, pubs.length, PAGE);
      setPubs((prev) => [...prev, ...page.publications]);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && pubs.length === 0 && item.total > 0) loadMore();
  };

  return (
    <div className="regional-level">
      <button type="button" className="regional-toggle" onClick={toggle} aria-expanded={open} disabled={item.total === 0}>
        <span className="regional-caret">{open ? "▾" : "▸"}</span>
        <span>{LEVEL_LABEL[item.level]}: {item.name}</span>
        <span className="regional-count">{item.total}</span>
      </button>
      {open && (
        <div className="regional-list">
          {pubs.map((p) => <PubCard key={p.publication_id} p={p} />)}
          {error && <div className="empty-state">Не удалось загрузить: {error}</div>}
          {loading && <div className="muted-text">Загрузка…</div>}
          {!loading && pubs.length < item.total && (
            <button type="button" className="regional-more" onClick={loadMore}>
              Показать ещё ({item.total - pubs.length})
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export default function RegionalPublications({ fieldId }) {
  const [levels, setLevels] = useState(null);

  useEffect(() => {
    let alive = true;
    setLevels(null);
    api.getFieldRegionalSummary(fieldId).then((r) => alive && setLevels(r)).catch(() => alive && setLevels([]));
    return () => { alive = false; };
  }, [fieldId]);

  if (!levels || levels.every((l) => l.total === 0)) return null;
  return (
    <>
      <div className="section-title">Работы о регионе в целом</div>
      <p className="muted-text regional-note">
        Не о самом месторождении: публикации, посвящённые его району, области или провинции.
      </p>
      {levels.map((l) => <Level key={l.level} item={l} />)}
    </>
  );
}
