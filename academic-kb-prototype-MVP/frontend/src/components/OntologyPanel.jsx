import { useEffect, useState } from "react";
import { api } from "../api";
import { publicationHref } from "../utils";

const LEVEL_ORDER = ["province", "oblast", "rayon", "field", "horizon", "deposit"];
const LEVEL_NAME = {
  province: "Провинции", oblast: "Области", rayon: "Районы", field: "Месторождения", horizon: "Пласты", deposit: "Залежи",
};

// initial — необязательное начальное состояние (используется при проверке отрисовки без сервера)
export default function OntologyPanel({ initial = {} }) {
  const [summary, setSummary] = useState(initial.summary ?? null);
  const [objects, setObjects] = useState(initial.objects ?? { objects: [], types: [] });
  const [mapping, setMapping] = useState(initial.mapping ?? null);
  const [error, setError] = useState(null);

  const [objKey, setObjKey] = useState("");
  const [typeId, setTypeId] = useState("");
  const [result, setResult] = useState(initial.result ?? null);
  const [busy, setBusy] = useState(false);
  const [openExpl, setOpenExpl] = useState(initial.openExpl ?? {});

  const [query, setQuery] = useState("");
  const [useInferred, setUseInferred] = useState(true);
  const [sparql, setSparql] = useState(initial.sparql ?? null);

  const [check, setCheck] = useState(initial.check ?? null);
  const [validate, setValidate] = useState(initial.validate ?? null);

  useEffect(() => {
    if (initial.summary) return;
    Promise.all([api.ontologySummary(), api.ontologyObjects(), api.ontologyMapping()])
      .then(([s, o, m]) => {
        setSummary(s);
        setObjects(o);
        setMapping(m);
        const rayon = o.objects.find((x) => x.kind === "rayon") || o.objects[0];
        if (rayon) setObjKey(`${rayon.kind}:${rayon.id}`);
        if (s.sparql_examples?.length) setQuery(s.sparql_examples[0].query);
      })
      .catch((e) => setError(e.message));
  }, []);

  async function run(fn) {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const find = () =>
    run(async () => {
      const [kind, id] = objKey.split(":");
      setOpenExpl({});
      setResult(await api.ontologyPublications(kind, id, typeId || null, true));
    });

  const runSparql = () => run(async () => setSparql(await api.ontologySparql(query, useInferred)));

  if (!summary) {
    return (
      <div className="onto-panel">
        <h2>Онтология и логический вывод</h2>
        {error ? <div className="import-warning">{error}</div> : <div className="muted-text">Строю онтологию и выполняю вывод…</div>}
      </div>
    );
  }

  const grouped = LEVEL_ORDER.map((k) => ({ k, items: objects.objects.filter((o) => o.kind === k) })).filter((g) => g.items.length);

  return (
    <div className="onto-panel">
      <h2>Онтология и логический вывод</h2>
      <p className="muted-text">
        Структуру (классы, свойства, аксиомы) задаёт ваша онтология — файл{" "}
        <code>{summary.base.path ? summary.base.path.split(/[\\/]/).pop() : "встроенная копия"}</code>: она создана раньше базы данных.
        База поставляет экземпляры и связывается с онтологией по таблице соответствия (ниже). Из записанных фактов
        рассуждатель выводит новые: например, публикация про пласт относится и к его месторождению, району, области и
        провинции, хотя в базе записан только пласт. Рассуждатель OWL 2 RL, результаты сверены со стандартным HermiT.
      </p>
      {error && <div className="import-warning">{error}</div>}

      <div className="onto-cards">
        <div><b>{summary.classes}</b><span>классов (из них определяемых {summary.defined_classes})</span></div>
        <div><b>{summary.object_properties + summary.data_properties}</b><span>свойств</span></div>
        <div><b>{summary.individuals_total}</b><span>экземпляров</span></div>
        <div><b>{summary.abox_triples}</b><span>фактов записано</span></div>
        <div className="accent"><b>{summary.inferred_meaningful}</b><span>фактов выведено</span></div>
        <div className={summary.consistent ? "ok" : "bad"}><b>{summary.consistent ? "да" : "нет"}</b><span>онтология согласована</span></div>
      </div>
      <div className="muted-text">
        Выведено по видам: {Object.entries(summary.inferred_by_kind).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k} — ${v}`).join("; ")}.
        Построение заняло {summary.build_seconds} с (вывод {summary.closure_seconds} с).
      </div>

      <details className="onto-box">
        <summary>Что приложение добавляет к вашей онтологии (расширения)</summary>
        <div className="muted-text">
          Основа: {summary.base.source}; в ней {summary.base.classes} классов, {summary.base.object_properties} объектных
          свойств и {summary.base.data_properties} свойств-данных. Всё ниже — расширения, они помечены в онтологии комментарием
          «Расширение приложения».
        </div>
        <ul className="onto-samples">{summary.extensions.map((t, i) => <li key={i}>{t}</li>)}</ul>
      </details>

      {summary.samples.length > 0 && (
        <details className="onto-box" open>
          <summary>Примеры выведенных фактов</summary>
          <ul className="onto-samples">{summary.samples.map((t, i) => <li key={i}>{t}</li>)}</ul>
        </details>
      )}

      <section className="onto-box">
        <h3>Запрос с логическим выводом</h3>
        <div className="import-controls">
          <select value={objKey} onChange={(e) => setObjKey(e.target.value)}>
            {grouped.map((g) => (
              <optgroup key={g.k} label={LEVEL_NAME[g.k]}>
                {g.items.map((o) => <option key={`${o.kind}:${o.id}`} value={`${o.kind}:${o.id}`}>{o.name}</option>)}
              </optgroup>
            ))}
          </select>
          <select value={typeId} onChange={(e) => setTypeId(e.target.value)}>
            <option value="">любой тип исследования</option>
            {objects.types.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
          <button className="import-primary" onClick={find} disabled={busy || !objKey}>Найти публикации</button>
        </div>
        {result && (
          <div>
            <div className="onto-result-head">
              {result.object.level} «{result.object.name}»{result.type ? `, тип «${result.type.name}»` : ""}:{" "}
              <b>напрямую в базе — {result.direct}</b>, <b className="gain">с логическим выводом — {result.with_inference}</b>
              {result.gain > 0 && <span className="gain"> (+{result.gain} найдено выводом)</span>}
            </div>
            {result.publications.length === 0 && <div className="muted-text">Публикаций не найдено.</div>}
            {result.publications.map((p) => {
              const href = publicationHref(p);
              return (
                <div className="pub-card" key={p.publication_id}>
                  <div className="pub-title">{href ? <a href={href} target="_blank" rel="noreferrer">{p.title}</a> : p.title}</div>
                  <div className="pub-meta">
                    {p.year || "год не указан"}{" "}
                    <span className={`import-tag ${p.how === "inferred" ? "onto-inferred" : ""}`}>
                      {p.how === "inferred" ? "найдена выводом" : "привязана напрямую"}
                    </span>{" "}
                    <button className="link-btn" onClick={() => setOpenExpl((o) => ({ ...o, [p.publication_id]: !o[p.publication_id] }))}>
                      {openExpl[p.publication_id] ? "скрыть объяснение" : "почему?"}
                    </button>
                  </div>
                  {openExpl[p.publication_id] && (
                    <ol className="onto-explain">
                      {p.explanation.map((s, i) => <li key={i} className={s.kind}>{s.text}</li>)}
                    </ol>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>

      <section className="onto-box">
        <h3>SPARQL</h3>
        <div className="import-controls">
          <select onChange={(e) => e.target.value !== "" && setQuery(summary.sparql_examples[Number(e.target.value)].query)} defaultValue="">
            <option value="">примеры запросов…</option>
            {summary.sparql_examples.map((x, i) => <option key={i} value={i}>{x.title}</option>)}
          </select>
          <label className="muted-text">
            <input type="checkbox" checked={useInferred} onChange={(e) => setUseInferred(e.target.checked)} /> с логическим выводом
          </label>
          <button className="import-primary" onClick={runSparql} disabled={busy}>Выполнить</button>
        </div>
        <textarea className="onto-sparql" value={query} onChange={(e) => setQuery(e.target.value)} spellCheck={false} rows={8} />
        <div className="muted-text">Префиксы «:», rdfs, rdf, owl, xsd подставляются сами. Разрешены только SELECT и ASK.</div>
        {sparql && (
          sparql.ask !== null ? (
            <div className="onto-result-head">Ответ: <b>{sparql.ask ? "да" : "нет"}</b></div>
          ) : (
            <div className="onto-table-wrap">
              <div className="muted-text">Строк: {sparql.rows.length}{sparql.truncated ? " (показаны первые 500)" : ""}</div>
              <table className="onto-table">
                <thead><tr>{sparql.vars.map((v) => <th key={v}>{v}</th>)}</tr></thead>
                <tbody>{sparql.rows.map((r, i) => <tr key={i}>{sparql.vars.map((v) => <td key={v}>{r[v]}</td>)}</tr>)}</tbody>
              </table>
            </div>
          )
        )}
      </section>

      <section className="onto-box">
        <h3>Проверки</h3>
        <div className="import-controls">
          <button className="import-secondary" onClick={() => run(async () => setCheck(await api.ontologyCheck(false)))} disabled={busy}>Проверить согласованность</button>
          <button className="import-secondary" onClick={() => run(async () => setCheck(await api.ontologyCheck(true)))} disabled={busy}>Показать на примере ошибки</button>
          <button className="import-secondary" onClick={() => run(async () => setValidate(await api.ontologyValidate()))} disabled={busy}>Сверить с реляционной базой</button>
        </div>
        {check && (
          <div className={`onto-note ${check.consistent ? "ok" : "bad"}`}>
            {check.demo && <div className="muted-text">{check.demo}</div>}
            {check.consistent ? "Противоречий не найдено." : "Рассуждатель нашёл противоречие:"}
            {check.errors.map((e, i) => <div key={i}>• {e}</div>)}
          </div>
        )}
        {validate && (
          <div className={`onto-note ${validate.all_match ? "ok" : "bad"}`}>
            {validate.all_match
              ? "Для каждого объекта публикации, найденные выводом, совпали с рекурсивными запросами к реляционной базе:"
              : "Есть расхождения с реляционной базой:"}
            <table className="onto-table">
              <thead><tr><th>уровень</th><th>объектов</th><th>совпало</th></tr></thead>
              <tbody>{validate.rows.map((r) => <tr key={r.level}><td>{r.level}</td><td>{r.objects}</td><td>{r.matching}</td></tr>)}</tbody>
            </table>
          </div>
        )}
      </section>

      {mapping && (
        <section className="onto-box">
          <h3>Соответствие онтологии и БД</h3>
          <div className="muted-text">
            Какой элемент онтологии какой таблице, столбцу или внешнему ключу соответствует. У каждого экземпляра в онтологии
            есть свойство «записьБД» — ключ его строки в БД (например, <code>rayon:1</code>).
          </div>
          <div className="onto-table-wrap">
            <table className="onto-table">
              <thead><tr><th>элемент онтологии</th><th>вид</th><th>соответствие в БД</th><th>происхождение</th></tr></thead>
              <tbody>
                {mapping.rows.map((r, i) => (
                  <tr key={i}>
                    <td><b>{r.element}</b></td><td>{r.kind}</td><td>{r.db}</td>
                    <td>{r.origin === "ваша онтология" ? r.origin : <span className="import-tag onto-inferred">{r.origin}</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="onto-box">
        <h3>Файл онтологии</h3>
        <div className="muted-text">
          Откройте в Protégé и запустите HermiT (Reasoner → HermiT → Start reasoner): результат совпадёт с выводом приложения.
        </div>
        <div className="import-controls">
          <a className="import-secondary" href={api.ontologyExportUrl("owl", false)}>Скачать .owl (только записанное)</a>
          <a className="import-secondary" href={api.ontologyExportUrl("owl", true)}>Скачать .owl с выведенными фактами</a>
          <a className="import-secondary" href={api.ontologyExportUrl("ttl", false)}>Скачать .ttl</a>
        </div>
      </section>
    </div>
  );
}
