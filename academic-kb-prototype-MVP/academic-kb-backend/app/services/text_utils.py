"""Общие функции нормализации для сравнения публикаций и авторов."""
import re
from typing import Optional


def norm_doi(doi: Optional[str]) -> Optional[str]:
    """DOI в единой форме: нижний регистр, без префикса https://doi.org/."""
    if not doi:
        return None
    d = doi.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    return d or None


def norm_title(title: Optional[str]) -> str:
    """Название для сравнения: только буквы и цифры, нижний регистр."""
    return re.sub(r"[\W_]+", " ", (title or "").lower()).strip()


# Слова, которые начинаются как предметное слово, но к теме базы не относятся («пластик», «газета»).
# Перед проверкой «по теме ли вопрос» их вырезают, иначе префиксный поиск («пласт», «газ») их принимает.
import re as _re

FALSE_FRIENDS = (
    "пластик", "пластмасс", "пластилин", "пластин", "пластыр",
    "газет", "газон", "газел", "газиров",
)
_FALSE_FRIENDS_RE = _re.compile(r"\b(?:" + "|".join(map(_re.escape, FALSE_FRIENDS)) + r")\w*", _re.IGNORECASE)


def strip_false_friends(text: str) -> str:
    """Заменяет пробелом слова из ``FALSE_FRIENDS`` (с любыми окончаниями)."""
    return _FALSE_FRIENDS_RE.sub(" ", text or "")
