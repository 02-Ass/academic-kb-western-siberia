"""
Интеллектуальный поиск (раздел 3.6 отчёта).

Что изменилось по сравнению с первой версией:
- поиск идёт по фрагментам текста (аннотация, полный текст), а не по склеенной
  в одну строку публикации; публикация оценивается по лучшему фрагменту, а в
  LLM уходят именно совпавшие фрагменты (services/index.py);
- запрос ищется на двух языках: исходный вопрос и его английская формулировка
  (её делает LLM), плюс предметный словарь переводов (services/glossary.py),
  поэтому русский вопрос находит англоязычные аннотации и наоборот;
- опционально добавляется смысловой (векторный) поиск через сервис
  эмбеддингов (app/embeddings.py), если он настроен.
"""
from dataclasses import dataclass, field as dc_field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy.orm import Session, selectinload

from . import embeddings, hierarchy, llm, models
from .services import glossary
from .services.index import get_index

# Переводы терминов (app/services/glossary.py) ищутся ОТДЕЛЬНЫМИ запросами со своим весом, а не
# подмешиваются к исходному запросу. Подмешивание размывало запрос и портило выдачу на языке
# вопроса (на английских вопросах MRR падал с 0,94 до 0,68). Русский вопрос -> английские
# публикации: вес 1,0; английский вопрос -> русские публикации: 0,7 (родной язык вопроса важнее).
# Оценка публикации, привязанной к объекту, но не совпавшей с запросом по тексту (retrieve(fill_unscored=True)).
# Положительная и очень малая: такие публикации остаются в выдаче, но идут после настоящих совпадений.
UNSCORED_FLOOR = 0.001
TRANSLATION_WEIGHT = 1.0
REVERSE_WEIGHT = 0.7

_EAGER = (
    selectinload(models.Publication.author_links).selectinload(models.PublicationAuthor.author),
    selectinload(models.Publication.research_types),
    selectinload(models.Publication.fields),
    selectinload(models.Publication.chunks),
)


@dataclass
class Hit:
    pub: models.Publication
    score: float
    matched_field: Optional[str] = None
    passages: List[str] = dc_field(default_factory=list)
    via: str = "lexical"  # lexical | semantic | both


def _batches(items: List[int], size: int = 500) -> Iterable[List[int]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _resolve_field_ids(db: Session, rayon_id: Optional[int], field_id: Optional[int]) -> List[int]:
    if field_id is not None:
        return [field_id]
    if rayon_id is not None:
        return hierarchy.descendant_field_ids(db, rayon_id=rayon_id)
    return []


def _apply_filters(
    db: Session, ranked: List[int], research_type_id: Optional[int],
    year_from: Optional[int], year_to: Optional[int],
) -> List[int]:
    if not ranked or (research_type_id is None and year_from is None and year_to is None):
        return ranked
    keep: Set[int] = set(ranked)
    if year_from is not None or year_to is not None:
        years: Dict[int, Optional[int]] = {}
        for batch in _batches(ranked):
            for pid, year in db.query(models.Publication.publication_id, models.Publication.year).filter(
                models.Publication.publication_id.in_(batch)
            ).all():
                years[pid] = year
        for pid in list(keep):
            y = years.get(pid)
            if y is None or (year_from is not None and y < year_from) or (year_to is not None and y > year_to):
                keep.discard(pid)
    if research_type_id is not None:
        with_type: Set[int] = set()
        for batch in _batches(ranked):
            with_type.update(
                row[0] for row in db.query(models.PublicationResearchType.publication_id).filter(
                    models.PublicationResearchType.type_id == research_type_id,
                    models.PublicationResearchType.publication_id.in_(batch),
                ).all()
            )
        keep &= with_type
    return [pid for pid in ranked if pid in keep]


def retrieve(
    db: Session,
    queries: List[str],
    *,
    rayon_id: Optional[int] = None,
    field_id: Optional[int] = None,
    research_type_id: Optional[int] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    top_k: int = 5,
    passages_per_pub: int = 3,
    trace: Optional[Dict[int, dict]] = None,
    allowed_ids: Optional[Set[int]] = None,
    fill_unscored: bool = False,
) -> Tuple[List[int], List[Hit]]:
    """Возвращает (id месторождений области поиска, найденные публикации).
    queries — варианты запроса (исходный вопрос, его английская формулировка…):
    публикация оценивается по лучшему из вариантов.
    trace — для диагностики: если передан словарь, в него записывается, какой вариант
    запроса и какая единица (карточка/фрагмент) дали публикации лучшую оценку.
    allowed_ids — искать только среди этих публикаций (например, привязанных к объекту по онтологии).
    fill_unscored — дополнить выдачу публикациями из allowed_ids, у которых нет совпадений с запросом
    (оценка UNSCORED_FLOOR, новые сверху): они привязаны к объекту в базе, и это уже сведения по объекту."""
    queries = [q for q in queries if q and q.strip()]
    resolved_field_ids = _resolve_field_ids(db, rayon_id, field_id)

    allowed: Optional[Set[int]] = None
    if rayon_id is not None or field_id is not None:
        allowed = hierarchy.publication_ids_for_level(db, rayon_id=rayon_id, field_id=field_id)
        if not allowed:
            return resolved_field_ids, []
    if allowed_ids is not None:
        allowed = set(allowed_ids) if allowed is None else (allowed & set(allowed_ids))
        if not allowed:
            return resolved_field_ids, []

    index = get_index(db)
    best: Dict[int, float] = {}
    chunk_scores: Dict[int, Dict[int, float]] = {}
    variants: List[Tuple[str, float, str]] = []
    for i, q in enumerate(queries):
        variants.append((q, 1.0, "вопрос" if i == 0 else "формулировка LLM"))
        fwd, rev = glossary.translations_split(q)
        if fwd:
            variants.append((fwd, TRANSLATION_WEIGHT, "перевод на английский"))
        if rev:
            variants.append((rev, REVERSE_WEIGHT, "перевод на русский"))
    for text, weight, label in variants:
        for u, s in index.score_units(text).items():
            s *= weight
            pid = index.unit_pub[u]
            if allowed is not None and pid not in allowed:
                continue
            if s > best.get(pid, 0.0):
                best[pid] = s
                if trace is not None:
                    trace[pid] = {"score": s, "variant": label, "unit": "карточка" if index.unit_chunk[u] == 0 else "фрагмент"}
            cid = index.unit_chunk[u]
            if cid:
                per_pub = chunk_scores.setdefault(pid, {})
                if s > per_pub.get(cid, 0.0):
                    per_pub[cid] = s

    # смысловой поиск (если настроен): добавляет публикации, найденные по смыслу
    semantic: Dict[int, float] = {}
    if embeddings.is_configured():
        semantic = embeddings.semantic_scores(db, queries, allowed)
    via: Dict[int, str] = {pid: "lexical" for pid in best}
    for pid, sem in semantic.items():
        if sem > best.get(pid, 0.0):
            via[pid] = "both" if pid in best else "semantic"
            best[pid] = sem
            if trace is not None:
                trace[pid] = {"score": sem, "variant": "эмбеддинги", "unit": "вектор"}
        elif pid in best:
            via[pid] = "both"

    ranked = sorted(best, key=best.get, reverse=True)
    ranked = _apply_filters(db, ranked, research_type_id, year_from, year_to)
    if fill_unscored and allowed is not None and len(ranked) < top_k:
        from sqlalchemy import func

        rest = (
            db.query(models.Publication.publication_id)
            .filter(models.Publication.publication_id.in_(allowed - set(ranked)))
            .order_by(func.coalesce(models.Publication.year, 0).desc(), models.Publication.publication_id)
            .limit(top_k - len(ranked)).all()
        )
        for (pid,) in rest:
            best[pid] = UNSCORED_FLOOR
            ranked.append(pid)
    ranked = ranked[:top_k]
    if not ranked:
        return resolved_field_ids, []

    pubs = {
        p.publication_id: p
        for p in db.query(models.Publication).options(*_EAGER).filter(models.Publication.publication_id.in_(ranked)).all()
    }

    hits: List[Hit] = []
    for pid in ranked:
        pub = pubs.get(pid)
        if pub is None:
            continue
        by_id = {c.chunk_id: c for c in pub.chunks}
        top_chunks = sorted(chunk_scores.get(pid, {}).items(), key=lambda kv: kv[1], reverse=True)[:passages_per_pub]
        passages = [by_id[cid].text[:900] for cid, _s in top_chunks if cid in by_id]
        if pub.chunks and len(passages) < passages_per_pub:
            # аннотация (первый фрагмент) входит в «карточку» и сама по себе не ранжируется —
            # добавляем её: она нужна модели как общий контекст (и единственный, если полного текста нет)
            first = sorted(pub.chunks, key=lambda c: (c.chunk_index, c.chunk_id))[0]
            if first.chunk_id not in {cid for cid, _s in top_chunks}:
                passages.append(first.text[:900])
        matched = next((f.name for f in pub.fields if f.field_id in resolved_field_ids), None)
        hits.append(Hit(pub=pub, score=float(best[pid]), matched_field=matched, passages=passages, via=via.get(pid, "lexical")))
    return resolved_field_ids, hits


def semantic_search(
    db: Session,
    query: str,
    rayon_id: Optional[int] = None,
    field_id: Optional[int] = None,
    research_type_id: Optional[int] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    top_k: int = 5,
    with_answer: bool = True,
    extra_queries: Optional[List[str]] = None,
):
    """Совместимая обёртка над retrieve(): (id месторождений, [(публикация,
    оценка, месторождение)], текст ответа). extra_queries — дополнительные
    формулировки запроса (например, английская)."""
    resolved_field_ids, hits = retrieve(
        db, [query] + list(extra_queries or []),
        rayon_id=rayon_id, field_id=field_id, research_type_id=research_type_id,
        year_from=year_from, year_to=year_to, top_k=top_k,
    )
    results = [(h.pub, h.score, h.matched_field) for h in hits]
    if not hits:
        filtered = any(v is not None for v in (rayon_id, field_id, research_type_id, year_from, year_to))
        msg = (
            "По заданным условиям (объект/тип исследования/период) релевантных публикаций в базе знаний не найдено."
            if filtered else "Информация по запросу не найдена в базе знаний."
        )
        return resolved_field_ids, results, msg
    answer = _build_answer(query, hits) if with_answer else ""
    return resolved_field_ids, results, answer


def passages_text(hit: Hit, limit: int = 1800) -> Optional[str]:
    """Фрагменты, найденные по запросу, одним текстом для контекста LLM."""
    text = " … ".join(hit.passages).strip()
    return text[:limit] or None


def _authors(pub: models.Publication) -> Optional[str]:
    return ", ".join(a.author.full_name for a in pub.author_links) or None


def _build_answer(query: str, hits: List[Hit]) -> str:
    if llm.is_configured():
        context = [
            {
                "title": h.pub.title, "authors": _authors(h.pub), "year": h.pub.year,
                "journal": h.pub.journal, "doi": h.pub.doi, "url": h.pub.link,
                "origin": "локальная база знаний", "text_snippet": passages_text(h),
                "object_name": h.matched_field,
            }
            for h in hits
        ]
        llm_answer = llm.generate_answer(query, context)
        if llm_answer is not None:
            # НОВОЕ: generate_answer возвращает dict; нужен только текст (строку тоже принимаем)
            return llm_answer["text"] if isinstance(llm_answer, dict) else llm_answer
    return _build_template_answer(query, hits)


def _build_template_answer(query: str, hits: List[Hit]) -> str:
    lines = [f"По запросу «{query}» найдены следующие материалы:"]
    for h in hits:
        authors = _authors(h.pub) or "автор не указан"
        note = f" (объект: {h.matched_field})" if h.matched_field else ""
        lines.append(f"— {h.pub.title} ({authors}, {h.pub.year or 'г. не указан'}){note}")
    lines.append("Источники перечислены выше; полные метаданные и ссылки доступны в карточках публикаций.")
    return "\n".join(lines)
