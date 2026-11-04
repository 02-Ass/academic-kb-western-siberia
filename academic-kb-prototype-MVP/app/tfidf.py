"""
Минимальная реализация TF-IDF + косинусного сходства на чистом Python —
без scikit-learn/numpy/scipy.

Почему не scikit-learn: на некоторых корпоративных/учебных ноутбуках с
Windows политика управления приложениями (Windows Defender Application
Control или аналог) блокирует скомпилированные .pyd-файлы сторонних
библиотек, скачанных через pip, — именно это происходило с
scikit-learn (`_argkmin_classmode`). В отличие от psycopg, у
scikit-learn нет "чистой" альтернативной реализации на системном
уровне, поэтому самый надёжный способ не зависеть от этой политики —
вообще не использовать скомпилированные пакеты для этой задачи.
Корпус документов здесь небольшой (десятки-сотни публикаций), так что
производительность чистого Python более чем достаточна.
"""
import math
import re
from collections import Counter
from typing import List

_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)

# Служебные и вопросительные слова, которые не несут тематики. Без них
# запрос «Какие исследования проводились ... после 2010 года?» совпадал
# с любой публикацией, где встречается «после» или «года».
_STOPWORDS = {
    "и", "в", "во", "на", "по", "к", "с", "со", "о", "об", "от", "до", "за",
    "из", "у", "для", "при", "про", "над", "под", "без", "между", "через",
    "не", "ни", "но", "а", "или", "же", "ли", "бы", "как", "что", "это",
    "то", "так", "также", "его", "ее", "их", "они", "он", "она", "оно",
    "какие", "какой", "какая", "какое", "каких", "каким", "где", "когда",
    "почему", "чем", "кто", "был", "была", "было", "были", "быть", "есть",
    "после", "перед", "года", "год", "году", "годы", "лет",
    "проводились", "проводилось", "проводилась", "проводился",
    "the", "of", "and", "in", "on", "for", "to", "a", "an", "is", "are",
    "what", "which", "how", "after", "before", "about",
}

# Окончания русских слов (от длинных к коротким) для лёгкой нормализации:
# «Шаимском» / «Шаимского» / «Шаимский» → «шаимск». Это не полноценный
# стеммер, но для небольшого корпуса научных названий даёт заметно
# лучшее совпадение словоформ, чем сравнение слов «как есть».
_SUFFIXES = sorted([
    "ыми", "ими", "ого", "его", "ому", "ему", "ией", "ями", "ами", "ов",
    "ев", "ей", "ом", "ем", "ам", "ям", "ах", "ях", "ых", "их", "ую", "юю",
    "ая", "яя", "ое", "ее", "ые", "ие", "ой", "ый", "ий", "ии", "ия", "ию",
    "ть", "ся", "а", "я", "у", "ю", "ы", "и", "е", "о", "ь", "й",
], key=len, reverse=True)


def _stem_latin(token: str) -> str:
    """Минимальная нормализация английских форм: «fields» → «field»,
    «reservoirs» → «reservoir», «studies» → «study». Не стеммер Портера —
    достаточно, чтобы множественное число не мешало совпадению."""
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def _stem(token: str) -> str:
    if not re.search(r"[а-яё]", token):
        return _stem_latin(token) if re.search(r"[a-z]", token) else token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def _tokenize(text: str) -> List[str]:
    tokens = _TOKEN_RE.findall((text or "").lower())
    return [_stem(t) for t in tokens if t not in _STOPWORDS]


def tokenize(text: str) -> List[str]:
    """Публичная точка входа: нормализованные токены (без стоп-слов)."""
    return _tokenize(text)


def tfidf_cosine_scores(corpus: List[str], query: str) -> List[float]:
    """
    Возвращает список оценок косинусного сходства TF-IDF между query и
    каждым документом corpus (в том же порядке). Если словарь пуст
    (например, запрос и корпус состоят только из незнакомых друг другу
    слов) — возвращает список нулей той же длины, что corpus, вместо
    ошибки.
    """
    doc_tokens = [_tokenize(doc) for doc in corpus]
    query_tokens = _tokenize(query)

    n_docs = len(doc_tokens)
    if n_docs == 0 or not query_tokens:
        return [0.0] * n_docs

    # document frequency — в скольких документах встречается каждое слово
    df = Counter()
    for tokens in doc_tokens:
        for term in set(tokens):
            df[term] += 1

    vocabulary = set(df.keys()) & set(query_tokens)
    if not vocabulary:
        return [0.0] * n_docs

    def idf(term: str) -> float:
        # сглаженный IDF (как в scikit-learn по умолчанию): +1 в числителе
        # и знаменателе, чтобы избежать деления на ноль и log(1)=0 для
        # слов, встречающихся во всех документах
        return math.log((1 + n_docs) / (1 + df.get(term, 0))) + 1

    def tfidf_vector(tokens: List[str]) -> dict:
        counts = Counter(tokens)
        total = len(tokens) or 1
        vec = {
            term: (count / total) * idf(term)
            for term, count in counts.items()
            if term in vocabulary
        }
        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm > 0:
            vec = {k: v / norm for k, v in vec.items()}
        return vec

    query_vec = tfidf_vector(query_tokens)
    if not query_vec:
        return [0.0] * n_docs

    scores = []
    for tokens in doc_tokens:
        doc_vec = tfidf_vector(tokens)
        # косинусное сходство двух уже L2-нормированных разреженных векторов
        # = скалярное произведение по общим координатам
        dot = sum(query_vec.get(term, 0.0) * weight for term, weight in doc_vec.items())
        scores.append(dot)

    return scores
