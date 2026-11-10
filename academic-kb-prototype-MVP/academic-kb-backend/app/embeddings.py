"""
Смысловой (векторный) поиск через сервис эмбеддингов — необязательный.

Зачем: поиск по словам с двуязычным словарём не находит публикацию, если
вопрос и текст выражают одно и то же разными словами («рассолы» и «brine»,
«засоление» и «salinization»). Эмбеддинги сопоставляют тексты по смыслу, в том
числе на разных языках.

Как устроено:
- используется ЛЮБОЙ сервис с OpenAI-совместимым методом /embeddings;
  локальная модель не нужна (на вашем Windows компилируемые библиотеки вроде
  torch блокируются политикой безопасности, а запрос к API — это обычный HTTPS);
- один вектор на публикацию (название + начало аннотации), хранится в таблице
  publication_embedding; сравнение — косинус на чистом Python (в базе сотни
  публикаций, этого достаточно);
- если сервис не настроен или недоступен, поиск молча работает без него.

Настройка (.env):
    EMBEDDINGS_API_KEY=...
    EMBEDDINGS_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/   # пример для Gemini
    EMBEDDINGS_MODEL=gemini-embedding-001
    EMBEDDINGS_MIN_SIM=0.55          # порог схожести; подбирается на вашей базе (см. README)
"""
import hashlib
import json
import math
import os
import threading
import time
from array import array
from typing import Dict, List, Optional, Sequence, Set

from sqlalchemy.orm import Session

from . import models

_vec_lock = threading.Lock()
_vec_cache: Dict[str, object] = {"model": None, "vecs": None}
_query_cache: Dict[tuple, List[float]] = {}
_warned = False


def _cfg() -> Optional[dict]:
    key = os.environ.get("EMBEDDINGS_API_KEY")
    if not key:
        return None
    base = os.environ.get("EMBEDDINGS_BASE_URL") or None
    default_model = "gemini-embedding-001" if base and "generativelanguage" in base else "text-embedding-3-small"
    return {"key": key, "base": base, "model": os.environ.get("EMBEDDINGS_MODEL") or default_model}


def is_configured() -> bool:
    return _cfg() is not None


def model_name() -> Optional[str]:
    cfg = _cfg()
    return cfg["model"] if cfg else None


def min_similarity() -> float:
    try:
        return float(os.environ.get("EMBEDDINGS_MIN_SIM", "0.55"))
    except ValueError:
        return 0.55


def _normalize(vec: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _call_api(texts: List[str]) -> List[List[float]]:
    """Один запрос к сервису; повтор при лимитах и временных сбоях."""
    from openai import OpenAI

    cfg = _cfg()
    client = OpenAI(api_key=cfg["key"], base_url=cfg["base"], timeout=30.0, max_retries=0)
    delay = 2.0
    for attempt in range(4):
        try:
            resp = client.embeddings.create(model=cfg["model"], input=texts)
            return [_normalize(d.embedding) for d in sorted(resp.data, key=lambda d: d.index)]
        except Exception as e:
            status = getattr(e, "status_code", None)
            transient = status in (429, 500, 502, 503, 504) or type(e).__name__ in ("APIConnectionError", "APITimeoutError")
            if attempt == 3 or not transient:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("недостижимо")


def embed_texts(texts: List[str], batch_size: int = 16) -> Optional[List[List[float]]]:
    """Векторы для текстов или None, если сервис не настроен или недоступен."""
    global _warned
    if not is_configured() or not texts:
        return None
    out: List[List[float]] = []
    try:
        for i in range(0, len(texts), batch_size):
            out.extend(_call_api([t[:6000] for t in texts[i:i + batch_size]]))
        return out
    except Exception as e:
        if not _warned:
            print(f"[embeddings] сервис эмбеддингов недоступен: {type(e).__name__}: {str(e)[:150]}")
            _warned = True
        return None


# -- тексты и хэши ----------------------------------------------------------
def publication_text(pub: models.Publication) -> str:
    first = sorted(pub.chunks, key=lambda c: (c.chunk_index, c.chunk_id))[:1]
    abstract = first[0].text[:1500] if first else ""
    return f"{pub.title}\n{abstract}".strip()


def _hash(model: str, text: str) -> str:
    return hashlib.sha1(f"{model}\n{text}".encode("utf-8")).hexdigest()


# -- хранение ---------------------------------------------------------------
def invalidate_vectors() -> None:
    with _vec_lock:
        _vec_cache["vecs"] = None


def ensure_embeddings(db: Session, publication_ids: Optional[Sequence[int]] = None, limit: int = 300) -> dict:
    """Считает и сохраняет векторы для публикаций, у которых их ещё нет или
    изменился текст. Повторный вызов ничего не пересчитывает."""
    model = model_name()
    if not model:
        return {"embedded": 0, "skipped": 0, "failed": 0, "configured": False}
    q = db.query(models.Publication)
    if publication_ids is not None:
        q = q.filter(models.Publication.publication_id.in_(list(publication_ids)))
    existing = {
        e.publication_id: e for e in db.query(models.PublicationEmbedding).all()
    }
    todo, skipped = [], 0
    for pub in q.all():
        text = publication_text(pub)
        h = _hash(model, text)
        e = existing.get(pub.publication_id)
        if e is not None and e.text_hash == h and e.model == model:
            skipped += 1
        else:
            todo.append((pub, text, h))
    todo = todo[:limit]
    vectors = embed_texts([t for _p, t, _h in todo]) if todo else []
    if todo and vectors is None:
        return {"embedded": 0, "skipped": skipped, "failed": len(todo), "configured": True}
    for (pub, _text, h), vec in zip(todo, vectors or []):
        row = existing.get(pub.publication_id)
        if row is None:
            db.add(models.PublicationEmbedding(
                publication_id=pub.publication_id, model=model, text_hash=h, vector=json.dumps(vec)))
        else:
            row.model, row.text_hash, row.vector = model, h, json.dumps(vec)
    db.commit()
    invalidate_vectors()
    return {"embedded": len(todo), "skipped": skipped, "failed": 0, "configured": True}


def _load_vectors(db: Session) -> Dict[int, array]:
    model = model_name()
    with _vec_lock:
        if _vec_cache["vecs"] is not None and _vec_cache["model"] == model:
            return _vec_cache["vecs"]  # type: ignore[return-value]
        vecs = {
            row.publication_id: array("f", json.loads(row.vector))
            for row in db.query(models.PublicationEmbedding).filter(models.PublicationEmbedding.model == model).all()
        }
        _vec_cache["vecs"], _vec_cache["model"] = vecs, model
        return vecs


def _query_vectors(queries: List[str]) -> Optional[List[List[float]]]:
    model = model_name()
    missing = [q for q in queries if (model, q) not in _query_cache]
    if missing:
        vecs = embed_texts(missing)
        if vecs is None:
            return None
        for q, v in zip(missing, vecs):
            if len(_query_cache) > 256:
                _query_cache.clear()
            _query_cache[(model, q)] = v
    return [_query_cache[(model, q)] for q in queries]


def similarities(db: Session, queries: List[str], allowed: Optional[Set[int]] = None) -> Dict[int, float]:
    """Косинусная близость публикации к лучшему из вариантов запроса (без порога)."""
    queries = [q.strip() for q in queries if q and q.strip()][:3]
    if not queries or not is_configured():
        return {}
    qvecs = _query_vectors(queries)
    if qvecs is None:
        return {}
    qarrs = [array("f", v) for v in qvecs]
    out: Dict[int, float] = {}
    for pid, vec in _load_vectors(db).items():
        if allowed is not None and pid not in allowed:
            continue
        best = max(sum(a * b for a, b in zip(vec, q)) for q in qarrs)
        out[pid] = best
    return out


def semantic_scores(db: Session, queries: List[str], allowed: Optional[Set[int]] = None) -> Dict[int, float]:
    """Оценки в диапазоне 0..1 только для публикаций выше порога схожести
    (не более 20 лучших): схожесть min_sim соответствует 0, идеальная — 1."""
    sims = similarities(db, queries, allowed)
    thr = min_similarity()
    ranked = sorted(((pid, s) for pid, s in sims.items() if s >= thr), key=lambda kv: kv[1], reverse=True)[:20]
    return {pid: min(1.0, (s - thr) / (1.0 - thr)) if thr < 1.0 else 1.0 for pid, s in ranked}
