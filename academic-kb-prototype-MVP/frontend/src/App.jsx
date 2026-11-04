import { useEffect, useState } from "react";
import MapView from "./components/MapView";
import FieldCard from "./components/FieldCard";
import SearchBar from "./components/SearchBar";
import SearchResults from "./components/SearchResults";
import ExternalSearchPanel from "./components/ExternalSearchPanel";
import ImportPanel from "./components/ImportPanel";
import OntologyPanel from "./components/OntologyPanel";
import HomePage from "./components/HomePage";
import PublicationsPage from "./components/PublicationsPage";
import Icon from "./components/Icon";
import AuthButton, { AuthModal } from "./components/AuthButton";
import ChatPage from "./components/ChatPage";
import AdminPanel from "./components/AdminPanel";
import { useRole } from "./roles";
import { api, setToken } from "./api";
import "./App.css";

export default function App() {
  const [apiOk, setApiOk] = useState(null);
  const [features, setFeatures] = useState([]);
  const [allFields, setAllFields] = useState([]);
  const [selectedField, setSelectedField] = useState(null);
  const [selectedPublications, setSelectedPublications] = useState([]);
  const [searchState, setSearchState] = useState(null);
  const [activeTab, setActiveTab] = useState("home");
  const [sidebarMode, setSidebarMode] = useState("empty");
  const [user, setUser] = useState(null);          // вошедший пользователь (null — гость)
  const [loginOpen, setLoginOpen] = useState(false);

  useEffect(() => {
    api.checkHealth().then(() => setApiOk(true)).catch(() => setApiOk(false));
    api.getGisFields().then((geojson) => setFeatures(geojson.features)).catch(() => {});
    api.getFields().then((fs) => setAllFields(fs)).catch(() => {});
    api.me().then((d) => setUser(d.user)).catch(() => {});
  }, []);

  const { role, isAdmin, tabs, can } = useRole(user);

  function logout() {
    setToken(null);
    setUser(null);
  }

  // переход по вкладкам через проверку прав: закрытая вкладка гостю предлагает войти, остальным — не открывается
  function go(tab) {
    if (can(tab)) setActiveTab(tab);
    else if (role === "guest") setLoginOpen(true);
  }

  // роль изменилась (выход, блокировка) или открыта закрытая страница — возвращаем на главную
  useEffect(() => {
    if (!can(activeTab)) setActiveTab("home");
  }, [role, activeTab]); // eslint-disable-line react-hooks/exhaustive-deps

  async function handleSelectField(id) {
    setSidebarMode("field");
    try {
      const [field, pubs] = await Promise.all([
        api.getField(id),
        api.getFieldPublications(id),
      ]);
      setSelectedField(field);
      setSelectedPublications(pubs);
    } catch (e) {
      console.error(e);
    }
  }

  async function handleSearch(query) {
    setSidebarMode("search");
    try {
      const data = await api.semanticSearch(query);
      setSearchState({ query, answer: data.answer, results: data.results });
    } catch (e) {
      console.error(e);
    }
  }


  let page;
  if (activeTab === "home") {
    page = <HomePage onNavigate={go} />;
  } else if (activeTab === "map") {
    page = (
      <div className="map-page">
        <div className="map-top">
          <div className="section-head left">
            <h2>Поиск по базе знаний</h2>
            <p>Найдите публикации, объекты и исследования</p>
          </div>
          <SearchBar onSearch={handleSearch} />
        </div>
        <div className="map-grid">
          <div id="map" className="card map-card">
            <MapView features={features} onSelectField={handleSelectField} />
          </div>
          <div id="sidebar" className="card">
            {sidebarMode === "empty" && (
              <div>
                <div className="empty-state">
                  <Icon name="pin" size={30} />
                  <p>
                    Выберите месторождение на карте или в списке ниже, чтобы увидеть его карточку, пласты и
                    связанные публикации, либо задайте поисковый запрос.
                  </p>
                </div>
                <div className="section-title">Месторождения в базе</div>
                <ul className="children-list">
                  {allFields.map((f) => (
                    <li key={f.field_id} onClick={() => handleSelectField(f.field_id)}>
                      {f.name}
                      {!f.coordinates && <span className="muted-text"> — нет координат, на карте не показано</span>}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {sidebarMode === "field" && <FieldCard field={selectedField} publications={selectedPublications} />}
            {sidebarMode === "search" && searchState && (
              <SearchResults query={searchState.query} answer={searchState.answer} results={searchState.results} />
            )}
          </div>
        </div>
      </div>
    );
  } else if (activeTab === "chat") {
    page = (
      <ChatPage user={user} canSave={isAdmin} fieldId={selectedField?.field_id ?? null} onRequireLogin={() => setLoginOpen(true)} onLogout={logout} />
    );
  } else if (activeTab === "publications") {
    page = <PublicationsPage />;
  } else if (activeTab === "external") {
    page = <div className="page-scroll"><ExternalSearchPanel fields={allFields} canSave={isAdmin} /></div>;
  } else if (activeTab === "import") {
    page = (
      <div className="page-scroll">
        <ImportPanel onImported={() => api.getFields().then(setAllFields).catch(() => {})} />
      </div>
    );
  } else if (activeTab === "admin") {
    page = <div className="page-scroll admin-page"><AdminPanel me={user} /></div>;
  } else {
    page = <div className="page-scroll"><OntologyPanel /></div>;
  }

  return (
    <div className="app-root">
      <nav className="navbar">
        <div className="navbar-inner">
          <button className="brand" onClick={() => setActiveTab("home")}>
            <span className="brand-mark"><Icon name="database" /></span>
            <span className="brand-name">НЕФТЕГАЗ.ЗНАНИЕ</span>
          </button>
          <div className="nav-links">
            {tabs.map(([key, label]) => (
              <button key={key} className={activeTab === key ? "active" : ""} onClick={() => go(key)}>
                {label}
              </button>
            ))}
          </div>
          <AuthButton user={user} onOpen={() => setLoginOpen(true)} onLogout={logout} />
          <span id="api-status" className={apiOk === null ? "" : apiOk ? "ok" : "error"} title="Состояние связи с сервером">
            <i /> {apiOk === null ? "проверка…" : apiOk ? "API" : "API недоступен"}
          </span>
        </div>
      </nav>
      <main>{page}</main>
      <AuthModal open={loginOpen} onClose={() => setLoginOpen(false)} onAuth={(u) => { setUser(u); setLoginOpen(false); }} />
    </div>
  );
}
