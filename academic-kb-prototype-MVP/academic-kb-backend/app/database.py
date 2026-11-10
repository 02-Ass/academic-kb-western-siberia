"""
Подключение к базе данных.

По умолчанию используется SQLite (файл kb.db) — этого достаточно для
прототипа/MVP и не требует поднятия отдельного сервера БД.

Для перехода на «боевую» конфигурацию с PostgreSQL + PostGIS достаточно
задать переменную окружения DATABASE_URL, например:

    export DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/academic_kb"

Используется драйвер psycopg3 (пакет psycopg[binary]), а не устаревший
psycopg2 — psycopg2-binary имеет известный баг на Windows с
не-английской локалью системы (UnicodeDecodeError при получении
служебных NOTICE-сообщений сервера на языке, отличном от английского).
psycopg3 этой проблемы лишён.

Модели (app/models.py) написаны на чистом SQLAlchemy без специфичных для
SQLite типов, поэтому переключение СУБД не требует правки кода моделей.
Единственное, что придётся добавить при переходе на PostgreSQL/PostGIS —
это тип геометрии (GeoAlchemy2.Geometry) вместо пары latitude/longitude,
используемой здесь для простоты.
"""
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# DATABASE_URL можно задать в файле .env (рядом с requirements.txt). Загружаем его ДО чтения переменной:
# раньше load_dotenv() вызывался в main.py уже после импорта этого модуля, и значение из .env игнорировалось.
# Переменная, заданная в терминале, по-прежнему важнее .env.
load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(os.path.dirname(__file__), "..", "kb.db"),
)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def describe_database() -> str:
    """Строка подключения без пароля — для вывода в консоль и в /api/stats."""
    return engine.url.render_as_string(hide_password=True)
