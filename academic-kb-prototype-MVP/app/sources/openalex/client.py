"""
Клиент OpenAlex API (https://api.openalex.org).

С 2026 года поиск (параметр search) — платный ($1 за 1000 запросов),
но с бесплатным API-ключом даётся бюджет $1/день — для дипломного
MVP (десятки запросов при разработке/защите) этого с большим запасом
достаточно. Ключ не обязателен для работы API вообще, но без него
дневной бесплатный лимит намного меньше ($0.01/день), поэтому ключ
настоятельно рекомендуется.

Получить бесплатный ключ: https://openalex.org/settings/api
"""
import os

import httpx


class OpenAlexClient:
    BASE_URL = "https://api.openalex.org"

    def __init__(self):
        self.api_key = os.environ.get("OPENALEX_API_KEY")
        self.email = os.environ.get("OPENALEX_EMAIL")

    async def search(self, query: str, limit: int = 20, language: str | None = None) -> dict:
        params = {"search": query, "per_page": min(limit, 100)}
        if language:
            # OpenAlex поддерживает фильтр по языку метаданных (ISO 639-1),
            # например filter=language:ru — реально сужает результаты до
            # публикаций с русскоязычным названием/аннотацией.
            params["filter"] = f"language:{language}"
        if self.api_key:
            params["api_key"] = self.api_key
        if self.email:
            params["mailto"] = self.email

        headers = {"User-Agent": f"AcademicKB/1.0 (mailto:{self.email or 'noreply@example.com'})"}

        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            response = await client.get(f"{self.BASE_URL}/works", params=params)
            response.raise_for_status()
            return response.json()

    async def get_work_by_doi(self, doi: str) -> dict | None:
        """Одна работа по DOI (нужна, чтобы найти ссылку на открытый PDF уже
        сохранённой публикации). None, если OpenAlex такой работы не знает."""
        params = {}
        if self.api_key:
            params["api_key"] = self.api_key
        if self.email:
            params["mailto"] = self.email
        headers = {"User-Agent": f"AcademicKB/1.0 (mailto:{self.email or 'noreply@example.com'})"}
        clean = doi.strip()
        if not clean.lower().startswith("http"):
            clean = f"https://doi.org/{clean}"
        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            response = await client.get(f"{self.BASE_URL}/works/{clean}", params=params)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
