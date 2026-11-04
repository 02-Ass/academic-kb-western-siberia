import { useEffect, useState } from "react";
import { api } from "../api";
import Icon from "./Icon";
import Footer from "./Footer";

const fmt = (n) => (n == null ? "—" : new Intl.NumberFormat("ru-RU").format(n));

function Derrick() {
  return (
    <svg width="80" height="80" viewBox="0 0 80 80" fill="none" aria-hidden="true">
      <rect x="36" y="20" width="8" height="48" rx="1" fill="#1a4b8c" opacity="0.2" />
      <polygon points="40,8 28,28 52,28" fill="#1a4b8c" opacity="0.3" />
      <rect x="30" y="28" width="20" height="3" rx="1" fill="#1a4b8c" opacity="0.25" />
      <line x1="26" y1="68" x2="36" y2="28" stroke="#1a4b8c" strokeWidth="2" opacity="0.3" />
      <line x1="54" y1="68" x2="44" y2="28" stroke="#1a4b8c" strokeWidth="2" opacity="0.3" />
      <circle cx="40" cy="14" r="3" fill="#3b8dd9" opacity="0.5" />
      <rect x="24" y="68" width="32" height="4" rx="2" fill="#1a4b8c" opacity="0.15" />
    </svg>
  );
}

export default function HomePage({ onNavigate }) {
  const [stats, setStats] = useState(null);
  useEffect(() => {
    api.getStats().then(setStats).catch(() => setStats({}));
  }, []);

  const items = [
    ["publications", "Публикаций"],
    ["fields", "Месторождений"],
    ["authors", "Авторов"],
    ["rayons", "Районов"],
  ];

  return (
    <div className="page-scroll home-bg">
      <section className="hero">
        <div className="hero-derrick anim-fade"><Derrick /></div>
        <h1 className="hero-title anim-up">
          Академическая база знаний<br />нефтегазовых объектов
        </h1>
        <p className="hero-sub anim-up d1">по исследованиям и разработке нефтегазовых месторождений</p>
        <p className="hero-sub2 anim-up d1">
          Семантический поиск научных публикаций по нефтегазовым исследованиям Западной Сибири
        </p>
        <div className="hero-actions anim-up d2">
          <button className="btn-primary" onClick={() => onNavigate("map")}>
            <Icon name="brain" /> Начать поиск
          </button>
          <button className="btn-secondary" onClick={() => onNavigate("chat")}>
            <Icon name="sparkles" /> ИИ-Ассистент
          </button>
        </div>
        <div className="stat-grid anim-up d3">
          {items.map(([key, label]) => (
            <div className="stat-card" key={key}>
              <div className="stat-num">{fmt(stats?.[key])}</div>
              <div className="stat-label">{label}</div>
            </div>
          ))}
        </div>
      </section>
      <Footer />
    </div>
  );
}
