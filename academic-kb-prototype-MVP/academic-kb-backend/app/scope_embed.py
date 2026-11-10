"""
Семантический классификатор темы вопроса: «по теме базы» или «вне темы».

Идея: берём набор эталонных фраз по предметной области (районы, месторождения, пласты, типы
исследований, термины на русском и английском), получаем их эмбеддинги и усредняем в центроид. Вопрос
пользователя тоже переводится в вектор; чем выше косинусная близость к центроиду, тем вероятнее, что
вопрос по теме. В отличие от списка ключевых слов, так распознаются и новые формулировки, и оффтоп,
которого нет ни в каком чёрном списке.

Два порога вместо одного (шкала близости зависит от модели эмбеддингов, универсального «0.7» нет):
- близость >= ``SCOPE_SIM_HIGH`` (по умолчанию 0.50) — «domain»;
- близость <= ``SCOPE_SIM_LOW``  (по умолчанию 0.35) — «offtopic»;
- между порогами — классификатор честно отвечает «не знаю» (None), и решают ключевые слова и LLM.

Пороги нужно подобрать на ВАШЕЙ модели: ``python -m scripts.eval_chat scope`` печатает близость для
вопросов из eval/scope_questions.json и рекомендует значения.

Сервис эмбеддингов необязателен: если ключа нет или сервис недоступен, функции возвращают None, а
``llm.classify_scope`` использует прежние ключевые слова. После сбоя сервис не опрашивается 60 секунд.

Настройка (.env), кроме общих ``EMBEDDINGS_*``:
    SCOPE_EMBED=1            # 0 — не использовать эмбеддинги для темы
    SCOPE_SIM_HIGH=0.50
    SCOPE_SIM_LOW=0.35
    SCOPE_METHOD=centroid    # centroid | top3 (среднее по трём ближайшим эталонам)
"""
import math
import os
import threading
import time
from typing import List, Optional

from . import embeddings

# Эталонные фразы, не зависящие от содержимого базы. Свои можно добавить через ``EXTRA_ANCHORS``.
STATIC_ANCHORS: List[str] = [
    # районы и месторождения
    "Шаимский район Западная Сибирь", "Шаимское месторождение нефти", "Самотлорское месторождение",
    "Приобское месторождение", "Западно-Сибирская нефтегазоносная провинция", "Широтное Приобье",
    "Ханты-Мансийский автономный округ нефть", "Тюменская область месторождения углеводородов",
    "West Siberian oil and gas province", "Samotlor oil field", "Shaim oil field Western Siberia",
    # геология и пласты
    "геологическое строение месторождения", "юрские отложения Западной Сибири", "пласт БС10 нижнемеловые отложения",
    "пласт ЮК1 васюганская свита", "баженовская свита нефтематеринские породы", "тюменская свита коллекторы",
    "стратиграфия и литология нефтегазоносных горизонтов", "фундамент доюрский комплекс Шаимского мегавала",
    "Upper Jurassic reservoirs Vasyugan Formation", "Bazhenov formation shale oil", "Tyumen Formation permeability",
    # свойства и ресурсы
    "пористость и проницаемость коллекторов", "запасы нефти и газа категории", "подсчёт запасов углеводородов",
    "обводнённость скважин и добыча нефти", "физико-химические свойства пластовой нефти",
    "reservoir porosity and permeability", "oil reserves estimation", "hydrocarbon resources assessment",
    # разработка
    "разработка нефтяных месторождений", "заводнение пластов и повышение нефтеотдачи", "бурение скважин и испытание пластов",
    "гидроразрыв пласта", "системы разработки и размещение скважин", "нестационарное заводнение",
    "oil field development waterflooding", "enhanced oil recovery", "well drilling and completion",
    # исследования
    "геофизические исследования скважин", "сейсморазведка 3D интерпретация", "геохимия органического вещества и нефти",
    "гидрогеология пластовые воды рассолы", "моделирование залежи цифровая геологическая модель",
    "geophysical well logging", "seismic exploration interpretation", "petroleum geochemistry biomarkers",
    "hydrogeology formation water brines", "reservoir modelling and simulation",
    # научная литература
    "научные публикации по геологии нефти и газа", "диссертация по нефтегазовой геологии", "обзор исследований нефтегазоносности региона",
]
EXTRA_ANCHORS: List[str] = []

_lock = threading.Lock()
_state = {"key": None, "vecs": None, "centroid": None, "anchors": None}
_disabled_until = 0.0
_COOLDOWN_SEC = 60.0


def _flag() -> bool:
    return os.environ.get("SCOPE_EMBED", "1").strip().lower() not in ("0", "false", "no", "off")


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def thresholds() -> tuple:
    """(нижний, верхний) пороги близости."""
    low = _float_env("SCOPE_SIM_LOW", 0.35)
    high = _float_env("SCOPE_SIM_HIGH", 0.50)
    return (min(low, high), max(low, high))


def is_available() -> bool:
    return _flag() and embeddings.is_configured() and time.time() >= _disabled_until


def reset() -> None:
    """Сбрасывает рассчитанные эталоны (после смены модели, набора фраз или в тестах)."""
    global _disabled_until
    with _lock:
        _state.update(key=None, vecs=None, centroid=None, anchors=None)
    _disabled_until = 0.0


def _db_anchors() -> List[str]:
    """Названия районов, месторождений, пластов и типов исследований из базы (если она доступна)."""
    try:
        from . import models
        from .database import SessionLocal

        db = SessionLocal()
        try:
            out: List[str] = []
            out += [f"{r.name} район" for r in db.query(models.Rayon).limit(40)]
            out += [f"{f.name} месторождение" for f in db.query(models.Field).limit(80)]
            out += [" ".join(x for x in (h.formation, h.index_code) if x) for h in db.query(models.Horizon).limit(60)]
            out += [t.name for t in db.query(models.ResearchType).limit(40)]
            return [a.strip() for a in out if a and a.strip()]
        finally:
            db.close()
    except Exception:  # база недоступна — обходимся статическим набором
        return []


def anchor_texts() -> List[str]:
    seen, out = set(), []
    for a in STATIC_ANCHORS + EXTRA_ANCHORS + _db_anchors():
        if a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


def _trip() -> None:
    global _disabled_until
    _disabled_until = time.time() + _COOLDOWN_SEC


def _ensure_anchors() -> bool:
    """Лениво считает эмбеддинги эталонов и центроид. False — сервис недоступен."""
    model = embeddings.model_name()
    with _lock:
        if _state["vecs"] is not None and _state["key"] == model:
            return True
    anchors = anchor_texts()
    vecs = embeddings.embed_texts(anchors)
    if not vecs or len(vecs) != len(anchors):
        _trip()
        return False
    vecs = [embeddings._normalize(v) for v in vecs]
    dim = len(vecs[0])
    centroid = embeddings._normalize([sum(v[i] for v in vecs) / len(vecs) for i in range(dim)])
    with _lock:
        _state.update(key=model, vecs=vecs, centroid=centroid, anchors=anchors)
    return True


def _dot(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def similarity(message: str) -> Optional[float]:
    """Близость вопроса к предметной области (косинус; чем выше, тем ближе) или None, если недоступно."""
    text = (message or "").strip()
    if not text or not is_available():
        return None
    if not _ensure_anchors():
        return None
    qv = embeddings.embed_texts([text])
    if not qv:
        _trip()
        return None
    q = embeddings._normalize(qv[0])
    with _lock:
        centroid, vecs = _state["centroid"], _state["vecs"]
    if os.environ.get("SCOPE_METHOD", "centroid").strip().lower() == "top3":
        top = sorted((_dot(q, v) for v in vecs), reverse=True)[:3]
        return sum(top) / len(top)
    return _dot(q, centroid)


def classify(message: str, has_history: bool = False) -> Optional[str]:
    """«domain», «offtopic» или None («не уверен» / сервис недоступен).

    Args:
        message: вопрос пользователя.
        has_history: есть ли предыдущие реплики. Для коротких уточнений («а после 2018?») близость
            к теме сама по себе ничего не говорит, поэтому при наличии истории и коротком вопросе
            «offtopic» не выносится — только «domain» при высокой близости.
    """
    sim = similarity(message)
    if sim is None or math.isnan(sim):
        return None
    low, high = thresholds()
    print(f"[scope] близость к теме: {sim:.3f} (пороги: {low:.2f} / {high:.2f})")
    if sim >= high:
        return "domain"
    if sim <= low:
        if has_history and len((message or "").split()) < 5:
            return None
        return "offtopic"
    return None


def warmup() -> None:
    """Заранее считает эмбеддинги эталонов (вызывается в фоне при старте сервера), чтобы первый вопрос
    не ждал десятки обращений к сервису. Ошибки молча игнорируются: классификатор работает лениво."""
    try:
        if is_available():
            _ensure_anchors()
    except Exception:  # noqa: BLE001
        pass
