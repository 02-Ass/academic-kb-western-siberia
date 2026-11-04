import Icon from "./Icon";

export default function Footer() {
  return (
    <footer className="site-footer">
      <div className="brand brand-footer">
        <span className="brand-mark small"><Icon name="database" size={16} /></span>
        <span>НЕФТЕГАЗ.ЗНАНИЕ</span>
      </div>
      <p>Академическая база знаний по исследованиям и разработке нефтегазовых месторождений Западной Сибири</p>
    </footer>
  );
}
