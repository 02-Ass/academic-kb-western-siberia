from ..base import PublicationSource
from ..models import PublicationResult
from .client import CrossrefClient
from .mapper import CrossrefMapper


class CrossrefAdapter(PublicationSource):
    def __init__(self):
        self.client = CrossrefClient()

    @property
    def name(self) -> str:
        return "Crossref"

    async def search(self, query: str, limit: int = 20, language: str | None = None) -> list[PublicationResult]:
        # Crossref не предоставляет надёжного фильтра по языку метаданных
        # (в отличие от OpenAlex) — параметр принимается для единообразия
        # интерфейса PublicationSource, но здесь не используется.
        data = await self.client.search(query=query, limit=limit)
        publications: list[PublicationResult] = []
        skipped = 0
        for item in (data.get("message") or {}).get("items") or []:
            try:
                pub = CrossrefMapper.to_publication(item)
            except Exception as e:  # одна проблемная запись не должна ронять весь ответ
                skipped += 1
                if skipped == 1:
                    print(f"[crossref] пропущена запись: {type(e).__name__}: {str(e).splitlines()[0][:120]}")
                continue
            if not pub.title:
                skipped += 1
                continue
            publications.append(pub)
        if skipped:
            print(f"[crossref] запрос «{query[:50]}»: пропущено записей без названия или с ошибкой: {skipped}")
        return publications
