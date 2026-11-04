import { publicationHref } from "../utils";

// Карточка публикации в списках (карточка месторождения, региональные работы)
export default function PubCard({ p }) {
  const href = publicationHref(p);
  return (
    <div className="pub-card">
      <div className="pub-title">
        {href ? <a href={href} target="_blank" rel="noreferrer">{p.title}</a> : p.title}
      </div>
      <div className="pub-meta">
        {p.year && <span className="year-chip">{p.year}</span>}
        {p.authors.map((a) => `${a.last_name} ${a.first_name?.[0] ?? ""}.`).join(", ") || "автор не указан"}
        {p.journal ? `, «${p.journal}»` : ""}
      </div>
      {p.doi && <div className="pub-meta muted-text">DOI {p.doi}</div>}
    </div>
  );
}
