import { useState, useRef, useEffect } from "react";
import { api } from "../api";
import Markdown from "./Markdown";
import { doiUrl, publicationHref } from "../utils";
import ExportMenu from "./ExportMenu";

function shortAuthors(list) {
  if (!list || list.length === 0) return "автор не указан";
  const first = list.slice(0, 3).join(", ");
  return list.length > 3 ? `${first} и др.` : first;
}

// итоговый ответ сервера (событие done или сохранённый в истории) -> сообщение ассистента в списке
export function toAssistantMessage(data) {
  return {
    role: "assistant",
    content: data.answer,
    sources: data.sources,
    externalSources: data.external_sources,
    externalQuery: data.external_query,
    externalErrors: data.external_errors,
    externalNote: data.external_note,
    citations: data.citations,
    citationWarnings: data.citation_warnings,
    confidence: data.confidence,
    isOfftopic: data.is_offtopic,
    suggestions: data.suggestions,
    kbObjects: data.kb_objects,
    localQueries: data.local_queries,
    ontology: data.ontology,
  };
}

// сообщения из БД -> сообщения чата (ответ ассистента восстанавливается вместе с источниками)
export function fromStored(rows) {
  return rows.map((m) => (m.role === "assistant" ? { ...toAssistantMessage(m.meta || { answer: m.content }), content: m.content } : { role: "user", content: m.content }));
}

export default function ChatPanel({ fieldId, initialMessages = [], ensureSession = null, onAnswered = null, topBar = null, canSave = false }) {
  const [messages, setMessages] = useState(initialMessages); // {role, content, sources, externalSources, ...}
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [useExternal, setUseExternal] = useState(true);
  const [externalLanguage, setExternalLanguage] = useState("ru");
  // подключён ли LLM — не зависит от того, нужен ли он был для последнего ответа (на «Привет» он не нужен)
  const [llmInfo, setLlmInfo] = useState(null);
  const [savedKeys, setSavedKeys] = useState(new Set());
  const bottomRef = useRef(null);
  const abortRef = useRef(null);

  useEffect(() => {
    // во время печати прокручиваем без анимации, иначе она «дёргается» на каждый кусок текста
    bottomRef.current?.scrollIntoView({ behavior: loading ? "auto" : "smooth" });
  }, [messages, loading]);

  // при уходе со вкладки чата незаконченный запрос прерываем
  useEffect(() => () => abortRef.current?.abort(), []);

  async function saveToBase(p, key) {
    try {
      await api.saveExternalPublication(p, null, null, true); // привязка по тексту
      setSavedKeys((prev) => new Set(prev).add(key));
    } catch (err) {
      alert(`Не удалось сохранить: ${err.message}`);
    }
  }

  // «Стоп»: прерывает чтение потока; сервер при этом прекращает генерацию и не тратит токены
  function stop() {
    abortRef.current?.abort();
  }

  async function send(e, preset) {
    e?.preventDefault();
    const text = (preset ?? input).trim();
    if (!text || loading) return;

    const controller = new AbortController();
    abortRef.current = controller;
    const newHistory = [...messages, { role: "user", content: text }];
    // пустое сообщение ассистента: в него по мере поступления дописывается ответ
    setMessages([...newHistory, { role: "assistant", content: "", streaming: true, status: "Отправляю вопрос…" }]);
    setInput("");
    setLoading(true);

    // обновляет последнее сообщение, пока оно ещё «печатается»
    const updateLast = (fn) =>
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (!last || !last.streaming) return prev;
        return [...prev.slice(0, -1), fn(last)];
      });

    try {
      // вошедшему пользователю сначала создаётся чат (один раз), ответ сохраняется в него после завершения генерации
      let sessionId = null;
      try { sessionId = ensureSession ? await ensureSession() : null; } catch { sessionId = null; }
      const data = await api.chatStream(text, messages, fieldId, useExternal, externalLanguage, {
        signal: controller.signal,
        sessionId,
        onStatus: (status) => updateLast((m) => ({ ...m, status })),
        onToken: (chunk) => updateLast((m) => ({ ...m, content: m.content + chunk, status: null })),
      });
      setLlmInfo({ configured: data.llm_configured, provider: data.llm_provider });
      // подставляем проверенный текст, источники и оценку надёжности вместо «сырого» потока
      setMessages([...newHistory, toAssistantMessage(data)]);
      if (sessionId && onAnswered) onAnswered();
    } catch (err) {
      if (err.name === "AbortError") {
        updateLast((m) => ({ ...m, streaming: false, status: null, stopped: true }));
      } else {
        setMessages([
          ...newHistory,
          { role: "assistant", content: `Ошибка запроса к чат-боту: ${err.message}` },
        ]);
      }
    } finally {
      abortRef.current = null;
      setLoading(false);
    }
  }

  return (
    <div className="chat-panel">
      <div className="chat-header">
        {topBar}
        <h3 className="chat-title">Чат-бот интеллектуального поиска</h3>
        {llmInfo !== null && (
          <span className={`llm-badge ${llmInfo.configured ? "on" : "off"}`}>
            {llmInfo.configured ? `LLM: ${llmInfo.provider}` : "LLM не настроен (шаблонный ответ)"}
          </span>
        )}
      </div>

      <div className="chat-messages">
        {messages.length === 0 && (
          <div className="empty-state">
            Задайте вопрос на естественном языке, например: «Какие исследования
            проводились в Шаимском районе после 2010 года?» Бот ищет в локальной
            базе знаний и, если включено, в OpenAlex и Crossref, и отвечает по
            найденным аннотациям.
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`chat-message ${m.role}`}>
            <div className={`chat-bubble ${m.role === "assistant" ? "md-bubble" : ""} ${m.streaming && m.content ? "chat-typing" : ""}`}>
              {m.streaming && m.status && !m.content && <div className="chat-status">{m.status}</div>}
              {m.role === "assistant" && (m.content || !m.streaming) ? (
                <Markdown text={m.content} sources={m.citations || []} />
              ) : m.role !== "assistant" ? (
                m.content
              ) : null}
              {m.stopped && (
                <div className="chat-status">
                  {m.content ? "Генерация остановлена. Ссылки в этом тексте не проверены." : "Генерация остановлена."}
                </div>
              )}
            </div>
            {m.role === "assistant" && m.suggestions && m.suggestions.length > 0 && (
              <div className="chat-chips">
                {m.suggestions.map((q) => (
                  <button key={q} type="button" disabled={loading} onClick={() => send(null, q)}>{q}</button>
                ))}
              </div>
            )}
            {m.role === "assistant" && m.confidence && !m.isOfftopic && (
              <div className={`chat-confidence ${m.confidence}`} title="Оценка по числу и полноте найденных источников, а не гарантия верности">
                Надёжность опоры на источники: {{ high: "высокая", medium: "средняя", low: "низкая" }[m.confidence] || m.confidence}
              </div>
            )}

            {m.kbObjects && m.kbObjects.length > 0 && (
              <div className="chat-sources">
                <div className="chat-sources-label">Использована справка из базы знаний: {m.kbObjects.join(", ")}</div>
              </div>
            )}

            {m.ontology && (
              <div className="chat-sources onto-chat">
                <div className="chat-sources-label">
                  По онтологии (логический вывод): {m.ontology.level} «{m.ontology.object_name}» — публикаций{" "}
                  {m.ontology.with_inference}, из них записано в базе напрямую {m.ontology.direct}
                </div>
                <details>
                  <summary>показать публикации и объяснение вывода</summary>
                  {m.ontology.publications.map((p) => (
                    <div key={p.publication_id} className="chat-source-item">
                      {p.title} ({p.year || "год не указан"}) —{" "}
                      {p.how === "inferred" ? "найдена выводом" : "привязана напрямую"}
                      {p.explanation && <div className="onto-chat-expl">{p.explanation}</div>}
                    </div>
                  ))}
                </details>
              </div>
            )}

            {m.citationWarnings && m.citationWarnings.length > 0 && (
              <div className="chat-sources chat-warning">
                Проверка ссылок: {m.citationWarnings.join(" ")}
              </div>
            )}

            {m.sources && m.sources.length > 0 && (
              <div className="chat-sources">
                <div className="chat-sources-label">
                  Из локальной базы знаний
                  {m.localQueries && m.localQueries.length > 1 ? ` (поиск: «${m.localQueries.join("», «")}»)` : ""}
                </div>
                {m.sources.map((s) => (
                  <div key={s.publication_id} className="chat-source-item">
                    {publicationHref(s) ? (
                      <a href={publicationHref(s)} target="_blank" rel="noreferrer">{s.title}</a>
                    ) : (
                      s.title
                    )}{" "}({(s.authors || []).map((a) => `${a.last_name} ${a.first_name[0]}.`).join(", ") || "автор не указан"}, {s.year || "—"})
                  </div>
                ))}
                <ExportMenu ids={m.sources.map((s) => s.publication_id)} />
              </div>
            )}

            {m.externalSources && m.externalSources.length > 0 && (
              <div className="chat-sources">
                <div className="chat-sources-label">
                  Из внешних баз (поиск: «{m.externalQuery}»)
                </div>
                {m.externalSources.map((p, j) => {
                  const href = doiUrl(p.doi) || p.url;
                  return (
                    <div key={j} className="chat-source-item">
                      {href ? (
                        <a href={href} target="_blank" rel="noreferrer">{p.title}</a>
                      ) : (
                        p.title
                      )}{" "}
                      ({shortAuthors(p.authors)}, {p.year || "—"}, {p.source}
                      {p.abstract ? "" : ", без аннотации"}){" "}
                      {canSave && (
                        <button
                          className="chat-save"
                          onClick={() => saveToBase(p, `${i}-${j}`)}
                          disabled={savedKeys.has(`${i}-${j}`)}
                        >
                          {savedKeys.has(`${i}-${j}`) ? "в базе ✓" : "в базу"}
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {m.externalNote && (
              <div className="chat-sources chat-warning">{m.externalNote}</div>
            )}

            {m.externalErrors && m.externalErrors.length > 0 && (
              <div className="chat-sources chat-warning">
                Часть внешних баз не ответила: {m.externalErrors.join("; ")}
              </div>
            )}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      <div className="chat-option">
        <label>
          <input
            type="checkbox"
            checked={useExternal}
            onChange={(e) => setUseExternal(e.target.checked)}
          />{" "}
          Искать также в OpenAlex и Crossref
        </label>
        <select
          value={externalLanguage}
          onChange={(e) => setExternalLanguage(e.target.value)}
          disabled={!useExternal}
          title="Язык публикаций из внешних баз"
        >
          <option value="ru">только русский</option>
          <option value="en">только английский</option>
          <option value="any">любой язык</option>
        </select>
      </div>

      <form className="chat-input-row" onSubmit={send}>
        <input
          type="text"
          placeholder="Ваш вопрос…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={loading}
        />
        {loading ? (
          <button type="button" className="chat-stop" onClick={stop} title="Прервать генерацию ответа">Стоп</button>
        ) : (
          <button type="submit">Отправить</button>
        )}
      </form>
      <div className="chat-chips">
        {["Исследования Шаимского района", "Геофизика Шаимского месторождения", "Что ты умеешь?"].map((q) => (
          <button key={q} type="button" disabled={loading} onClick={() => send(null, q)}>{q}</button>
        ))}
      </div>
    </div>
  );
}
