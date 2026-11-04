"""
Навигация по геологической иерархии (province → oblast → rayon → field →
horizon → deposit).

В первой версии прототипа структура была универсальной ("objects" с
parent_id), и для неё приходилось отдельно вычислять транзитивное
замыкание через OWL-онтологию (app/ontology.py, rdflib). В новой схеме
(Лабораторная работа №9, IDEF1X) иерархия ТИПИЗИРОВАНА — у каждого
уровня своя таблица с явным внешним ключом на родителя. Поэтому
навигация — это просто обход внешних ключей, без графа и rdflib.

Отдельный OWL-файл (data/oil-gas-ontology.owl) при желании можно
использовать параллельно для семантических задач (раздел 3.2.3 отчёта
по гибридной архитектуре), но для построения иерархии он больше не
нужен — структура уже есть в самой реляционной схеме.
"""
from sqlalchemy.orm import Session, selectinload

from . import models


def descendant_field_ids(db: Session, *, province_id=None, oblast_id=None, rayon_id=None, field_id=None) -> list[int]:
    """Возвращает id всех месторождений, попадающих под заданный уровень
    иерархии (включая сам field_id, если он передан напрямую)."""
    if field_id is not None:
        return [field_id]

    query = db.query(models.Field.field_id)
    if rayon_id is not None:
        query = query.filter(models.Field.rayon_id == rayon_id)
    elif oblast_id is not None:
        query = query.join(models.Rayon, models.Field.rayon_id == models.Rayon.rayon_id) \
                      .filter(models.Rayon.oblast_id == oblast_id)
    elif province_id is not None:
        query = query.join(models.Rayon, models.Field.rayon_id == models.Rayon.rayon_id) \
                      .join(models.Oblast, models.Rayon.oblast_id == models.Oblast.oblast_id) \
                      .filter(models.Oblast.province_id == province_id)
    else:
        return []

    return [row[0] for row in query.all()]


def _scope_ids(db: Session, *, province_id=None, oblast_id=None, rayon_id=None) -> dict:
    """Идентификаторы объектов верхних уровней, лежащих внутри заданного (включая его самого)."""
    provinces, oblasts, rayons = set(), set(), set()
    if province_id is not None:
        provinces.add(province_id)
        oblasts.update(r[0] for r in db.query(models.Oblast.oblast_id).filter(models.Oblast.province_id == province_id))
    if oblast_id is not None:
        oblasts.add(oblast_id)
    if oblasts:
        rayons.update(r[0] for r in db.query(models.Rayon.rayon_id).filter(models.Rayon.oblast_id.in_(oblasts)))
    if rayon_id is not None:
        rayons.add(rayon_id)
    return {"provinces": provinces, "oblasts": oblasts, "rayons": rayons}


def publication_ids_for_level(db: Session, *, province_id=None, oblast_id=None, rayon_id=None, field_id=None) -> set:
    """Идентификаторы публикаций, связанных с объектом заданного уровня или любым объектом ниже.

    Публикация может быть привязана к месторождению (publication_field), пласту (publication_horizon),
    залежи (publication_deposit) либо к объекту верхнего уровня в целом: району (publication_rayon),
    области (publication_oblast), провинции (publication_province). Вверх по иерархии публикация
    «поднимается» (работа о месторождении относится и к его району), вниз — нет (работа о всей провинции
    не становится работой о конкретном месторождении)."""
    scope = _scope_ids(db, province_id=province_id, oblast_id=oblast_id, rayon_id=rayon_id)
    field_ids = descendant_field_ids(
        db, province_id=province_id, oblast_id=oblast_id, rayon_id=rayon_id, field_id=field_id
    )
    pub_ids = set()
    for model, col, ids in (
        (models.PublicationProvince, models.PublicationProvince.province_id, scope["provinces"]),
        (models.PublicationOblast, models.PublicationOblast.oblast_id, scope["oblasts"]),
        (models.PublicationRayon, models.PublicationRayon.rayon_id, scope["rayons"]),
    ):
        if ids:
            pub_ids.update(row[0] for row in db.query(model.publication_id).filter(col.in_(ids)).all())
    if not field_ids:
        return pub_ids

    horizon_ids = [
        row[0] for row in db.query(models.Horizon.horizon_id)
        .filter(models.Horizon.field_id.in_(field_ids)).all()
    ]
    deposit_ids = []
    if horizon_ids:
        deposit_ids = [
            row[0] for row in db.query(models.Deposit.deposit_id)
            .filter(models.Deposit.horizon_id.in_(horizon_ids)).all()
        ]

    pub_ids.update(
        row[0] for row in db.query(models.PublicationField.publication_id)
        .filter(models.PublicationField.field_id.in_(field_ids)).all()
    )
    if horizon_ids:
        pub_ids.update(
            row[0] for row in db.query(models.PublicationHorizon.publication_id)
            .filter(models.PublicationHorizon.horizon_id.in_(horizon_ids)).all()
        )
    if deposit_ids:
        pub_ids.update(
            row[0] for row in db.query(models.PublicationDeposit.publication_id)
            .filter(models.PublicationDeposit.deposit_id.in_(deposit_ids)).all()
        )
    return pub_ids


def publications_for_level(db: Session, *, province_id=None, oblast_id=None, rayon_id=None, field_id=None):
    """Публикации (объекты) для заданного уровня иерархии — см.
    publication_ids_for_level."""
    pub_ids = publication_ids_for_level(
        db, province_id=province_id, oblast_id=oblast_id, rayon_id=rayon_id, field_id=field_id
    )
    if not pub_ids:
        return []
    return (
        db.query(models.Publication)
        .options(
            selectinload(models.Publication.author_links).selectinload(models.PublicationAuthor.author),
            selectinload(models.Publication.chunks),
            selectinload(models.Publication.research_types),
        )
        .filter(models.Publication.publication_id.in_(pub_ids))
        .all()
    )


def rayon_ancestors(db: Session, rayon: models.Rayon) -> dict:
    oblast = rayon.oblast
    province = oblast.province if oblast else None
    return {"province": province, "oblast": oblast}


def field_ancestors(db: Session, field: models.Field) -> dict:
    rayon = field.rayon
    oblast = rayon.oblast if rayon else None
    province = oblast.province if oblast else None
    return {"province": province, "oblast": oblast, "rayon": rayon}


REGIONAL_LEVELS = {
    "rayon": (models.Rayon, "rayon_id", models.PublicationRayon),
    "oblast": (models.Oblast, "oblast_id", models.PublicationOblast),
    "province": (models.Province, "province_id", models.PublicationProvince),
}


def regional_levels_for_field(db: Session, field: models.Field) -> list:
    """Объекты верхних уровней, к которым относится месторождение: район, область, провинция."""
    anc = field_ancestors(db, field)
    return [(lvl, anc[lvl]) for lvl in ("rayon", "oblast", "province") if anc.get(lvl) is not None]


def regional_publications_query(db: Session, level: str, object_id: int):
    """Запрос публикаций, привязанных именно к этому объекту верхнего уровня (не к объектам под ним).
    Новые сверху; публикации без года — в конце."""
    from sqlalchemy import func

    _model, attr, link = REGIONAL_LEVELS[level]
    ids = db.query(link.publication_id).filter(getattr(link, attr) == object_id)
    return (
        db.query(models.Publication)
        .options(
            selectinload(models.Publication.author_links).selectinload(models.PublicationAuthor.author),
            selectinload(models.Publication.research_types),
        )
        .filter(models.Publication.publication_id.in_(ids))
        .order_by(func.coalesce(models.Publication.year, 0).desc(), models.Publication.publication_id)
    )
