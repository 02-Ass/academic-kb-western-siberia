import PubCard from "./PubCard";
import RegionalPublications from "./RegionalPublications";

export default function FieldCard({ field, publications }) {
  if (!field) return null;

  const breadcrumb = [field.province, field.oblast, field.rayon].filter(Boolean);

  return (
    <div>
      <div className="breadcrumb">
        {breadcrumb.map((o) => (
          <span key={o.name}>{o.name}</span>
        ))}
      </div>
      <h2>{field.name}</h2>
      <span className="type-label">Месторождение</span>

      <div className="field-stats">
        {/* в базе известен год; дата хранится как YYYY-MM-DD, поэтому показываем только год */}
        {field.discovery_date && <div><span className="muted-text">Год открытия:</span> {String(field.discovery_date).slice(0, 4)}</div>}
        {field.coordinates && <div><span className="muted-text">Координаты:</span> {field.coordinates}</div>}
        {field.depth != null && <div><span className="muted-text">Глубина залегания:</span> {field.depth} м</div>}
        {field.water_cut != null && <div><span className="muted-text">Обводнённость:</span> {field.water_cut}%</div>}
        {!field.coordinates && field.depth == null && field.water_cut == null && (
          <div className="muted-text">Координаты и числовые параметры в базе не заполнены.</div>
        )}
      </div>
      {field.description && <p className="muted-text">{field.description}</p>}

      {field.horizons?.length > 0 && (
        <>
          <div className="section-title">Пласты</div>
          <ul className="children-list">
            {field.horizons.map((h) => (
              <li key={h.horizon_id}>
                {h.index_code}
                {h.formation && <span className="muted-text"> — {h.formation} свита</span>}
                {h.thickness != null && <span className="muted-text">, мощность {h.thickness} м</span>}
              </li>
            ))}
          </ul>
        </>
      )}

      <div className="section-title">Публикации, включая пласты и залежи месторождения</div>
      {publications.length === 0 ? (
        <div className="empty-state">Публикаций по самому месторождению не найдено.</div>
      ) : (
        publications.map((p) => <PubCard key={p.publication_id} p={p} />)
      )}

      <RegionalPublications fieldId={field.field_id} />
    </div>
  );
}
