"""
Центральный компонент модуля sources: опрашивает все зарегистрированные
источники параллельно (asyncio.gather) и объединяет результаты. Сбой
одного источника (таймаут, недоступность API) не должен ронять весь
поиск — такие ошибки логируются и просто исключаются из выдачи.
"""
import asyncio

from .crossref.adapter import CrossrefAdapter
from .models import PublicationResult
from .openalex.adapter import OpenAlexAdapter


# источники, выключенные в админ-панели (имена в нижнем регистре: «openalex», «crossref»)
DISABLED: set = set()


def _describe(exc: Exception) -> str:
    """Короткое описание сбоя для пользователя: для HTTP-ошибок — код ответа
    (429 — лимит запросов, 403 — доступ/ключ), для остальных — тип и начало
    сообщения."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return f"HTTP {status}"
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:80] if str(exc) else ''}".rstrip(": ")


class SourceManager:
    def __init__(self):
        self.sources = [
            OpenAlexAdapter(),
            CrossrefAdapter(),
        ]

    async def search_detailed(
        self, query: str, limit: int = 20, language: str | None = None
    ) -> tuple[list[PublicationResult], list[str]]:
        """Возвращает (публикации, список ошибок источников). Сбой одного
        источника не роняет поиск, но вызывающий код может сообщить
        пользователю, что часть внешних баз была недоступна."""
        active = [s for s in self.sources if s.name.lower() not in DISABLED]
        tasks = [source.search(query=query, limit=limit, language=language) for source in active]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        publications: list[PublicationResult] = []
        errors: list[str] = []
        for source, result in zip(active, results):
            if isinstance(result, Exception):
                print(f"[sources] Ошибка источника {source.name}: {result}")
                errors.append(f"{source.name}: {_describe(result)}")
                continue
            publications.extend(result)

        return publications, errors

    async def search(self, query: str, limit: int = 20, language: str | None = None) -> list[PublicationResult]:
        publications, _ = await self.search_detailed(query=query, limit=limit, language=language)
        return publications
