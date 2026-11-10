"""
Основа онтологии (ваш файл) и расширения приложения.

load_base — читает data/oil-gas-ontology.owl (или файл из переменной ONTOLOGY_FILE; Turtle или RDF/XML) и берёт
из него ТОЛЬКО структуру: классы, свойства, подклассы, транзитивность, обратные свойства, области определения
и значений. Экземпляры файла (демонстрационные Иванов 2005 и т. д.) отбрасываются: экземпляры поставляет БД.
Если файла нет, используется запасная копия той же структуры (base_fallback.py).

apply_extensions — добавляет сверху то, чего требует логический вывод и связь с БД. Каждое расширение
помечено в онтологии комментарием «Расширение приложения: …» и перечислено в EXTENSIONS.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from rdflib import BNode, Graph, Literal, OWL, RDF, RDFS, URIRef, XSD
from rdflib.collection import Collection

from . import vocab as V
from .base_fallback import FALLBACK_TTL

DEFAULT_FILE = Path(__file__).resolve().parents[2] / "data" / "oil-gas-ontology.owl"

# Что приложение добавляет к вашей онтологии. Меняйте это перечисление вместе с apply_extensions.
EXTENSIONS: List[str] = [
    "цепочка свойств относитсяКОбъекту ∘ входитВ ⊑ относитсяКОбъекту: публикация про пласт относится и к его месторождению, району, области, провинции",
    "свойство имеетПубликацию — обратное к относитсяКОбъекту (удобно для запросов)",
    "непересекающиеся классы (провинция, область, район, зона, месторождение, пласт, залежь, публикация, исследование): ошибки данных находятся как противоречия",
    "определяемые классы «Публикация района X», «Публикация области Y», «Публикация провинции Z», «Публикация типа T»: публикации классифицируются автоматически",
    "свойства-данные DOI, описание, годОткрытия, координаты (в вашей онтологии их нет, в БД есть)",
    "свойство-данные записьБД: ключ записи БД («таблица:ключ») у каждого экземпляра — явная связь онтологии с БД",
    "классы для типов исследований из БД, которых нет в онтологии (подклассы класса Исследование), если такие типы появятся",
]


@dataclass
class BaseInfo:
    source: str                                  # откуда взята основа
    path: Optional[str]
    classes: Set[str] = field(default_factory=set)
    object_props: Set[str] = field(default_factory=set)
    data_props: Set[str] = field(default_factory=set)
    type_classes: Dict[str, URIRef] = field(default_factory=dict)   # имя типа (в нижнем регистре) -> класс
    extension_classes: List[str] = field(default_factory=list)      # классы, добавленные для типов из БД


def find_base_file() -> Optional[Path]:
    env = os.environ.get("ONTOLOGY_FILE")
    for cand in ([Path(env)] if env else []) + [DEFAULT_FILE]:
        if cand.is_file():
            return cand
    return None


def _parse(text: str) -> Graph:
    g = Graph()
    g.parse(data=text, format="xml" if text.lstrip().startswith("<") else "turtle")
    return g


def _structure_only(src: Graph) -> Graph:
    """Из графа файла оставляет структуру: объявления классов и свойств, шапку онтологии и всё, что к ним
    относится (включая безымянные узлы ограничений). Экземпляры отбрасываются."""
    entities = {s for t in (OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty)
                for s in src.subjects(RDF.type, t) if isinstance(s, URIRef)}
    header = set(src.subjects(RDF.type, OWL.Ontology))
    keep, stack, seen = Graph(), list(entities | header), set()
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        for p, o in src.predicate_objects(n):
            keep.add((n, p, o))
            if isinstance(o, BNode):
                stack.append(o)
    return keep


def load_base(g: Graph) -> BaseInfo:
    path = find_base_file()
    if path is not None:
        base = _structure_only(_parse(path.read_text(encoding="utf-8-sig")))
        source, shown = f"файл онтологии {path.name}", str(path)
    else:
        base = _structure_only(_parse(FALLBACK_TTL))
        source, shown = "встроенная копия структуры (файл data/oil-gas-ontology.owl не найден)", None
    for t in base:
        g.add(t)
    info = BaseInfo(source=source, path=shown)
    ns = V.NS
    for kind, bucket in ((OWL.Class, info.classes), (OWL.ObjectProperty, info.object_props), (OWL.DatatypeProperty, info.data_props)):
        for s in base.subjects(RDF.type, kind):
            if isinstance(s, URIRef) and str(s).startswith(ns):
                bucket.add(str(s))
    for sub in base.subjects(RDFS.subClassOf, V.C_RESEARCH):
        if isinstance(sub, URIRef):
            info.type_classes[V.short(sub).lower()] = sub
    return info


# типы исследований в БД называются иначе, чем классы в онтологии
TYPE_ALIASES = {
    "исследования и данные разработки": "разработка", "исследования разработки": "разработка",
    "данные разработки": "разработка", "разработка месторождений": "разработка",
}


def type_class_for(base: BaseInfo, db_name: str) -> URIRef:
    """Класс онтологии для типа исследования из БД. Если подходящего класса нет, создаётся подкласс
    класса Исследование (это расширение, оно будет перечислено в сводке)."""
    key = (db_name or "").strip().lower()
    key = TYPE_ALIASES.get(key, key)
    if key in base.type_classes:
        return base.type_classes[key]
    for cls_key, cls in base.type_classes.items():
        if cls_key.startswith(key[:8]) and len(key) >= 8:
            return cls
    cls = V.O[V.slug(db_name) or "Тип"]
    base.type_classes[key] = cls
    base.extension_classes.append(db_name)
    return cls


def apply_extensions(g: Graph, lit: Graph, base: BaseInfo, reg) -> List[str]:
    def note(iri, text):
        lit.add((iri, RDFS.comment, Literal("Расширение приложения: " + text, lang="ru")))

    def declare(iri, kind):
        g.add((iri, RDF.type, kind))

    # 1. обратное свойство и цепочка свойств
    declare(V.P_HAS_PUB, OWL.ObjectProperty)
    g.add((V.P_HAS_PUB, OWL.inverseOf, V.P_ABOUT))
    lit.add((V.P_HAS_PUB, RDFS.label, Literal("имеет публикацию", lang="ru")))
    note(V.P_HAS_PUB, "обратное к относитсяКОбъекту.")
    chain = BNode()
    Collection(g, chain, [V.P_ABOUT, V.P_IN])
    g.add((V.P_ABOUT, OWL.propertyChainAxiom, chain))
    note(V.P_ABOUT, "цепочка свойств относитсяКОбъекту ∘ входитВ ⊑ относитсяКОбъекту (публикация про пласт относится и к его "
                    "месторождению, району, области, провинции).")

    # 2. непересекающиеся классы
    disjoint = [V.C_PROVINCE, V.C_OBLAST, V.C_RAYON, V.C_ZONE, V.C_FIELD, V.C_HORIZON, V.C_DEPOSIT, V.C_PUB, V.C_RESEARCH]
    adc, members = BNode(), BNode()
    g.add((adc, RDF.type, OWL.AllDisjointClasses))
    Collection(g, members, disjoint)
    g.add((adc, OWL.members, members))

    # 3. свойства-данные, которых нет в онтологии, но есть в БД
    for iri, label, dom, rng in [
        (V.D_DOI, "DOI", V.C_PUB, XSD.string), (V.D_DESC, "описание", V.C_OBJECT, XSD.string),
        (V.D_DISCOVERY, "год открытия", V.C_FIELD, XSD.integer), (V.D_COORD, "координаты", V.C_FIELD, XSD.string),
        (V.D_DBREF, "запись БД", None, XSD.string),
    ]:
        declare(iri, OWL.DatatypeProperty)
        lit.add((iri, RDFS.label, Literal(label, lang="ru")))
        if dom is not None:
            g.add((iri, RDFS.domain, dom))
        g.add((iri, RDFS.range, rng))
        note(iri, "свойство-данные отсутствует в исходной онтологии, значения берутся из БД." if iri != V.D_DBREF else
             "ключ записи БД («таблица:первичный ключ») у каждого экземпляра.")

    # 4. классы для типов исследований из БД, которых нет в онтологии
    for name in base.extension_classes:
        cls = V.O[V.slug(name) or "Тип"]
        declare(cls, OWL.Class)
        g.add((cls, RDFS.subClassOf, V.C_RESEARCH))
        lit.add((cls, RDFS.label, Literal(name, lang="ru")))
        note(cls, f"класс для типа исследования «{name}» из БД, которого нет в исходной онтологии.")

    # 5. определяемые классы: Публикация И (свойство значение X)
    def defined(cls_iri, label, comment, prop, value):
        restriction, items, expr = BNode(), BNode(), BNode()
        g.add((restriction, RDF.type, OWL.Restriction))
        g.add((restriction, OWL.onProperty, prop))
        g.add((restriction, OWL.hasValue, value))
        Collection(g, items, [V.C_PUB, restriction])
        g.add((expr, RDF.type, OWL.Class))
        g.add((expr, OWL.intersectionOf, items))
        g.add((cls_iri, RDF.type, OWL.Class))
        g.add((cls_iri, OWL.equivalentClass, expr))
        lit.add((cls_iri, RDFS.label, Literal(label, lang="ru")))
        note(cls_iri, comment)

    used: Set[URIRef] = set()
    for info in reg.types.values():
        iri = V.O["Публикация_типа_" + V.slug(info["name"])]
        if iri in used:
            continue
        used.add(iri)
        defined(iri, f"Публикации типа «{info['name']}»",
                "определяемый класс: публикация этого типа исследования; членство выводится автоматически.",
                V.P_RESEARCH, info["iri"])
    for obj in reg.objects.values():
        if obj["level"] not in V.DEFINED_PREFIX:
            continue
        iri = V.O[V.DEFINED_PREFIX[obj["level"]] + V.slug(obj["name"])]
        if iri in used:
            continue
        used.add(iri)
        defined(iri, V.DEFINED_LABEL[obj["level"]].format(obj["name"]),
                "определяемый класс: публикация, относящаяся к объекту, в том числе через вложенные объекты "
                "(следует из цепочки свойств).", V.P_ABOUT, obj["iri"])
    return list(EXTENSIONS)
