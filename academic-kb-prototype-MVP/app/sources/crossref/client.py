"""
Клиент Crossref REST API (https://api.crossref.org).

Полностью бесплатный, без ключа и без регистрации. Указание mailto
переводит запросы в "вежливый пул" (polite pool) — более быстрые и
надёжные ответы, это просто хорошая практика, не требование.
"""
import os

import httpx


class CrossrefClient:
    BASE_URL = "https://api.crossref.org"

    def __init__(self):
        self.email = os.environ.get("CROSSREF_EMAIL")

    async def search(self, query: str, limit: int = 20) -> dict:
        params = {
            "query.bibliographic": query,
            "rows": min(limit, 100),
        }
        if self.email:
            params["mailto"] = self.email

        headers = {"User-Agent": f"AcademicKB/1.0 (mailto:{self.email or 'noreply@example.com'})"}

        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            response = await client.get(f"{self.BASE_URL}/works", params=params)
            response.raise_for_status()
            return response.json()
