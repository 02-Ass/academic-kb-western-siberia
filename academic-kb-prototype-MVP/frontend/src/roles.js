// Роли и доступ к вкладкам. Это только интерфейс: настоящая защита — на сервере (403 на любом закрытом маршруте).
//   гость  — Главная, Поиск и карта, ИИ-Ассистент (один текущий чат без сохранения)
//   user   — + Публикации, Внешний поиск, история чатов, экспорт
//   admin  — + Наполнение базы, Онтология, Админ-панель
export const ROLE_RANK = { guest: 0, user: 1, admin: 2 };

// [ключ вкладки, название, минимальная роль]
export const TABS = [
  ["home", "Главная", "guest"],
  ["map", "Поиск и карта", "guest"],
  ["chat", "ИИ-Ассистент", "guest"],
  ["publications", "Публикации", "user"],
  ["external", "Внешний поиск", "user"],
  ["import", "Наполнение базы", "admin"],
  ["ontology", "Онтология", "admin"],
  ["admin", "Админ-панель", "admin"],
];

export function roleOf(user) {
  return user && ROLE_RANK[user.role] !== undefined ? user.role : user ? "user" : "guest";
}

export function canAccess(role, tab) {
  const entry = TABS.find(([key]) => key === tab);
  return !!entry && ROLE_RANK[role] >= ROLE_RANK[entry[2]];
}

export function visibleTabs(role) {
  return TABS.filter(([key]) => canAccess(role, key));
}

// useRole(user): роль, список доступных вкладок и проверка доступа одним объектом.
export function useRole(user) {
  const role = roleOf(user);
  return { role, isAdmin: role === "admin", tabs: visibleTabs(role), can: (tab) => canAccess(role, tab) };
}
