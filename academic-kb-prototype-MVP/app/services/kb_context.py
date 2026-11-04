"""
Справка из базы знаний для LLM: структурированные данные об объектах,
которые упомянуты в вопросе (иерархия «провинция → область → район →
месторождение → пласт → залежь», параметры, число связанных публикаций).

Раздел 3.6.7 отчёта предполагает, что в контекст модели попадают не только
фрагменты документов, но и сведения из реляционной БД и онтологической
структуры. Эта справка делает именно это — и только на основе реальных,
заполненных полей: пустые параметры в справку не попадают, чтобы модель не
домысливала их.
"""
import re
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from .. import hierarchy, models
from ..tfidf import tokenize

_GENERIC_WORDS = (
    "нефтегазоносная нефтегазоносный нефтеносный провинция область район месторождение "
    "залежь пласт свита горизонт нефтегазовый"
)
_GENERIC = set(tokenize(_GENERIC_WORDS))
_STRUCTURE_RE = re.compile(r"иерарх|структур|входят|входит|состав|перечисл|какие\s+(?:месторожд|район|област|пласт|залеж)", re.I)
_LEVEL_WORDS = {
    "field": re.compile(r"месторожд"), "rayon": re.compile(r"район"), "oblast": re.compile(r"област"),
    "province": re.compile(r"провинц|бассейн"), "horizon": re.compile(r"пласт|горизонт|свит"),
    "deposit": re.compile(r"залеж"),
}


def _match(a: str, b: str) -> bool:
    if a == b:
        return True
    return min(len(a), len(b)) >= 5 and (a.startswith(b) or b.startswith(a))


def _name_matches(name: str, q_tokens: List[str]) -> bool:
    sig = [t for t in tokenize(name) if t not in _GENERIC]
    return bool(sig) and all(any(_match(s, q) for q in q_tokens) for s in sig)


def _year(d) -> Optional[int]:
    return d.year if d else None


def _line(head: str, parts: List[Optional[str]]) -> str:
    parts = [p.strip().rstrip(".").strip() for p in parts if p and p.strip()]
    return f"• {head}" + (": " + "; ".join(parts) if parts else "") + "."


def _params(**kw) -> List[str]:
    return [f"{label}: {value}" for label, value in kw.items() if value not in (None, "")]


def match_objects(db: Session, question: str) -> list:
    """Объекты базы (провинция, область, район, месторождение, пласт, залежь), названные в вопросе:
    список (уровень, объект ORM). Если в вопросе названо, о чём речь («район», «месторождение»),
    остаётся только этот уровень."""
    q_low = (question or "").lower()
    q_tokens = tokenize(question)
    if not q_tokens:
        return []
    matched = []
    for p in db.query(models.Province).all():
        if _name_matches(p.name, q_tokens):
            matched.append(("province", p))
    for o in db.query(models.Oblast).all():
        if _name_matches(o.name, q_tokens):
            matched.append(("oblast", o))
    for r in db.query(models.Rayon).all():
        if _name_matches(r.name, q_tokens):
            matched.append(("rayon", r))
    for f in db.query(models.Field).all():
        if _name_matches(f.name, q_tokens):
            matched.append(("field", f))
    has_level_word = {k: bool(rx.search(q_low)) for k, rx in _LEVEL_WORDS.items()}
    for h in db.query(models.Horizon).all():
        by_code = h.index_code and h.index_code.lower() in q_tokens
        by_formation = h.formation and _name_matches(h.formation, q_tokens) and has_level_word["horizon"]
        if by_code or by_formation:
            matched.append(("horizon", h))
    for d in db.query(models.Deposit).all():
        if has_level_word["deposit"] and _name_matches(d.name, q_tokens):
            matched.append(("deposit", d))
    # «Шаимский район» совпадает и с районом, и с месторождением «Шаимское»
    asked_levels = {lvl for lvl, present in has_level_word.items() if present}
    narrowed = [m for m in matched if m[0] in asked_levels]
    return narrowed or matched


def build_kb_context(db: Session, question: str) -> Tuple[Optional[str], List[str]]:
    """Возвращает (текст справки или None, список названий найденных объектов)."""
    q_low = (question or "").lower()
    q_tokens = tokenize(question)
    if not q_tokens:
        return None, []

    provinces = db.query(models.Province).all()
    matched = match_objects(db, question)

    want_tree = bool(_STRUCTURE_RE.search(q_low))
    if not matched and not want_tree:
        return None, []

    lines: List[str] = []
    names: List[str] = []

    def pubs(**kw) -> str:
        """Число публикаций объекта: по онтологии с логическим выводом (с разбивкой «напрямую / через
        вложенные объекты»); если онтология недоступна — по реляционному запросу."""
        try:
            from .. import ontology
            kind = next(k[:-3] for k in kw)              # province_id -> province
            obj_id = next(iter(kw.values()))
            st = ontology.get_state_nowait(db)       # не ждём пересчёта: вопрос в чате не должен тормозить
            if st is None:
                raise RuntimeError("онтология ещё строится")
            res = ontology.queries.publications_for(st, kind, obj_id, None, True, 0)
            if res["with_inference"] != res["direct"]:
                return (f"публикаций в базе (по онтологии, с логическим выводом): {res['with_inference']}, "
                        f"из них привязано к объекту напрямую: {res['direct']}")
            return f"публикаций в базе: {res['with_inference']}"
        except Exception:
            return f"публикаций в базе: {len(hierarchy.publication_ids_for_level(db, **kw))}"

    for level, obj in matched[:6]:
        if level == "province":
            kids = [o.name for o in obj.oblasts]
            lines.append(_line(f"Провинция «{obj.name}»", [
                obj.description, f"области в базе: {', '.join(kids)}" if kids else None,
                pubs(province_id=obj.province_id)]))
            names.append(f"провинция «{obj.name}»")
        elif level == "oblast":
            kids = [r.name for r in obj.rayons]
            lines.append(_line(f"Область «{obj.name}» (провинция «{obj.province.name}»)", [
                obj.description, f"районы: {', '.join(kids)}" if kids else None,
                pubs(oblast_id=obj.oblast_id)]))
            names.append(f"область «{obj.name}»")
        elif level == "rayon":
            ob = obj.oblast
            kids = [f.name for f in obj.fields]
            lines.append(_line(f"Район «{obj.name}» (область «{ob.name}», провинция «{ob.province.name}»)", [
                obj.description, f"месторождения района: {', '.join(kids)}" if kids else None,
                pubs(rayon_id=obj.rayon_id)]))
            names.append(f"район «{obj.name}»")
        elif level == "field":
            hz = [f"{h.index_code}" + (f" ({h.formation} свита)" if h.formation else "") for h in obj.horizons]
            facts = _params(**{"год открытия": _year(obj.discovery_date), "координаты (шир., долг.)": obj.coordinates,
                               "глубина, м": obj.depth, "обводнённость, %": obj.water_cut})
            lines.append(_line(f"Месторождение «{obj.name}» (район «{obj.rayon.name}»)", [
                *facts, obj.description, f"пласты: {', '.join(hz)}" if hz else None,
                pubs(field_id=obj.field_id)]))
            names.append(f"месторождение «{obj.name}»")
        elif level == "horizon":
            facts = _params(**{"свита": obj.formation, "мощность, м": obj.thickness, "тип коллектора": obj.reservoir_type,
                               "проницаемость, мД": obj.permeability, "пористость, %": obj.porosity})
            dep = [d.name for d in obj.deposits]
            lines.append(_line(f"Пласт «{obj.index_code}» (месторождение «{obj.field.name}»)", [
                *facts, f"залежи: {', '.join(dep)}" if dep else None]))
            names.append(f"пласт «{obj.index_code}»")
        elif level == "deposit":
            facts = _params(**{"тип нефти": obj.oil_type, "запасы": obj.reserves})
            lines.append(_line(
                f"Залежь «{obj.name}» (пласт «{obj.horizon.index_code}», месторождение «{obj.horizon.field.name}»)", facts))
            names.append(f"залежь «{obj.name}»")

    if want_tree:
        tree = []
        for p in provinces:
            for o in p.oblasts:
                for r in o.rayons:
                    fl = ", ".join(f.name for f in r.fields) or "месторождения не заведены"
                    tree.append(f"{p.name} → {o.name} → {r.name} → {fl}")
        if tree:
            lines.append("• Иерархия объектов в базе: " + "; ".join(tree) + ".")
            names.append("иерархия объектов")

    if not lines:
        return None, []
    header = ("Справка из базы знаний (структурированные данные локальной БД; параметры, которых здесь нет, "
              "в базе не заполнены — не домысливай их):")
    return header + "\n" + "\n".join(lines), names
