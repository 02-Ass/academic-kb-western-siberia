import { useEffect, useState } from "react";
import { api } from "../api";
import { doiUrl } from "../utils";

// Русскоязычной считаем публикацию, у которой язык указан как ru или название
// в основном на кириллице (Crossref язык часто не сообщает)
const LEVEL_LABEL = { field: "месторождение", rayon: "район", oblast: "область", province: "провинция" };

function isRussian(p) {
  if (p.language === "ru") return true;
  const title = p.title || "";
  const letters = (title.match(/\p{L}/gu) || []).length;
  const cyrillic = (title.match(/[а-яё]/gi) || []).length;
  return letters > 0 && cyrillic / letters >= 0.5;
}

const FULLTEXT_STATUS = {
  ok: "загружен",
  already: "уже был",
  no_doi: "нет DOI",
  no_open_access: "нет открытого доступа",
  not_pdf: "по ссылке не PDF",
  too_big: "файл слишком большой",
  no_text: "скан без текста",
  download_failed: "не удалось скачать",
  error: "ошибка",
};

export default function ImportPanel({ onImported }) {
  const [queriesText, setQueriesText] = useState("");
  const [keySet, setKeySet] = useState(true);
  const [perQuery, setPerQuery] = useState(15);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState(null);
  const [selected, setSelected] = useState(new Set());
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [ftStatus, setFtStatus] = useState(null);
  const [ftBusy, setFtBusy] = useState(false);
  const [ftResult, setFtResult] = useState(null);

  function loadFtStatus() {
    api.fulltextStatus().then(setFtStatus).catch(() => {});
  }

  async function fetchFulltexts() {
    setFtBusy(true);
    setFtResult(null);
    try {
      setFtResult(await api.fulltextFetch(15));
      loadFtStatus();
    } catch (e) {
      setError(e.message);
    } finally {
      setFtBusy(false);
    }
  }

  useEffect(() => {
    loadFtStatus();
    api
      .getImportDefaults()
      .then((d) => {
        setQueriesText(d.queries.join("\n"));
        setKeySet(d.openalex_key_set);
      })
      .catch((e) => setError(e.message));
  }, []);

  async function runPreview() {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const queries = queriesText.split("\n").map((q) => q.trim()).filter(Boolean);
      const data = await api.importPreview(queries, perQuery);
      setPreview(data);
      setSelected(new Set(data.candidates.map((_, i) => i)));
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function commit() {
    const items = preview.candidates.filter((_, i) => selected.has(i));
    if (items.length === 0) return;
    setSaving(true);
    setError(null);
    try {
      const res = await api.importCommit(items);
      setResult(res);
      loadFtStatus();
      setPreview(null);
      setSelected(new Set());
      onImported?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  function toggle(i) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });
  }

  const total = preview?.candidates.length ?? 0;
  const counts = preview
    ? {
        abstract: preview.candidates.filter((c) => c.publication.abstract).length,
        russian: preview.candidates.filter((c) => isRussian(c.publication)).length,
        field: preview.candidates.filter((c) => c.field_ids.length > 0).length,
      }
    : null;

  return (
    <div className="external-search-panel">
      <h2>Наполнение базы знаний</h2>
      <p className="muted-text">
        Система ищет публикации по набору запросов в OpenAlex и Crossref, отбрасывает
        не относящееся к теме и уже имеющееся в базе, и предлагает привязку к
        месторождению и типу исследования. Привязка делается по ключевым словам, поэтому
        проверьте список: сомнительное можно не отмечать. Аннотации сохраняются вместе с
        публикацией, и именно по ним чат-бот отвечает по существу. Пока вы не нажмёте
        «Сохранить», ничего в базу не попадает.
      </p>

      <details className="fulltext-box" open={ftStatus?.candidates > 0}>
        <summary>Полные тексты открытых статей</summary>
        <p className="muted-text">
          Сейчас чат-бот отвечает в основном по аннотациям. Для статей в открытом доступе можно
          скачать полный текст: он попадёт в поиск, и ответы станут содержательнее. Закрытые статьи
          не загружаются; сканы без текстового слоя пропускаются.
        </p>
        {ftStatus && (
          <div className="muted-text">
            В базе публикаций: {ftStatus.publications}; с DOI: {ftStatus.with_doi}; полный текст
            уже загружен: {ftStatus.with_fulltext}; можно попробовать загрузить: {ftStatus.candidates}.
          </div>
        )}
        <div className="import-controls">
          <button className="import-primary" onClick={fetchFulltexts} disabled={ftBusy || !ftStatus?.candidates}>
            {ftBusy ? "Загружаю, это может занять несколько минут…" : "Загрузить полные тексты (до 15 за раз)"}
          </button>
        </div>
        {ftResult && (
          <div className="import-result">
            Загружено: <b>{ftResult.ok}</b>, пропущено: {ftResult.skipped}, не удалось: {ftResult.failed}.
            <ul className="ft-list">
              {ftResult.results.map((r) => (
                <li key={r.publication_id}>
                  {r.title.slice(0, 80)}: {FULLTEXT_STATUS[r.status] || r.status}
                  {r.status === "ok" ? ` (${r.chunks} фрагм.)` : r.detail ? ` (${r.detail})` : ""}
                </li>
              ))}
            </ul>
          </div>
        )}
      </details>

      {!keySet && (
        <div className="import-warning">
          Не задан OPENALEX_API_KEY. Без ключа дневной лимит поиска в OpenAlex очень мал,
          и часть запросов может не пройти. Бесплатный ключ: openalex.org/settings/api,
          затем добавьте его в файл .env и перезапустите backend.
        </div>
      )}

      <label className="muted-text">Запросы (по одному в строке; можно менять и добавлять свои)</label>
      <textarea
        className="import-queries"
        rows={9}
        value={queriesText}
        onChange={(e) => setQueriesText(e.target.value)}
      />
      <div className="import-controls">
        <label className="muted-text">
          Результатов на запрос и источник:{" "}
          <select value={perQuery} onChange={(e) => setPerQuery(Number(e.target.value))}>
            <option value={10}>10</option>
            <option value={15}>15</option>
            <option value={25}>25</option>
          </select>
        </label>
        <button className="import-primary" onClick={runPreview} disabled={loading || saving}>
          {loading ? "Ищу, это займёт до минуты…" : "Найти кандидатов"}
        </button>
      </div>

      {error && <div className="import-warning">Ошибка: {error}</div>}

      {result && (
        <div className="import-result">
          Сохранено новых: <b>{result.created}</b>
          {result.already_existed > 0 && <>, уже были в базе: {result.already_existed}</>}
          {result.failed > 0 && <>, не удалось: {result.failed} ({result.errors.join("; ")})</>}.
          Теперь они доступны в карточках месторождений, поиске и чат-боте.
        </div>
      )}

      {preview && (
        <>
          <div className="import-stats">
            Найдено уникальных: {preview.stats.found_total}. Новых по теме:{" "}
            <b>{preview.stats.new}</b>. Уже в базе: {preview.stats.duplicates_in_db}. Отсеяно как
            не относящееся к теме: {preview.stats.irrelevant}.
          </div>
          <div className="muted-text">
            Из новых: с аннотацией {counts.abstract}, на русском {counts.russian}, с определённым
            месторождением {counts.field}.
          </div>
          {preview.warnings.map((w, i) => (
            <div key={i} className="import-warning">{w}</div>
          ))}
          {preview.errors.length > 0 && (
            <div className="import-warning">
              Часть запросов не прошла: {preview.errors.slice(0, 4).join("; ")}
              {preview.errors.length > 4 ? ` и ещё ${preview.errors.length - 4}` : ""}
            </div>
          )}
          {preview.irrelevant_examples.length > 0 && (
            <details className="muted-text">
              <summary>Примеры отсеянного (чтобы оценить фильтр)</summary>
              <ul>
                {preview.irrelevant_examples.map((t, i) => (
                  <li key={i}>{t}</li>
                ))}
              </ul>
            </details>
          )}

          <div className="import-bar">
            <span>Выбрано {selected.size} из {total}</span>
            <button onClick={() => setSelected(new Set(preview.candidates.map((_, i) => i)))}>Выбрать все</button>
            <button onClick={() => setSelected(new Set())}>Снять все</button>
            <button
              title="Оставить отмеченными только публикации, для которых определено месторождение"
              onClick={() =>
                setSelected(new Set(preview.candidates.flatMap((c, i) => (c.field_ids.length > 0 ? [i] : []))))
              }
            >
              Только с месторождением
            </button>
            <button
              title="Оставить отмеченными только публикации с аннотацией (по ним чат-бот может ответить по существу)"
              onClick={() =>
                setSelected(new Set(preview.candidates.flatMap((c, i) => (c.publication.abstract ? [i] : []))))
              }
            >
              Только с аннотацией
            </button>
            <button
              title="Оставить отмеченными только публикации на русском языке"
              onClick={() =>
                setSelected(new Set(preview.candidates.flatMap((c, i) => (isRussian(c.publication) ? [i] : []))))
              }
            >
              Только на русском
            </button>
            <button className="import-primary" onClick={commit} disabled={saving || selected.size === 0}>
              {saving ? "Сохраняю…" : `Сохранить выбранные (${selected.size})`}
            </button>
          </div>

          {preview.candidates.map((c, i) => {
            const p = c.publication;
            const href = doiUrl(p.doi) || p.url;
            return (
              <label key={i} className={`pub-card import-item ${selected.has(i) ? "" : "import-off"}`}>
                <input type="checkbox" checked={selected.has(i)} onChange={() => toggle(i)} />
                <div>
                  <span className="source-label">{p.source}</span>
                  <div className="pub-title">
                    {href ? (
                      <a href={href} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
                        {p.title}
                      </a>
                    ) : (
                      p.title
                    )}
                  </div>
                  <div className="pub-meta">
                    {p.year && <span className="year-chip">{p.year}</span>}
                    {(p.authors || []).slice(0, 4).join(", ") || "автор не указан"}
                    {p.authors?.length > 4 ? " и др." : ""}
                    {p.journal ? `, «${p.journal}»` : ""}
                  </div>
                  <div className="import-tags">
                    {c.field_names.length > 0 ? (
                      c.field_names.map((n) => <span key={n} className="import-tag">месторождение: {n}</span>)
                    ) : c.level && c.place_names?.length > 0 ? (
                      c.place_names.map((n) => (
                        <span key={n} className="import-tag">{LEVEL_LABEL[c.level]}: {n}</span>
                      ))
                    ) : (
                      <span className="import-tag import-tag-none">регион не определён</span>
                    )}
                    {c.research_type_names.map((n) => (
                      <span key={n} className="import-tag">{n}</span>
                    ))}
                    {isRussian(p) && <span className="import-tag">на русском</span>}
                    {!p.abstract && <span className="import-tag import-tag-none">без аннотации</span>}
                  </div>
                </div>
              </label>
            );
          })}
        </>
      )}
    </div>
  );
}
