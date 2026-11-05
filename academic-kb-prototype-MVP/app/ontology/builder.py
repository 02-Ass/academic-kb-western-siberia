"""
Экземпляры онтологии (ABox) из реляционной базы — по таблице соответствия (см. mapping.py).

Структуру (классы и свойства) задаёт наша онтология (tbox.load_base), БД поставляет экземпляры:
- строка таблицы иерархии → экземпляр класса (province → Провинция, … deposit → Залежь);
- внешний ключ «ребёнок → родитель» → входитВ (как в вашем файле), а со стороны родителя там, где в онтологии
  есть такое свойство, → имеетМесторождение / имеетПласт / имеетЗалежь;
- publication → Публикация (название, год, журнал, ссылка из нашей онтологии; DOI — расширение);
- ФИО авторов → значения свойства-данных автор (как в нашей онтологии, без отдельных объектов-авторов);
- research_type → экземпляр класса-типа исследования (Геологическое → Геологическое_исследование);
- publication_field / publication_horizon / publication_deposit и publication_rayon / publication_oblast / publication_province → относитсяКОбъекту;
- publication_research_type → относитсяКТипуИсследования.
У каждого экземпляра есть свойство записьБД («таблица:ключ»): прямая связь онтологии с записью БД.

В граф для вывода (g) попадают только связи и типы, а подписи, названия, годы, DOI — в граф литералов (lit):
они не участвуют в выводе, и вывод идёт быстрее.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple

from rdflib import Graph, Literal, RDF, RDFS, OWL, URIRef, XSD
from sqlalchemy.orm import Session

from .. import models
from . import vocab as V
from .tbox import BaseInfo, type_class_for


@dataclass
class Registry:
    objects: Dict[Tuple[str, int], dict] = field(default_factory=dict)   # (уровень, id) -> {iri, name, level, id}
    by_iri: Dict[str, Tuple[str, int]] = field(default_factory=dict)
    publications: Dict[int, URIRef] = field(default_factory=dict)
    pub_by_iri: Dict[str, int] = field(default_factory=dict)
    types: Dict[int, dict] = field(default_factory=dict)                  # type_id -> {iri, name, class_iri}
    type_by_iri: Dict[str, int] = field(default_factory=dict)
    individuals: Set[str] = field(default_factory=set)
    pub_info: Dict[int, dict] = field(default_factory=dict)


class _Namer:
    def __init__(self):
        self.used: Set[str] = set()

    def make(self, prefix: str, label: str, uid: int, extra: str = "") -> URIRef:
        base = V.slug(f"{label}_{extra}" if extra else label) or str(uid)
        name = f"{prefix}_{base}" if prefix else base
        if name in self.used:
            name = f"{name}_{uid}"
        self.used.add(name)
        return V.O[name]


def build_abox(g: Graph, lit: Graph, db: Session, base: BaseInfo) -> Registry:
    reg = Registry()
    namer = _Namer()

    def individual(iri: URIRef, cls: URIRef, label: str, table: str, key: int) -> None:
        g.add((iri, RDF.type, cls))
        g.add((iri, RDF.type, OWL.NamedIndividual))
        lit.add((iri, RDFS.label, Literal(label)))
        lit.add((iri, V.D_DBREF, Literal(f"{table}:{key}")))
        reg.individuals.add(str(iri))

    def register(kind: str, obj_id: int, iri: URIRef, label: str, table: str, description: Optional[str] = None) -> None:
        reg.objects[(kind, obj_id)] = {"iri": iri, "name": label, "level": kind, "id": obj_id}
        reg.by_iri[str(iri)] = (kind, obj_id)
        individual(iri, V.LEVEL_CLASS[kind], label, table, obj_id)
        if description:
            lit.add((iri, V.D_DESC, Literal(description)))

    def parent_of(kind: str, obj_id: int) -> URIRef:
        return reg.objects[(kind, obj_id)]["iri"]

    provinces = db.query(models.Province).order_by(models.Province.province_id).all()
    oblasts = db.query(models.Oblast).order_by(models.Oblast.oblast_id).all()
    rayons = db.query(models.Rayon).order_by(models.Rayon.rayon_id).all()
    fields = db.query(models.Field).order_by(models.Field.field_id).all()
    horizons = db.query(models.Horizon).order_by(models.Horizon.horizon_id).all()
    deposits = db.query(models.Deposit).order_by(models.Deposit.deposit_id).all()

    for p in provinces:
        register("province", p.province_id, namer.make("Провинция", p.name, p.province_id), p.name, "province", p.description)
    for o in oblasts:
        register("oblast", o.oblast_id, namer.make("Область", o.name, o.oblast_id), o.name, "oblast", o.description)
        g.add((parent_of("oblast", o.oblast_id), V.P_IN, parent_of("province", o.province_id)))
    for r in rayons:
        register("rayon", r.rayon_id, namer.make("Район", r.name, r.rayon_id), r.name, "rayon", r.description)
        g.add((parent_of("rayon", r.rayon_id), V.P_IN, parent_of("oblast", r.oblast_id)))
    for f in fields:
        register("field", f.field_id, namer.make("Месторождение", f.name, f.field_id), f.name, "field", f.description)
        iri = parent_of("field", f.field_id)
        g.add((iri, V.P_IN, parent_of("rayon", f.rayon_id)))
        g.add((parent_of("rayon", f.rayon_id), V.P_HAS_FIELD, iri))
        if f.discovery_date:
            lit.add((iri, V.D_DISCOVERY, Literal(f.discovery_date.year, datatype=XSD.integer)))
        if f.coordinates:
            lit.add((iri, V.D_COORD, Literal(f.coordinates)))
    field_name = {f.field_id: f.name for f in fields}
    for h in horizons:
        label = h.index_code + (f" ({h.formation})" if h.formation else "")
        register("horizon", h.horizon_id, namer.make("Пласт", h.index_code, h.horizon_id, field_name.get(h.field_id, "")),
                 f"{label}, {field_name.get(h.field_id, '')}".strip(", "), "horizon")
        iri = parent_of("horizon", h.horizon_id)
        g.add((iri, V.P_IN, parent_of("field", h.field_id)))
        g.add((parent_of("field", h.field_id), V.P_HAS_HORIZON, iri))
    for d in deposits:
        register("deposit", d.deposit_id, namer.make("Залежь", d.name, d.deposit_id), d.name, "deposit")
        iri = parent_of("deposit", d.deposit_id)
        g.add((iri, V.P_IN, parent_of("horizon", d.horizon_id)))
        g.add((parent_of("horizon", d.horizon_id), V.P_HAS_DEPOSIT, iri))

    # типы исследований: строка research_type -> экземпляр класса-типа из онтологии (Геологическое -> Геологическое_исследование)
    for t in db.query(models.ResearchType).order_by(models.ResearchType.type_id).all():
        cls = type_class_for(base, t.name)
        iri = namer.make("", V.short(cls) + "_исследование", t.type_id)
        individual(iri, cls, t.name, "research_type", t.type_id)
        reg.types[t.type_id] = {"iri": iri, "name": t.name, "class_iri": cls}
        reg.type_by_iri[str(iri)] = t.type_id

    authors: Dict[int, list] = {}
    for pid, last, first, middle in (
        db.query(models.PublicationAuthor.publication_id, models.Author.last_name, models.Author.first_name, models.Author.middle_name)
        .join(models.Author, models.Author.author_id == models.PublicationAuthor.author_id)
        .order_by(models.PublicationAuthor.publication_id, models.PublicationAuthor.author_order).all()
    ):
        authors.setdefault(pid, []).append(" ".join(x for x in (last, first, middle) if x))

    for p in db.query(models.Publication).order_by(models.Publication.publication_id).all():
        iri = V.O[f"Публикация_{p.publication_id}"]
        reg.publications[p.publication_id] = iri
        reg.pub_by_iri[str(iri)] = p.publication_id
        individual(iri, V.C_PUB, p.title, "publication", p.publication_id)
        reg.pub_info[p.publication_id] = {"title": p.title, "year": p.year, "doi": p.doi, "link": p.link}
        lit.add((iri, V.D_TITLE, Literal(p.title)))
        if p.year is not None:
            lit.add((iri, V.D_YEAR, Literal(p.year, datatype=XSD.integer)))
        for prop, value in ((V.D_DOI, p.doi), (V.D_JOURNAL, p.journal), (V.D_LINK, p.link)):
            if value:
                lit.add((iri, prop, Literal(value)))
        for name in authors.get(p.publication_id, []):
            lit.add((iri, V.D_AUTHOR, Literal(name)))

    def link(model, key_attr: str, kind: str) -> None:
        for row in db.query(model).all():
            target = reg.objects.get((kind, getattr(row, key_attr)))
            src = reg.publications.get(row.publication_id)
            if target and src:
                g.add((src, V.P_ABOUT, target["iri"]))

    link(models.PublicationProvince, "province_id", "province")
    link(models.PublicationOblast, "oblast_id", "oblast")
    link(models.PublicationRayon, "rayon_id", "rayon")
    link(models.PublicationField, "field_id", "field")
    link(models.PublicationHorizon, "horizon_id", "horizon")
    link(models.PublicationDeposit, "deposit_id", "deposit")

    for row in db.query(models.PublicationResearchType).all():
        t, src = reg.types.get(row.type_id), reg.publications.get(row.publication_id)
        if t and src:
            g.add((src, V.P_RESEARCH, t["iri"]))
    return reg
