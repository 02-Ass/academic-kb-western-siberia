"""
Запросы к онтологии с логическим выводом: публикации по объекту и типу с объяснением, SPARQL,
проверка согласованности, сверка с реляционной базой, экспорт.
"""
import itertools
import re
from typing import Dict, List, Optional, Set, Tuple

from rdflib import Graph, Literal, OWL, RDF, RDFS, URIRef, XSD
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.algebra import translateQuery
from sqlalchemy.orm import Session

from .. import hierarchy, models
from . import vocab as V
from .reasoner import OntologyState, run_closure

_PREFIXES = {"": V.NS, "rdfs": str(RDFS), "rdf": str(RDF), "owl": str(OWL), "xsd": str(XSD)}


# ---------------------------------------------------------------------------
# Объекты и публикации
# ---------------------------------------------------------------------------

def list_objects(state: OntologyState) -> List[dict]:
    order = {lvl: i for i, lvl in enumerate(V.LEVELS)}
    items = [
        {"kind": o["level"], "id": o["id"], "name": o["name"], "level": V.LEVEL_NOM[o["level"]]}
        for o in state.reg.objects.values()
    ]
    return sorted(items, key=lambda x: (order[x["kind"]], x["name"]))


def _name(state: OntologyState, iri) -> Tuple[str, str]:
    kind, _id = state.reg.by_iri[str(iri)]
    return kind, state.reg.objects[(kind, _id)]["name"]


def explain(state: OntologyState, pub_iri: URIRef, target: URIRef) -> List[dict]:
    """Цепочка рассуждения: что записано в базе и что из этого выведено."""
    t_kind, t_name = _name(state, target)
    start = state.about_explicit.get(str(pub_iri), [])
    if target in start:
        return [{"kind": "asserted",
                 "text": f"Публикация напрямую привязана к {V.LEVEL_DAT[t_kind]} «{t_name}» (записано в базе)"}]
    # поиск в ширину вверх по родителям из утверждённых связей
    best: Optional[List[Tuple[URIRef, str]]] = None
    for s0 in start:
        queue = [(s0, [(s0, "")])]
        seen = {str(s0)}
        while queue:
            node, path = queue.pop(0)
            if node == target:
                if best is None or len(path) < len(best):
                    best = path
                break
            for parent, rel in state.parents.get(str(node), []):
                if str(parent) not in seen:
                    seen.add(str(parent))
                    queue.append((parent, path + [(parent, rel)]))
    if not best:
        return []
    steps = []
    k0, n0 = _name(state, best[0][0])
    steps.append({"kind": "asserted", "text": f"Публикация привязана к {V.LEVEL_DAT[k0]} «{n0}» (записано в базе)"})
    for (a, _), (b, rel) in zip(best, best[1:]):
        ka, na = _name(state, a)
        kb, nb = _name(state, b)
        steps.append({"kind": "asserted",
                      "text": f"{V.LEVEL_NOM[ka].capitalize()} «{na}» входит в {V.LEVEL_ACC[kb]} «{nb}» (записано в базе)"})
    steps.append({"kind": "inferred",
                  "text": f"Вывод: публикация относится к {V.LEVEL_DAT[t_kind]} «{t_name}» "
                          f"(цепочка свойств относитсяКОбъекту ∘ входитВ)"})
    return steps


def _pub_ids(state: OntologyState, iris) -> Set[int]:
    return {state.reg.pub_by_iri[str(i)] for i in iris if str(i) in state.reg.pub_by_iri}


def publications_for(state: OntologyState, kind: str, obj_id: int, type_id: Optional[int] = None,
                     use_inference: bool = True, limit: int = 300) -> dict:
    obj = state.reg.objects.get((kind, obj_id))
    if obj is None:
        raise KeyError(f"объект {kind}:{obj_id} не найден")
    iri = obj["iri"]
    direct = _pub_ids(state, state.direct_by_obj.get(str(iri), set()))
    with_inference = _pub_ids(state, state.full.subjects(V.P_ABOUT, iri))
    type_info = None
    if type_id is not None:
        type_info = state.reg.types.get(type_id)
        if type_info is None:
            raise KeyError(f"тип исследования {type_id} не найден")
        typed = _pub_ids(state, state.full.subjects(V.P_RESEARCH, type_info["iri"]))
        direct &= typed
        with_inference &= typed
    shown = with_inference if use_inference else direct
    rows = []
    for pid in sorted(shown, key=lambda p: (-(state.reg.pub_info[p]["year"] or 0), p))[:limit]:
        info = state.reg.pub_info[pid]
        how = "direct" if pid in direct else "inferred"
        rows.append({
            "publication_id": pid, "title": info["title"], "year": info["year"], "doi": info["doi"], "link": info["link"],
            "how": how,
            "explanation": explain(state, state.reg.publications[pid], iri),
        })
    return {
        "object": {"kind": kind, "id": obj_id, "name": obj["name"], "level": V.LEVEL_NOM[kind]},
        "type": {"id": type_id, "name": type_info["name"]} if type_info else None,
        "direct": len(direct), "with_inference": len(with_inference), "gain": len(with_inference) - len(direct),
        "publications": rows,
    }


# ---------------------------------------------------------------------------
# SPARQL
# ---------------------------------------------------------------------------

def _fmt(term) -> str:
    if isinstance(term, URIRef):
        return ":" + V.short(term) if str(term).startswith(V.NS) else str(term)
    return str(term)[:300]


def _single_prefix(text: str) -> str:
    """Приводит запрос к одному префиксу вашей онтологии. В файле онтологии и в запросах лабораторной работы
    используется «ontology:», в примерах приложения — пустой «:». Библиотека rdflib не умеет держать два
    префикса на один адрес (запоминает только один), поэтому «ontology:Имя» переписывается в «:Имя», а объявление
    PREFIX ontology: убирается. Чужой префикс с таким именем (другой адрес) не трогается."""
    decl = re.compile(r"(?im)^[ \t]*PREFIX[ \t]+ontology[ \t]*:[ \t]*<([^>]*)>[ \t]*$")
    m = decl.search(text)
    if m and m.group(1) != V.NS:
        return text
    text = decl.sub("", text)
    return re.sub(r"(?<![\w<:/#.-])ontology:", ":", text)


def sparql(state: OntologyState, query: str, inferred: bool = True, max_rows: int = 500) -> dict:
    text = (query or "").strip()
    if not text:
        raise ValueError("Запрос пустой")
    if re.search(r"(?i)\bSERVICE\b", text):
        raise ValueError("Обращения к внешним источникам (SERVICE) запрещены")
    text = _single_prefix(text)
    # Префиксы (: rdfs: rdf: owl: xsd:) подставляются сами, после тех, что написал пользователь
    lines = text.splitlines()
    n_head = 0
    while n_head < len(lines) and re.match(r"(?i)^\s*(PREFIX|BASE)\b", lines[n_head]):
        n_head += 1
    declared = set(re.findall(r"(?im)^\s*PREFIX\s+([\w-]*)\s*:", "\n".join(lines[:n_head])))
    add = [f"PREFIX {p}: <{u}>" for p, u in _PREFIXES.items() if p not in declared]
    full_query = "\n".join(lines[:n_head] + add + lines[n_head:])
    try:
        kind = translateQuery(parseQuery(full_query)).algebra.name
    except Exception as e:
        raise ValueError(f"Ошибка в запросе: {str(e)[:200]}")
    if kind not in ("SelectQuery", "AskQuery"):
        raise ValueError("Разрешены только запросы SELECT и ASK")
    graph = state.full if inferred else state.explicit_full
    res = graph.query(full_query)
    if kind == "AskQuery":
        return {"ask": bool(res.askAnswer), "vars": [], "rows": [], "truncated": False}
    cols = [str(v) for v in res.vars]
    rows = []
    truncated = False
    for i, row in enumerate(res):
        if i >= max_rows:
            truncated = True
            break
        rows.append({c: (_fmt(row[c]) if row[c] is not None else "") for c in cols})
    return {"ask": None, "vars": cols, "rows": rows, "truncated": truncated}


def _first(state: OntologyState, level: str) -> Optional[dict]:
    items = [o for o in state.reg.objects.values() if o["level"] == level]
    return items[0] if items else None


def sparql_examples(state: OntologyState) -> List[dict]:
    rayon, field = _first(state, "rayon"), _first(state, "field")
    types = list(state.reg.types.values())
    ex = []
    if rayon:
        ex.append({"title": "Публикации по району (с выводом)", "query":
                   "SELECT ?pub ?title WHERE {\n  ?pub a :Публикация ;\n       :относитсяКОбъекту ?obj ;\n       rdfs:label ?title .\n"
                   f"  ?obj rdfs:label \"{rayon['name']}\" .\n}}"})
        ex.append({"title": "Что входит в район (транзитивно)", "query":
                   "SELECT ?label WHERE {\n  ?obj :входитВ ?r ;\n       rdfs:label ?label .\n"
                   f"  ?r rdfs:label \"{rayon['name']}\" .\n}}"})
        if types:
            ex.append({"title": "Публикации по району и типу исследования", "query":
                       "SELECT ?title WHERE {\n  ?pub :относитсяКОбъекту ?obj ;\n       :относитсяКТипуИсследования ?t ;\n       rdfs:label ?title .\n"
                       f"  ?obj rdfs:label \"{rayon['name']}\" .\n  ?t rdfs:label \"{types[0]['name']}\" .\n}}"})
        cls = V.DEFINED_PREFIX["rayon"] + V.slug(rayon["name"])
        ex.append({"title": "Публикации, автоматически отнесённые к определяемому классу",
                   "query": f"SELECT ?title WHERE {{\n  ?pub a :{cls} ;\n       rdfs:label ?title .\n}}"})
    if field:
        ex.append({"title": "Публикации о месторождении без вывода (только записанное в базе)", "query":
                   "SELECT ?title WHERE {\n  ?pub :относитсяКОбъекту ?obj ;\n       rdfs:label ?title .\n"
                   f"  ?obj rdfs:label \"{field['name']}\" .\n}}"})
    return ex


# ---------------------------------------------------------------------------
# Согласованность
# ---------------------------------------------------------------------------

def _ru_error(msg: str) -> str:
    m = re.match(r"Disjoint classes (\S+) and (\S+) have a common individual (\S+)", msg)
    if m:
        a, b, x = (V.short(g) for g in m.groups())
        return f"Классы «{a}» и «{b}» не пересекаются, но у них есть общий объект «{x}»"
    return msg.replace(V.NS, ":")


def check(state: OntologyState, demo: bool = False) -> dict:
    if not demo:
        return {"consistent": not state.errors, "errors": [_ru_error(e) for e in state.errors], "demo": None}
    field = _first(state, "field")
    if field is None:
        return {"consistent": True, "errors": [], "demo": "Для примера нужно хотя бы одно месторождение."}
    g = Graph()
    for t in state.structure:
        g.add(t)
    g.add((field["iri"], RDF.type, V.C_RAYON))   # намеренная ошибка данных
    errors, _ = run_closure(g)
    return {
        "consistent": not errors, "errors": [_ru_error(e) for e in errors],
        "demo": f"Для проверки в копию онтологии добавлена ошибка: объект «{field['name']}» объявлен ещё и районом. "
                f"Рабочая онтология не изменена.",
    }


# ---------------------------------------------------------------------------
# Сверка с реляционной базой
# ---------------------------------------------------------------------------

def _sql_ids(db: Session, kind: str, obj_id: int) -> Set[int]:
    if kind in ("province", "oblast", "rayon", "field"):
        return set(hierarchy.publication_ids_for_level(db, **{f"{kind}_id": obj_id}))
    if kind == "horizon":
        ids = {r[0] for r in db.query(models.PublicationHorizon.publication_id).filter(models.PublicationHorizon.horizon_id == obj_id)}
        deposits = [r[0] for r in db.query(models.Deposit.deposit_id).filter(models.Deposit.horizon_id == obj_id)]
        if deposits:
            ids |= {r[0] for r in db.query(models.PublicationDeposit.publication_id).filter(models.PublicationDeposit.deposit_id.in_(deposits))}
        return ids
    return {r[0] for r in db.query(models.PublicationDeposit.publication_id).filter(models.PublicationDeposit.deposit_id == obj_id)}


def validate_against_sql(state: OntologyState, db: Session) -> dict:
    """Для каждого объекта сравнивает публикации, найденные выводом, с рекурсивным реляционным запросом."""
    per_level: Dict[str, dict] = {k: {"level": V.LEVEL_NOM[k], "objects": 0, "matching": 0, "mismatches": []} for k in V.LEVELS}
    for (kind, obj_id), obj in state.reg.objects.items():
        onto_ids = _pub_ids(state, state.full.subjects(V.P_ABOUT, obj["iri"]))
        sql_ids = _sql_ids(db, kind, obj_id)
        row = per_level[kind]
        row["objects"] += 1
        if onto_ids == sql_ids:
            row["matching"] += 1
        else:
            row["mismatches"].append(obj["name"])
    rows = [v for k, v in per_level.items() if v["objects"]]
    return {"rows": rows, "all_match": all(r["objects"] == r["matching"] for r in rows)}


# ---------------------------------------------------------------------------
# Сводка и экспорт
# ---------------------------------------------------------------------------

def _title(state: OntologyState, iri) -> str:
    return state.reg.pub_info[state.reg.pub_by_iri[str(iri)]]["title"]


def _samples(state: OntologyState, limit_each: int = 3) -> List[str]:
    deep, about, typed = [], [], []
    for s, p, o in sorted(state.inferred, key=lambda t: (str(t[0]), str(t[1]), str(t[2]))):
        if p == V.P_IN and str(o) in state.reg.by_iri:
            ks, ns = _name(state, s); ko, no = _name(state, o)
            if V.LEVELS.index(ko) <= V.LEVELS.index(ks) - 2:
                deep.append(f"«{ns}» ({V.LEVEL_NOM[ks]}) входит в {V.LEVEL_ACC[ko]} «{no}»: выведено по транзитивности")
        elif p == V.P_ABOUT and str(s) in state.reg.pub_by_iri:
            ko, no = _name(state, o)
            about.append(f"Публикация «{_title(state, s)[:70]}» относится к {V.LEVEL_DAT[ko]} «{no}»: выведено по цепочке свойств")
        elif p == RDF.type and str(s) in state.reg.pub_by_iri and V.short(o).startswith("Публикация_"):
            typed.append(f"Публикация «{_title(state, s)[:70]}» автоматически отнесена к классу «{V.short(o)}»")
    # самые «дальние» выводы первыми — они нагляднее
    return deep[-limit_each:] + about[:limit_each] + typed[:limit_each]


def summary(state: OntologyState) -> dict:
    explicit_graph = state.explicit
    named = lambda t: sum(1 for s in state.explicit if s[1] == RDF.type and s[2] == t and str(s[0]).startswith(V.NS))
    classes = named(OWL.Class)
    defined = sum(1 for s in state.explicit if s[1] == OWL.equivalentClass)
    obj_props = named(OWL.ObjectProperty)
    data_props = named(OWL.DatatypeProperty)
    by_pred: Dict[str, int] = {}
    for s, p, o in state.inferred:
        key = "членство в классах" if p == RDF.type else V.short(p)
        by_pred[key] = by_pred.get(key, 0) + 1
    counts = {V.LEVEL_NOM[k]: sum(1 for o in state.reg.objects.values() if o["level"] == k) for k in V.LEVELS}
    counts.update({"публикации": len(state.reg.publications), "типы исследований": len(state.reg.types)})
    return {
        "built_at": state.built_at.strftime("%Y-%m-%d %H:%M:%S"),
        "build_seconds": round(state.build_seconds, 2), "closure_seconds": round(state.closure_seconds, 2),
        "classes": classes, "defined_classes": defined, "object_properties": obj_props, "data_properties": data_props,
        "individuals": counts, "individuals_total": len(state.reg.individuals),
        "triples_asserted": len(state.explicit), "abox_triples": state.abox_triples,
        "inferred_meaningful": len(state.inferred), "inferred_by_kind": by_pred,
        "consistent": not state.errors, "errors": [_ru_error(e) for e in state.errors],
        "samples": _samples(state), "sparql_examples": sparql_examples(state),
        "reasoner": "OWL 2 RL (owlrl); результаты сверяются с HermiT: scripts/ontology_check.py --hermit",
        "base": {"source": state.base.source, "path": state.base.path, "classes": len(state.base.classes),
                 "object_properties": len(state.base.object_props), "data_properties": len(state.base.data_props)},
        "extensions": state.extensions,
        "extension_classes": state.base.extension_classes,
    }


def export(state: OntologyState, fmt: str = "owl", inferred: bool = False) -> Tuple[bytes, str, str]:
    """(содержимое, тип, имя файла). owl — RDF/XML для Protégé, ttl — Turtle."""
    g = Graph()
    for t in state.explicit_full:
        g.add(t)
    if inferred:
        for t in state.inferred:
            g.add(t)
    g.bind("", V.O)
    g.bind("ontology", V.O)
    g.bind("owl", OWL); g.bind("rdfs", RDFS); g.bind("xsd", XSD)
    suffix = "_inferred" if inferred else ""
    if fmt == "ttl":
        return g.serialize(format="turtle").encode("utf-8"), "text/turtle; charset=utf-8", f"oilgas_ontology{suffix}.ttl"
    return g.serialize(format="xml").encode("utf-8"), "application/rdf+xml; charset=utf-8", f"oilgas_ontology{suffix}.owl"


def chat_hits(state: OntologyState, kind: str, obj_id: int, limit: int = 6) -> Optional[dict]:
    """Короткий результат для чата: сколько публикаций нашлось с выводом и без него и первые несколько с объяснением."""
    try:
        res = publications_for(state, kind, obj_id, None, True, limit)
    except KeyError:
        return None
    for row in res["publications"]:
        row["explanation_text"] = " → ".join(s["text"] for s in row["explanation"])
    return res
