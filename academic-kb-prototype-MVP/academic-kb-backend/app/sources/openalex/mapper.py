from typing import Optional

from ..models import PublicationResult


def abstract_from_inverted_index(inverted_index: Optional[dict]) -> Optional[str]:
    """OpenAlex отдаёт аннотацию не текстом, а «инвертированным индексом»
    {слово: [позиции в тексте]} (из-за лицензионных ограничений издателей).
    Восстанавливаем исходный порядок слов по позициям."""
    if not inverted_index:
        return None
    positions = []
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions.append((i, word))
    if not positions:
        return None
    positions.sort(key=lambda x: x[0])
    return " ".join(word for _, word in positions)


class OpenAlexMapper:
    @staticmethod
    def to_publication(item: dict) -> PublicationResult:
        # OpenAlex иногда отдаёт null вместо списка или строки (особенно у
        # записей без заголовка) — любое поле читаем через "or", а не через
        # значение по умолчанию у .get(), которое при явном null не срабатывает.
        authors = [
            a["author"]["display_name"]
            for a in (item.get("authorships") or [])
            if a and a.get("author") and a["author"].get("display_name")
        ]

        primary_location = item.get("primary_location") or {}
        source = primary_location.get("source") or {}
        journal = source.get("display_name")

        landing_page = primary_location.get("landing_page_url") or item.get("id")
        best_oa = item.get("best_oa_location") or {}
        pdf_url = best_oa.get("pdf_url") or primary_location.get("pdf_url")
        open_access = item.get("open_access") or {}

        return PublicationResult(
            title=(item.get("display_name") or item.get("title") or "").strip(),
            authors=authors,
            year=item.get("publication_year"),
            journal=journal,
            abstract=abstract_from_inverted_index(item.get("abstract_inverted_index")),
            doi=item.get("doi"),
            url=landing_page,
            external_id=item.get("id"),
            source="OpenAlex",
            keywords=[
                k.get("display_name")
                for k in (item.get("keywords") or [])
                if k and k.get("display_name")
            ],
            citation_count=item.get("cited_by_count"),
            is_open_access=open_access.get("is_oa"),
            language=item.get("language"),
            pdf_url=pdf_url,
        )
