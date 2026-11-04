"""
Определяет уровень иерархии (месторождение → район → область → провинция) для уже сохранённых публикаций
и добавляет недостающие привязки.

Зачем: при импорте большинство публикаций посвящено региону в целом («Западная Сибирь», «Шаимский район»), а
не конкретному месторождению, и раньше оставались вообще без привязки. Теперь у такой публикации хранится
самый конкретный найденный уровень (вышестоящие выводятся по иерархии).

Запуск (по умолчанию ничего не меняет, только показывает, что будет сделано):
  python -m scripts.assign_regions                 # пробный прогон: статистика и примеры
  python -m scripts.assign_regions --apply         # добавить привязки к району/области/провинции
  python -m scripts.assign_regions --apply --tighten
        # дополнительно заменить привязку к месторождению на привязку к району там, где в тексте упомянут
        # только район (старое правило привязывало «Shaim region» к Шаимскому МЕСТОРОЖДЕНИЮ).
        # Касается только публикаций, загруженных из внешних источников (есть аннотация из OpenAlex/Crossref);
        # демонстрационные данные и ручные привязки не трогаются.
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from app import models  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app.services import classifier  # noqa: E402


def publication_text(pub) -> str:
    chunk = next((c.text for c in sorted(pub.chunks, key=lambda c: c.chunk_index)
                  if (c.source_filename or "").startswith("аннотация")), "")
    return f"{pub.title or ''} {chunk}"


def is_imported(pub) -> bool:
    """Публикация пришла из внешнего источника (у неё есть аннотация OpenAlex/Crossref)."""
    return any((c.source_filename or "").startswith("аннотация (") for c in pub.chunks)


def assign(db, apply: bool = False, tighten: bool = False) -> dict:
    fields = [(f.field_id, f.name) for f in db.query(models.Field)]
    rayons = [(r.rayon_id, r.name) for r in db.query(models.Rayon)]
    oblasts = [(o.oblast_id, o.name) for o in db.query(models.Oblast)]
    provinces = [(p.province_id, p.name) for p in db.query(models.Province)]
    stats = Counter()
    examples = []
    for pub in db.query(models.Publication).all():
        stats["total"] += 1
        place = classifier.locate(publication_text(pub), fields, rayons, oblasts, provinces)
        has_object = bool(pub.fields or pub.horizons or pub.deposits)
        has_region = bool(pub.rayons or pub.oblasts or pub.provinces)

        if has_object and not (tighten and is_imported(pub)):
            stats["уже привязана к месторождению/пласту/залежи"] += 1
            continue
        if has_object and place.level in ("rayon", "oblast", "province") and not pub.horizons and not pub.deposits:
            # привязка к месторождению построена только на упоминании района — заменяем на привязку к району
            stats["месторождение → район/область/провинция"] += 1
            if apply:
                db.query(models.PublicationField).filter(models.PublicationField.publication_id == pub.publication_id).delete()
        elif has_object:
            stats["привязка к месторождению подтверждена"] += 1
            continue
        elif has_region:
            stats["уже привязана к району/области/провинции"] += 1
            continue
        if place.level is None:
            stats["регион не определён"] += 1
            if len(examples) < 8:
                examples.append(pub.title)
            continue
        stats[f"новая привязка: {classifier.LEVEL_NAMES[place.level]}"] += 1
        if apply:
            for model, attr, ids in (
                (models.PublicationRayon, "rayon_id", place.rayon_ids),
                (models.PublicationOblast, "oblast_id", place.oblast_ids),
                (models.PublicationProvince, "province_id", place.province_ids),
            ):
                for obj_id in ids:
                    db.add(model(publication_id=pub.publication_id, **{attr: obj_id}))
    if apply:
        db.commit()
    return {"stats": stats, "unplaced_examples": examples}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="записать изменения (без флага — только показать)")
    ap.add_argument("--tighten", action="store_true", help="заменить «районную» привязку к месторождению на привязку к району")
    args = ap.parse_args(argv)
    models.Base.metadata.create_all(bind=engine)  # новые таблицы связей создаются, существующие данные не трогаются
    db = SessionLocal()
    try:
        res = assign(db, apply=args.apply, tighten=args.tighten)
        print(("ПРИМЕНЕНО" if args.apply else "ПРОБНЫЙ ПРОГОН (ничего не записано)") + ":")
        for k, v in res["stats"].most_common():
            print(f"  {k}: {v}")
        for t in res["unplaced_examples"]:
            print(f"  · регион не определён: {t[:90]}")
        if not args.apply:
            print("Чтобы записать: python -m scripts.assign_regions --apply [--tighten]")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
