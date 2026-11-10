"""
Логический вывод над онтологией и его кэш.

Рассуждатель — owlrl (OWL 2 RL, чистый Python, без компилируемых библиотек). Полный OWL DL (HermiT)
сильнее, но все аксиомы нашей модели лежат в профиле RL, и результаты совпадают с HermiT
(проверяется командой python -m scripts.ontology_check --hermit).

Граф строится из БД, вывод занимает секунды (1,5 с на 132 публикациях, ~8 с на 1000), результат
кэшируется и пересоздаётся только при изменении данных.
"""
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from owlrl.OWLRL import OWLRL_Semantics
from rdflib import Graph, RDF, URIRef
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from . import vocab as V
from .builder import Registry, build_abox
from .tbox import BaseInfo, find_base_file, load_base

_lock = threading.Lock()
_state: Optional["OntologyState"] = None
_rebuilding = False

_TABLES = [
    models.Province, models.Oblast, models.Rayon, models.Field, models.Horizon, models.Deposit,
    models.ResearchType, models.Author, models.Publication, models.PublicationAuthor,
    models.PublicationField, models.PublicationHorizon, models.PublicationDeposit, models.PublicationResearchType,
    models.PublicationRayon, models.PublicationOblast, models.PublicationProvince,
]


@dataclass
class OntologyState:
    fingerprint: tuple
    built_at: datetime
    build_seconds: float
    closure_seconds: float
    reg: Registry
    explicit: Set[tuple]                     # аксиомы и факты ДО вывода (без литералов)
    literals: Graph                          # подписи и данные (название, год, DOI...)
    full: Graph                              # утверждённое + выведенное + литералы
    inferred: Set[tuple]                     # выведенные значимые факты
    errors: List[str]
    parents: Dict[str, List[Tuple[URIRef, str]]]   # ребёнок -> [(родитель, свойство в базе)]
    about_explicit: Dict[str, List[URIRef]]        # публикация -> объекты, указанные в базе
    structure: Graph = None                  # граф без литералов (для проверки согласованности)
    base: BaseInfo = None                    # откуда взята структура онтологии (ваш файл или запасная копия)
    extensions: List[str] = field(default_factory=list)   # что приложение добавило к вашей онтологии
    explicit_full: Graph = None              # утверждённое + литералы (для экспорта и запросов «без вывода»)
    direct_by_obj: Dict[str, Set[str]] = field(default_factory=dict)  # объект -> публикации, указанные в базе

    @property
    def abox_triples(self) -> int:
        return sum(1 for t in self.explicit if str(t[0]) in self.reg.individuals)


def _fingerprint(db: Session) -> tuple:
    counts = tuple(db.query(func.count()).select_from(m).scalar() for m in _TABLES)
    last_pub = db.query(func.max(models.Publication.publication_id)).scalar()
    # время изменения файла онтологии: правка в Protégé подхватывается без перезапуска приложения
    f = find_base_file()
    return counts + (last_pub, f.stat().st_mtime_ns if f else None, str(f) if f else None)


def _meaningful(t: tuple, reg: Registry) -> bool:
    s, p, o = t
    if str(s) not in reg.individuals:
        return False
    if p in V.MEANINGFUL_PREDICATES:
        return str(o) in reg.individuals
    if p == RDF.type and isinstance(o, URIRef) and str(o).startswith(V.NS):
        return True
    return False


def run_closure(g: Graph) -> Tuple[List[str], float]:
    """Замыкание OWL 2 RL на месте; возвращает (сообщения о противоречиях, секунды)."""
    t = time.time()
    sem = OWLRL_Semantics(g, False, False, False)
    sem.closure()
    sem.flush_stored_triples()
    return list(sem.error_messages), time.time() - t


def build_state(db: Session, fingerprint: Optional[tuple] = None) -> OntologyState:
    t0 = time.time()
    g, lit = Graph(), Graph()
    base = load_base(g)                         # структура — из вашей онтологии
    reg = build_abox(g, lit, db, base)          # экземпляры — из БД по таблице соответствия
    extensions = V.build_tbox(g, lit, base, reg)  # расширения приложения: цепочка свойств, непересечение, определяемые классы

    explicit = set(g)
    structure = Graph()
    for t in explicit:
        structure.add(t)

    errors, closure_seconds = run_closure(g)

    inferred = {t for t in g if t not in explicit and _meaningful(t, reg)}
    full = g
    for t in lit:
        full.add(t)

    parents: Dict[str, List[Tuple[URIRef, str]]] = {}
    about: Dict[str, List[URIRef]] = {}
    direct_by_obj: Dict[str, Set[str]] = {}
    for s, p, o in explicit:
        if p == V.P_IN:
            parents.setdefault(str(s), []).append((o, "входитВ"))
        elif p == V.P_ABOUT:
            about.setdefault(str(s), []).append(o)
            direct_by_obj.setdefault(str(o), set()).add(str(s))

    explicit_full = Graph()
    for t in explicit:
        explicit_full.add(t)
    for t in lit:
        explicit_full.add(t)

    return OntologyState(
        fingerprint=fingerprint or _fingerprint(db), built_at=datetime.now(), build_seconds=time.time() - t0,
        closure_seconds=closure_seconds, reg=reg, explicit=explicit, literals=lit, full=full, inferred=inferred,
        errors=errors, parents=parents, about_explicit=about, structure=structure,
        explicit_full=explicit_full, direct_by_obj=direct_by_obj, base=base, extensions=extensions,
    )


def get_state(db: Session) -> OntologyState:
    """Кэшированное состояние онтологии; пересоздаётся, если данные в базе изменились."""
    global _state
    fp = _fingerprint(db)
    with _lock:
        if _state is None or _state.fingerprint != fp:
            _state = build_state(db, fp)
        return _state


def invalidate() -> None:
    global _state
    with _lock:
        _state = None


def get_state_nowait(db: Session) -> Optional[OntologyState]:
    """Для чата: возвращает готовое состояние и НЕ ждёт пересчёта. Если данные изменились, пересчёт
    запускается в фоне, а до его окончания отдаётся прежнее состояние (или None, если его ещё не было):
    вопрос в чате не должен ждать несколько секунд вывода после каждого импорта."""
    global _rebuilding
    fp = _fingerprint(db)
    with _lock:
        st = _state
        if st is not None and st.fingerprint == fp:
            return st
        if _rebuilding:
            return st
        _rebuilding = True

    def work():
        global _rebuilding
        from ..database import SessionLocal
        session = SessionLocal()
        try:
            get_state(session)
        except Exception as e:
            print(f"[ontology] фоновый пересчёт не удался: {type(e).__name__}: {str(e)[:120]}")
        finally:
            session.close()
            with _lock:
                _rebuilding = False

    threading.Thread(target=work, daemon=True).start()
    return st
