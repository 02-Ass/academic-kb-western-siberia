"""
Backend академической базы знаний — реализация по физической модели
данных IDEF1X (Лабораторная работа №9, db/schema_lab9.sql):

    Province → Oblast → Rayon → Field → Horizon → Deposit

Публикации связаны с объектами через publication_field /
publication_horizon / publication_deposit, авторы нормализованы в
отдельную таблицу и связаны через publication_author.

Запуск:
    uvicorn app.main:app --reload --port 8000
"""
import asyncio
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import admin as admin_module, auth, document_processing, embeddings, hierarchy, llm, models, ontology, schemas, scope_embed, search, telemetry
from .services import citations, kb_context, routing
from .services import intent as intent_mod
from .services import index as search_index
from .database import SessionLocal, describe_database, engine, get_db
from . import runtime_settings
from .ontology.api import router as ontology_router
from .services import classifier, fulltext, importer
from .services import export as export_service
from .services.publication_deduplicator import deduplicate
from .services.publication_saver import AuthorIndex, ExistingIndex, save_publication
from .services.publication_saver import delete_publication as delete_publication_record
from .services.text_utils import norm_doi
from .sources.language import prefer_language
from .sources.manager import SourceManager

load_dotenv()

models.Base.metadata.create_all(bind=engine)

@asynccontextmanager
async def lifespan(_app):
    """Прогрев поискового индекса в фоне: при тысячах фрагментов полного текста
    первое построение занимает секунды, и пользователь не должен ждать его на
    первом вопросе."""
    def warm():
        try:
            db = SessionLocal()
            try:
                search_index.get_index(db)
                ontology.get_state(db)   # вывод занимает секунды — делаем его заранее
            finally:
                db.close()
        except Exception as e:
            print(f"[index] прогрев индекса не удался: {type(e).__name__}")
    print(f"[db] подключено к базе: {describe_database()}")
    try:
        _db = SessionLocal()
        try:
            runtime_settings.apply(runtime_settings.load(_db))     # настройки из админ-панели
        finally:
            _db.close()
    except Exception as e:  # noqa: BLE001
        print(f"[settings] не удалось загрузить настройки: {type(e).__name__}")
    threading.Thread(target=warm, daemon=True).start()
    threading.Thread(target=scope_embed.warmup, daemon=True).start()  # эталоны классификатора темы
    yield


app = FastAPI(
    title="Академическая база знаний — нефтегазовые месторождения Западной Сибири",
    description="Backend по физической модели данных IDEF1X (Лабораторная работа №9).",
    version="0.3.0",
    lifespan=lifespan,
)
app.include_router(ontology_router)
app.include_router(admin_module.router)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://academic-kb-.*\.amvera\.tech",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _field_detail(db: Session, f: models.Field) -> schemas.FieldDetailOut:
    anc = hierarchy.field_ancestors(db, f)
    out = schemas.FieldDetailOut.model_validate(f)
    out.rayon = schemas.RayonOut.model_validate(anc["rayon"]) if anc["rayon"] else None
    out.oblast = schemas.OblastOut.model_validate(anc["oblast"]) if anc["oblast"] else None
    out.province = schemas.ProvinceOut.model_validate(anc["province"]) if anc["province"] else None
    out.horizons = [schemas.HorizonOut.model_validate(h) for h in f.horizons]
    return out


def _publication_out(pub: models.Publication) -> schemas.PublicationOut:
    out = schemas.PublicationOut.model_validate(pub)
    out.authors = [schemas.AuthorOut.model_validate(link.author) for link in pub.author_links]
    out.research_types = [schemas.ResearchTypeOut.model_validate(rt) for rt in pub.research_types]
    return out


# --------------------------------------------------------------------------
# Геологическая иерархия
# --------------------------------------------------------------------------

@app.get("/api/provinces", response_model=List[schemas.ProvinceOut], tags=["hierarchy"])
def list_provinces(db: Session = Depends(get_db)):
    return db.query(models.Province).order_by(models.Province.province_id).all()


@app.get("/api/oblasts", response_model=List[schemas.OblastOut], tags=["hierarchy"])
def list_oblasts(province_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    q = db.query(models.Oblast)
    if province_id is not None:
        q = q.filter(models.Oblast.province_id == province_id)
    return q.order_by(models.Oblast.oblast_id).all()


@app.get("/api/rayons", response_model=List[schemas.RayonOut], tags=["hierarchy"])
def list_rayons(oblast_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    q = db.query(models.Rayon)
    if oblast_id is not None:
        q = q.filter(models.Rayon.oblast_id == oblast_id)
    return q.order_by(models.Rayon.rayon_id).all()


@app.get("/api/rayons/{rayon_id}", response_model=schemas.RayonDetailOut, tags=["hierarchy"])
def get_rayon(rayon_id: int, db: Session = Depends(get_db)):
    r = db.query(models.Rayon).filter(models.Rayon.rayon_id == rayon_id).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Район не найден")
    anc = hierarchy.rayon_ancestors(db, r)
    out = schemas.RayonDetailOut.model_validate(r)
    out.oblast = schemas.OblastOut.model_validate(anc["oblast"]) if anc["oblast"] else None
    out.province = schemas.ProvinceOut.model_validate(anc["province"]) if anc["province"] else None
    out.fields = [schemas.FieldOut.model_validate(f) for f in r.fields]
    return out


@app.get("/api/fields", response_model=List[schemas.FieldOut], tags=["hierarchy"])
def list_fields(rayon_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    q = db.query(models.Field)
    if rayon_id is not None:
        q = q.filter(models.Field.rayon_id == rayon_id)
    return q.order_by(models.Field.field_id).all()


@app.get("/api/fields/{field_id}", response_model=schemas.FieldDetailOut, tags=["hierarchy"])
def get_field(field_id: int, db: Session = Depends(get_db)):
    f = db.query(models.Field).filter(models.Field.field_id == field_id).first()
    if f is None:
        raise HTTPException(status_code=404, detail="Месторождение не найдено")
    return _field_detail(db, f)


@app.get("/api/horizons", response_model=List[schemas.HorizonOut], tags=["hierarchy"])
def list_horizons(field_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    q = db.query(models.Horizon)
    if field_id is not None:
        q = q.filter(models.Horizon.field_id == field_id)
    return q.order_by(models.Horizon.horizon_id).all()


@app.get("/api/deposits", response_model=List[schemas.DepositOut], tags=["hierarchy"])
def list_deposits(horizon_id: Optional[int] = Query(None), db: Session = Depends(get_db)):
    q = db.query(models.Deposit)
    if horizon_id is not None:
        q = q.filter(models.Deposit.horizon_id == horizon_id)
    return q.order_by(models.Deposit.deposit_id).all()


# --------------------------------------------------------------------------
# Публикации по уровню иерархии (агрегация через publication_field)
# --------------------------------------------------------------------------

@app.get("/api/fields/{field_id}/publications", response_model=List[schemas.PublicationOut], tags=["hierarchy"])
def field_publications(field_id: int, db: Session = Depends(get_db)):
    f = db.query(models.Field).filter(models.Field.field_id == field_id).first()
    if f is None:
        raise HTTPException(status_code=404, detail="Месторождение не найдено")
    pubs = hierarchy.publications_for_level(db, field_id=field_id)
    return [_publication_out(p) for p in pubs]


@app.get("/api/fields/{field_id}/regional-publications", response_model=List[schemas.RegionalLevelOut], tags=["hierarchy"])
def field_regional_publications(field_id: int, db: Session = Depends(get_db)):
    """Сводка для карточки месторождения: сколько публикаций посвящено его району, области и провинции в целом.
    Это работы НЕ о самом месторождении, поэтому в /api/fields/{id}/publications они не входят."""
    f = db.get(models.Field, field_id)
    if f is None:
        raise HTTPException(status_code=404, detail="Месторождение не найдено")
    out = []
    for level, obj in hierarchy.regional_levels_for_field(db, f):
        object_id = getattr(obj, hierarchy.REGIONAL_LEVELS[level][1])
        total = hierarchy.regional_publications_query(db, level, object_id).count()
        out.append(schemas.RegionalLevelOut(level=level, object_id=object_id, name=obj.name, total=total))
    return out


@app.get("/api/regional-publications", response_model=schemas.RegionalPublicationsOut, tags=["hierarchy"])
def regional_publications(
    level: str = Query(..., pattern="^(rayon|oblast|province)$"),
    object_id: int = Query(..., ge=1),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Публикации, привязанные непосредственно к району, области или провинции (постранично)."""
    model, attr, _link = hierarchy.REGIONAL_LEVELS[level]
    obj = db.get(model, object_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Объект не найден")
    query = hierarchy.regional_publications_query(db, level, object_id)
    total = query.count()
    pubs = query.offset(offset).limit(limit).all()
    return schemas.RegionalPublicationsOut(
        level=level, object_id=object_id, name=obj.name, total=total, offset=offset,
        publications=[_publication_out(p) for p in pubs],
    )


@app.get("/api/rayons/{rayon_id}/publications", response_model=List[schemas.PublicationOut], tags=["hierarchy"])
def rayon_publications(rayon_id: int, db: Session = Depends(get_db)):
    r = db.query(models.Rayon).filter(models.Rayon.rayon_id == rayon_id).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Район не найден")
    pubs = hierarchy.publications_for_level(db, rayon_id=rayon_id)
    return [_publication_out(p) for p in pubs]


@app.get("/api/oblasts/{oblast_id}/publications", response_model=List[schemas.PublicationOut], tags=["hierarchy"])
def oblast_publications(oblast_id: int, db: Session = Depends(get_db)):
    pubs = hierarchy.publications_for_level(db, oblast_id=oblast_id)
    return [_publication_out(p) for p in pubs]


@app.get("/api/provinces/{province_id}/publications", response_model=List[schemas.PublicationOut], tags=["hierarchy"])
def province_publications(province_id: int, db: Session = Depends(get_db)):
    pubs = hierarchy.publications_for_level(db, province_id=province_id)
    return [_publication_out(p) for p in pubs]


# --------------------------------------------------------------------------
# GIS — координаты есть только у field (раздел 3.4 лабораторной работы)
# --------------------------------------------------------------------------

@app.get("/api/gis/fields", tags=["gis"])
def gis_fields(db: Session = Depends(get_db)):
    fields = db.query(models.Field).all()
    features = []
    for f in fields:
        if f.latitude is None or f.longitude is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [f.longitude, f.latitude]},
            "properties": {
                "id": f.field_id,
                "name": f.name,
                "object_type": "Месторождение",
                "description": f.description,
            },
        })
    return {"type": "FeatureCollection", "features": features}


@app.get("/api/gis/fields/nearby", tags=["gis"])
def gis_fields_nearby(
    lat: float = Query(...),
    lon: float = Query(...),
    radius_km: float = Query(50),
    db: Session = Depends(get_db),
):
    from .database import DATABASE_URL
    from sqlalchemy import text

    if not DATABASE_URL.startswith("postgresql"):
        raise HTTPException(
            status_code=501,
            detail="Пространственный поиск по радиусу требует PostgreSQL/PostGIS.",
        )

    rows = db.execute(text(
        """
        SELECT field_id, name,
               ST_Distance(geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography) / 1000 AS distance_km
        FROM field
        WHERE geom IS NOT NULL
          AND ST_DWithin(geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :radius_m)
        ORDER BY distance_km
        """
    ), {"lon": lon, "lat": lat, "radius_m": radius_km * 1000}).mappings().all()

    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# Загрузка документов (раздел 3.6.1-3.6.3 отчёта)
# --------------------------------------------------------------------------

@app.post("/api/documents/upload", response_model=schemas.DocumentUploadResponse, tags=["documents"])
async def upload_document(
    file: UploadFile = File(...),
    publication_id: int = Form(..., description="Публикация, к которой привязать документ"),
    db: Session = Depends(get_db),
    _user=Depends(auth.require_admin),
):
    pub = db.query(models.Publication).filter(models.Publication.publication_id == publication_id).first()
    if pub is None:
        raise HTTPException(status_code=404, detail="Публикация не найдена")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir) / file.filename
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        try:
            raw_text = document_processing.extract_text(tmp_path, file.filename)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    text = document_processing.clean_text(raw_text)
    pieces = document_processing.chunk_text(text)

    for i, piece in enumerate(pieces):
        db.add(models.DocumentChunk(
            publication_id=publication_id,
            source_filename=file.filename,
            chunk_index=i,
            text=piece,
        ))
    db.commit()
    search_index.invalidate()

    return schemas.DocumentUploadResponse(
        filename=file.filename,
        publication_id=publication_id,
        chunks_created=len(pieces),
        preview=text[:200],
    )


# --------------------------------------------------------------------------
# Поиск публикаций во внешних источниках (OpenAlex + Crossref)
# --------------------------------------------------------------------------

_source_manager = SourceManager()


@app.get("/api/search/external", response_model=schemas.ExternalSearchResponse, tags=["external-search"])
async def search_external_publications(
    q: str = Query(..., min_length=2),
    limit: int = Query(10, ge=1, le=50),
    language: Optional[str] = Query(
        None, description="Фильтр по языку метаданных, ISO 639-1 (например 'ru'). Работает только для OpenAlex."
    ),
):
    raw_results = await _source_manager.search(query=q, limit=limit, language=language)
    deduped = prefer_language(deduplicate(raw_results), language)
    return schemas.ExternalSearchResponse(query=q, count=len(deduped), results=deduped)


@app.post("/api/search/external/save", response_model=schemas.PublicationOut, tags=["external-search"])
def save_external_publication(payload: schemas.SavePublicationRequest, db: Session = Depends(get_db), _user=Depends(auth.require_admin)):
    """Сохраняет одну публикацию из внешнего поиска. Если такая уже есть (по
    DOI или названию), дубликат не создаётся — возвращается существующая, и к
    ней добавляются недостающие связи. auto_link=True — месторождение и тип
    исследования определяются автоматически по тексту публикации."""
    p = payload.publication
    field_ids: list = []
    type_ids: list = []
    rayon_ids: list = []
    oblast_ids: list = []
    province_ids: list = []

    if payload.field_id is not None:
        if db.get(models.Field, payload.field_id) is None:
            raise HTTPException(status_code=404, detail="Месторождение не найдено")
        field_ids.append(payload.field_id)
    if payload.research_type_id is not None:
        if db.get(models.ResearchType, payload.research_type_id) is None:
            raise HTTPException(status_code=404, detail="Тип исследования не найден")
        type_ids.append(payload.research_type_id)

    if payload.auto_link:
        text = classifier.publication_text(p)
        fields = [(f.field_id, f.name) for f in db.query(models.Field).all()]
        types = [(t.type_id, t.name) for t in db.query(models.ResearchType).all()]
        place = classifier.locate(
            text, fields,
            [(r.rayon_id, r.name) for r in db.query(models.Rayon).all()],
            [(o.oblast_id, o.name) for o in db.query(models.Oblast).all()],
            [(pr.province_id, pr.name) for pr in db.query(models.Province).all()],
        )
        field_ids += [i for i in place.field_ids if i not in field_ids]
        type_ids += [i for i in classifier.match_research_types(text, types) if i not in type_ids]
        if not field_ids:  # конкретного месторождения нет — привязываем к району/области/провинции
            rayon_ids, oblast_ids, province_ids = place.rayon_ids, place.oblast_ids, place.province_ids

    record, _created = save_publication(
        db, p, field_ids=field_ids, type_ids=type_ids,
        rayon_ids=rayon_ids, oblast_ids=oblast_ids, province_ids=province_ids,
    )
    db.commit()
    search_index.invalidate()
    _embed_best_effort(db, [record.publication_id])
    db.refresh(record)
    return _publication_out(record)


# --------------------------------------------------------------------------
# Массовое наполнение базы публикациями из внешних источников
# --------------------------------------------------------------------------

@app.get("/api/import/default-queries", response_model=schemas.DefaultQueriesResponse, tags=["import"])
def import_default_queries():
    return schemas.DefaultQueriesResponse(
        queries=importer.DEFAULT_QUERIES, openalex_key_set=importer.openalex_key_configured()
    )


@app.post("/api/import/preview", response_model=schemas.ImportPreviewResponse, tags=["import"])
async def import_preview(payload: schemas.ImportPreviewRequest, db: Session = Depends(get_db), _user=Depends(auth.require_admin)):
    """Шаг 1: ищет публикации по набору запросов, отбрасывает нерелевантные и
    уже имеющиеся в базе, размечает месторождения и типы исследований.
    НИЧЕГО не сохраняет — результат нужно просмотреть."""
    queries = [q.strip() for q in (payload.queries or importer.DEFAULT_QUERIES) if q.strip()]
    if not queries:
        raise HTTPException(status_code=422, detail="Не задано ни одного запроса")
    result = await importer.build_preview(
        db, _source_manager, queries[:30], per_query=max(1, min(payload.per_query, 50))
    )
    return result


@app.post("/api/import/commit", response_model=schemas.ImportCommitResponse, tags=["import"])
def import_commit(payload: schemas.ImportCommitRequest, db: Session = Depends(get_db), _user=Depends(auth.require_admin)):
    """Шаг 2: сохраняет выбранные пользователем кандидаты. Каждая публикация
    сохраняется в отдельной вложенной транзакции: сбой на одной не отменяет
    остальные."""
    existing = ExistingIndex(db)
    authors = AuthorIndex(db)
    created = already = failed = 0
    errors: list = []
    ids: list = []
    for item in payload.items:
        try:
            with db.begin_nested():
                record, is_new = save_publication(
                    db, item.publication,
                    field_ids=item.field_ids, type_ids=item.research_type_ids,
                    rayon_ids=item.rayon_ids, oblast_ids=item.oblast_ids, province_ids=item.province_ids,
                    existing=existing, authors=authors,
                )
            ids.append(record.publication_id)
            created += 1 if is_new else 0
            already += 0 if is_new else 1
        except Exception as e:  # одна проблемная запись не должна ронять весь импорт
            failed += 1
            errors.append(f"{item.publication.title[:60]}: {type(e).__name__}")
            # индексы могли запомнить записи из откатанной транзакции — пересоздаём
            existing = ExistingIndex(db)
            authors = AuthorIndex(db)
    db.commit()
    search_index.invalidate()
    _embed_best_effort(db, ids)
    return schemas.ImportCommitResponse(
        created=created, already_existed=already, failed=failed, errors=errors, publication_ids=ids
    )


# --------------------------------------------------------------------------
# Интеллектуальный поиск и чат-бот
# --------------------------------------------------------------------------

def _authors_str(pub: models.Publication) -> Optional[str]:
    return ", ".join(a.author.full_name for a in pub.author_links) or None


def _chat_template_answer(local, external) -> str:
    """Ответ без LLM (ключ не задан или API недоступен): просто перечень
    найденного, чтобы пользователь всё равно получил реальные данные."""
    lines = ["Найдены следующие материалы (LLM недоступен, поэтому без пересказа содержания):"]
    for pub, _score, _mf in local:
        lines.append(f"— [локальная база] {pub.title} ({_authors_str(pub) or 'автор не указан'}, {pub.year or 'г. не указан'})")
    for p in external:
        ref = p.doi or p.url or ""
        authors = ", ".join(p.authors[:3]) or "автор не указан"
        lines.append(f"— [{p.source}] {p.title} ({authors}, {p.year or 'г. не указан'}) {ref}".rstrip())
    return "\n".join(lines)


def _ontology_answer(db: Session, message: str) -> Optional[schemas.OntologyAnswerOut]:
    """Если в вопросе назван объект базы, показывает публикации по нему с учётом логического вывода
    и объяснением (раньше эти связи приходилось бы искать вручную по уровням иерархии)."""
    try:
        matched = kb_context.match_objects(db, message)
        if not matched:
            return None
        level, obj = matched[0]
        obj_id = getattr(obj, f"{level}_id")
        state = ontology.get_state_nowait(db)   # не ждём пересчёта после импорта
        if state is None:
            return None
        res = ontology.queries.chat_hits(state, level, obj_id, limit=6)
        if not res or res["with_inference"] == 0:
            return None
        return schemas.OntologyAnswerOut(
            object_kind=level, object_id=obj_id, object_name=res["object"]["name"], level=res["object"]["level"],
            direct=res["direct"], with_inference=res["with_inference"],
            publications=[
                schemas.OntologyPubOut(publication_id=r["publication_id"], title=r["title"], year=r["year"],
                                       how=r["how"], explanation=r["explanation_text"])
                for r in res["publications"]
            ],
        )
    except Exception as e:  # онтология — дополнение; её сбой не должен ломать чат
        print(f"[ontology] ответ по онтологии недоступен: {type(e).__name__}: {str(e)[:120]}")
        return None


def _write_chat_log(payload: schemas.ChatRequest, stats: dict, response, started: float, stream: bool = False) -> None:
    """Одна строка в ``logs/chat_logs.jsonl`` на запрос (для обычного и потокового чата одинаково)."""
    try:
        ext = list(getattr(response, "external_sources", None) or []) if response else []
        loc = list(getattr(response, "sources", None) or []) if response else []
        telemetry.log_chat({
            "query": payload.message,
            "user": stats.get("user"),
            "scope": stats.get("scope") or (response.intent if response else None),
            "scope_method": stats.get("scope_method"),
            "intent": response.intent if response else None,
            "query_intent": stats.get("query_intent") or (getattr(response, "query_intent", None) if response else None),
            "route": stats.get("route"),
            "rewritten_query": stats.get("rewritten_query"),
            "external_query": getattr(response, "external_query", None) if response else None,
            "sources_found": len(loc) + len(ext),
            "provider": getattr(response, "llm_provider", None) if response else None,
            "llm_used": getattr(response, "llm_used", None) if response else None,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "tokens_used": stats.get("tokens_used", 0),
            "llm_calls": stats.get("llm_calls", 0),
            "retries": stats.get("retries", 0),
            "cache_hits": stats.get("cache_hits", 0),
            "stream": stream,
            "error": response is None,
        })
    except Exception as e:  # noqa: BLE001 — журнал не должен ломать ответ
        print(f"[telemetry] {type(e).__name__}: {str(e)[:100]}")


# --------------------------------------------------------------------------
# История чатов (только для вошедших пользователей)
# --------------------------------------------------------------------------

DEFAULT_CHAT_TITLE = "Новый чат"


def _need_user(user: Optional[models.User]) -> models.User:
    if user is None:
        raise HTTPException(status_code=401, detail="Войдите, чтобы сохранять историю чатов", headers={"WWW-Authenticate": "Bearer"})
    return user


def _own_session(db: Session, session_id: str, user: models.User) -> models.ChatSession:
    """Чужой или несуществующий чат одинаково отвечает 404: по ответу нельзя узнать, есть ли чужой id."""
    sess = db.get(models.ChatSession, session_id)
    if sess is None or sess.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Чат не найден")
    return sess


def _session_owner_check(db: Session, payload: schemas.ChatRequest, user: Optional[models.User]) -> Optional[int]:
    """user_id, в чей чат сохранять ответ, или None (гость или сессия не указана). Чужой чат — 404 до начала генерации."""
    if user is None or not payload.session_id:
        return None
    _own_session(db, payload.session_id, user)
    return user.user_id


def _persist_chat(payload: schemas.ChatRequest, response, user_id: Optional[int]) -> None:
    """Сохраняет вопрос и готовый ответ. Свой сеанс БД: вызывается и из потокового ответа, где сеанс запроса уже закрыт.
    Сбой сохранения не должен ломать ответ пользователю."""
    if user_id is None or not payload.session_id or response is None:
        return
    db = SessionLocal()
    try:
        sess = db.get(models.ChatSession, payload.session_id)
        if sess is None or sess.user_id != user_id:
            return
        db.add(models.ChatMessage(session_id=sess.id, role="user", content=payload.message))
        db.add(models.ChatMessage(session_id=sess.id, role="assistant", content=response.answer,
                                  meta=json.dumps(response.model_dump(mode="json"), ensure_ascii=False)))
        if not sess.title or sess.title == DEFAULT_CHAT_TITLE:
            text = " ".join(payload.message.split())
            sess.title = text[:30] + ("…" if len(text) > 30 else "")
        sess.updated_at = func.now()
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(f"[chat] не удалось сохранить историю: {type(e).__name__}: {str(e)[:150]}")
    finally:
        db.close()


@app.get("/api/chat/sessions", response_model=List[schemas.ChatSessionOut], tags=["chat-history"])
def list_chat_sessions(db: Session = Depends(get_db), user: Optional[models.User] = Depends(auth.current_user)):
    user = _need_user(user)
    return (db.query(models.ChatSession).filter(models.ChatSession.user_id == user.user_id)
            .order_by(models.ChatSession.updated_at.desc(), models.ChatSession.created_at.desc()).limit(200).all())


@app.post("/api/chat/sessions", response_model=schemas.ChatSessionOut, status_code=201, tags=["chat-history"])
def create_chat_session(payload: Optional[schemas.ChatSessionCreate] = None, db: Session = Depends(get_db),
                        user: Optional[models.User] = Depends(auth.current_user)):
    user = _need_user(user)
    title = " ".join(((payload.title if payload else None) or "").split())[:200] or DEFAULT_CHAT_TITLE
    sess = models.ChatSession(id=str(uuid.uuid4()), user_id=user.user_id, title=title)
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


@app.get("/api/chat/sessions/{session_id}/messages", response_model=List[schemas.ChatMessageOut], tags=["chat-history"])
def chat_session_messages(session_id: str, db: Session = Depends(get_db),
                          user: Optional[models.User] = Depends(auth.current_user)):
    sess = _own_session(db, session_id, _need_user(user))
    out = []
    for m in sess.messages:
        try:
            meta = json.loads(m.meta) if m.meta else None
        except ValueError:
            meta = None
        out.append(schemas.ChatMessageOut(id=m.id, role=m.role, content=m.content, meta=meta, created_at=m.created_at))
    return out


@app.delete("/api/chat/sessions/{session_id}", status_code=204, tags=["chat-history"])
def delete_chat_session(session_id: str, db: Session = Depends(get_db),
                        user: Optional[models.User] = Depends(auth.current_user)):
    sess = _own_session(db, session_id, _need_user(user))
    db.delete(sess)
    db.commit()


@app.post("/api/chat", response_model=schemas.ChatResponse, tags=["search"])
async def chat_endpoint(payload: schemas.ChatRequest, db: Session = Depends(get_db),
                        user: Optional[models.User] = Depends(auth.current_user)):
    """Обёртка над ``_chat_impl``: замеряет время, считает токены LLM и пишет строку в журнал
    ``logs/chat_logs.jsonl`` (по одной записи на запрос, для любой ветки ответа)."""
    user_id = _session_owner_check(db, payload, user)
    stats = telemetry.start_request()
    telemetry.note("user", user.email if user else None)
    started = time.perf_counter()
    response = None
    try:
        response = await _chat_impl(payload, db)
        _persist_chat(payload, response, user_id)          # только после того, как ответ готов целиком
        return response
    finally:
        _write_chat_log(payload, stats, response, started)


def _sse(event: str, data) -> str:
    """Одно событие Server-Sent Events. Данные — JSON в одной строке (переводы строк экранируются)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream", tags=["search"])
async def chat_stream_endpoint(payload: schemas.ChatRequest, db0: Session = Depends(get_db),
                               user: Optional[models.User] = Depends(auth.current_user)):
    """Тот же чат, но ответ идёт потоком (Server-Sent Events), чтобы текст появлялся сразу.

    События:
    - ``status`` — этап работы: ``{"text": "Ищу источники…"}`` (пока идёт поиск, до первого токена);
    - ``token``  — кусок текста ответа: ``{"text": "..."}``. Текст ещё НЕ проверен: ссылки проверяются
      только на полном тексте;
    - ``done``   — итог, та же структура, что у ``POST /api/chat`` (ChatResponse): проверенный текст
      ``answer``, источники, ``citations``, ``confidence``, предупреждения о ссылках и т. д.
      Клиент заменяет накопленный текст на ``answer`` из этого события;
    - ``error``  — ``{"message": "..."}``, если запрос не удалось выполнить.

    Ответы без LLM (шаблон, отказ вне темы, «привет») приходят одним событием ``token`` и затем ``done``.
    Если клиент закрыл соединение (кнопка «Стоп»), поиск и чтение потока у провайдера прекращаются.
    """
    user_id = _session_owner_check(db0, payload, user)
    stats = telemetry.start_request()
    telemetry.note("user", user.email if user else None)
    started = time.perf_counter()
    queue: asyncio.Queue = asyncio.Queue()
    streamed = {"tokens": False}

    async def emit(event: str, data) -> None:
        if event == "token":
            streamed["tokens"] = True
        await queue.put((event, data))

    async def run() -> None:
        # своя сессия БД: зависимость get_db закрывается раньше, чем заканчивается потоковый ответ
        db = SessionLocal()
        response = None
        try:
            response = await _chat_impl(payload, db, emit)
            if not streamed["tokens"]:
                await emit("token", {"text": response.answer})
            _persist_chat(payload, response, user_id)      # после завершения генерации: пустых ответов в истории не будет
            await emit("done", response.model_dump(mode="json"))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[chat/stream] {type(e).__name__}: {str(e)[:200]}")
            await emit("error", {"message": "Не удалось получить ответ. Попробуйте ещё раз."})
        finally:
            db.close()
            _write_chat_log(payload, stats, response, started, stream=True)
            await queue.put(None)

    async def event_source():
        task = asyncio.create_task(run())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"  # комментарий SSE: держит соединение, пока идёт долгий поиск
                    continue
                if item is None:
                    break
                yield _sse(*item)
        finally:
            if not task.done():
                task.cancel()  # клиент отключился: прекращаем поиск и генерацию

    return StreamingResponse(
        event_source(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _chat_impl(payload: schemas.ChatRequest, db: Session, emit=None):
    """
    Чат-бот (раздел 3.6.7 отчёта). Контекст для LLM собирается из трёх мест:
    1) справка из базы знаний: структурированные данные об объектах, названных
       в вопросе (иерархия, заполненные параметры, число публикаций);
    2) локальная база публикаций: поиск по фрагментам на двух языках (вопрос
       и его английская формулировка), в LLM уходят совпавшие фрагменты;
    3) живой поиск в OpenAlex и Crossref (аннотации найденных статей).
    Ответ после генерации проверяется: несуществующие номера источников
    удаляются, непроверяемые строки списка литературы помечаются.
    """
    llm_history = [{"role": m.role, "content": m.content} for m in payload.history]
    lang = payload.external_language if payload.external_language in ("ru", "en") else None
    llm_on = llm.is_configured()
    provider_label = llm.active_provider_label() if llm_on else None

    async def status(text: str) -> None:
        """Этап работы для потокового режима (в обычном ``emit`` не задан)."""
        if emit is not None:
            await emit("status", {"text": text})

    def reply_only(kind: str, text: str, query_intent: Optional[str] = None) -> schemas.ChatResponse:
        """Ответ без поиска и без LLM (разговор, справка о возможностях, вопрос вне темы)."""
        return schemas.ChatResponse(
            answer=text, llm_used=False, llm_provider=provider_label, llm_configured=llm_on, intent=kind,
            query_intent=query_intent,
        )

    # 0. Приветствие, «как дела», «что ты умеешь» — не вопросы к базе: ни поиск, ни LLM не нужны
    intent = intent_mod.classify(payload.message)
    if intent.kind == "smalltalk":
        return reply_only("smalltalk", intent_mod.smalltalk_reply(intent, db))
    if intent.kind == "capabilities":
        return reply_only("capabilities", intent_mod.capabilities_reply(intent, db, llm_on))
    message = intent.message or payload.message  # без приветствия: «Привет, расскажи про X» → «расскажи про X»

    # 0а. НОВОЕ: жёсткий блок вопроса вне темы ДО любых запросов: ни переписывания запроса, ни поиска в
    # локальной базе, ни обращения к OpenAlex/Crossref. Сначала быстрые ключевые слова, затем LLM
    # (без LLM работают только ключевые слова; неясные случаи проверяются позже по результатам поиска).
    scope = await run_in_threadpool(llm.classify_scope, message, llm_history)
    if scope is None and not llm_history and not intent_mod.looks_domain(message):
        scope = "offtopic"  # LLM не ответил определённо, а в вопросе нет ни одного термина предметной области
    print(f"[chat] тема вопроса: {scope!r} ({message[:60]!r})")
    telemetry.note("scope", scope)
    def hard_offtopic(why: str) -> schemas.ChatResponse:
        telemetry.note("query_intent", "OFFTOPIC")
        telemetry.note("route", f"offtopic:{why}")
        return schemas.ChatResponse(
            answer=intent_mod.offtopic_hard_reply(intent), llm_used=False, llm_provider=provider_label,
            llm_configured=llm_on, intent="offtopic", sources=[], is_offtopic=True,
            confidence="high", suggestions=intent_mod.offtopic_suggestions(intent), query_intent="OFFTOPIC",
        )

    if scope == "offtopic":
        return hard_offtopic("scope")

    await status("Ищу в базе знаний…")
    # формулировки запроса: английская нужна и локальному поиску (публикации в
    # основном англоязычные), и внешнему; русская — только внешнему в режиме «ru»
    en_task = run_in_threadpool(llm.generate_search_query, message, llm_history, "en") if llm_on else None
    ru_task = (
        run_in_threadpool(llm.generate_search_query, message, llm_history, "ru")
        if llm_on and payload.use_external and lang == "ru" else None
    )

    async def _nothing():
        return None

    # намерение определяется параллельно с переписыванием запроса: отдельное ожидание добавило бы лишнее обращение к LLM
    en_query, ru_query, query_intent = await asyncio.gather(
        en_task or _nothing(), ru_task or _nothing(), run_in_threadpool(llm.detect_intent, message, llm_history),
    )
    telemetry.note("rewritten_query", en_query)

    # 0б. Намерение «вне темы» от LLM учитываем, только если тему вопроса не подтвердили раньше (ключевые слова,
    # эмбеддинги, предметные термины) и это не уточнение к предыдущему вопросу: иначе два фильтра противоречили бы друг другу.
    if query_intent == "OFFTOPIC":
        follow_up = bool(llm_history) and llm._depends_on_history(message, llm_history)
        if scope == "domain" or follow_up or intent_mod.looks_domain(message):
            telemetry.note("intent_overridden", "OFFTOPIC")
            query_intent = llm.DEFAULT_INTENT
        else:
            return hard_offtopic("intent")
    telemetry.note("query_intent", query_intent)

    local_queries = [message] + ([en_query] if en_query else [])
    answer_intent = query_intent       # намерение, по которому строится ответ (может смениться, если стратегия не сработала)
    route = "standard"
    hits = None
    if query_intent == "COMPARE_OBJECTS":
        pair = routing.split_compare(message)
        if pair is None and llm_on:
            names = await run_in_threadpool(llm.extract_compare_objects, message)
            pair = routing.build_pair(*names) if names else None
        if pair is not None:
            hits = await run_in_threadpool(
                lambda: routing.compare_hits(db, pair, top_k=payload.top_k, rayon_id=payload.rayon_id, field_id=payload.field_id)
            )
            local_queries = [pair.a_query, pair.b_query] + ([en_query] if en_query else [])
            route = "compare"
        else:  # двух объектов в вопросе не нашлось: обычный поиск и обычная форма ответа
            answer_intent, route = "FIND_PUBLICATION", "standard:compare-not-split"
    elif query_intent == "FIND_OBJECT":
        matched = kb_context.match_objects(db, message)
        if matched:
            obj_hits, notes = await run_in_threadpool(
                lambda: routing.object_hits(db, matched, local_queries, top_k=payload.top_k,
                                            rayon_id=payload.rayon_id, field_id=payload.field_id)
            )
            if obj_hits:
                _i, std = await run_in_threadpool(
                    lambda: search.retrieve(db, local_queries, rayon_id=payload.rayon_id, field_id=payload.field_id,
                                            top_k=payload.top_k)
                )
                seen = {h.pub.publication_id for h in obj_hits}
                hits = (obj_hits + [h for h in std if h.pub.publication_id not in seen])[: payload.top_k + 2]
                route = "object"
                telemetry.note("route_detail", "; ".join(notes))
    if hits is None:
        _ids, hits = await run_in_threadpool(
            lambda: search.retrieve(
                db, local_queries, rayon_id=payload.rayon_id, field_id=payload.field_id, top_k=payload.top_k,
            )
        )
    telemetry.note("route", route)

    # 1. Справка из БД и локальный поиск
    kb_text, kb_objects = kb_context.build_kb_context(db, message)
    onto_answer = _ontology_answer(db, message)
    local = [h for h in hits if h.score > 0]

    # 1а. В базе ничего не нашлось: прежде чем идти во внешние источники, выясняем, относится ли
    # вопрос к теме вообще. Для «погоды» или «рецепта борща» искать публикации бессмысленно, а
    # случайные найденные статьи только запутали бы ответ.
    if not local and not kb_text:
        decided_by_llm = scope is not None  # здесь scope либо «domain» (решили выше), либо None (неясно)
        if scope is None:
            scope = "domain" if intent_mod.looks_domain(message) else "offtopic"
        if scope == "offtopic":
            telemetry.note("query_intent", "OFFTOPIC")
            return reply_only("offtopic", intent_mod.offtopic_reply(intent, db, confident=decided_by_llm), "OFFTOPIC")

    # 1б. Сравнение: оба объекта передаются модели явно, даже если по одному из них в базе пусто
    if route == "compare":
        compare_text = routing.compare_context(db, pair, local)
        kb_text = compare_text + (("\n\n" + kb_text) if kb_text else "")

    # 2. Живой поиск во внешних источниках
    external: list = []
    external_query: Optional[str] = None
    external_errors: list = []
    external_note: Optional[str] = None
    if payload.use_external:
        await status("Ищу в OpenAlex и Crossref…")
        external_query = ru_query if lang == "ru" else en_query
        if not llm_on:
            external_query = message  # без LLM переписать запрос нечем: ищем по самому вопросу, как раньше
        raw: list = []
        if external_query:
            raw, external_errors = await _source_manager.search_detailed(
                query=external_query, limit=10 if lang else 6, language=lang
            )
        else:
            # НОВОЕ: LLM подключён, но не дала пригодную поисковую формулировку (сбой или служебный ответ):
            # во внешние базы не идём, чтобы не искать по мусору; остаётся локальная база.
            external_note = (
                "Не удалось сформулировать поисковый запрос для внешних баз (OpenAlex, Crossref), поэтому "
                "показаны только результаты локальной базы. Попробуйте переформулировать вопрос."
            )
        found = prefer_language(deduplicate(raw), lang)
        if raw and not found and lang:
            label = "русском" if lang == "ru" else "английском"
            external_note = (
                f"По запросу «{external_query}» во внешних базах нет публикаций на {label} языке. "
                "Выберите «Любой язык» под чатом, чтобы увидеть и остальные."
            )
        local_dois = {norm_doi(h.pub.doi) for h in local if h.pub.doi}
        found = [p for p in found if not (norm_doi(p.doi) and norm_doi(p.doi) in local_dois)]
        # Внешние базы ищут по словам, а не по теме: «месторождение Дарасун» совпало с «месторождением Шаим».
        # Оставляем только то, что относится к Западной Сибири и нефтегазовой геологии (те же правила, что при импорте).
        if found and os.getenv("EXTERNAL_DOMAIN_FILTER", "1") != "0":
            kept = [p for p in found if classifier.is_relevant(p)]
            telemetry.note("external_filtered", len(found) - len(kept))
            if not kept:
                external_note = external_note or (
                    "Внешние базы вернули только работы вне предметной области (Западная Сибирь, нефтегаз) — они скрыты."
                )
            found = kept
        found.sort(key=lambda p: 0 if p.abstract else 1)
        external = found[:8]

    # 3. Ответ
    llm_used = False
    confidence: Optional[str] = None
    cited_cards: list = []
    citation_warnings: list = []
    if not local and not external and not kb_text:
        answer = (
            "Релевантных публикаций не найдено ни в локальной базе знаний, "
            "ни во внешних источниках."
            + (f" (Не ответили: {', '.join(external_errors)}.)" if external_errors else "")
        )
    else:
        answer = None
        if llm_on:
            context = [
                {
                    "title": h.pub.title, "authors": _authors_str(h.pub), "year": h.pub.year,
                    "journal": h.pub.journal, "doi": h.pub.doi, "url": h.pub.link,
                    "origin": "локальная база знаний", "text_snippet": search.passages_text(h),
                    "object_name": h.matched_field,
                }
                for h in local
            ] + [
                {
                    "title": p.title, "authors": ", ".join(p.authors[:6]) or None, "year": p.year,
                    "journal": p.journal, "doi": p.doi, "url": p.url,
                    "origin": f"внешняя база {p.source}",
                    "text_snippet": (p.abstract or "")[:1200] or None,
                }
                for p in external
            ]
            await status("Формулирую ответ…")
            if emit is not None:
                # потоковый режим: куски текста уходят клиенту сразу, итог (с проверкой ссылок) — в конце
                raw_answer = None
                async for kind, value in llm.generate_answer_stream(
                    message, context, llm_history, kb_context=kb_text, intent=answer_intent
                ):
                    if kind == "token":
                        await emit("token", {"text": value})
                    else:
                        raw_answer = value
            else:
                raw_answer = await run_in_threadpool(
                    lambda: llm.generate_answer(message, context, llm_history, kb_context=kb_text, intent=answer_intent)
                )
            if raw_answer is not None:
                # НОВОЕ: generate_answer возвращает dict (текст, уверенность, битые ссылки); строку тоже принимаем
                if isinstance(raw_answer, dict):
                    confidence = raw_answer.get("confidence")
                    bad_nums = sorted(set(raw_answer.get("bad_citations") or []))
                    raw_answer = raw_answer["text"]
                else:
                    bad_nums = []
                answer, report = citations.verify_answer(raw_answer, context)
                citation_warnings = report["warnings"]
                if bad_nums:  # llm.py уже заменил такие номера на [?], поэтому проверка выше их не увидит
                    citation_warnings.insert(
                        0, f"В ответе были ссылки на несуществующие источники {bad_nums}; такие номера заменены на [?]."
                    )
                cited_cards = citations.citation_cards(report["cited"], context)
                llm_used = True
        if answer is None:
            answer = _chat_template_answer([(h.pub, h.score, h.matched_field) for h in local], external)
            if kb_text:
                answer = kb_text + "\n\n" + answer

    return schemas.ChatResponse(
        answer=answer,
        llm_used=llm_used,
        llm_provider=provider_label,
        llm_configured=llm_on,
        confidence=confidence,
        intent="domain",
        query_intent=query_intent,
        search_strategy=route,
        sources=[_publication_out(h.pub) for h in local],
        external_sources=external,
        external_query=external_query,
        external_errors=external_errors,
        external_note=external_note,
        citations=cited_cards,
        citation_warnings=citation_warnings,
        kb_objects=kb_objects,
        local_queries=local_queries,
        ontology=onto_answer,
    )


def _embed_best_effort(db: Session, publication_ids) -> None:
    """Считает векторы новых публикаций, если сервис эмбеддингов настроен.
    Любой сбой игнорируется: сохранение публикаций от него не зависит, а
    недостающие векторы можно досчитать (POST /api/embeddings/build)."""
    if not embeddings.is_configured() or not publication_ids:
        return
    try:
        embeddings.ensure_embeddings(db, publication_ids)
    except Exception as e:
        print(f"[embeddings] векторы не посчитаны: {type(e).__name__}")


@app.get("/api/embeddings/status", response_model=schemas.EmbeddingsStatus, tags=["embeddings"])
def embeddings_status(db: Session = Depends(get_db)):
    total = db.query(models.Publication).count()
    done = 0
    if embeddings.is_configured():
        done = db.query(models.PublicationEmbedding).filter(
            models.PublicationEmbedding.model == embeddings.model_name()
        ).count()
    return schemas.EmbeddingsStatus(
        configured=embeddings.is_configured(), model=embeddings.model_name(),
        publications=total, embedded=done, min_similarity=embeddings.min_similarity(),
    )


@app.post("/api/embeddings/build", tags=["embeddings"])
def embeddings_build(limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db), _user=Depends(auth.require_admin)):
    """Досчитывает векторы для публикаций, у которых их ещё нет."""
    if not embeddings.is_configured():
        raise HTTPException(status_code=409, detail="Сервис эмбеддингов не настроен (EMBEDDINGS_API_KEY в .env).")
    return embeddings.ensure_embeddings(db, None, limit)


# --------------------------------------------------------------------------
# Полные тексты открытых статей
# --------------------------------------------------------------------------

@app.get("/api/fulltext/status", response_model=schemas.FulltextStatus, tags=["fulltext"])
def fulltext_status(db: Session = Depends(get_db)):
    pubs = db.query(models.Publication).all()
    with_ft = sum(1 for p in pubs if fulltext.has_fulltext(p))
    with_doi = sum(1 for p in pubs if p.doi)
    candidates = sum(1 for p in pubs if p.doi and not fulltext.has_fulltext(p))
    return schemas.FulltextStatus(publications=len(pubs), with_doi=with_doi, with_fulltext=with_ft, candidates=candidates)


@app.post("/api/fulltext/fetch", response_model=schemas.FulltextResponse, tags=["fulltext"])
async def fulltext_fetch(payload: schemas.FulltextRequest, db: Session = Depends(get_db), _user=Depends(auth.require_admin)):
    """Скачивает полные тексты ОТКРЫТЫХ статей (ссылку на PDF сообщает OpenAlex)
    и добавляет их в локальный поиск. За один вызов — не более limit статей."""
    results = await fulltext.fetch_fulltexts(db, payload.publication_ids, payload.limit)
    search_index.invalidate()
    ok = sum(1 for r in results if r["status"] == "ok")
    skipped = sum(1 for r in results if r["status"] in ("already", "no_doi", "no_open_access"))
    return schemas.FulltextResponse(
        results=[schemas.FulltextItemResult(**r) for r in results],
        ok=ok, skipped=skipped, failed=len(results) - ok - skipped,
    )


# --------------------------------------------------------------------------
# Управление публикациями в локальной базе
# --------------------------------------------------------------------------

@app.get("/api/publications", response_model=List[schemas.PublicationListItem], tags=["publications"])
def list_publications(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    """Список публикаций локальной базы (новые первыми) — чтобы найти id
    записей, которые нужно удалить."""
    pubs = db.query(models.Publication).order_by(models.Publication.publication_id.desc()).limit(limit).all()
    return [
        schemas.PublicationListItem(
            publication_id=p.publication_id, title=p.title, year=p.year, journal=p.journal,
            doi=p.doi, link=p.link, authors=[l.author.full_name for l in p.author_links],
        )
        for p in pubs
    ]


def _publications_by_level(db: Session) -> dict:
    """Сколько публикаций привязано к каждому уровню иерархии (самый конкретный уровень публикации)."""
    ids = lambda m: {r[0] for r in db.query(m.publication_id)}  # noqa: E731
    field = ids(models.PublicationField) | ids(models.PublicationHorizon) | ids(models.PublicationDeposit)
    rayon = ids(models.PublicationRayon) - field
    oblast = ids(models.PublicationOblast) - field - rayon
    province = ids(models.PublicationProvince) - field - rayon - oblast
    total = db.query(models.Publication.publication_id).count()
    return {"field": len(field), "rayon": len(rayon), "oblast": len(oblast), "province": len(province),
            "unlinked": max(0, total - len(field | rayon | oblast | province))}


@app.get("/api/stats", tags=["meta"])
def stats(db: Session = Depends(get_db)):
    """Реальные счётчики для главной страницы (публикации, месторождения, авторы, районы)."""
    from sqlalchemy import func
    count = lambda m: db.query(func.count()).select_from(m).scalar() or 0
    return {
        "publications": count(models.Publication),
        "fields": count(models.Field),
        "authors": count(models.Author),
        "rayons": count(models.Rayon),
        "publications_by_level": _publications_by_level(db),
        "database": describe_database(),  # к какой базе подключён бэкенд (пароль скрыт)
    }


@app.delete("/api/publications/{publication_id}", status_code=204, tags=["publications"])
def delete_publication(publication_id: int, db: Session = Depends(get_db), _user=Depends(auth.require_admin)):
    """Удаляет публикацию вместе со связями (месторождения, пласты, залежи,
    типы исследований, чанки документов) и авторами, которые остались
    без единой публикации."""
    pub = db.get(models.Publication, publication_id)
    if pub is None:
        raise HTTPException(status_code=404, detail="Публикация не найдена")
    delete_publication_record(db, pub)
    db.commit()
    search_index.invalidate()


@app.post("/api/search/semantic", response_model=schemas.SemanticSearchResponse, tags=["search"])
def semantic_search_endpoint(payload: schemas.SemanticSearchRequest, db: Session = Depends(get_db)):
    en_query = llm.generate_search_query(payload.query, None, "en") if llm.is_configured() else None
    resolved_ids, results, answer = search.semantic_search(
        db,
        query=payload.query,
        extra_queries=[en_query] if en_query else None,
        rayon_id=payload.rayon_id,
        field_id=payload.field_id,
        research_type_id=payload.research_type_id,
        year_from=payload.year_from,
        year_to=payload.year_to,
        top_k=payload.top_k,
    )

    result_items = []
    for pub, score, matched_field in results:
        base = _publication_out(pub)
        item = schemas.PublicationSearchResult(**base.model_dump(), score=round(score, 4), matched_field=matched_field)
        result_items.append(item)

    return schemas.SemanticSearchResponse(
        query=payload.query, resolved_field_ids=resolved_ids, results=result_items, answer=answer,
    )


# --------------------------------------------------------------------------
# Авторизация и экспорт
# --------------------------------------------------------------------------

def _user_out(u: models.User) -> schemas.UserOut:
    return schemas.UserOut(user_id=u.user_id, email=u.email, full_name=u.full_name, role=u.role)


@app.post("/api/auth/register", response_model=schemas.TokenResponse, status_code=201, tags=["auth"])
def auth_register(payload: schemas.RegisterRequest, db: Session = Depends(get_db)):
    """Регистрация. Первый пользователь в базе становится администратором."""
    user = auth.register_user(db, payload.email, payload.password, payload.full_name)
    return schemas.TokenResponse(access_token=auth.create_token(user), user=_user_out(user))


@app.post("/api/auth/login", response_model=schemas.TokenResponse, tags=["auth"])
def auth_login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = auth.authenticate(db, payload.email, payload.password)
    return schemas.TokenResponse(access_token=auth.create_token(user), user=_user_out(user))


@app.get("/api/auth/me", tags=["auth"])
def auth_me(user: Optional[models.User] = Depends(auth.current_user)):
    """Текущий пользователь (или null) и включена ли защита — фронтенд по этому показывает кнопки входа."""
    return {"user": _user_out(user) if user else None, "auth_enforced": auth.enforced()}


@app.post("/api/export", tags=["export"])
def export_publications(payload: schemas.ExportRequest, db: Session = Depends(get_db), _user=Depends(auth.require_user)):
    """Выгрузка выбранных публикаций (например, результатов поиска) в csv / bibtex / ris / json."""
    if payload.format not in export_service.FORMATS:
        raise HTTPException(status_code=422, detail=f"Формат должен быть один из: {', '.join(export_service.FORMATS)}")
    ids = list(dict.fromkeys(payload.publication_ids))[:500]
    if not ids:
        raise HTTPException(status_code=422, detail="Не выбрано ни одной публикации")
    found = {p.publication_id: p for p in db.query(models.Publication).filter(models.Publication.publication_id.in_(ids))}
    pubs = [found[i] for i in ids if i in found]       # порядок как в выдаче
    if not pubs:
        raise HTTPException(status_code=404, detail="Публикации не найдены")
    text, ctype, ext = export_service.export(pubs, payload.format)
    return Response(content=text.encode("utf-8"), media_type=ctype,
                    headers={"Content-Disposition": f'attachment; filename="publications.{ext}"'})


@app.get("/", tags=["meta"])
def root():
    return {"name": "Академическая база знаний — API", "docs": "/docs"}
