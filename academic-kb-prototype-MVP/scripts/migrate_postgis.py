"""
Добавление геометрической колонки PostGIS к таблице field (у field —
единственная сущность с координатами в новой схеме, раздел 3.4
лабораторной работы). Координаты хранятся как VARCHAR "широта,долгота"
(field.coordinates) — эта миграция разбирает строку и заполняет
geography-колонку для пространственных запросов (ST_DWithin и т.п.).

Запуск (после того как DATABASE_URL указывает на PostgreSQL с
установленным PostGIS):
    python -m scripts.migrate_postgis
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from app.database import engine, DATABASE_URL


def run():
    if not DATABASE_URL.startswith("postgresql"):
        print(
            "DATABASE_URL сейчас не указывает на PostgreSQL "
            f"(текущее значение: {DATABASE_URL}).\n"
            "Эта миграция имеет смысл только для PostgreSQL/PostGIS."
        )
        return

    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))

        conn.execute(text(
            "ALTER TABLE field ADD COLUMN IF NOT EXISTS geom geography(Point, 4326)"
        ))

        conn.execute(text(
            """
            UPDATE field
            SET geom = ST_SetSRID(
                ST_MakePoint(
                    split_part(coordinates, ',', 2)::float,
                    split_part(coordinates, ',', 1)::float
                ), 4326
            )::geography
            WHERE coordinates IS NOT NULL AND coordinates LIKE '%,%'
            """
        ))

        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_field_geom ON field USING GIST (geom)"
        ))

    print("Готово: колонка geom в таблице field добавлена, заполнена из "
          "coordinates и проиндексирована (GIST).")


if __name__ == "__main__":
    run()
