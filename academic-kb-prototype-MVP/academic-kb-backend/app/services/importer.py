"""
Массовое наполнение локальной базы публикациями из внешних источников.

Двухшаговый процесс: build_preview() ищет и размечает кандидатов, но
ничего не сохраняет; пользователь просматривает список и выбирает нужные;
затем сохраняет выбранные через save_publication().
"""
import asyncio
import os
from typing import List, Optional

from sqlalchemy.orm import Session

from .. import models
from ..sources.manager import SourceManager
from ..sources.models import PublicationResult
from . import classifier
from .publication_deduplicator import deduplicate
from .publication_saver import ExistingIndex

DEFAULT_QUERIES: List[str] = [
    # английские — основной охват OpenAlex/Crossref
    "Shaim oil and gas region West Siberia",
    "Shaim oil field Tyumen formation",
    "Shaimsky petroleum district Jurassic",
    "Shushma oil field West Siberia",
    "Lovinskoye oil field Shaim",
    "West Siberian basin Jurassic reservoir geology",
    "West Siberian basin groundwater hydrogeology oil fields",
    "West Siberian basin tectonics geodynamics petroleum",
    "pre-Jurassic basement oil Western Siberia Shaim",
    # русские — русскоязычные работы
    "Шаимский нефтегазоносный район",
    "Шаимское месторождение нефти",
    "Шушминское месторождение",
    "Ловинское месторождение",
    "тюменская свита Западная Сибирь коллекторы",
    "Приуральская нефтегазоносная область",
    "гидрогеология Шаимского района",
]


def openalex_key_configured() -> bool:
    return bool(os.environ.get("OPENALEX_API_KEY"))


async def build_preview(
    db: Session,
    source_manager: SourceManager,
    queries: List[str],
    per_query: int = 15,
) -> dict:
    existing = ExistingIndex(db)
    fields = [(f.field_id, f.name) for f in db.query(models.Field).all()]
    rayons = [(r.rayon_id, r.name) for r in db.query(models.Rayon).all()]
    oblasts = [(o.oblast_id, o.name) for o in db.query(models.Oblast).all()]
    provinces = [(p.province_id, p.name) for p in db.query(models.Province).all()]
    place_names = {"field": dict(fields), "rayon": dict(rayons), "oblast": dict(oblasts), "province": dict(provinces)}
    types = [(t.type_id, t.name) for t in db.query(models.ResearchType).all()]
    field_names = {fid: name for fid, name in fields}
    type_names = {tid: name for tid, name in types}

    sem = asyncio.Semaphore(2)  # не более 2 одновременных запросов — бережём лимиты API

    async def run(q: str):
        async with sem:
            pubs, errs = await source_manager.search_detailed(query=q, limit=per_query)
            return q, pubs, errs

    results = await asyncio.gather(*[run(q) for q in queries])

    errors: List[str] = []
    tagged: List[tuple] = []  # (запрос, публикация) в порядке запросов
    for q, pubs, errs in results:
        errors.extend(f"«{q}»: {e}" for e in errs)
        tagged.extend((q, p) for p in pubs)

    # одна публикация может найтись разными запросами и в обоих источниках
    unique: List[PublicationResult] = deduplicate([p for _q, p in tagged])
    first_query = {}
    for q, p in tagged:
        key = (p.doi or "").lower() or p.title.lower()
        first_query.setdefault(key, q)

    stats = {"found_total": len(unique), "duplicates_in_db": 0, "irrelevant": 0, "new": 0}
    irrelevant_examples: List[str] = []
    candidates = []

    for p in unique:
        if not classifier.is_relevant(p):
            stats["irrelevant"] += 1
            if len(irrelevant_examples) < 8:
                irrelevant_examples.append(p.title)
            continue
        if existing.find(p.doi, p.title) is not None:
            stats["duplicates_in_db"] += 1
            continue
        text = classifier.publication_text(p)
        place = classifier.locate(text, fields, rayons, oblasts, provinces)
        field_ids = place.field_ids
        type_ids = classifier.match_research_types(text, types)
        key = (p.doi or "").lower() or p.title.lower()
        candidates.append({
            "publication": p,
            "field_ids": field_ids,
            "field_names": [field_names[i] for i in field_ids],
            "level": place.level,
            "rayon_ids": place.rayon_ids,
            "oblast_ids": place.oblast_ids,
            "province_ids": place.province_ids,
            "place_names": [place_names[place.level][i] for i in place.ids()] if place.level else [],
            "research_type_ids": type_ids,
            "research_type_names": [type_names[i] for i in type_ids],
            "matched_query": first_query.get(key),
        })
        stats["new"] += 1

    # сначала публикации с аннотацией и привязкой к месторождению, затем новые
    candidates.sort(key=lambda c: (
        0 if c["publication"].abstract else 1,
        0 if c["field_ids"] else 1,
        -(c["publication"].year or 0),
    ))

    warnings: List[str] = []
    if not openalex_key_configured():
        warnings.append(
            "Не задан OPENALEX_API_KEY: по документации OpenAlex без ключа дневной лимит поиска "
            "очень мал, часть запросов может не пройти. Бесплатный ключ: https://openalex.org/settings/api"
        )

    return {
        "candidates": candidates,
        "stats": stats,
        "irrelevant_examples": irrelevant_examples,
        "errors": errors,
        "warnings": warnings,
    }
