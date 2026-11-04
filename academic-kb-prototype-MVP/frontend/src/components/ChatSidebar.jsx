// Левая панель чата: «Новый чат», список сохранённых чатов, профиль / «Войти».
const Svg = ({ children, size = 16 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>
);
const PlusIcon = () => <Svg><path d="M12 5v14M5 12h14" /></Svg>;
const MessageIcon = () => <Svg><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2Z" /></Svg>;
const TrashIcon = () => <Svg size={15}><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6" /></Svg>;

// сегодня — время, раньше — дата: «14:05», «31.10»
export function shortTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" });
}

export default function ChatSidebar({ open, user, sessions, activeId, loading, onNew, onSelect, onDelete, onLogin, onLogout, onClose }) {
  return (
    <>
      {open && <div className="chat-sidebar-overlay" onClick={onClose} />}
      <aside className={`chat-sidebar ${open ? "open" : ""}`} aria-label="История чатов">
        <button className="chat-new" onClick={onNew}><PlusIcon /> Новый чат</button>

        <div className="chat-list">
          {!user ? (
            <div className="chat-list-empty">Вы в гостевом режиме: история не сохраняется после перезагрузки страницы.</div>
          ) : loading ? (
            <div className="chat-list-empty">Загрузка…</div>
          ) : sessions.length === 0 ? (
            <div className="chat-list-empty">У вас пока нет сохраненных чатов</div>
          ) : (
            sessions.map((s) => (
              <div key={s.id} className={`chat-item ${s.id === activeId ? "active" : ""}`}>
                <button className="chat-item-main" onClick={() => onSelect(s.id)} title={s.title}>
                  <MessageIcon />
                  <span className="chat-item-title">{s.title}</span>
                  <span className="chat-item-time">{shortTime(s.updated_at || s.created_at)}</span>
                </button>
                <button className="chat-item-del" onClick={() => onDelete(s)} title="Удалить чат" aria-label="Удалить чат">
                  <TrashIcon />
                </button>
              </div>
            ))
          )}
        </div>

        <div className="chat-profile">
          {user ? (
            <>
              <span className="chat-avatar">{(user.full_name || user.email)[0].toUpperCase()}</span>
              <span className="chat-profile-name" title={user.email}>{user.full_name || user.email}</span>
              <button className="chat-profile-btn" onClick={onLogout}>Выйти</button>
            </>
          ) : (
            <button className="chat-login" onClick={onLogin}>Войти</button>
          )}
        </div>
      </aside>
    </>
  );
}
