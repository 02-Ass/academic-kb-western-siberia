"""
Досчитывает векторы публикаций для смыслового поиска (app/embeddings.py).
Нужен EMBEDDINGS_API_KEY в .env. Повторный запуск пересчитывает только
изменившиеся или новые публикации.

    python -m scripts.build_embeddings
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

from app import embeddings
from app.database import SessionLocal


def run():
    if not embeddings.is_configured():
        print("Сервис эмбеддингов не настроен: добавьте EMBEDDINGS_API_KEY (и при необходимости "
              "EMBEDDINGS_BASE_URL, EMBEDDINGS_MODEL) в .env.")
        return
    db = SessionLocal()
    try:
        while True:
            r = embeddings.ensure_embeddings(db, None, 100)
            print(r)
            if r["embedded"] == 0 or r["failed"]:
                break
    finally:
        db.close()


if __name__ == "__main__":
    run()
