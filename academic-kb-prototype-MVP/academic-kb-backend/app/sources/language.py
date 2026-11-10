"""
Фильтрация результатов внешних источников по языку.

OpenAlex умеет фильтровать по языку на стороне сервера (filter=language:ru),
у Crossref такого фильтра нет — его сотрудники прямо пишут об этом на
форуме сообщества. Поэтому язык проверяется ещё и на нашей стороне, по
самому названию: для русского режима в названии должна быть кириллица.
"""
from typing import List, Optional

from .models import PublicationResult


def is_cyrillic(text: Optional[str], threshold: float = 0.5) -> bool:
    """True, если не менее threshold всех букв текста — кириллица."""
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return False
    cyr = sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё")
    return cyr / len(letters) >= threshold


def prefer_language(pubs: List[PublicationResult], language: Optional[str]) -> List[PublicationResult]:
    """language: "ru", "en" или None (без фильтра).

    Для "ru": оставляем публикации, у которых есть название на кириллице.
    Если журнал передал несколько названий (русское и английское), а первым
    стоит английское, подставляем русское. Для "en": оставляем только те,
    у которых название не на кириллице."""
    if not language:
        return list(pubs)

    result: List[PublicationResult] = []
    for pub in pubs:
        if language == "ru":
            candidates = [pub.title, *pub.alt_titles]
            russian = next((t for t in candidates if is_cyrillic(t)), None)
            if russian is None:
                continue
            result.append(pub if russian == pub.title else pub.model_copy(update={"title": russian}))
        elif language == "en":
            if not is_cyrillic(pub.title):
                result.append(pub)
        else:
            result.append(pub)
    return result
