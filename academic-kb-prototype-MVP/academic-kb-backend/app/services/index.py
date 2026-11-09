"""
Индекс локальной базы для поиска по фрагментам (TF-IDF, чистый Python).

Единица поиска — не вся публикация целиком, а отдельный фрагмент текста:
«карточка» публикации (название, авторы, журнал и аннотация вместе) и каждый
дополнительный фрагмент полного текста. Название и аннотацию нельзя оценивать
порознь: короткое название легко набирает высокую оценку на общих словах
(«oil», «field»), и точные работы вытесняются (это было замечено на реальной
базе при сравнении с прежним поиском). Публикация оценивается по лучшему из своих
фрагментов, а LLM получает не первые N символов, а именно те фрагменты,
которые совпали с вопросом. Это важно, когда у публикации есть полный текст
на десятки страниц: склеивать его целиком и сравнивать с запросом нельзя.

Индекс хранится в памяти в компактном виде (массивы чисел вместо
объектов Python) и пересоздаётся автоматически при изменении базы:
отпечаток (число публикаций/фрагментов/связей авторов) проверяется на каждый
поиск тремя дешёвыми запросами. После крупных изменений (импорт, удаление,
загрузка полного текста) вызывается invalidate().
"""
import math
import threading
from array import array
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..tfidf import tokenize

_lock = threading.Lock()
_cache: Optional["CorpusIndex"] = None


def _fingerprint(db: Session) -> tuple:
    p = db.query(func.count(models.Publication.publication_id), func.max(models.Publication.publication_id)).one()
    c = db.query(func.count(models.DocumentChunk.chunk_id), func.max(models.DocumentChunk.chunk_id)).one()
    a = db.query(func.count(models.PublicationAuthor.link_id)).scalar()
    return (p[0], p[1], c[0], c[1], a)


class CorpusIndex:
    def __init__(self, db: Session, fingerprint: Optional[tuple] = None):
        self.fingerprint = fingerprint or _fingerprint(db)
        self.unit_pub = array("q")      # id публикации для каждой единицы поиска
        self.unit_chunk = array("q")    # id фрагмента (0 — «карточка» публикации)
        self.postings: Dict[str, Tuple[array, array]] = {}  # слово -> (номера единиц, tf)
        self._build(db)

    # -- построение ---------------------------------------------------------
    def _add_unit(self, pub_id: int, chunk_id: int, text: str) -> None:
        tokens = tokenize(text)
        if not tokens:
            return
        u = len(self.unit_pub)
        self.unit_pub.append(pub_id)
        self.unit_chunk.append(chunk_id)
        total = len(tokens)
        for term, count in Counter(tokens).items():
            entry = self.postings.get(term)
            if entry is None:
                entry = (array("I"), array("f"))
                self.postings[term] = entry
            entry[0].append(u)
            entry[1].append(count / total)

    def _build(self, db: Session) -> None:
        authors: Dict[int, List[str]] = defaultdict(list)
        for pid, last, first, middle in (
            db.query(
                models.PublicationAuthor.publication_id,
                models.Author.last_name, models.Author.first_name, models.Author.middle_name,
            ).join(models.Author, models.Author.author_id == models.PublicationAuthor.author_id).all()
        ):
            authors[pid].append(" ".join(p for p in (last, first, middle) if p))

        # аннотация — первый фрагмент публикации — входит в «карточку»
        first: Dict[int, tuple] = {}
        for cid, pid, ci in db.query(
            models.DocumentChunk.chunk_id, models.DocumentChunk.publication_id, models.DocumentChunk.chunk_index
        ).all():
            if pid not in first or (ci, cid) < first[pid][:2]:
                first[pid] = (ci, cid)
        cid_to_pid = {v[1]: pid for pid, v in first.items()}
        merged_ids = set(cid_to_pid)
        abstract: Dict[int, str] = {}
        ids = list(merged_ids)
        for i in range(0, len(ids), 500):
            for cid, text in db.query(models.DocumentChunk.chunk_id, models.DocumentChunk.text).filter(
                models.DocumentChunk.chunk_id.in_(ids[i:i + 500])
            ).all():
                abstract[cid_to_pid[cid]] = text

        for pid, title, journal in db.query(
            models.Publication.publication_id, models.Publication.title, models.Publication.journal
        ).all():
            self._add_unit(pid, 0, " ".join([title or "", " ".join(authors.get(pid, [])), journal or "", abstract.get(pid, "")]))

        for chunk_id, pid, text in db.query(
            models.DocumentChunk.chunk_id, models.DocumentChunk.publication_id, models.DocumentChunk.text
        ).yield_per(500):
            if chunk_id in merged_ids:
                continue
            self._add_unit(pid, chunk_id, text)

        n_units = max(len(self.unit_pub), 1)
        self.idf: Dict[str, float] = {
            term: math.log((1 + n_units) / (1 + len(ids))) + 1 for term, (ids, _tf) in self.postings.items()
        }
        sq = [0.0] * len(self.unit_pub)
        for term, (ids, tfs) in self.postings.items():
            idf = self.idf[term]
            for u, tf in zip(ids, tfs):
                sq[u] += (tf * idf) ** 2
        self.norm = [math.sqrt(v) or 1.0 for v in sq]

    # -- поиск --------------------------------------------------------------
    @property
    def n_units(self) -> int:
        return len(self.unit_pub)

    def score_units(self, query_text: str) -> Dict[int, float]:
        """Оценка единиц, где есть общие слова с запросом: косинусное сходство TF-IDF,
        умноженное на покрытие запроса — долю его слов (с учётом их веса), найденных в
        единице. Покрытие нужно потому, что обычный косинус завышает оценку коротких
        текстов: запись из одного названия, где совпали 2 слова из 5, обгоняла статью,
        где найдены все 5, но которая длиннее. На реальной базе из-за этого точность на
        английских вопросах была ниже, чем у прежнего поиска."""
        tokens = tokenize(query_text)
        if not tokens:
            return {}
        counts = Counter(tokens)
        total = len(tokens)
        qvec = {
            t: (c / total) * self.idf[t] for t, c in counts.items() if t in self.postings
        }
        qnorm = math.sqrt(sum(v * v for v in qvec.values()))
        if qnorm == 0:
            return {}
        acc: Dict[int, float] = defaultdict(float)
        cov: Dict[int, float] = defaultdict(float)
        for term, qw in qvec.items():
            ids, tfs = self.postings[term]
            idf = self.idf[term]
            for u, tf in zip(ids, tfs):
                acc[u] += qw * tf * idf
                cov[u] += qw * qw
        q2 = qnorm * qnorm
        return {u: (dot / (qnorm * self.norm[u])) * (cov[u] / q2) for u, dot in acc.items()}


def get_index(db: Session) -> CorpusIndex:
    global _cache
    fp = _fingerprint(db)
    with _lock:
        if _cache is None or _cache.fingerprint != fp:
            _cache = CorpusIndex(db, fp)
        return _cache


def invalidate() -> None:
    """Сбросить индекс после изменения данных (будет пересоздан при следующем поиске)."""
    global _cache
    with _lock:
        _cache = None
    from .. import embeddings, ontology  # локальный импорт, чтобы не было циклических зависимостей
    embeddings.invalidate_vectors()
    ontology.invalidate()
