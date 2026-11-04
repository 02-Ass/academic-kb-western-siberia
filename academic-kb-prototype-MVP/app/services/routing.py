"""
Стратегии поиска по намерению пользователя (Intent-Aware Routing).

Намерение определяет ``llm.detect_intent``; здесь — то, что от него зависит в поиске:

- ``COMPARE_OBJECTS``: вопрос делится на два объекта, для каждого делается свой поиск, результаты
  чередуются (чтобы один объект не вытеснил другой) и помечаются названием объекта;
- ``FIND_OBJECT``: объект ищется в базе (район, месторождение, пласт…), а поиск расширяется по иерархии
  (онтологии): сначала публикации самого объекта, затем его родительских объектов — месторождения,
  района, области, провинции.

``FIND_PUBLICATION``, ``EXPLAIN`` и ``UNKNOWN`` идут обычным поиском по всем публикациям.
"""
import re
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from .. import hierarchy, models, search

_SEP = r"(?:и|с|со|против|vs\.?|versus|and|with|to)"
_COMPARE_RULES = [
    re.compile(rf"(?:сравн\w*|сопостав\w*|compare)\s+(?P<a>.+?)\s+{_SEP}\s+(?P<b>.+)$", re.I),
    re.compile(r"(?:различи\w*|сходств\w*|разниц\w*|differences?|similarities)\s+между\s+(?P<a>.+?)\s+и\s+(?P<b>.+)$", re.I),
    re.compile(r"(?:differences?|similarities)\s+between\s+(?P<a>.+?)\s+and\s+(?P<b>.+)$", re.I),
    re.compile(r"чем\s+(?P<a>.+?)\s+(?:отличается|отличаются)\s+от\s+(?P<b>.+)$", re.I),
    re.compile(r"(?P<a>.+?)\s+(?:vs\.?|versus|против)\s+(?P<b>.+)$", re.I),
]
_LEAD = re.compile(r"^(?:пожалуйста|please|а|ну)\s+", re.I)


@dataclass
class ComparePair:
    """Два сравниваемых объекта: подпись для источников и текст поискового запроса."""
    a_label: str
    a_query: str
    b_label: str
    b_query: str


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip(" \t.,;:!?«»\"'()")).strip()


def _label(text: str) -> str:
    """Подпись объекта: слова с заглавной буквы («геологию Шаима» → «Шаима»); если таких нет — всё выражение."""
    caps = [w for w in text.split() if w[:1].isupper()]
    return " ".join(caps) or text


def _aspect(text: str) -> str:
    """Что именно сравнивают («геологию», «запасы»): слова без заглавной буквы в первой части вопроса."""
    return " ".join(w for w in text.split() if not w[:1].isupper())


def build_pair(a: str, b: str) -> Optional[ComparePair]:
    a, b = _clean(a), _clean(b)
    if not a or not b or a.lower() == b.lower():
        return None
    aspect = _aspect(a)
    # «сравни геологию Шаима и Самотлора»: предмет сравнения указан один раз, а искать его нужно у обоих.
    # Только если в первой части есть и предмет (строчные слова), и название объекта (с заглавной буквы), а во
    # второй — одно название: «сравни методы заводнения и ГРП» не меняется.
    shared = bool(aspect) and any(w[:1].isupper() for w in a.split()) and not _aspect(b)
    b_query = f"{b} {aspect}" if shared else b
    return ComparePair(_label(a), a, _label(b), b_query)


def split_compare(message: str) -> Optional[ComparePair]:
    """Делит просьбу о сравнении на два объекта правилами; None, если правила не сработали
    (тогда используется запасной вариант через LLM: ``llm.extract_compare_objects``)."""
    text = _LEAD.sub("", _clean(message))
    for rule in _COMPARE_RULES:
        m = rule.search(text)
        if m:
            pair = build_pair(m.group("a"), m.group("b"))
            if pair:
                return pair
    return None


def _per_object(top_k: int) -> int:
    return max(2, (top_k + 1) // 2)


def _mentions(db: Session, hit, label: str) -> bool:
    """Упоминается ли объект с подписью ``label`` в источнике: по названию, фрагментам или связанным объектам базы."""
    from ..tfidf import tokenize
    from .kb_context import _GENERIC, _match

    want = [t for t in tokenize(label) if t not in _GENERIC]
    if not want:
        return True                       # в подписи нет различающих слов — фильтровать нечем
    pub = hit.pub
    names = [pub.title or "", " ".join(hit.passages or [])]
    names += [o.name for rel in (pub.fields, pub.rayons, pub.oblasts, pub.provinces) for o in rel]
    names += [h.index_code for h in pub.horizons if h.index_code]
    have = set(tokenize(" ".join(names)))
    if all(any(_match(w, t) for t in have) for w in want):
        return True
    # работа на английском: название объекта по глоссарию («Шаим» → shaim) находится в её названии или аннотации
    from .glossary import translations_split
    latin = [t for t in tokenize(translations_split(label)[0]) if t not in _GENERIC]
    return bool(latin) and any(_match(w, t) for w in latin for t in have)


def compare_hits(db: Session, pair: ComparePair, *, top_k: int = 5, rayon_id=None, field_id=None) -> list:
    """Два поиска (по одному на объект) с чередованием результатов. У каждого найденного источника
    ``matched_field`` — название объекта, по которому он найден (в контексте LLM это поле «Объект»)."""
    per = _per_object(top_k)
    a_hits = search.retrieve(db, [pair.a_query], rayon_id=rayon_id, field_id=field_id, top_k=per)[1]
    b_hits = search.retrieve(db, [pair.b_query], rayon_id=rayon_id, field_id=field_id, top_k=per)[1]
    # Общие слова запроса («месторождения», «геология») находят работы о другом объекте: источник засчитывается
    # объекту, только если в нём (название, привязки, найденные фрагменты) встречается само название объекта.
    a_hits = [h for h in a_hits if h.score > 0 and _mentions(db, h, pair.a_label)]
    b_hits = [h for h in b_hits if h.score > 0 and _mentions(db, h, pair.b_label)]
    merged, seen = [], {}
    for i in range(max(len(a_hits), len(b_hits))):
        for hits, label in ((a_hits, pair.a_label), (b_hits, pair.b_label)):
            if i >= len(hits):
                continue
            h = hits[i]
            pid = h.pub.publication_id
            if pid in seen:                                    # работа подходит обоим объектам
                seen[pid].matched_field = f"{seen[pid].matched_field} и {label}"
                continue
            h.matched_field = label
            seen[pid] = h
            merged.append(h)
    return merged


NO_DATA = "Нет данных (в базе знаний)"


def compare_context(db: Session, pair: ComparePair, hits: list) -> str:
    """Справка для LLM при сравнении: оба объекта названы явно (столбцы таблицы), у каждого указано, сколько источников
    найдено и что о нём известно в базе. Объект без данных всё равно перечислен — модель не должна его терять."""
    from . import kb_context

    def count(label: str) -> int:
        return sum(1 for h in hits if label and label in (getattr(h, "matched_field", None) or ""))

    parts = [
        f"Сравнение объектов. Столбцы таблицы — ровно эти два объекта: 1) «{pair.a_label}»; 2) «{pair.b_label}». "
        f"Найдено источников в локальной базе: «{pair.a_label}» — {count(pair.a_label)}, «{pair.b_label}» — {count(pair.b_label)}. "
        f"Если по объекту нет ни справки, ни источников, в каждой ячейке его столбца пиши «{NO_DATA}»."
    ]
    for label, query in ((pair.a_label, pair.a_query), (pair.b_label, pair.b_query)):
        text = None
        for q in dict.fromkeys([query, label]):           # сначала весь запрос объекта, затем одно название
            text, _names = kb_context.build_kb_context(db, q)
            if text:
                break
        body = "\n".join(text.splitlines()[1:]) if text else f"• Объект «{label}» в структурированной справке базы не найден."
        parts.append(f"Справка по объекту «{label}»:\n{body}")
    return "\n\n".join(parts)


# --- FIND_OBJECT: расширение поиска по иерархии --------------------------------------------------

def _object_ids(db: Session, level: str, obj) -> Set[int]:
    """Публикации объекта, включая всё, что ниже него по иерархии."""
    if level in ("province", "oblast", "rayon", "field"):
        key = f"{level}_id"
        return hierarchy.publication_ids_for_level(db, **{key: getattr(obj, key)})
    if level == "horizon":
        ids = {r[0] for r in db.query(models.PublicationHorizon.publication_id)
               .filter(models.PublicationHorizon.horizon_id == obj.horizon_id)}
        deps = [d.deposit_id for d in obj.deposits]
        if deps:
            ids |= {r[0] for r in db.query(models.PublicationDeposit.publication_id)
                    .filter(models.PublicationDeposit.deposit_id.in_(deps))}
        return ids
    return {r[0] for r in db.query(models.PublicationDeposit.publication_id)
            .filter(models.PublicationDeposit.deposit_id == obj.deposit_id)}


_LEVEL_NAME = {"province": "провинция", "oblast": "область", "rayon": "район", "field": "месторождение",
               "horizon": "пласт", "deposit": "залежь"}


def object_chain(db: Session, level: str, obj) -> List[Tuple[str, object]]:
    """Объект и его родители от конкретного к общему: пласт → месторождение → район → область → провинция."""
    chain = [(level, obj)]
    field = None
    if level == "deposit":
        horizon = obj.horizon
        chain.append(("horizon", horizon))
        field = horizon.field
    elif level == "horizon":
        field = obj.field
    elif level == "field":
        field = obj
    if level in ("deposit", "horizon"):
        chain.append(("field", field))
    if field is not None:
        anc = hierarchy.field_ancestors(db, field)
        chain += [(lvl, anc[lvl]) for lvl in ("rayon", "oblast", "province") if anc.get(lvl) is not None]
    elif level == "rayon":
        anc = hierarchy.rayon_ancestors(db, obj)
        chain += [(lvl, anc[lvl]) for lvl in ("oblast", "province") if anc.get(lvl) is not None]
    elif level == "oblast":
        chain.append(("province", obj.province))
    return chain


def _obj_name(o) -> str:
    """У пласта нет поля name — его называет индекс (Т1, БС10)."""
    return getattr(o, "name", None) or getattr(o, "index_code", None) or "?"


def object_tiers(db: Session, level: str, obj) -> List[Tuple[str, Set[int]]]:
    """Ярусы публикаций от конкретного к общему: каждый следующий ярус — только то, чего нет в предыдущих.
    [("пласт T1", {…}), ("месторождение Шаимское", {…}), ("район …", {…}), …]."""
    tiers, seen = [], set()
    for lvl, o in object_chain(db, level, obj):
        ids = _object_ids(db, lvl, o) - seen
        seen |= ids
        tiers.append((f"{_LEVEL_NAME[lvl]} «{_obj_name(o)}»", ids))
    return tiers


def object_hits(db: Session, matched: list, queries: List[str], *, top_k: int = 5, rayon_id=None, field_id=None) -> Tuple[list, List[str]]:
    """Публикации по найденному в вопросе объекту: сначала его собственные (даже если текст почти не
    совпал с вопросом — привязка к объекту в базе уже сведение), затем родительских объектов, затем обычный
    поиск по всей базе. Возвращает (источники, пояснение откуда)."""
    if not matched:
        return [], []
    level, obj = matched[0]
    hits, seen, notes = [], set(), []
    for label, ids in object_tiers(db, level, obj):
        if len(hits) >= top_k or not ids:
            continue
        need = top_k - len(hits)
        found = search.retrieve(db, queries, rayon_id=rayon_id, field_id=field_id, top_k=need + len(seen),
                                allowed_ids=ids, fill_unscored=True)[1]
        added = 0
        for h in found:
            if h.pub.publication_id in seen or h.score <= 0:
                continue
            seen.add(h.pub.publication_id)
            h.matched_field = label
            hits.append(h)
            added += 1
            if len(hits) >= top_k:
                break
        if added:
            notes.append(f"{label}: {added}")
    return hits, notes
