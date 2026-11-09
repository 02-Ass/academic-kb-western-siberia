"""
Единый формат публикации, к которому приводятся результаты любого
внешнего источника (OpenAlex, Crossref, и в перспективе — другие).

Не путать с app.models.Publication (SQLAlchemy-модель локальной БД) —
PublicationResult это DTO для данных, ещё не сохранённых в PostgreSQL.
"""
from typing import Optional
from pydantic import BaseModel, Field


class PublicationResult(BaseModel):
    title: str
    authors: list[str] = Field(default_factory=list)
    year: Optional[int] = None
    journal: Optional[str] = None
    abstract: Optional[str] = None
    doi: Optional[str] = None
    url: Optional[str] = None
    external_id: Optional[str] = None
    source: str
    keywords: list[str] = Field(default_factory=list)
    citation_count: Optional[int] = None
    is_open_access: Optional[bool] = None
    language: Optional[str] = None  # ISO 639-1, если источник его сообщает
    pdf_url: Optional[str] = None  # прямая ссылка на открытый PDF (только OpenAlex, только open access)
    alt_titles: list[str] = Field(default_factory=list)  # другие названия (например, английское)
