import re
from typing import Optional

from ..models import PublicationResult

_TAG_RE = re.compile(r"<[^>]+>")


def clean_abstract(raw: Optional[str]) -> Optional[str]:
    """Crossref отдаёт аннотацию в JATS-разметке (<jats:p>…</jats:p>) —
    убираем теги и лишние пробелы, чтобы LLM получал чистый текст."""
    if not raw:
        return None
    text = _TAG_RE.sub(" ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


class CrossrefMapper:
    @staticmethod
    def to_publication(item: dict) -> PublicationResult:
        authors = []
        for author in (item.get("author") or []):
            given = author.get("given") or ""
            family = author.get("family") or ""
            full_name = f"{given} {family}".strip()
            if full_name:
                authors.append(full_name)

        title_list = item.get("title") or []
        title = (title_list[0] if title_list else "") or ""
        alt_titles = [t for t in title_list[1:] if t]

        journal_list = item.get("container-title") or []
        journal = journal_list[0] if journal_list else None

        year = None
        date_parts = (item.get("published") or {}).get("date-parts") or []
        if date_parts and date_parts[0] and date_parts[0][0]:
            year = date_parts[0][0]

        doi = item.get("DOI")

        return PublicationResult(
            title=title,
            authors=authors,
            year=year,
            journal=journal,
            abstract=clean_abstract(item.get("abstract")),
            doi=doi,
            url=item.get("URL"),
            external_id=doi,
            source="Crossref",
            citation_count=item.get("is-referenced-by-count"),
            language=item.get("language"),
            alt_titles=alt_titles,
        )
