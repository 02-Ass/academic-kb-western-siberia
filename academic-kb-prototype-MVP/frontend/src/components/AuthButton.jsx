import { useEffect, useState } from "react";
import { api, setToken } from "../api";

// Кнопка в шапке: «Войти» или имя пользователя + «Выйти». Состояние входа хранит App (его же использует чат).
export default function AuthButton({ user, onOpen, onLogout }) {
  return user ? (
    <span className="auth-chip" title={user.email}>
      {user.full_name || user.email}{user.role === "admin" ? " · админ" : ""}
      <button className="auth-link" onClick={onLogout}>Выйти</button>
    </span>
  ) : (
    <button className="auth-link" onClick={onOpen}>Войти</button>
  );
}

// Окно входа / регистрации поверх страницы.
export function AuthModal({ open, onClose, onAuth }) {
  const [mode, setMode] = useState("login");
  const [form, setForm] = useState({ email: "", password: "", full_name: "" });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  // каждое открытие окна начинается с обычного входа и без старой ошибки
  useEffect(() => { if (open) { setMode("login"); setError(null); } }, [open]);
  if (!open) return null;

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const data = mode === "login"
        ? await api.login(form.email, form.password)
        : await api.register(form.email, form.password, form.full_name);
      setToken(data.access_token);
      setForm({ email: "", password: "", full_name: "" });
      onAuth(data.user);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-backdrop" onClick={onClose}>
      <form className="auth-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <h3>{mode === "login" ? "Вход" : "Регистрация"}</h3>
        {mode === "register" && (
          <input placeholder="Имя (необязательно)" value={form.full_name}
            onChange={(e) => setForm({ ...form, full_name: e.target.value })} />
        )}
        <input type="email" placeholder="Email" required autoFocus value={form.email}
          onChange={(e) => setForm({ ...form, email: e.target.value })} />
        <input type="password" placeholder="Пароль (не короче 8 символов)" required minLength={mode === "register" ? 8 : 1}
          value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
        {error && <div className="auth-error">{error}</div>}
        <button className="btn-primary" type="submit" disabled={busy}>{mode === "login" ? "Войти" : "Зарегистрироваться"}</button>
        <button type="button" className="auth-link" onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(null); }}>
          {mode === "login" ? "Нет аккаунта? Зарегистрироваться" : "Уже есть аккаунт? Войти"}
        </button>
      </form>
    </div>
  );
}
