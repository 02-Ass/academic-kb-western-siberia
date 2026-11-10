from datetime import date, datetime
from typing import List, Optional
from pydantic import BaseModel, ConfigDict


# --------------------------------------------------------------------------
# Геологическая иерархия
# --------------------------------------------------------------------------

class ProvinceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    province_id: int
    name: str
    description: Optional[str] = None


class OblastOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    oblast_id: int
    province_id: int
    name: str
    description: Optional[str] = None


class RayonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    rayon_id: int
    oblast_id: int
    name: str
    description: Optional[str] = None


class FieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    field_id: int
    rayon_id: int
    name: str
    discovery_date: Optional[date] = None
    coordinates: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    depth: Optional[float] = None
    water_cut: Optional[float] = None
    description: Optional[str] = None


class HorizonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    horizon_id: int
    field_id: int
    index_code: str
    formation: Optional[str] = None
    thickness: Optional[float] = None
    reservoir_type: Optional[str] = None
    permeability: Optional[float] = None
    porosity: Optional[float] = None


class DepositOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    deposit_id: int
    horizon_id: int
    name: str
    oil_type: Optional[str] = None
    reserves: Optional[float] = None


class RayonDetailOut(RayonOut):
    oblast: Optional[OblastOut] = None
    province: Optional[ProvinceOut] = None
    fields: List[FieldOut] = []


class FieldDetailOut(FieldOut):
    rayon: Optional[RayonOut] = None
    oblast: Optional[OblastOut] = None
    province: Optional[ProvinceOut] = None
    horizons: List[HorizonOut] = []


# --------------------------------------------------------------------------
# Авторы, типы исследований, публикации
# --------------------------------------------------------------------------

class AuthorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    author_id: int
    last_name: str
    first_name: str
    middle_name: Optional[str] = None
    affiliation: Optional[str] = None
    email: Optional[str] = None


class ResearchTypeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    type_id: int
    name: str
    description: Optional[str] = None


class PublicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    publication_id: int
    title: str
    year: Optional[int] = None
    journal: Optional[str] = None
    doi: Optional[str] = None
    type: str
    link: Optional[str] = None
    authors: List[AuthorOut] = []
    research_types: List[ResearchTypeOut] = []


class RegionalLevelOut(BaseModel):
    """Объект верхнего уровня иерархии и число публикаций, привязанных именно к нему."""
    level: str          # rayon | oblast | province
    object_id: int
    name: str
    total: int


class RegionalPublicationsOut(RegionalLevelOut):
    offset: int = 0
    publications: List[PublicationOut] = []


class PublicationSearchResult(PublicationOut):
    score: float
    matched_field: Optional[str] = None


# --------------------------------------------------------------------------
# Поиск, чат, документы — интерфейс не менялся с предыдущей версии,
# только источник данных теперь новая схема
# --------------------------------------------------------------------------

class SemanticSearchRequest(BaseModel):
    query: str
    rayon_id: Optional[int] = None
    field_id: Optional[int] = None
    research_type_id: Optional[int] = None
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    top_k: int = 5


class SemanticSearchResponse(BaseModel):
    query: str
    resolved_field_ids: List[int] = []
    results: List[PublicationSearchResult]
    answer: str


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: List[ChatMessage] = []
    rayon_id: Optional[int] = None
    field_id: Optional[int] = None
    top_k: int = 5
    use_external: bool = True  # дополнять контекст живым поиском в OpenAlex/Crossref
    external_language: str = "ru"  # "ru" | "en" | "any" — язык публикаций из внешних баз
    session_id: Optional[str] = None  # UUID чата: для вошедшего пользователя реплики сохраняются в эту сессию


class CitationOut(BaseModel):
    n: int
    title: str
    url: Optional[str] = None
    origin: str = ""


class OntologyPubOut(BaseModel):
    publication_id: int
    title: str
    year: Optional[int] = None
    how: str  # direct — привязана в базе напрямую, inferred — найдена логическим выводом
    explanation: str = ""


class OntologyAnswerOut(BaseModel):
    object_kind: str
    object_id: int
    object_name: str
    level: str
    direct: int  # публикаций, привязанных к объекту напрямую
    with_inference: int  # публикаций с учётом логического вывода (вложенные объекты)
    publications: List[OntologyPubOut] = []


class ChatResponse(BaseModel):
    answer: str
    llm_used: bool  # ответ сформирован языковой моделью
    llm_provider: Optional[str] = None
    llm_configured: bool = False  # LLM подключён (не зависит от того, нужен ли он был для этого ответа)
    confidence: Optional[str] = None  # high | medium | low — эвристическая оценка по числу и полноте источников (только если ответила LLM)
    intent: str = "domain"  # domain | smalltalk | capabilities | offtopic
    query_intent: Optional[str] = None  # намерение по смыслу вопроса: FIND_PUBLICATION | FIND_OBJECT | COMPARE_OBJECTS | EXPLAIN | OFFTOPIC | UNKNOWN
    search_strategy: Optional[str] = None  # standard | compare | object — как выполнялся поиск
    suggestions: List[str] = []  # подсказки-вопросы (кнопки) при отказе по теме
    is_offtopic: bool = False  # вопрос вне темы: поиск и LLM не выполнялись
    sources: List[PublicationOut] = []  # публикации из локальной базы
    external_sources: List["ExternalPublicationOut"] = []  # найденные во внешних базах
    external_query: Optional[str] = None  # запрос, реально ушедший во внешние базы
    external_errors: List[str] = []  # источники, которые не ответили
    external_note: Optional[str] = None  # пояснение пользователю (например, «на русском ничего не найдено»)
    citations: List[CitationOut] = []  # источники, на которые ссылается ответ ([n] → ссылка)
    citation_warnings: List[str] = []  # результат проверки ссылок в ответе
    kb_objects: List[str] = []  # объекты БД, по которым в LLM передана справка
    local_queries: List[str] = []  # формулировки запроса, использованные для поиска в локальной базе
    ontology: Optional[OntologyAnswerOut] = None  # результат по онтологии с логическим выводом, если в вопросе назван объект


class DocumentUploadResponse(BaseModel):
    filename: str
    publication_id: int
    chunks_created: int
    preview: str


# --------------------------------------------------------------------------
# Внешний поиск публикаций (OpenAlex + Crossref) — без изменений
# --------------------------------------------------------------------------

class ExternalPublicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    title: str
    authors: List[str] = []
    year: Optional[int] = None
    journal: Optional[str] = None
    abstract: Optional[str] = None
    doi: Optional[str] = None
    url: Optional[str] = None
    external_id: Optional[str] = None
    source: str
    keywords: List[str] = []
    citation_count: Optional[int] = None
    is_open_access: Optional[bool] = None
    language: Optional[str] = None
    pdf_url: Optional[str] = None


class ExternalSearchResponse(BaseModel):
    query: str
    count: int
    results: List[ExternalPublicationOut]


class SavePublicationRequest(BaseModel):
    publication: ExternalPublicationOut
    field_id: Optional[int] = None
    research_type_id: Optional[int] = None
    # True — месторождение и тип исследования определяются автоматически по
    # тексту публикации (для кнопки «В базу» в чате); False — только то, что
    # выбрано явно (вкладка «Внешний поиск»)
    auto_link: bool = False


ChatResponse.model_rebuild()


class PublicationListItem(BaseModel):
    publication_id: int
    title: str
    year: Optional[int] = None
    journal: Optional[str] = None
    doi: Optional[str] = None
    link: Optional[str] = None
    authors: List[str] = []


# --------------------------------------------------------------------------
# Массовое наполнение базы
# --------------------------------------------------------------------------

class ImportPreviewRequest(BaseModel):
    queries: Optional[List[str]] = None  # None — стандартный набор запросов
    per_query: int = 15


class ImportCandidate(BaseModel):
    publication: ExternalPublicationOut
    field_ids: List[int] = []
    field_names: List[str] = []
    level: Optional[str] = None            # field | rayon | oblast | province | None — уровень привязки к иерархии
    rayon_ids: List[int] = []
    oblast_ids: List[int] = []
    province_ids: List[int] = []
    place_names: List[str] = []            # названия объектов того уровня, к которому привязана публикация
    research_type_ids: List[int] = []
    research_type_names: List[str] = []
    matched_query: Optional[str] = None


class ImportStats(BaseModel):
    found_total: int
    duplicates_in_db: int
    irrelevant: int
    new: int


class ImportPreviewResponse(BaseModel):
    candidates: List[ImportCandidate]
    stats: ImportStats
    irrelevant_examples: List[str] = []
    errors: List[str] = []
    warnings: List[str] = []


class ImportCommitRequest(BaseModel):
    items: List[ImportCandidate]


class ImportCommitResponse(BaseModel):
    created: int
    already_existed: int
    failed: int
    errors: List[str] = []
    publication_ids: List[int] = []


class DefaultQueriesResponse(BaseModel):
    queries: List[str]
    openalex_key_set: bool


# --------------------------------------------------------------------------
# Полные тексты открытых статей и смысловой поиск
# --------------------------------------------------------------------------

class FulltextRequest(BaseModel):
    publication_ids: Optional[List[int]] = None  # None — публикации с DOI, у которых ещё нет полного текста
    limit: int = 15


class FulltextItemResult(BaseModel):
    publication_id: int
    title: str
    status: str  # ok | already | no_doi | no_open_access | not_pdf | too_big | no_text | download_failed | error
    chunks: int = 0
    detail: str = ""


class FulltextResponse(BaseModel):
    results: List[FulltextItemResult]
    ok: int
    skipped: int
    failed: int


class FulltextStatus(BaseModel):
    publications: int
    with_doi: int
    with_fulltext: int
    candidates: int  # есть DOI, полного текста нет


class EmbeddingsStatus(BaseModel):
    configured: bool
    model: Optional[str] = None
    publications: int
    embedded: int
    min_similarity: float


# --- авторизация и экспорт (v35) ---

class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: Optional[str] = None


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    user_id: int
    email: str
    full_name: Optional[str] = None
    role: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class ExportRequest(BaseModel):
    publication_ids: List[int]
    format: str = "csv"


class ChatSessionOut(BaseModel):
    id: str
    title: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ChatSessionCreate(BaseModel):
    title: Optional[str] = None


class ChatMessageOut(BaseModel):
    id: int
    role: str
    content: str
    meta: Optional[dict] = None
    created_at: Optional[datetime] = None
