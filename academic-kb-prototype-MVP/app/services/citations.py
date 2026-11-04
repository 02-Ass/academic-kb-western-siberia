"""
Проверка ссылок в ответе LLM.

Модель обязана ссылаться на источники по номерам [n] и перечислять их в конце.
Но языковые модели иногда выдумывают номера, которых нет в контексте, или
вписывают в список «Источники» работы, которых ей не передавали. Для научной
базы знаний это недопустимо (раздел 3.8.3: «отсутствие вымышленных
источников»), поэтому ответ проверяется после генерации:

- номера [n] вне диапазона реально переданных источников удаляются из текста
  (и сообщается, какие);
- строки списка «Источники», которые не удаётся сопоставить ни с одним из
  переданных источников (по названию или DOI), помечаются как непроверенные.
"""
import difflib
import re
from typing import Dict, List, Optional, Tuple

from .text_utils import norm_doi

_MARK_RE = re.compile(r"\[\s*(?:Источник(?:и|ов)?\s*)?(\d+(?:\s*[,;–—-]\s*\d+)*)\s*\]", re.IGNORECASE)
_PROSE_RE = re.compile(r"(?<!\[)\bИсточник(?:а|ов|е|и)?\s+(\d+)\b", re.IGNORECASE)
_SOURCES_HEADER_RE = re.compile(r"^\W*(?:использованные\s+)?источники\b", re.IGNORECASE)
_LINE_ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*\S)")


def _numbers(spec: str) -> List[int]:
    nums: List[int] = []
    for part in re.split(r"\s*[,;]\s*", spec.strip()):
        m = re.fullmatch(r"(\d+)\s*[–—-]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < a <= b and b - a < 20:
                nums.extend(range(a, b + 1))
                continue
        nums.extend(int(x) for x in re.findall(r"\d+", part))
    return nums


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", " ", (text or "").lower()).strip()


def _traceable(line: str, context: List[dict]) -> bool:
    line_n = _norm(line)
    line_low = (line or "").lower()
    for item in context:
        title_n = _norm(item.get("title") or "")
        if len(title_n) >= 8 and title_n in line_n:
            return True
        doi = norm_doi(item.get("doi"))
        if doi and doi in line_low:
            return True
        if title_n and len(title_n) >= 8:
            m = difflib.SequenceMatcher(None, line_n, title_n).find_longest_match(0, len(line_n), 0, len(title_n))
            if m.size / len(title_n) >= 0.6:
                return True
    return False


def verify_answer(answer: str, context: List[dict]) -> Tuple[str, dict]:
    """Возвращает (очищенный ответ, отчёт). context — тот же список источников
    в том же порядке, который был передан модели (нумерация с 1)."""
    n = len(context)
    valid_cited: set = set()
    invalid: set = set()
    removed = 0

    def repl(match: "re.Match") -> str:
        nonlocal removed
        nums = _numbers(match.group(1))
        ok = [x for x in nums if 1 <= x <= n]
        bad = [x for x in nums if not (1 <= x <= n)]
        valid_cited.update(ok)
        invalid.update(bad)
        if bad:
            removed += 1
        if not ok:
            return "[?]"  # заметная пометка вместо молчаливой дыры в предложении
        return "[" + ", ".join(str(x) for x in dict.fromkeys(ok)) + "]"

    clean = _MARK_RE.sub(repl, answer or "")
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r" +([,.;:])", r"\1", clean)

    prose_bad = sorted({int(x) for x in _PROSE_RE.findall(clean) if not (1 <= int(x) <= n)})
    invalid.update(prose_bad)

    # строки списка «Источники»: можно ли сопоставить их с переданным контекстом
    unverified: List[str] = []
    in_sources = False
    for raw in clean.splitlines():
        if _SOURCES_HEADER_RE.match(raw.strip().lstrip("#").strip("*_ ")):
            in_sources = True
            continue
        if not in_sources:
            continue
        m = _LINE_ITEM_RE.match(raw)
        if m and len(m.group(1)) >= 12 and not _traceable(m.group(1), context):
            unverified.append(m.group(1)[:160])

    warnings: List[str] = []
    if invalid:
        nums = ", ".join(f"[{x}]" for x in sorted(invalid))
        warnings.append(f"В ответе были ссылки на несуществующие источники {nums}; такие номера заменены на [?].")
    if unverified:
        warnings.append(
            "Не удалось сопоставить с найденными источниками строки списка литературы: "
            + "; ".join(unverified[:3]) + ". Проверьте их вручную."
        )

    report = {
        "cited": sorted(valid_cited),
        "invalid": sorted(invalid),
        "unverified_lines": unverified,
        "removed_markers": removed,
        "context_size": n,
        "warnings": warnings,
    }
    return clean, report


def citation_cards(cited: List[int], context: List[dict]) -> List[dict]:
    """Источники, на которые ссылается ответ (для кликабельных [n] в интерфейсе)."""
    cards = []
    for n in cited:
        if 1 <= n <= len(context):
            item = context[n - 1]
            doi = item.get("doi")
            url = (f"https://doi.org/{norm_doi(doi)}" if norm_doi(doi) else None) or item.get("url")
            cards.append({"n": n, "title": item.get("title") or "", "url": url, "origin": item.get("origin") or ""})
    return cards
