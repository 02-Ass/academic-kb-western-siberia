"""
Сохранение публикации из внешнего источника в локальную базу.

Исправляет проблемы первой версии сохранения:
- дубликаты: одна и та же статья (по DOI или названию) не сохраняется дважды;
- авторы: человек, уже существующий в таблице author, переиспользуется, а не
  создаётся заново при каждой публикации;
- длины полей: значения обрезаются до размеров колонок (в PostgreSQL слишком
  длинная строка вызывает ошибку, в SQLite — нет, поэтому на SQLite такой
  баг не виден);
- ФИО: корректный разбор и «Иванов И.И.», и «Иван Иванов», и
  «Абдрашитова Римма Наильевна».
"""
import re
from typing import Dict, Iterable, Optional, Tuple

from sqlalchemy.orm import Session

from .. import models
from .text_utils import norm_doi, norm_title

_PATRONYMIC = re.compile(r"(ович|евич|ьич|овна|евна|ична|инична|оглы|кызы)$", re.IGNORECASE)
_INITIALS = re.compile(r"^(?:[A-ZА-ЯЁ][a-zа-яё]?\.){1,3}$|^[A-ZА-ЯЁ]{1,3}$")


def parse_author_name(full_name: str) -> Tuple[str, str, Optional[str]]:
    """Эвристика: (фамилия, имя, отчество) из цельной строки внешнего
    источника. Однозначно разобрать ФИО из одной строки нельзя, поэтому это
    приближение; оно учитывает инициалы и русские отчества."""
    name = " ".join((full_name or "").replace(",", " ").split())
    if not name:
        return ("Автор не указан", "", None)
    parts = name.split(" ")
    if len(parts) == 1:
        return (parts[0], "", None)

    first_is_initials = bool(_INITIALS.match(parts[0]))
    last_is_initials = bool(_INITIALS.match(parts[-1]))

    if first_is_initials and not last_is_initials:      # «И.И. Иванов», «M. G. Poluyanov»
        return (parts[-1], parts[0], " ".join(parts[1:-1]) or None)
    if last_is_initials and not first_is_initials:      # «Иванов И.И.», «Иванов И. И.»
        return (parts[0], parts[1], " ".join(parts[2:]) or None)
    if len(parts) == 3 and _PATRONYMIC.search(parts[2]):  # «Абдрашитова Римма Наильевна»
        return (parts[0], parts[1], parts[2])
    if len(parts) == 3 and _PATRONYMIC.search(parts[1]):  # «Инна Николаевна Пономарева»
        return (parts[2], parts[0], parts[1])
    return (parts[-1], parts[0], " ".join(parts[1:-1]) or None)  # «Andrey G. Plavnik»


def _cut(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value[:limit] if value else None


class ExistingIndex:
    """Индекс уже сохранённых публикаций для проверки дубликатов."""

    def __init__(self, db: Session):
        self.by_doi: Dict[str, int] = {}
        self.by_title: Dict[str, int] = {}
        for pid, title, doi in db.query(
            models.Publication.publication_id, models.Publication.title, models.Publication.doi
        ).all():
            self.add(pid, doi, title)

    def add(self, publication_id: int, doi: Optional[str], title: Optional[str]) -> None:
        d = norm_doi(doi)
        if d:
            self.by_doi[d] = publication_id
        t = norm_title(title)
        if t:
            self.by_title[t] = publication_id

    def find(self, doi: Optional[str], title: Optional[str]) -> Optional[int]:
        d = norm_doi(doi)
        if d and d in self.by_doi:
            return self.by_doi[d]
        t = norm_title(title)
        if t and t in self.by_title:
            return self.by_title[t]
        return None


class AuthorIndex:
    """Индекс авторов: сравнение без учёта регистра выполняется в Python
    (lower() в SQL зависит от локали сервера и для кириллицы ненадёжен)."""

    def __init__(self, db: Session):
        self._db = db
        self._map: Dict[Tuple[str, str, str], int] = {}
        for a in db.query(models.Author).all():
            self._map[self._key(a.last_name, a.first_name, a.middle_name)] = a.author_id

    @staticmethod
    def _key(last: str, first: str, middle: Optional[str]) -> Tuple[str, str, str]:
        return ((last or "").lower(), (first or "").lower(), (middle or "").lower())

    def get_or_create(self, full_name: str) -> int:
        last, first, middle = parse_author_name(full_name)
        last, first, middle = _cut(last, 50) or "—", _cut(first, 50) or "", _cut(middle, 50)
        key = self._key(last, first, middle)
        if key in self._map:
            return self._map[key]
        author = models.Author(last_name=last, first_name=first, middle_name=middle)
        self._db.add(author)
        self._db.flush()
        self._map[key] = author.author_id
        return author.author_id


def save_publication(
    db: Session,
    pub,
    *,
    field_ids: Iterable[int] = (),
    type_ids: Iterable[int] = (),
    rayon_ids: Iterable[int] = (),
    oblast_ids: Iterable[int] = (),
    province_ids: Iterable[int] = (),
    existing: Optional[ExistingIndex] = None,
    authors: Optional[AuthorIndex] = None,
) -> Tuple[models.Publication, bool]:
    """Сохраняет публикацию и связи. Возвращает (публикация, создана_ли_новая).
    Если такая публикация уже есть — новая не создаётся, но недостающие
    связи с месторождениями/типами исследований добавляются."""
    existing = existing or ExistingIndex(db)
    authors = authors or AuthorIndex(db)

    found_id = existing.find(pub.doi, pub.title)
    if found_id is not None:
        record = db.get(models.Publication, found_id)
        created = False
    else:
        record = models.Publication(
            title=_cut(pub.title, 500) or "Без названия",
            year=pub.year,
            journal=_cut(pub.journal, 200),
            doi=_cut(norm_doi(pub.doi) or None, 100),
            type="статья",
            link=_cut(pub.url, 500),
        )
        db.add(record)
        db.flush()
        for order, author_name in enumerate(pub.authors or [], start=1):
            db.add(models.PublicationAuthor(
                publication_id=record.publication_id,
                author_id=authors.get_or_create(author_name),
                author_order=order,
            ))
        existing.add(record.publication_id, pub.doi, pub.title)
        created = True

    # В схеме publication (лаб. работа №9) нет колонки под аннотацию, а схему
    # менять нельзя — поэтому аннотация хранится как фрагмент текста в служебной
    # таблице document_chunk. Так она участвует в локальном поиске и попадает в
    # контекст LLM, и чат отвечает по содержанию, а не только по названию.
    abstract = (getattr(pub, "abstract", None) or "").strip()
    if abstract and not any((c.source_filename or "").startswith("аннотация") for c in record.chunks):
        origin = getattr(pub, "source", None)
        db.add(models.DocumentChunk(
            publication_id=record.publication_id,
            source_filename=f"аннотация ({origin})" if origin else "аннотация",
            chunk_index=0,
            text=abstract,
        ))

    have_fields = {f.field_id for f in record.fields}
    for field_id in field_ids:
        if field_id not in have_fields:
            db.add(models.PublicationField(publication_id=record.publication_id, field_id=field_id))
            have_fields.add(field_id)
    for ids, have, model, attr in (
        (rayon_ids, {r.rayon_id for r in record.rayons}, models.PublicationRayon, "rayon_id"),
        (oblast_ids, {o.oblast_id for o in record.oblasts}, models.PublicationOblast, "oblast_id"),
        (province_ids, {p.province_id for p in record.provinces}, models.PublicationProvince, "province_id"),
    ):
        for obj_id in ids:
            if obj_id not in have:
                db.add(model(publication_id=record.publication_id, **{attr: obj_id}))
                have.add(obj_id)
    have_types = {t.type_id for t in record.research_types}
    for type_id in type_ids:
        if type_id not in have_types:
            db.add(models.PublicationResearchType(publication_id=record.publication_id, type_id=type_id))
            have_types.add(type_id)
    db.flush()
    return record, created


def delete_publication(db: Session, pub: models.Publication) -> None:
    """Удаляет публикацию со всеми связями (месторождения, пласты, залежи,
    типы исследований, фрагменты текста, векторы) и авторами, у которых не
    осталось ни одной публикации. Коммит выполняет вызывающий код."""
    author_ids = {link.author_id for link in pub.author_links}
    db.query(models.PublicationEmbedding).filter(
        models.PublicationEmbedding.publication_id == pub.publication_id
    ).delete(synchronize_session=False)
    db.delete(pub)
    db.flush()
    for author_id in author_ids:
        still_used = db.query(models.PublicationAuthor).filter(
            models.PublicationAuthor.author_id == author_id
        ).first()
        if still_used is None:
            author = db.get(models.Author, author_id)
            if author is not None:
                db.delete(author)
