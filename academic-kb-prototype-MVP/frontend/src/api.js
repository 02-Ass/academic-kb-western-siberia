const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

// Токен входа хранится в localStorage (если он недоступен — работаем без входа, в рамках одной вкладки).
const TOKEN_KEY = "kb_token";
let memoryToken = null;
export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || memoryToken; } catch { return memoryToken; }
}
export function setToken(token) {
  memoryToken = token;
  try { token ? localStorage.setItem(TOKEN_KEY, token) : localStorage.removeItem(TOKEN_KEY); } catch { /* нет доступа к хранилищу */ }
}
function authHeaders() {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// Сообщение об ошибке из ответа сервера: {"detail": "..."} → "...".
function errorText(status, body) {
  try {
    const d = JSON.parse(body).detail;
    if (typeof d === "string") return d;
  } catch { /* не JSON */ }
  return `${status}: ${body}`;
}

async function request(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...(options.headers || {}) },
  });
  if (!res.ok) {
    const body = await res.text();
    const err = new Error(errorText(res.status, body));
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// В историю уходят только роль и текст (без списков источников); пустые реплики (например, остановленный
// ответ без текста) провайдеры LLM не принимают, поэтому отбрасываем их.
function chatHistory(history) {
  return history
    .filter((m) => typeof m.content === "string" && m.content.trim() && !m.streaming)
    .map((m) => ({ role: m.role, content: m.content }));
}

export const api = {
  checkHealth: () => request("/"),
  me: () => request("/api/auth/me"),
  login: (email, password) => request("/api/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  register: (email, password, full_name) =>
    request("/api/auth/register", { method: "POST", body: JSON.stringify({ email, password, full_name }) }),
  // Выгрузка выбранных публикаций; формат: csv | bibtex | ris | json. Скачивает файл через браузер.
  exportPublications: async (ids, format = "csv") => {
    const res = await fetch(`${API_BASE}/api/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ publication_ids: ids, format }),
    });
    if (!res.ok) {
      const err = new Error(errorText(res.status, await res.text()));
      err.status = res.status;
      throw err;
    }
    const ext = { csv: "csv", bibtex: "bib", ris: "ris", json: "json" }[format] || "txt";
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = `publications.${ext}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  },
  getStats: () => request("/api/stats"),
  listPublications: (limit = 200) => request(`/api/publications?limit=${limit}`),

  // Иерархия: province → oblast → rayon → field → horizon → deposit
  getRayons: (oblastId) => request(`/api/rayons${oblastId ? `?oblast_id=${oblastId}` : ""}`),
  getRayon: (id) => request(`/api/rayons/${id}`),
  getFields: (rayonId) => request(`/api/fields${rayonId ? `?rayon_id=${rayonId}` : ""}`),
  getField: (id) => request(`/api/fields/${id}`),
  getHorizons: (fieldId) => request(`/api/horizons?field_id=${fieldId}`),

  getRayonPublications: (id) => request(`/api/rayons/${id}/publications`),
  getFieldPublications: (id) => request(`/api/fields/${id}/publications`),
  // Работы о районе/области/провинции месторождения в целом (не о самом месторождении)
  getFieldRegionalSummary: (id) => request(`/api/fields/${id}/regional-publications`),
  getRegionalPublications: (level, objectId, offset = 0, limit = 20) =>
    request(`/api/regional-publications?level=${level}&object_id=${objectId}&offset=${offset}&limit=${limit}`),

  getGisFields: () => request("/api/gis/fields"),

  semanticSearch: (query, top_k = 5) =>
    request("/api/search/semantic", {
      method: "POST",
      body: JSON.stringify({ query, top_k }),
    }),
  chat: (message, history = [], fieldId = null, useExternal = true, externalLanguage = "ru", sessionId = null) =>
    request("/api/chat", {
      method: "POST",
      body: JSON.stringify({
        message,
        history: chatHistory(history),
        field_id: fieldId,
        use_external: useExternal,
        external_language: externalLanguage,
        session_id: sessionId,
      }),
    }),
  // Админ-панель (только роль admin)
  adminUsers: () => request("/api/admin/users"),
  adminPatchUser: (id, body) => request(`/api/admin/users/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  adminPublications: (q = "", limit = 50, offset = 0) =>
    request(`/api/admin/publications?q=${encodeURIComponent(q)}&limit=${limit}&offset=${offset}`),
  adminAddPublication: (body) => request("/api/admin/publications", { method: "POST", body: JSON.stringify(body) }),
  adminEditPublication: (id, body) => request(`/api/admin/publications/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  adminDeletePublication: async (id) => {
    const res = await fetch(`${API_BASE}/api/publications/${id}`, { method: "DELETE", headers: authHeaders() });
    if (!res.ok) throw new Error(errorText(res.status, await res.text()));
  },
  adminLogs: (limit = 100, q = "") => request(`/api/admin/logs?limit=${limit}&q=${encodeURIComponent(q)}`),
  adminSettings: () => request("/api/admin/settings"),
  adminSaveSettings: (body) => request("/api/admin/settings", { method: "PUT", body: JSON.stringify(body) }),

  // История чатов (только для вошедших пользователей)
  listChatSessions: () => request("/api/chat/sessions"),
  createChatSession: (title = null) => request("/api/chat/sessions", { method: "POST", body: JSON.stringify({ title }) }),
  getChatMessages: (id) => request(`/api/chat/sessions/${id}/messages`),
  deleteChatSession: async (id) => {
    const res = await fetch(`${API_BASE}/api/chat/sessions/${id}`, { method: "DELETE", headers: authHeaders() });
    if (!res.ok) throw new Error(errorText(res.status, await res.text()));
  },
  // Потоковый чат (Server-Sent Events). Возвращает итоговый ответ (то же, что api.chat), а по пути вызывает:
  //   onStatus(text) — этап работы («Ищу в OpenAlex и Crossref…»),
  //   onToken(text)  — очередной кусок ответа (текст ещё не проверен: ссылки проверяются в конце),
  // signal — AbortSignal для кнопки «Стоп». EventSource не подходит: он умеет только GET.
  chatStream: async (message, history = [], fieldId = null, useExternal = true, externalLanguage = "ru",
                     { onStatus, onToken, signal, sessionId = null } = {}) => {
    const res = await fetch(`${API_BASE}/api/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      signal,
      body: JSON.stringify({
        message,
        history: chatHistory(history),
        field_id: fieldId,
        use_external: useExternal,
        external_language: externalLanguage,
        session_id: sessionId,
      }),
    });
    if (!res.ok || !res.body) throw new Error(`${res.status}: ${await res.text()}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let final = null;

    const handle = (block) => {
      let event = "message";
      const dataLines = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
        // строки, начинающиеся с «:», — служебные («ping»), пропускаем
      }
      if (!dataLines.length) return;
      const data = JSON.parse(dataLines.join("\n"));
      if (event === "status") onStatus?.(data.text);
      else if (event === "token") onToken?.(data.text);
      else if (event === "done") final = data;
      else if (event === "error") throw new Error(data.message || "Ошибка при получении ответа");
    };

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        handle(block);
      }
    }
    if (buffer.trim()) handle(buffer);
    if (!final) throw new Error("Соединение прервалось до окончания ответа");
    return final;
  },
  searchExternal: (q, limit = 10, language = "") =>
    request(
      `/api/search/external?q=${encodeURIComponent(q)}&limit=${limit}` +
        (language ? `&language=${language}` : "")
    ),
  saveExternalPublication: (publication, fieldId, researchTypeId, autoLink = false) =>
    request("/api/search/external/save", {
      method: "POST",
      body: JSON.stringify({
        publication,
        field_id: fieldId,
        research_type_id: researchTypeId,
        auto_link: autoLink,
      }),
    }),
  // Онтология и логический вывод
  ontologySummary: () => request("/api/ontology/summary"),
  ontologyObjects: () => request("/api/ontology/objects"),
  ontologyMapping: () => request("/api/ontology/mapping"),
  ontologyPublications: (kind, id, typeId, inference = true) =>
    request(
      `/api/ontology/publications?kind=${kind}&id=${id}&inference=${inference}` + (typeId ? `&type_id=${typeId}` : "")
    ),
  ontologySparql: (query, inferred = true) =>
    request("/api/ontology/sparql", { method: "POST", body: JSON.stringify({ query, inferred }) }),
  ontologyCheck: (demo = false) => request(`/api/ontology/check?demo=${demo}`),
  ontologyValidate: () => request("/api/ontology/validate"),
  ontologyExportUrl: (format = "owl", inferred = false) =>
    `${API_BASE}/api/ontology/export?format=${format}&inferred=${inferred}`,

  fulltextStatus: () => request("/api/fulltext/status"),
  fulltextFetch: (limit = 15) =>
    request("/api/fulltext/fetch", {
      method: "POST",
      body: JSON.stringify({ limit }),
    }),
  getImportDefaults: () => request("/api/import/default-queries"),
  importPreview: (queries, perQuery) =>
    request("/api/import/preview", {
      method: "POST",
      body: JSON.stringify({ queries, per_query: perQuery }),
    }),
  importCommit: (items) =>
    request("/api/import/commit", {
      method: "POST",
      body: JSON.stringify({ items }),
    }),
};

export { API_BASE };
