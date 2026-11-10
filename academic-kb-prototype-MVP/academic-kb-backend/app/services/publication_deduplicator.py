"""
Одна и та же публикация может найтись одновременно в OpenAlex и Crossref
(и по нескольким запросам) — нельзя показывать или сохранять её дважды.

Совпадением считается одинаковый DOI в нормализованном виде (OpenAlex отдаёт
DOI как URL «https://doi.org/10.…», Crossref — просто «10.…», и сравнение
«как строк» их не склеивало) либо одинаковое нормализованное название.
При слиянии выигрывает запись с более полными метаданными, а недостающие
поля (аннотация, DOI, журнал…) добираются из второй.
"""
from typing import List

from app.sources.models import PublicationResult

from .text_utils import norm_doi, norm_title

_FILL_FIELDS = ("abstract", "doi", "journal", "year", "url", "language", "citation_count", "is_open_access")


def _richness(p: PublicationResult) -> tuple:
    return (bool(p.abstract), bool(p.doi), bool(p.journal), len(p.authors))


def _merge(a: PublicationResult, b: PublicationResult) -> PublicationResult:
    primary, other = (a, b) if _richness(a) >= _richness(b) else (b, a)
    update = {f: getattr(other, f) for f in _FILL_FIELDS
              if getattr(primary, f) in (None, "") and getattr(other, f) not in (None, "")}
    update["keywords"] = list(dict.fromkeys([*primary.keywords, *other.keywords]))
    update["alt_titles"] = list(dict.fromkeys([*primary.alt_titles, *other.alt_titles]))
    return primary.model_copy(update=update)


def deduplicate(publications: List[PublicationResult]) -> List[PublicationResult]:
    result: List[PublicationResult] = []
    by_doi: dict = {}
    by_title: dict = {}

    for pub in publications:
        doi, title = norm_doi(pub.doi), norm_title(pub.title)

        idx = by_doi.get(doi) if doi else None
        if idx is None and title:
            candidate = by_title.get(title)
            if candidate is not None:
                other_doi = norm_doi(result[candidate].doi)
                # одинаковое название, но разные DOI — это разные публикации
                if not (doi and other_doi and doi != other_doi):
                    idx = candidate

        if idx is None:
            result.append(pub)
            idx = len(result) - 1
        else:
            result[idx] = _merge(result[idx], pub)

        merged = result[idx]
        for d in (doi, norm_doi(merged.doi)):
            if d:
                by_doi[d] = idx
        if title:
            by_title.setdefault(title, idx)

    return result
