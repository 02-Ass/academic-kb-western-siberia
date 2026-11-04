import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "—");

function useLoad(fn, deps) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try { setData(await fn()); } catch (e) { setError(e.message); } finally { setLoading(false); }
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { reload(); }, [reload]);
  return { data, error, loading, reload, setData };
}

// ---------------- Пользователи ----------------
function UsersTab({ me }) {
  const { data, error, loading, reload } = useLoad(() => api.adminUsers(), []);
  const [msg, setMsg] = useState(null);

  async function patch(u, body) {
    setMsg(null);
    try { await api.adminPatchUser(u.user_id, body); await reload(); } catch (e) { setMsg(e.message); }
  }

  return (
    <div>
      {(error || msg) && <div className="admin-error">{error || msg}</div>}
      <table className="admin-table">
        <thead>
          <tr><th>ID</th><th>Email</th><th>Имя</th><th>Роль</th><th>Зарегистрирован</th><th>Чатов</th><th>Статус</th><th></th></tr>
        </thead>
        <tbody>
          {(data || []).map((u) => {
            const self = me && u.user_id === me.user_id;
            return (
              <tr key={u.user_id} className={u.is_active ? "" : "row-muted"}>
                <td>{u.user_id}</td>
                <td>{u.email}</td>
                <td>{u.full_name || "—"}</td>
                <td>
                  <select value={u.role} disabled={self} onChange={(e) => patch(u, { role: e.target.value })}>
                    <option value="user">user</option>
                    <option value="admin">admin</option>
                  </select>
                </td>
                <td>{fmtDate(u.created_at)}</td>
                <td>{u.chats}</td>
                <td>{u.is_active ? "активен" : "заблокирован"}</td>
                <td className="admin-actions">
                  <button disabled={self} onClick={() => patch(u, { is_active: !u.is_active })}>
                    {u.is_active ? "Заблокировать" : "Разблокировать"}
                  </button>
                </td>
              </tr>
            );
          })}
          {!loading && data && data.length === 0 && <tr><td colSpan={8}>Пользователей нет.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

// ---------------- Публикации ----------------
const EMPTY = { title: "", authors: "", year: "", journal: "", doi: "", link: "", type: "" };

function PublicationForm({ initial, onCancel, onSave }) {
  const [f, setF] = useState(initial);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try { await onSave(f); } catch (err) { setError(err.message); setBusy(false); }
  }
  return (
    <div className="auth-backdrop" onClick={onCancel}>
      <form className="admin-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <h3>{initial.publication_id ? `Публикация №${initial.publication_id}` : "Добавить публикацию вручную"}</h3>
        <label>Название<input required minLength={3} value={f.title} onChange={set("title")} /></label>
        {!initial.publication_id && (
          <label>Авторы (через «;»)<input value={f.authors} onChange={set("authors")} placeholder="Иванов Иван; Петров Пётр" /></label>
        )}
        <div className="admin-form-row">
          <label>Год<input type="number" min="1500" max="2100" value={f.year} onChange={set("year")} /></label>
          <label>Тип<input value={f.type} onChange={set("type")} placeholder="статья" /></label>
        </div>
        <label>Журнал<input value={f.journal} onChange={set("journal")} /></label>
        <label>DOI<input value={f.doi} onChange={set("doi")} placeholder="10.xxxx/xxxxx" /></label>
        <label>Ссылка<input value={f.link} onChange={set("link")} placeholder="https://…" /></label>
        {error && <div className="auth-error">{error}</div>}
        <div className="admin-form-row">
          <button className="btn-primary" type="submit" disabled={busy}>Сохранить</button>
          <button type="button" className="auth-link" onClick={onCancel}>Отмена</button>
        </div>
      </form>
    </div>
  );
}

function PublicationsTab() {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [form, setForm] = useState(null);       // null | объект формы
  const [msg, setMsg] = useState(null);
  const LIMIT = 50;
  const { data, error, loading, reload } = useLoad(() => api.adminPublications(query, LIMIT, offset), [query, offset]);

  const toBody = (f, withAuthors) => ({
    title: f.title, year: f.year === "" ? null : Number(f.year), journal: f.journal, doi: f.doi, link: f.link,
    type: f.type || null,
    ...(withAuthors ? { authors: f.authors.split(";").map((a) => a.trim()).filter(Boolean) } : {}),
  });

  async function save(f) {
    if (f.publication_id) await api.adminEditPublication(f.publication_id, toBody(f, false));
    else await api.adminAddPublication(toBody(f, true));
    setForm(null);
    await reload();
  }

  async function remove(p) {
    if (!window.confirm(`Удалить публикацию №${p.publication_id}?\n«${p.title}»`)) return;
    setMsg(null);
    try { await api.adminDeletePublication(p.publication_id); await reload(); } catch (e) { setMsg(e.message); }
  }

  return (
    <div>
      <div className="admin-toolbar">
        <form onSubmit={(e) => { e.preventDefault(); setOffset(0); setQuery(q); }}>
          <input placeholder="Поиск по названию, DOI, журналу…" value={q} onChange={(e) => setQ(e.target.value)} />
          <button type="submit">Найти</button>
        </form>
        <button className="admin-primary" onClick={() => setForm({ ...EMPTY })}>+ Добавить вручную</button>
        <span className="admin-count">{data ? `Всего: ${data.total}` : ""}</span>
      </div>
      {(error || msg) && <div className="admin-error">{error || msg}</div>}
      <table className="admin-table">
        <thead><tr><th>ID</th><th>Название</th><th>Авторы</th><th>Год</th><th>Журнал</th><th>DOI</th><th></th></tr></thead>
        <tbody>
          {(data?.items || []).map((p) => (
            <tr key={p.publication_id}>
              <td>{p.publication_id}</td>
              <td className="cell-wide" title={p.title}>{p.title}</td>
              <td className="cell-mid" title={p.authors.join(", ")}>{p.authors.slice(0, 2).join(", ")}{p.authors.length > 2 ? " …" : ""}</td>
              <td>{p.year || "—"}</td>
              <td className="cell-mid" title={p.journal || ""}>{p.journal || "—"}</td>
              <td>{p.doi || "—"}</td>
              <td className="admin-actions">
                <button onClick={() => setForm({ publication_id: p.publication_id, title: p.title, authors: "", year: p.year ?? "",
                  journal: p.journal || "", doi: p.doi || "", link: p.link || "", type: p.type || "" })}>Редактировать</button>
                <button className="danger" onClick={() => remove(p)}>Удалить</button>
              </td>
            </tr>
          ))}
          {!loading && data && data.items.length === 0 && <tr><td colSpan={7}>Ничего не найдено.</td></tr>}
        </tbody>
      </table>
      <div className="admin-pager">
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - LIMIT))}>← Назад</button>
        <span>{data ? `${Math.min(offset + 1, data.total)}–${Math.min(offset + LIMIT, data.total)} из ${data.total}` : ""}</span>
        <button disabled={!data || offset + LIMIT >= data.total} onClick={() => setOffset(offset + LIMIT)}>Вперёд →</button>
      </div>
      {form && <PublicationForm initial={form} onCancel={() => setForm(null)} onSave={save} />}
    </div>
  );
}

// ---------------- Журнал запросов ----------------
function LogsTab() {
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const { data, error, loading, reload } = useLoad(() => api.adminLogs(150, query), [query]);
  return (
    <div>
      <div className="admin-toolbar">
        <form onSubmit={(e) => { e.preventDefault(); setQuery(q); }}>
          <input placeholder="Фильтр: email, вопрос, маршрут…" value={q} onChange={(e) => setQ(e.target.value)} />
          <button type="submit">Найти</button>
        </form>
        <button onClick={reload}>Обновить</button>
        <span className="admin-count">{data ? `Записей: ${data.items.length}` : ""}</span>
      </div>
      {error && <div className="admin-error">{error}</div>}
      {data && !data.enabled && <div className="admin-error">Журнал выключен (CHAT_LOG=0 в .env).</div>}
      <table className="admin-table">
        <thead><tr><th>Время</th><th>Кто</th><th>Вопрос</th><th>Намерение</th><th>Маршрут</th><th>Источн.</th><th>мс</th><th>Токены</th></tr></thead>
        <tbody>
          {(data?.items || []).map((r, i) => (
            <tr key={i} className={r.error ? "row-error" : ""}>
              <td>{(r.timestamp || "").replace("T", " ")}</td>
              <td>{r.user || "гость"}</td>
              <td className="cell-wide" title={r.query}>{r.query}</td>
              <td>{r.query_intent || r.intent || "—"}</td>
              <td>{r.route || "—"}</td>
              <td>{r.sources_found ?? "—"}</td>
              <td>{r.latency_ms ?? "—"}</td>
              <td>{r.tokens_used ?? 0}</td>
            </tr>
          ))}
          {!loading && data && data.items.length === 0 && <tr><td colSpan={8}>Записей нет.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

// ---------------- Настройки ----------------
function SettingsTab() {
  const { data, error, loading, setData } = useLoad(() => api.adminSettings(), []);
  const [model, setModel] = useState("");
  const [msg, setMsg] = useState(null);
  useEffect(() => { if (data) setModel(data.llm_model || ""); }, [data?.llm_model]); // eslint-disable-line react-hooks/exhaustive-deps

  async function save(body) {
    setMsg(null);
    try { setData(await api.adminSaveSettings(body)); setMsg("Сохранено."); } catch (e) { setMsg(e.message); }
  }
  if (loading && !data) return <div>Загрузка…</div>;
  if (!data) return <div className="admin-error">{error}</div>;

  return (
    <div className="admin-settings">
      <h4>Внешние источники публикаций</h4>
      {[["openalex_enabled", "OpenAlex"], ["crossref_enabled", "Crossref"]].map(([key, label]) => (
        <label key={key} className="admin-switch">
          <input type="checkbox" checked={data[key]} onChange={(e) => save({ [key]: e.target.checked })} />
          {label} {data[key] ? "включён" : "выключен"}
        </label>
      ))}
      <p className="admin-hint">Выключенный источник не опрашивается ни в чате, ни во вкладках «Внешний поиск» и «Наполнение базы».</p>

      <h4>Модель LLM</h4>
      {data.llm_configured ? (
        <>
          <p className="admin-hint">Стандартная модель: <b>{data.llm_default_model}</b>. Пустое поле — использовать стандартную.</p>
          <form className="admin-toolbar" onSubmit={(e) => { e.preventDefault(); save({ llm_model: model }); }}>
            <input value={model} onChange={(e) => setModel(e.target.value)} placeholder={data.llm_default_model || "название модели"} />
            <button type="submit">Применить</button>
          </form>
          <p className="admin-hint">Смена модели сбрасывает кэш ответов. Название должно поддерживаться вашим провайдером, иначе чат вернётся к шаблонным ответам.</p>
        </>
      ) : (
        <p className="admin-hint">LLM не подключён: в .env нет ключа (DEEPSEEK_API_KEY и т. п.).</p>
      )}
      {msg && <div className={msg === "Сохранено." ? "admin-ok" : "admin-error"}>{msg}</div>}
    </div>
  );
}

const SUBTABS = [["users", "Пользователи"], ["publications", "Публикации"], ["logs", "Логи"], ["settings", "Настройки"]];

export default function AdminPanel({ me }) {
  const [tab, setTab] = useState("users");
  return (
    <div className="admin-shell">
      <div className="admin-head">
        <h2>Админ-панель</h2>
        <nav>
          {SUBTABS.map(([key, label]) => (
            <button key={key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>{label}</button>
          ))}
        </nav>
      </div>
      <div className="admin-body">
        {tab === "users" && <UsersTab me={me} />}
        {tab === "publications" && <PublicationsTab />}
        {tab === "logs" && <LogsTab />}
        {tab === "settings" && <SettingsTab />}
      </div>
    </div>
  );
}
