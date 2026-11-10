"""
Контракт для любого источника научных публикаций. Каждый источник
(OpenAlex, Crossref, в перспективе — eLIBRARY/Google Scholar/CyberLeninka
через разрешённые механизмы доступа, если/когда они появятся) обязан
реализовать один метод: search(query, limit).
"""
from abc import ABC, abstractmethod

from .models import PublicationResult


class PublicationSource(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def search(self, query: str, limit: int = 20, language: str | None = None) -> list[PublicationResult]:
        ...
