import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { publicationHref } from "../utils";
import Icon from "./Icon";
import Footer from "./Footer";
import ExportMenu from "./ExportMenu";

function shortAuthors(list) {
  if (!list || list.length === 0) return "автор не указан";
  const first = list.slice(0, 3).join(", ");
  return list.length > 3 ? `${first} и др.` : first;
}

export default function PublicationsPage() {
  const [pubs, setPubs] = useState(null);
  const [q, setQ] = useState("");
  const [period, setPeriod] = useState("all");

  useEffect(() => {
    api.listPublications(300).then(setPubs).catch(() => setPubs([]));
  }, []);

  const periods = [
    ["all", "Все", () => true],
    ["new", "С 2020", (y) => y >= 2020],
    ["mid", "2010–2019", (y) => y >= 2010 && y < 2020],
    ["old", "До 2010", (y) => y < 2010],
  ];

  const shown = useMemo(() => {
    if (!pubs) return [];
    const test = periods.find((p) => p[0] === period)[2];
    const needle = q.trim().toLowerCase();
    return pubs.filter((p) => {
      if (period !== "all" && (p.year == null || !test(p.year))) return false;
      if (!needle) return true;
      return `${p.title} ${(p.authors || []).join(" ")} ${p.journal || ""}`.toLowerCase().includes(needle);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pubs, q, period]);

  return (
    <div className="page-scroll">
      <section className="section narrow">
        <div className="section-head">
          <h2>Публикации</h2>
          <p>Последние добавленные научные работы{pubs ? ` · показано ${shown.length} из ${pubs.length}` : ""}</p>
        </div>
        <div className="pill-row">
          {periods.map(([key, label]) => (
            <button key={key} className={`pill ${period === key ? "active" : ""}`} onClick={() => setPeriod(key)}>
              {label}
            </button>
          ))}
          <input className="pill-search" placeholder="Фильтр по названию, автору, журналу…" value={q}
            onChange={(e) => setQ(e.target.value)} />
        </div>
        <ExportMenu ids={shown.map((p) => p.publication_id)} />
        {pubs === null ? (
          <div className="empty-state">Загрузка…</div>
        ) : shown.length === 0 ? (
          <div className="empty-state">Публикаций не найдено.</div>
        ) : (
          <div className="pub-grid">
            {shown.map((p) => {
              const href = publicationHref(p);
              return (
                <div className="pub-tile card-hover" key={p.publication_id}>
                  <div className="pub-icon"><Icon name="file" /></div>
                  <div className="pub-body">
                    <h4>{p.title}</h4>
                    <div className="pub-tags">
                      <span className="muted-text">{shortAuthors(p.authors)}</span>
                      {p.year && <span className="tag">{p.year}</span>}
                      {p.journal && <span className="tag tag-blue">{p.journal}</span>}
                    </div>
                    {href && (
                      <a className="open-link" href={href} target="_blank" rel="noreferrer">
                        <Icon name="link" size={13} /> Открыть
                      </a>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>
      <Footer />
    </div>
  );
}
