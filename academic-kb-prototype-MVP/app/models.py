"""
SQLAlchemy-модели — реализация физической модели данных IDEF1X из
Лабораторной работы №9 (см. db/schema_lab9.sql).

Иерархия геологических объектов теперь ТИПИЗИРОВАНА (отдельная таблица
на каждый уровень), а не универсальная "objects" таблица, как в первой
версии прототипа:

    Province → Oblast → Rayon → Field → Horizon → Deposit

Публикация связывается с геологическими объектами через три
ассоциативные сущности: PublicationField, PublicationHorizon,
PublicationDeposit (последние две добавлены по вашему запросу сверх
исходной лабораторной работы, где была только publication_field).

Авторы нормализованы в отдельную таблицу Author и связаны с
публикацией через PublicationAuthor (многие-ко-многим, с ролью и
порядком автора).
"""
from sqlalchemy import (
    Column, Integer, String, Float, Numeric, Date, Text, ForeignKey, DateTime, Boolean
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base


class Province(Base):
    __tablename__ = "province"

    province_id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)

    oblasts = relationship("Oblast", back_populates="province", cascade="all, delete-orphan")


class Oblast(Base):
    __tablename__ = "oblast"

    oblast_id = Column(Integer, primary_key=True)
    province_id = Column(Integer, ForeignKey("province.province_id"), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)

    province = relationship("Province", back_populates="oblasts")
    rayons = relationship("Rayon", back_populates="oblast", cascade="all, delete-orphan")


class Rayon(Base):
    __tablename__ = "rayon"

    rayon_id = Column(Integer, primary_key=True)
    oblast_id = Column(Integer, ForeignKey("oblast.oblast_id"), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)

    oblast = relationship("Oblast", back_populates="rayons")
    fields = relationship("Field", back_populates="rayon", cascade="all, delete-orphan")


class Field(Base):
    __tablename__ = "field"

    field_id = Column(Integer, primary_key=True)
    rayon_id = Column(Integer, ForeignKey("rayon.rayon_id"), nullable=False)
    name = Column(String(100), nullable=False)
    discovery_date = Column(Date, nullable=True)
    coordinates = Column(String(50), nullable=True)  # "широта,долгота"
    depth = Column(Numeric(10, 2), nullable=True)
    water_cut = Column(Numeric(5, 2), nullable=True)
    description = Column(Text, nullable=True)

    rayon = relationship("Rayon", back_populates="fields")
    horizons = relationship("Horizon", back_populates="field", cascade="all, delete-orphan")
    publications = relationship(
        "Publication", secondary="publication_field", back_populates="fields"
    )

    @property
    def latitude(self):
        if not self.coordinates or "," not in self.coordinates:
            return None
        try:
            return float(self.coordinates.split(",")[0].strip())
        except ValueError:
            return None

    @property
    def longitude(self):
        if not self.coordinates or "," not in self.coordinates:
            return None
        try:
            return float(self.coordinates.split(",")[1].strip())
        except ValueError:
            return None


class Horizon(Base):
    __tablename__ = "horizon"

    horizon_id = Column(Integer, primary_key=True)
    field_id = Column(Integer, ForeignKey("field.field_id"), nullable=False)
    index_code = Column(String(20), nullable=False)
    formation = Column(String(50), nullable=True)
    thickness = Column(Numeric(10, 2), nullable=True)
    reservoir_type = Column(String(50), nullable=True)
    permeability = Column(Numeric(10, 2), nullable=True)
    porosity = Column(Numeric(5, 2), nullable=True)

    field = relationship("Field", back_populates="horizons")
    deposits = relationship("Deposit", back_populates="horizon", cascade="all, delete-orphan")
    publications = relationship(
        "Publication", secondary="publication_horizon", back_populates="horizons"
    )


class Deposit(Base):
    __tablename__ = "deposit"

    deposit_id = Column(Integer, primary_key=True)
    horizon_id = Column(Integer, ForeignKey("horizon.horizon_id"), nullable=False)
    name = Column(String(100), nullable=False)
    oil_type = Column(String(50), nullable=True)
    reserves = Column(Numeric(15, 2), nullable=True)

    horizon = relationship("Horizon", back_populates="deposits")
    publications = relationship(
        "Publication", secondary="publication_deposit", back_populates="deposits"
    )


class ResearchType(Base):
    __tablename__ = "research_type"

    type_id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)

    publications = relationship(
        "Publication", secondary="publication_research_type", back_populates="research_types"
    )


class Author(Base):
    __tablename__ = "author"

    author_id = Column(Integer, primary_key=True)
    last_name = Column(String(50), nullable=False)
    first_name = Column(String(50), nullable=False)
    middle_name = Column(String(50), nullable=True)
    affiliation = Column(String(200), nullable=True)
    email = Column(String(100), nullable=True)

    @property
    def full_name(self) -> str:
        parts = [self.last_name, self.first_name, self.middle_name]
        return " ".join(p for p in parts if p)


class Publication(Base):
    __tablename__ = "publication"

    publication_id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    year = Column(Integer, nullable=True)
    journal = Column(String(200), nullable=True)
    doi = Column(String(100), nullable=True)
    type = Column(String(50), nullable=False, default="статья")
    link = Column(String(500), nullable=True)
    file_path = Column(Text, nullable=True)

    fields = relationship("Field", secondary="publication_field", back_populates="publications")
    horizons = relationship("Horizon", secondary="publication_horizon", back_populates="publications")
    deposits = relationship("Deposit", secondary="publication_deposit", back_populates="publications")
    # Привязка к объектам верхних уровней (публикация о районе/области/провинции в целом). Хранится самый
    # конкретный уровень, который удалось определить; вышестоящие выводятся по иерархии (app/hierarchy.py).
    rayons = relationship("Rayon", secondary="publication_rayon")
    oblasts = relationship("Oblast", secondary="publication_oblast")
    provinces = relationship("Province", secondary="publication_province")
    research_types = relationship(
        "ResearchType", secondary="publication_research_type", back_populates="publications"
    )
    author_links = relationship(
        "PublicationAuthor", back_populates="publication", cascade="all, delete-orphan",
        order_by="PublicationAuthor.author_order",
    )
    chunks = relationship("DocumentChunk", back_populates="publication", cascade="all, delete-orphan")


class PublicationAuthor(Base):
    __tablename__ = "publication_author"

    link_id = Column(Integer, primary_key=True)
    publication_id = Column(Integer, ForeignKey("publication.publication_id"), nullable=False)
    author_id = Column(Integer, ForeignKey("author.author_id"), nullable=False)
    author_role = Column(String(50), nullable=True)
    author_order = Column(Integer, nullable=True)

    publication = relationship("Publication", back_populates="author_links")
    author = relationship("Author")


class PublicationResearchType(Base):
    __tablename__ = "publication_research_type"

    link_id = Column(Integer, primary_key=True)
    publication_id = Column(Integer, ForeignKey("publication.publication_id"), nullable=False)
    type_id = Column(Integer, ForeignKey("research_type.type_id"), nullable=False)


class PublicationField(Base):
    __tablename__ = "publication_field"

    link_id = Column(Integer, primary_key=True)
    publication_id = Column(Integer, ForeignKey("publication.publication_id"), nullable=False)
    field_id = Column(Integer, ForeignKey("field.field_id"), nullable=False)
    relevance_score = Column(Numeric(5, 2), nullable=True)


class PublicationRayon(Base):
    """Публикация о нефтеносном районе в целом (расширение схемы лаб. работы №9)."""
    __tablename__ = "publication_rayon"

    link_id = Column(Integer, primary_key=True)
    publication_id = Column(Integer, ForeignKey("publication.publication_id"), nullable=False)
    rayon_id = Column(Integer, ForeignKey("rayon.rayon_id"), nullable=False)
    relevance_score = Column(Numeric(5, 2), nullable=True)


class PublicationOblast(Base):
    """Публикация о нефтегазоносной области в целом (расширение схемы)."""
    __tablename__ = "publication_oblast"

    link_id = Column(Integer, primary_key=True)
    publication_id = Column(Integer, ForeignKey("publication.publication_id"), nullable=False)
    oblast_id = Column(Integer, ForeignKey("oblast.oblast_id"), nullable=False)
    relevance_score = Column(Numeric(5, 2), nullable=True)


class PublicationProvince(Base):
    """Публикация о нефтегазоносной провинции в целом (расширение схемы)."""
    __tablename__ = "publication_province"

    link_id = Column(Integer, primary_key=True)
    publication_id = Column(Integer, ForeignKey("publication.publication_id"), nullable=False)
    province_id = Column(Integer, ForeignKey("province.province_id"), nullable=False)
    relevance_score = Column(Numeric(5, 2), nullable=True)


class PublicationHorizon(Base):
    __tablename__ = "publication_horizon"

    link_id = Column(Integer, primary_key=True)
    publication_id = Column(Integer, ForeignKey("publication.publication_id"), nullable=False)
    horizon_id = Column(Integer, ForeignKey("horizon.horizon_id"), nullable=False)
    relevance_score = Column(Numeric(5, 2), nullable=True)


class PublicationDeposit(Base):
    __tablename__ = "publication_deposit"

    link_id = Column(Integer, primary_key=True)
    publication_id = Column(Integer, ForeignKey("publication.publication_id"), nullable=False)
    deposit_id = Column(Integer, ForeignKey("deposit.deposit_id"), nullable=False)
    relevance_score = Column(Numeric(5, 2), nullable=True)


class DocumentChunk(Base):
    """Фрагмент текста загруженного документа (раздел 3.6.1-3.6.3 отчёта),
    используется для семантического поиска (app/search.py)."""

    __tablename__ = "document_chunk"

    chunk_id = Column(Integer, primary_key=True)
    publication_id = Column(Integer, ForeignKey("publication.publication_id"), nullable=False)
    source_filename = Column(String(512), nullable=True)
    chunk_index = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    publication = relationship("Publication", back_populates="chunks")


class PublicationEmbedding(Base):
    """Вектор публикации для смыслового поиска (app/embeddings.py).
    Служебная таблица вне схемы лабораторной работы; создаётся автоматически."""

    __tablename__ = "publication_embedding"

    publication_id = Column(Integer, ForeignKey("publication.publication_id"), primary_key=True)
    model = Column(String(100), nullable=False)
    text_hash = Column(String(40), nullable=False)
    vector = Column(Text, nullable=False)  # JSON-массив чисел
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """Пользователь системы (авторизация, см. app/auth.py). Таблица создаётся автоматически при запуске."""
    __tablename__ = "app_user"

    user_id = Column(Integer, primary_key=True)
    email = Column(String(254), nullable=False, unique=True)
    password_hash = Column(String(300), nullable=False)
    full_name = Column(String(200), nullable=True)
    role = Column(String(20), nullable=False, default="user")  # user | admin
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ChatSession(Base):
    """Сохранённый чат пользователя (только для вошедших; гость историю не хранит)."""
    __tablename__ = "chat_sessions"

    id = Column(String(36), primary_key=True)            # UUID
    user_id = Column(Integer, ForeignKey("app_user.user_id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(200), nullable=False, default="Новый чат")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan",
                            order_by="ChatMessage.id")


class ChatMessage(Base):
    """Одна реплика чата. ``meta`` — JSON ответа ассистента (источники, цитаты, оценка), чтобы старый чат открывался как был."""
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True)
    session_id = Column(String(36), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)             # user | assistant
    content = Column(Text, nullable=False)
    meta = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    session = relationship("ChatSession", back_populates="messages")


class AppSetting(Base):
    """Настройки, которые администратор меняет в админ-панели (ключ → значение в JSON)."""
    __tablename__ = "app_setting"

    key = Column(String(50), primary_key=True)
    value = Column(Text, nullable=False)
class ChatLog(Base):
    """Журнал запросов к чат-боту (для админ-панели)."""
    __tablename__ = "chat_logs"

    id = Column(Integer, primary_key=True)
    user_email = Column(String(254), nullable=True)
    query = Column(Text, nullable=False)
    intent = Column(String(50), nullable=True)
    route = Column(String(50), nullable=True)
    sources_count = Column(Integer, default=0)
    tokens_used = Column(Integer, default=0)
    latency_ms = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())