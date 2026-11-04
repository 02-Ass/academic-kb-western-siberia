import { useState } from "react";
import { api } from "../api";

export default function ExternalSearchPanel({ fields, canSave = false }) {
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState(null);
  const [savedIds, setSavedIds] = useState(new Set());
  const [linkFieldId, setLinkFieldId] = useState("");
  const [language, setLanguage] = useState("");

  async function handleSearch(e) {
    e.preventDefault();
    if (!query.trim()) return;
    setLoading(true);
    try {
      const data = await api.searchExternal(query.trim(), 10, language);
      setResults(data);
    } catch (err) {
      setResults({ query, count: 0, results: [], error: err.message });
    } finally {
      setLoading(false);
    }
  }

  async function handleSave(pub, index) {
    try {
      await api.saveExternalPublication(
        pub,
        linkFieldId ? Number(linkFieldId) : null,
        null
      );
      setSavedIds((prev) => new Set(prev).add(index));
    } catch (err) {
      alert(`Не удалось сохранить: ${err.message}`);
    }
  }

  return (
    <div className="external-search-panel">
      <h2>Поиск публикаций во внешних источниках</h2>
      <p className="muted-text">
        Запрос отправляется одновременно в OpenAlex и Crossref, дубликаты
        убираются автоматически. Найденную публикацию можно сохранить в
        локальную базу знаний и привязать к месторождению (авторы
        нормализуются в отдельные записи автоматически).
      </p>

      <form className="external-search-form" onSubmit={handleSearch}>
        <input
          type="text"
          placeholder="Например: West Siberia oil geology"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select value={language} onChange={(e) => setLanguage(e.target.value)} title="Фильтр по языку (только OpenAlex)">
          <option value="">Любой язык</option>
          <option value="ru">Русский</option>
          <option value="en">English</option>
        </select>
        <select value={linkFieldId} onChange={(e) => setLinkFieldId(e.target.value)}>
          <option value="">Не привязывать к месторождению</option>
          {fields.map((f) => (
            <option key={f.field_id} value={f.field_id}>
              {f.name}
            </option>
          ))}
        </select>
        <button type="submit" disabled={loading}>
          {loading ? "Ищу…" : "Найти"}
        </button>
      </form>

      {results && (
        <div className="external-results">
          {results.error && (
            <div className="empty-state">Ошибка поиска: {results.error}</div>
          )}
          {!results.error && results.count === 0 && (
            <div className="empty-state">Ничего не найдено.</div>
          )}
          {!results.error && results.count > 0 && (
            <>
              <div className="section-title">Найдено: {results.count}</div>
              {results.results.map((pub, i) => (
                <div className="pub-card" key={i}>
                  <span className="source-label">{pub.source}</span>
                  <div className="pub-title">{pub.title}</div>
                  <div className="pub-meta">
                    {pub.year && <span className="year-chip">{pub.year}</span>}
                    {pub.authors?.join(", ") || "автор не указан"}
                    {pub.journal ? `, «${pub.journal}»` : ""}
                  </div>
                  {pub.doi && <div className="pub-meta muted-text">DOI {pub.doi}</div>}
                  <div className="external-pub-actions">
                    {pub.url && (
                      <a href={pub.url} target="_blank" rel="noreferrer">
                        Открыть публикацию
                      </a>
                    )}
                    {canSave && (
                      <button
                        onClick={() => handleSave(pub, i)}
                        disabled={savedIds.has(i)}
                      >
                        {savedIds.has(i) ? "Сохранено ✓" : "Сохранить в базу"}
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}
