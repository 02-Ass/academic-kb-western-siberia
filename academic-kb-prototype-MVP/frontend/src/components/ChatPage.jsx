import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import ChatPanel, { fromStored } from "./ChatPanel";
import ChatSidebar from "./ChatSidebar";

// Вкладка «ИИ-Ассистент»: слева история чатов, справа сам чат.
// Гость: один текущий чат без сохранения. Вошедший: чаты хранятся в БД и привязаны к его аккаунту.
export default function ChatPage({ user, canSave = false, fieldId, onRequireLogin, onLogout }) {
  const [sessions, setSessions] = useState([]);
  const [listLoading, setListLoading] = useState(false);
  const [activeId, setActiveId] = useState(null);
  const activeRef = useRef(null);          // id чата, в который идёт сохранение (меняется без пересоздания окна чата)
  const [chatKey, setChatKey] = useState(0);
  const [initial, setInitial] = useState([]);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [guestDialog, setGuestDialog] = useState(false);
  const [error, setError] = useState(null);

  const setActive = (id) => { activeRef.current = id; setActiveId(id); };
  const freshChat = (messages = []) => { setInitial(messages); setChatKey((k) => k + 1); };

  const refresh = useCallback(async () => {
    try { setSessions(await api.listChatSessions()); } catch { /* список не обновился — оставляем прежний */ }
  }, []);

  // вход / выход: подгружаем список или сбрасываем в новый гостевой чат
  useEffect(() => {
    setActive(null);
    setError(null);
    freshChat();
    if (user) {
      setListLoading(true);
      api.listChatSessions().then(setSessions).catch(() => setSessions([])).finally(() => setListLoading(false));
    } else {
      setSessions([]);
    }
  }, [user?.user_id]); // eslint-disable-line react-hooks/exhaustive-deps

  function newChat() {
    if (!user) { setGuestDialog(true); setSidebarOpen(false); return; }
    setActive(null);
    freshChat();
    setSidebarOpen(false);
  }

  async function selectSession(id) {
    setSidebarOpen(false);
    if (id === activeRef.current) return;
    setError(null);
    try {
      const rows = await api.getChatMessages(id);
      setActive(id);
      freshChat(fromStored(rows));
    } catch (e) {
      setError(`Не удалось открыть чат: ${e.message}`);
      refresh();
    }
  }

  async function removeSession(s) {
    if (!window.confirm(`Удалить чат «${s.title}»?`)) return;
    try {
      await api.deleteChatSession(s.id);
      setSessions((prev) => prev.filter((x) => x.id !== s.id));
      if (activeRef.current === s.id) { setActive(null); freshChat(); }
    } catch (e) {
      setError(`Не удалось удалить чат: ${e.message}`);
    }
  }

  // чат создаётся при первом вопросе, чтобы в базе не копились пустые чаты
  async function ensureSession() {
    if (!user) return null;
    if (activeRef.current) return activeRef.current;
    const created = await api.createChatSession();
    setActive(created.id);
    setSessions((prev) => [created, ...prev]);
    return created.id;
  }

  const hamburger = (
    <button className="chat-burger" onClick={() => setSidebarOpen(true)} aria-label="Показать историю чатов">☰</button>
  );

  return (
    <div className="chat-layout">
      <ChatSidebar
        open={sidebarOpen} user={user} sessions={sessions} activeId={activeId} loading={listLoading}
        onNew={newChat} onSelect={selectSession} onDelete={removeSession}
        onLogin={() => { setSidebarOpen(false); onRequireLogin(); }} onLogout={onLogout} onClose={() => setSidebarOpen(false)}
      />
      <section className="chat-main">
        {error && <div className="chat-banner" onClick={() => setError(null)}>{error}</div>}
        <ChatPanel
          key={chatKey} fieldId={fieldId} canSave={canSave} initialMessages={initial} topBar={hamburger}
          ensureSession={ensureSession} onAnswered={refresh}
        />
      </section>

      {guestDialog && (
        <div className="auth-backdrop" onClick={() => setGuestDialog(false)}>
          <div className="auth-modal" onClick={(e) => e.stopPropagation()}>
            <h3>Нужен вход</h3>
            <p className="guest-text">Войдите, чтобы сохранять историю и использовать все возможности.</p>
            <button className="btn-primary" onClick={() => { setGuestDialog(false); onRequireLogin(); }}>Войти</button>
            <button className="auth-link" onClick={() => setGuestDialog(false)}>Продолжить как гость</button>
          </div>
        </div>
      )}
    </div>
  );
}
