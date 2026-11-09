from ..base import PublicationSource
from ..models import PublicationResult
from .client import OpenAlexClient
from .mapper import OpenAlexMapper


class OpenAlexAdapter(PublicationSource):
    def __init__(self):
        self.client = OpenAlexClient()

    @property
    def name(self) -> str:
        return "OpenAlex"

    async def search(self, query: str, limit: int = 20, language: str | None = None) -> list[PublicationResult]:
        data = await self.client.search(query=query, limit=limit, language=language)
        publications: list[PublicationResult] = []
        skipped = 0
        for item in data.get("results") or []:
            try:
                pub = OpenAlexMapper.to_publication(item)
            except Exception as e:  # одна проблемная запись не должна ронять весь ответ
                skipped += 1
                if skipped == 1:
                    print(f"[openalex] пропущена запись: {type(e).__name__}: {str(e).splitlines()[0][:120]}")
                continue
            if not pub.title:
                skipped += 1
                continue
            publications.append(pub)
        if skipped:
            print(f"[openalex] запрос «{query[:50]}»: пропущено записей без названия или с ошибкой: {skipped}")
        return publications
