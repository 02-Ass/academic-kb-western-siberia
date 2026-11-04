"""
Перенос существующей базы на проверенные данные.

В первой версии тестовых данных я (разработчик) подставил в месторождения,
пласт и залежь числа, которых нет ни в одном источнике: глубину, обводнённость,
запасы, пористость, а также координаты и даты открытия. Интерфейс показывал их
как факты. Этот скрипт заменяет их проверенными значениями (с источниками в
описании) или очищает поле.

Безопасность:
- по умолчанию ничего не меняется — скрипт только показывает план (dry-run);
- меняются ТОЛЬКО значения, которые всё ещё равны прежним выдуманным: всё, что
  вы заполнили или поправили сами, остаётся как есть;
- публикации с фиктивными ссылками example.ru (5 демонстрационных записей из
  исходных данных) удаляются только с флагом --delete-demo-publications.

Запуск:
    python -m scripts.clean_demo_data                       # посмотреть план
    python -m scripts.clean_demo_data --apply               # применить
    python -m scripts.clean_demo_data --apply --delete-demo-publications
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app import models
from app.database import DATABASE_URL, SessionLocal
from app.services import index as search_index
from app.services.publication_saver import delete_publication

SHAIM_DESC = (
    "Первое промышленное месторождение нефти Западной Сибири (Трёхозёрное). "
    "Официальной датой открытия считается 21 июня 1960 г. (скважина Р-6), "
    "промышленная разработка с 1964 г. Источник: ЦДУ ТЭК (cdu.ru), 2020."
)

# старое выдуманное значение -> что поставить вместо него (None = очистить)
FIELD_FIXES = {
    "Шаимское": {
        "discovery_date": (date(1960, 1, 1), date(1960, 6, 21)),
        "coordinates": ("60.05,64.35", None),
        "depth": (1800.0, None),
        "water_cut": (65.5, None),
        "description": ("Первое открытое месторождение района.", SHAIM_DESC),
    },
    "Шушминское": {
        "discovery_date": (date(1961, 1, 1), date(1985, 1, 1)),
        "coordinates": ("60.10,64.20", "61.166667,64.133333"),
        "depth": (1750.0, None),
        "water_cut": (58.0, None),
        "description": (None, "Открыто в 1985 г.; по запасам мелкое, по геологическому строению сложное. "
                              "Источник: каталог НефтьГазИнформ (oilgasinform.ru)."),
    },
    "Ловинское": {
        "discovery_date": (date(1962, 1, 1), date(1973, 1, 1)),
        "coordinates": ("59.95,64.10", "61.25,65.00"),
        "depth": (1820.0, None),
        "water_cut": (70.2, None),
        "description": (None, "Открыто в 1973 г. скважиной № 31. Источник: nftn.ru (каталог месторождений)."),
    },
}
HORIZON_FIXES = {"thickness": 12.5, "reservoir_type": "терригенный поровый", "permeability": 45.0, "porosity": 18.0}
DEPOSIT_FIXES = {"reserves": 15000000.0}


def _same(current, old) -> bool:
    if current is None or old is None:
        return current is None and old is None
    if isinstance(old, (int, float)):
        try:
            return abs(float(current) - float(old)) < 1e-6
        except (TypeError, ValueError):
            return False
    return current == old


def run(apply: bool, delete_demo: bool) -> None:
    db = SessionLocal()
    changes = []
    try:
        for field in db.query(models.Field).all():
            for attr, (old, new) in FIELD_FIXES.get(field.name, {}).items():
                if old is None and attr == "description":
                    ok = not (field.description or "").strip()
                else:
                    ok = _same(getattr(field, attr), old)
                if ok and not _same(getattr(field, attr), new):
                    changes.append((f"Месторождение «{field.name}»", attr, getattr(field, attr), new))
                    if apply:
                        setattr(field, attr, new)

        for h in db.query(models.Horizon).filter(models.Horizon.index_code == "Т1").all():
            for attr, old in HORIZON_FIXES.items():
                if _same(getattr(h, attr), old):
                    changes.append((f"Пласт «{h.index_code}»", attr, getattr(h, attr), None))
                    if apply:
                        setattr(h, attr, None)
        for d in db.query(models.Deposit).filter(models.Deposit.name == "Шаимская залежь").all():
            for attr, old in DEPOSIT_FIXES.items():
                if _same(getattr(d, attr), old):
                    changes.append((f"Залежь «{d.name}»", attr, getattr(d, attr), None))
                    if apply:
                        setattr(d, attr, None)

        demo = db.query(models.Publication).filter(models.Publication.link.like("%example.ru%")).all()

        print("План изменений полей:" if not apply else "Изменено:")
        if not changes:
            print("  (нечего менять: выдуманных значений не найдено)")
        for obj, attr, old, new in changes:
            print(f"  {obj}: {attr}: {old!r} -> {new!r}")
        print(f"\nДемонстрационные публикации (ссылка example.ru): {len(demo)}")
        for p in demo:
            print(f"  [{p.publication_id}] {p.title}")
        if demo and not delete_demo:
            print("  (не удаляются; добавьте --delete-demo-publications, если они вам не нужны)")
        if apply and delete_demo:
            for p in demo:
                delete_publication(db, p)
            print(f"  удалено: {len(demo)}")

        if apply:
            db.commit()
            search_index.invalidate()
            if DATABASE_URL.startswith("postgresql"):
                try:  # обновить геометрию для карты, если колонка geom уже создана
                    db.execute(text(
                        "UPDATE field SET geom = CASE WHEN coordinates LIKE '%,%' THEN "
                        "ST_SetSRID(ST_MakePoint(split_part(coordinates, ',', 2)::float, "
                        "split_part(coordinates, ',', 1)::float), 4326)::geography ELSE NULL END"
                    ))
                    db.commit()
                    print("\nГеометрия PostGIS (field.geom) обновлена.")
                except Exception:
                    db.rollback()
            print("\nГотово.")
        else:
            print("\nЭто только план. Чтобы применить: python -m scripts.clean_demo_data --apply")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="применить изменения (иначе только показать план)")
    ap.add_argument("--delete-demo-publications", action="store_true", help="удалить демонстрационные публикации example.ru")
    args = ap.parse_args()
    run(args.apply, args.delete_demo_publications)
