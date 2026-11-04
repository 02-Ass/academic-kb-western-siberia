"""Админ-панель: пользователи, публикации, журнал запросов, настройки. Все маршруты — только для роли admin."""
import json
from collections import deque
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import auth, llm, models, runtime_settings, telemetry
from .database import get_db
from .services import index as search_index
from .services.publication_saver import save_publication
from .sources.models import PublicationResult

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(auth.require_admin)])


# --- пользователи -----------------------------------------------------------------------------

class UserPatch(BaseModel):
    is_active: Optional[bool] = None
    role: Optional[str] = Field(None, pattern="^(user|admin)$")


@router.get("/users")
def list_users(db: Session = Depends(get_db)):
    from sqlalchemy import func
    chats = dict(db.query(models.ChatSession.user_id, func.count()).group_by(models.ChatSession.user_id).all())
    return [
        {"user_id": u.user_id, "email": u.email, "full_name": u.full_name, "role": u.role, "is_active": u.is_active,
         "created_at": u.created_at, "chats": chats.get(u.user_id, 0)}
        for u in db.query(models.User).order_by(models.User.user_id).all()
    ]


@router.patch("/users/{user_id}")
def patch_user(user_id: int, payload: UserPatch, db: Session = Depends(get_db), me: models.User = Depends(auth.require_admin)):
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    new_active = user.is_active if payload.is_active is None else payload.is_active
    new_role = user.role if payload.role is None else payload.role
    losing_admin = user.role == "admin" and user.is_active and (not new_active or new_role != "admin")
    if losing_admin:
        if me is not None and user.user_id == me.user_id:
            raise HTTPException(status_code=409, detail="Нельзя заблокировать или разжаловать самого себя")
        if db.query(models.User).filter(models.User.role == "admin", models.User.is_active.is_(True)).count() <= 1:
            raise HTTPException(status_code=409, detail="Нельзя лишить доступа последнего администратора")
    user.is_active, user.role = new_active, new_role
    db.commit()
    return {"user_id": user.user_id, "role": user.role, "is_active": user.is_active}


# --- публикации -------------------------------------------------------------------------------

class PublicationIn(BaseModel):
    title: str = Field(..., min_length=3, max_length=500)
    authors: List[str] = []
    year: Optional[int] = Field(None, ge=1500, le=2100)
    journal: Optional[str] = Field(None, max_length=200)
    doi: Optional[str] = Field(None, max_length=100)
    link: Optional[str] = Field(None, max_length=500)
    type: Optional[str] = Field(None, max_length=50)


class PublicationPatch(BaseModel):
    title: Optional[str] = Field(None, min_length=3, max_length=500)
    year: Optional[int] = Field(None, ge=1500, le=2100)
    journal: Optional[str] = Field(None, max_length=200)
    doi: Optional[str] = Field(None, max_length=100)
    link: Optional[str] = Field(None, max_length=500)
    type: Optional[str] = Field(None, max_length=50)


def _pub_row(p: models.Publication) -> dict:
    return {"publication_id": p.publication_id, "title": p.title, "year": p.year, "journal": p.journal, "doi": p.doi,
            "link": p.link, "type": p.type, "authors": [l.author.full_name for l in p.author_links]}


@router.get("/publications")
def list_publications(q: str = "", limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    query = db.query(models.Publication)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(models.Publication.title.ilike(like) | models.Publication.doi.ilike(like) | models.Publication.journal.ilike(like))
    total = query.count()
    items = query.order_by(models.Publication.publication_id.desc()).offset(offset).limit(limit).all()
    return {"total": total, "items": [_pub_row(p) for p in items]}


@router.post("/publications", status_code=201)
def add_publication(payload: PublicationIn, db: Session = Depends(get_db)):
    """Ручное добавление. Дубликат (по DOI или названию) не создаётся — 409. Привязка к объектам:
    ``python -m scripts.assign_regions --apply``."""
    rec, created = save_publication(db, PublicationResult(
        title=payload.title.strip(), authors=[a.strip() for a in payload.authors if a.strip()], year=payload.year,
        journal=payload.journal, doi=payload.doi or None, url=payload.link or None, source="manual",
    ))
    if not created:
        raise HTTPException(status_code=409, detail="Такая публикация уже есть в базе (совпали DOI или название)")
    if payload.type:
        rec.type = payload.type.strip()
    db.commit()
    search_index.invalidate()
    return _pub_row(rec)


@router.patch("/publications/{publication_id}")
def edit_publication(publication_id: int, payload: PublicationPatch, db: Session = Depends(get_db)):
    pub = db.get(models.Publication, publication_id)
    if pub is None:
        raise HTTPException(status_code=404, detail="Публикация не найдена")
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key in ("title", "type") and not (value or "").strip():
            raise HTTPException(status_code=422, detail=f"Поле «{key}» не может быть пустым")
        setattr(pub, key, (value.strip() or None) if isinstance(value, str) and key not in ("title", "type") else value)
    db.commit()
    search_index.invalidate()
    return _pub_row(pub)


# --- журнал запросов --------------------------------------------------------------------------

@router.get("/logs")
def read_logs(limit: int = Query(100, ge=1, le=500), q: str = ""):
    """Последние записи ``chat_logs.jsonl`` (новые первыми): кто и что спрашивал, какой маршрут и намерение."""
    path = telemetry.log_path()
    if not path.exists():
        return {"enabled": telemetry.log_enabled(), "items": []}
    needle = q.strip().lower()
    tail: deque = deque(maxlen=5000)
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            tail.append(line)
    items = []
    for line in reversed(tail):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if needle and needle not in json.dumps(rec, ensure_ascii=False).lower():
            continue
        items.append(rec)
        if len(items) >= limit:
            break
    return {"enabled": telemetry.log_enabled(), "items": items}


# --- настройки --------------------------------------------------------------------------------

def _settings_out(db: Session) -> dict:
    return {**runtime_settings.load(db), "llm_default_model": llm.default_model(), "llm_configured": llm.is_configured()}


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    return _settings_out(db)


@router.put("/settings")
def put_settings(payload: dict, db: Session = Depends(get_db)):
    try:
        runtime_settings.save(db, payload)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return _settings_out(db)
