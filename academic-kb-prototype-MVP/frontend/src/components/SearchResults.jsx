import { publicationHref } from "../utils";
import ExportMenu from "./ExportMenu";

function formatAuthors(authors) {
  if (!authors || authors.length === 0) return "автор не указан";
  return authors.map((a) => `${a.last_name} ${a.first_name[0]}.`).join(", ");
}

export default function SearchResults({ query, answer, results }) {
  return (
    <div>
      <h2>Результаты поиска</h2>
      <div className="answer-box">{answer}</div>
      <ExportMenu ids={results.map((p) => p.publication_id)} />
      {results.length === 0 ? (
        <div className="empty-state">Ничего не найдено.</div>
      ) : (
        results.map((p) => {
          const href = publicationHref(p);
          return (
          <div className="pub-card" key={p.publication_id}>
            <span className="score">{p.score.toFixed(3)}</span>
            <div className="pub-title">
              {href ? <a href={href} target="_blank" rel="noreferrer">{p.title}</a> : p.title}
            </div>
            <div className="pub-meta">
              {p.year && <span className="year-chip">{p.year}</span>}
              {formatAuthors(p.authors)}
            </div>
            {p.doi && <div className="pub-meta muted-text">DOI {p.doi}</div>}
            {p.matched_field && (
              <div className="pub-meta muted-text">
                Найдено через месторождение «{p.matched_field}»
              </div>
            )}
          </div>
          );
        })
      )}
    </div>
  );
}
