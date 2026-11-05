"""
Массовое наполнение базы публикациями из OpenAlex и Crossref без участия браузера.

Делает то же, что вкладка «Наполнение базы» (предпросмотр + сохранение), но целиком из командной
строки и с отчётом о покрытии: сколько публикаций привязано к каждому месторождению и типу
исследования, как они распределены по годам и языкам, у скольких есть аннотация и DOI.

Запросы берутся из eval/import_queries.txt (можно указать свой файл через --queries-file).

Примеры:
  python -m scripts.bulk_import --dry-run               # только показать, что нашлось, ничего не сохраняя
  python -m scripts.bulk_import                         # найти и сохранить
  python -m scripts.bulk_import --per-query 40 --require-abstract --min-year 1990
  python -m scripts.bulk_import --report-only           # только отчёт о том, что уже в базе
  python -m scripts.bulk_import --require-field         # сохранять только то, что привязалось к месторождению

После импорта пересчитайте эмбеддинги (если задан EMBEDDINGS_API_KEY):  python -m scripts.build_embeddings
Отчёт сохраняется в eval/report-data-ГГГГММДД-ЧЧММ.md — его можно вставить в главу о данных.
"""
import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import func  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app.services import importer  # noqa: E402
from app.services.publication_saver import AuthorIndex, ExistingIndex, save_publication  # noqa: E402

DEFAULT_QUERIES_FILE = ROOT / "eval" / "import_queries.txt"


def load_queries(path: Optional[Path]) -> List[str]:
    """Читает запросы из файла; без файла — встроенный список importer.DEFAULT_QUERIES."""
    path = Path(path) if path else DEFAULT_QUERIES_FILE
    if not path.exists():
        return list(importer.DEFAULT_QUERIES)
    out: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def select_candidates(candidates: List[dict], *, require_abstract: bool = False, min_year: Optional[int] = None,
                      require_field: bool = False, limit: Optional[int] = None) -> List[dict]:
    """Оставляет кандидатов, подходящих под условия импорта. 
    Добавлена жесткая привязка к Шаиму по ключевым словам."""
    out = []
    SHAIM_KEYWORDS = ["shaim", "шаим", "шушмин", "шушим", "ловин"]
    
    for c in candidates:
        p = c["publication"]
        
        if require_abstract and not (p.abstract or "").strip():
            continue
        # Фильтр по году
        if min_year and (p.year or 0) < min_year:
            continue
         
        text_to_check = f"{p.title or ''} {p.abstract or ''}".lower()
        if any(kw in text_to_check for kw in SHAIM_KEYWORDS):
            
            if 1 not in c["field_ids"]:
                c["field_ids"] = list(c["field_ids"]) + [1]
        
        if require_field and not c["field_ids"]:
            continue
            
        out.append(c)
        
    return out[:limit] if limit else out

def commit_candidates(db: Session, candidates: List[dict]) -> Dict[str, object]:
    """Сохраняет кандидатов; сбой одной записи не отменяет остальные."""
    existing, authors = ExistingIndex(db), AuthorIndex(db)
    created = already = failed = 0
    errors: List[str] = []
    ids: List[int] = []
    for c in candidates:
        try:
            with db.begin_nested():
                record, is_new = save_publication(
                    db, c["publication"], field_ids=c["field_ids"], type_ids=c["research_type_ids"],
                    rayon_ids=c.get("rayon_ids", ()), oblast_ids=c.get("oblast_ids", ()),
                    province_ids=c.get("province_ids", ()),
                    existing=existing, authors=authors,
                )
            ids.append(record.publication_id)
            created += 1 if is_new else 0
            already += 0 if is_new else 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            errors.append(f"{c['publication'].title[:60]}: {type(e).__name__}")
            existing, authors = ExistingIndex(db), AuthorIndex(db)
    db.commit()
    return {"created": created, "already": already, "failed": failed, "errors": errors, "ids": ids}


def _is_cyr(text: Optional[str]) -> bool:
    letters = [c for c in (text or "") if c.isalpha()]
    return bool(letters) and sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё") / len(letters) >= 0.5


def _table(headers: List[str], rows: List[List[object]]) -> List[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return out + [""]


def coverage_report(db: Session) -> str:
    """Отчёт о содержимом базы: объём, полнота метаданных, распределение по объектам, типам, годам, языкам."""
    pubs = db.query(models.Publication).all()
    total = len(pubs)
    lines = [f"# Отчёт о наполнении базы ({datetime.now():%Y-%m-%d %H:%M})", "", f"Публикаций: **{total}**.", ""]
    if not total:
        lines.append("База пуста: запустите `python -m scripts.bulk_import`.")
        return "\n".join(lines)

    with_doi = sum(1 for p in pubs if p.doi)
    with_chunks = sum(1 for p in pubs if p.chunks)
    with_field = sum(1 for p in pubs if p.fields)
    with_type = sum(1 for p in pubs if p.research_types)
    with_authors = sum(1 for p in pubs if p.author_links)
    pct = lambda n: f"{n} ({100 * n // total}%)"  # noqa: E731
    lines += ["## Полнота метаданных", ""]
    lines += _table(["Признак", "Публикаций"], [
        ["есть DOI", pct(with_doi)], ["есть текст/аннотация (фрагменты)", pct(with_chunks)],
        ["указаны авторы", pct(with_authors)], ["привязаны к месторождению", pct(with_field)],
        ["указан тип исследования", pct(with_type)],
    ])

    def ids(m):
        return {r[0] for r in db.query(m.publication_id)}
    field_ids = ids(models.PublicationField) | ids(models.PublicationHorizon) | ids(models.PublicationDeposit)
    rayon_ids = ids(models.PublicationRayon) - field_ids
    oblast_ids = ids(models.PublicationOblast) - field_ids - rayon_ids
    province_ids = ids(models.PublicationProvince) - field_ids - rayon_ids - oblast_ids
    linked = len(field_ids | rayon_ids | oblast_ids | province_ids)
    lines += ["## Уровень привязки к иерархии", ""] + _table(["Самый конкретный уровень", "Публикаций"], [
        ["месторождение / пласт / залежь", pct(len(field_ids))], ["район", pct(len(rayon_ids))],
        ["область", pct(len(oblast_ids))], ["провинция", pct(len(province_ids))],
        ["не привязаны (запустите python -m scripts.assign_regions)", pct(total - linked)],
    ])

    by_field = db.query(models.Field.name, func.count(models.PublicationField.publication_id)) \
        .outerjoin(models.PublicationField, models.PublicationField.field_id == models.Field.field_id) \
        .group_by(models.Field.field_id, models.Field.name).order_by(models.Field.name).all()
    lines += ["## Публикации по месторождениям", ""] + _table(["Месторождение", "Публикаций"], [[n, c] for n, c in by_field])
    empty = [n for n, c in by_field if c == 0]
    if empty:
        lines += ["Без публикаций: " + ", ".join(empty) + ". Добавьте запросы с их названиями в eval/import_queries.txt.", ""]

    by_type = db.query(models.ResearchType.name, func.count(models.PublicationResearchType.publication_id)) \
        .outerjoin(models.PublicationResearchType, models.PublicationResearchType.type_id == models.ResearchType.type_id) \
        .group_by(models.ResearchType.type_id, models.ResearchType.name).order_by(models.ResearchType.name).all()
    lines += ["## Публикации по типам исследований", ""] + _table(["Тип", "Публикаций"], [[n, c] for n, c in by_type])

    decades = Counter(((p.year // 10) * 10) if p.year else 0 for p in pubs)
    lines += ["## По десятилетиям", ""] + _table(
        ["Период", "Публикаций"], [[f"{d}-е" if d else "год не указан", decades[d]] for d in sorted(decades)])

    langs = Counter("русский" if _is_cyr(p.title) else "английский/другой" for p in pubs)
    lines += ["## По языку названия", ""] + _table(["Язык", "Публикаций"], [[k, v] for k, v in langs.most_common()])
    return "\n".join(lines)


async def _preview(db: Session, queries: List[str], per_query: int) -> dict:
    from app.sources.manager import SourceManager
    return await importer.build_preview(db, SourceManager(), queries, per_query=per_query)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queries-file", type=Path, default=None)
    ap.add_argument("--per-query", type=int, default=25, help="сколько результатов брать на запрос")
    ap.add_argument("--limit", type=int, default=None, help="максимум новых публикаций за запуск")
    ap.add_argument("--min-year", type=int, default=None)
    ap.add_argument("--require-abstract", action="store_true", help="пропускать публикации без аннотации")
    ap.add_argument("--require-field", action="store_true", help="сохранять только привязанные к месторождению")
    ap.add_argument("--dry-run", action="store_true", help="ничего не сохранять")
    ap.add_argument("--report-only", action="store_true", help="только отчёт о текущем содержимом базы")
    args = ap.parse_args(argv)

    models.Base.metadata.create_all(bind=engine)  # новые таблицы связей создаются, существующие данные не трогаются
    db = SessionLocal()
    try:
        if not args.report_only:
            queries = load_queries(args.queries_file)
            print(f"Запросов: {len(queries)}, на запрос до {args.per_query} результатов. Это может занять несколько минут…")
            preview = asyncio.run(_preview(db, queries, args.per_query))
            for w in preview["warnings"]:
                print("Внимание:", w)
            for e in preview["errors"][:10]:
                print("Ошибка источника:", e)
            if len(preview["errors"]) > 10:
                print(f"…и ещё {len(preview['errors']) - 10} ошибок источников")
            s = preview["stats"]
            print(f"Найдено уникальных: {s['found_total']}; не по теме: {s['irrelevant']}; "
                  f"уже в базе: {s['duplicates_in_db']}; новых кандидатов: {s['new']}")
            chosen = select_candidates(preview["candidates"], require_abstract=args.require_abstract,
                                       min_year=args.min_year, require_field=args.require_field, limit=args.limit)
            print(f"Отобрано по условиям: {len(chosen)}")
            if args.dry_run:
                for c in chosen[:15]:
                    p = c["publication"]
                    print(f"  • {p.year or '—'} | {p.title[:90]} | объекты: {', '.join(c['field_names']) or '—'}")
                print("Режим --dry-run: ничего не сохранено.")
                return 0
            res = commit_candidates(db, chosen)
            print(f"Сохранено новых: {res['created']}, уже были: {res['already']}, ошибок: {res['failed']}")
            for e in res["errors"][:10]:
                print("  не сохранено:", e)
        report = coverage_report(db)
        print("\n" + report)
        out = ROOT / "eval" / f"report-data-{datetime.now():%Y%m%d-%H%M}.md"
        out.parent.mkdir(exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"\nОтчёт сохранён: {out.relative_to(ROOT)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
