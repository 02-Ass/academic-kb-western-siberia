"""
Сравнение онтологий по критериям статьи Lippolis et al. (arXiv 2503.05388).

    python -m scripts.compare_ontologies                      # три онтологии из ontology_llm/mappings
    python -m scripts.compare_ontologies --extra имя=файл.ttl  # добавить онтологию, сгенерированную другой LLM
                                                             # (для неё нужен файл соответствия слотов, см. ниже)
    python -m scripts.compare_ontologies --no-hermit          # без HermiT (не нужна Java)

Что считается (по статье):
  1. Доля смоделированных компетентностных вопросов (CQ): CQ считается смоделированным, если каждый нужный ему
     «слот» (класс/свойство предметной области) присутствует в онтологии, подходит по области определения, а
     вопросы, требующие вывода (транзитивность «входит в», наследование связи публикации вверх по иерархии),
     выполняются на тестовом наборе данных после вывода OWL 2 RL.
  2. Лишние элементы: классы и свойства, которые не нужны ни одному CQ (доля от всех элементов своего вида),
     и дубликаты (несколько элементов на один слот).
  3. Ловушки в стиле OOPS!: P04, P05, P06, P08, P10, P11, P13, P19, P29, P34, P35, P39.
  4. Согласованность: owlrl (OWL 2 RL) и HermiT; число «загрязняющих» типов, выведенных из неверных областей
     определения/значений.
Файл соответствия (ontology_llm/mappings/<имя>.json) — это решение «эксперта»: какой элемент онтологии закрывает какой слот.
Скрипт проверяет, что названные элементы существуют и подходят по области определения.
"""
import argparse
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import owlrl
from rdflib import BNode, Graph, Literal, OWL, RDF, RDFS, URIRef
from rdflib.collection import Collection

CRITICAL = {"P05", "P06", "P19", "P29", "P39"}
OWNER = {"pub": "cls.publication", "field": "cls.field", "horizon": "cls.horizon", "deposit": "cls.deposit",
         "author": "cls.author", "rel.pubAbout": "cls.publication", "rel.pubType": "cls.publication",
         "rel.authored": "cls.publication"}


# ------------------------------------------------------------------ загрузка
def load(cfg: dict) -> Graph:
    g = Graph()
    if cfg["kind"] == "file":
        path = ROOT / cfg["file"]
        g.parse(str(path), format="xml" if path.read_text(encoding="utf-8-sig").lstrip().startswith("<") else "turtle")
        # только схема: отбрасываем экземпляры
        for s in list(g.subjects(RDF.type, OWL.NamedIndividual)):
            for t in list(g.triples((s, None, None))):
                g.remove(t)
    else:                                   # v22: файл пользователя + расширения приложения (уровень схемы)
        from app.ontology import tbox

        class Reg:
            types, objects = {}, {}
        base = tbox.load_base(g)
        lit = Graph()
        tbox.apply_extensions(g, lit, base, Reg())
        g += lit
    return g


def ents(g: Graph, ns: str):
    def of(t):
        return sorted({s for s in g.subjects(RDF.type, t) if isinstance(s, URIRef) and str(s).startswith(ns)}, key=str)
    return of(OWL.Class), of(OWL.ObjectProperty), of(OWL.DatatypeProperty)


def loc(iri, ns):
    return str(iri)[len(ns):]


def ancestors(g, c):
    seen, stack = set(), [c]
    while stack:
        x = stack.pop()
        for p in g.objects(x, RDFS.subClassOf):
            if isinstance(p, URIRef) and p not in seen:
                seen.add(p)
                stack.append(p)
    return seen


# ------------------------------------------------------------------ слоты и CQ
def resolve(val):
    """-> (режим, [имена])"""
    if isinstance(val, dict):
        return "all", val["all"]
    if isinstance(val, list):
        return "any", val
    return "any", [val]


def slot_check(g, ns, slots, slot, behaviour):
    """-> (ok, замечание)"""
    if slot in ("rel.partOf.transitive", "rel.pubAbout.inherit"):
        key = "transitive" if slot.endswith("transitive") else "inherit"
        if slot not in slots:
            return False, "нет свойства"
        return behaviour.get(key, False), "проверено выводом на тестовых данных"
    if slot not in slots:
        return False, "нет в онтологии"
    mode, names = resolve(slots[slot])
    iris = [URIRef(ns + n) for n in names]
    exists = [(i, any((i, RDF.type, t) in g for t in (OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty, OWL.NamedIndividual)))
              for i in iris]
    if not all(e for _, e in exists) and mode == "all":
        return False, "не найден элемент"
    ok_iris = [i for i, e in exists if e]
    if not ok_iris:
        return False, "не найден элемент"
    owner_slot = None
    if slot.startswith("rel."):
        owner_slot = OWNER.get(slot)
    elif "." in slot and slot.split(".")[0] in OWNER:
        owner_slot = OWNER[slot.split(".")[0]]
    if owner_slot and owner_slot in slots:
        owner = URIRef(ns + resolve(slots[owner_slot])[1][0])
        good = False
        for i in ok_iris:
            doms = [d for d in g.objects(i, RDFS.domain) if isinstance(d, URIRef)]
            if not doms or any(d == owner or d in ancestors(g, owner) for d in doms):
                good = True
        if not good:
            return False, "область определения не подходит"
    return True, ""


# ------------------------------------------------------------------ поведение (вывод)
def behaviour_test(g: Graph, ns: str, slots: dict, use_hermit: bool):
    res = {"transitive": False, "inherit": False, "pollution": [], "owlrl_consistent": None, "hermit": None}
    need = ["cls.province", "cls.oblast", "cls.rayon", "cls.field", "cls.horizon", "cls.deposit", "cls.publication", "rel.partOf", "rel.pubAbout"]
    if any(n not in slots for n in need):
        return res
    first = lambda s: URIRef(ns + resolve(slots[s])[1][0])
    chain = ["cls.province", "cls.oblast", "cls.rayon", "cls.field", "cls.horizon", "cls.deposit"]
    ind = {c: URIRef(ns + "test_" + c.split(".")[1]) for c in chain}
    pub = URIRef(ns + "test_pub")
    part, about = first("rel.partOf"), first("rel.pubAbout")
    w = Graph()
    for t in g:
        w.add(t)
    for c in chain:
        w.add((ind[c], RDF.type, OWL.NamedIndividual))
        w.add((ind[c], RDF.type, first(c)))
    w.add((pub, RDF.type, OWL.NamedIndividual))
    w.add((pub, RDF.type, first("cls.publication")))
    for lo, hi in zip(chain[1:], chain[:-1]):
        w.add((ind[lo], part, ind[hi]))
    w.add((pub, about, ind["cls.deposit"]))
    asserted = {i: {first(c)} | ancestors(w, first(c)) for c, i in ind.items()}
    asserted[pub] = {first("cls.publication")} | ancestors(w, first("cls.publication"))
    base = Graph()
    for t in w:
        base.add(t)
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics, rdfs_closure=False, axiomatic_triples=False, datatype_axioms=False).expand(w)
    res["transitive"] = (ind["cls.deposit"], part, ind["cls.province"]) in w
    res["inherit"] = (pub, about, ind["cls.rayon"]) in w and (pub, about, ind["cls.province"]) in w
    res["owlrl_consistent"] = not any(True for _ in w.subjects(RDF.type, OWL.Nothing))
    for i, ok in asserted.items():
        for t in w.objects(i, RDF.type):
            if isinstance(t, URIRef) and str(t).startswith(ns) and t not in ok and (t, RDF.type, OWL.Class) in g:
                res["pollution"].append(f"{loc(i, ns)} : {loc(t, ns)}")
    if use_hermit:
        res["hermit"] = hermit(g, base)
    return res


def hermit(tbox_g: Graph, abox_g: Graph):
    try:
        from owlready2 import default_world, get_ontology, sync_reasoner_hermit
    except ImportError:
        return {"error": "owlready2 не установлен"}
    out = {}
    for name, graph in (("tbox", tbox_g), ("tbox+abox", abox_g)):
        try:
            d = Path(tempfile.mkdtemp())
            f = d / "o.owl"
            graph.serialize(f, format="xml")
            from owlready2 import World
            world = World()
            onto = world.get_ontology(f.as_uri()).load()
            with onto:
                sync_reasoner_hermit(world, debug=0)
            out[name] = {"consistent": True, "unsatisfiable": [c.name for c in world.inconsistent_classes()]}
        except Exception as e:
            msg = str(e)
            out[name] = {"consistent": "Inconsistent" not in msg and "inconsistent" not in msg.lower(), "error": msg[:160]}
    return out


# ------------------------------------------------------------------ ловушки OOPS!
def pitfalls(g: Graph, ns: str):
    cl, op, dp = ents(g, ns)
    props = op + dp
    P = defaultdict(list)
    dom = lambda p: [d for d in g.objects(p, RDFS.domain) if isinstance(d, URIRef)]
    rng = lambda p: [d for d in g.objects(p, RDFS.range) if isinstance(d, URIRef)]
    used = set()
    for p in props:
        used.update(dom(p) + rng(p))
    for c in cl:
        if not list(g.objects(c, RDFS.subClassOf)) and not list(g.subjects(RDFS.subClassOf, c)) and c not in used:
            P["P04"].append(loc(c, ns))
    for p in op:
        for q in g.objects(p, OWL.inverseOf):
            if isinstance(q, URIRef) and dom(p) and rng(p) and dom(q) and rng(q) and (set(dom(p)) != set(rng(q)) or set(rng(p)) != set(dom(q))):
                P["P05"].append(f"{loc(p, ns)} ↔ {loc(q, ns)}")
    for c in cl:                                         # циклы
        if c in ancestors(g, c):
            P["P06"].append(loc(c, ns))
    for e in cl + props:
        if not list(g.objects(e, RDFS.label)) and not list(g.objects(e, RDFS.comment)):
            P["P08"].append(loc(e, ns))
    if not (list(g.subjects(RDF.type, OWL.AllDisjointClasses)) or list(g.subject_objects(OWL.disjointWith))):
        P["P10"].append("нет аксиом непересечения классов")
    for p in op:
        if not dom(p) or not rng(p):
            P["P11"].append(loc(p, ns))
    for p in dp:
        if not dom(p):
            P["P11"].append(loc(p, ns) + " (нет области определения)")
    inv = {str(p) for p in op if list(g.objects(p, OWL.inverseOf))} | {str(q) for p in op for q in g.objects(p, OWL.inverseOf)} | \
          {str(s) for p in op for s in g.subjects(OWL.inverseOf, p)}
    for p in op:
        if str(p) not in inv:
            P["P13"].append(loc(p, ns))
    for p in props:
        if len(dom(p)) > 1 or len(rng(p)) > 1:
            P["P19"].append(f"{loc(p, ns)}: области определения {[loc(x, ns) for x in dom(p)]}, значений {[loc(x, ns) for x in rng(p)]}")
    for p in g.subjects(RDF.type, OWL.TransitiveProperty):
        if isinstance(p, URIRef) and str(p).startswith(ns) and set(dom(p)) != set(rng(p)):
            P["P29"].append(f"{loc(p, ns)}: области определения {[loc(x, ns) for x in dom(p)]} ≠ значений {[loc(x, ns) for x in rng(p)]}")
    declared = set(cl) | set(props)
    for s, p, o in g:
        if p in (RDFS.subClassOf, RDFS.domain, RDFS.range) and isinstance(o, URIRef) and str(o).startswith(ns) and o not in declared:
            P["P34"].append(loc(o, ns))
        if p in (OWL.inverseOf, RDFS.subPropertyOf) and isinstance(o, URIRef) and str(o).startswith(ns) and o not in declared:
            P["P35"].append(loc(o, ns))
    for k in ("P34", "P35"):
        P[k] = sorted(set(P[k]))
    onts = [o for o in g.subjects(RDF.type, OWL.Ontology) if isinstance(o, URIRef)]
    if not onts:
        P["P39"].append("не объявлен owl:Ontology (нет IRI онтологии)")
    elif not any(str(o) + "#" == ns or str(o) == ns or str(o) + "/" == ns for o in onts):
        P["P39"].append("IRI онтологии не согласован с пространством имён элементов")
    return P


# ------------------------------------------------------------------ основной расчёт
def evaluate(name, cfg, cqs, use_hermit):
    ns, slots = cfg["ns"], cfg["slots"]
    g = load(cfg)
    cl, op, dp = ents(g, ns)
    beh = behaviour_test(g, ns, slots, use_hermit)
    st = {s: slot_check(g, ns, slots, s, beh) for s in cqs["slots"]}
    cq_res = []
    for cq in cqs["cqs"]:
        miss = [n for n in cq["needs"] if not st[n][0]]
        kind = "полностью" if not miss else ("частично" if len(miss) < len(cq["needs"]) else "нет")
        cq_res.append({"id": cq["id"], "status": kind, "missing": miss})
    used = set()
    slot_to_elems = defaultdict(list)
    for s, v in slots.items():
        for n in resolve(v)[1]:
            used.add(n)
            slot_to_elems[s].append(n)
    just = set(cfg.get("justified", {}))
    sup = {}
    for kind, lst in (("classes", cl), ("object_props", op), ("data_props", dp)):
        names = [loc(e, ns) for e in lst]
        raw = [n for n in names if n not in used]
        adj = [n for n in raw if n not in just]
        sup[kind] = {"total": len(names), "raw": raw, "after_justified": adj}
    dups = {s: e for s, e in slot_to_elems.items() if len(e) > 1 and not isinstance(slots[s], dict)}
    pf = pitfalls(g, ns)
    n_axioms = len(g)
    return {"name": name, "title": cfg["title"], "counts": {"classes": len(cl), "object_props": len(op), "data_props": len(dp), "triples": n_axioms},
            "cq": cq_res, "slots": {s: {"ok": v[0], "note": v[1]} for s, v in st.items()}, "superfluous": sup, "duplicates": dups,
            "pitfalls": {k: v for k, v in sorted(pf.items())}, "behaviour": beh, "justified": cfg.get("justified", {})}


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "—"


def report(results, cqs):
    L = ["# Сравнение онтологий по критериям статьи (Lippolis et al., 2025)\n"]
    L.append("## 1. Размер\n")
    L.append("| Онтология | Классы | Объектные свойства | Свойства-данные | Триплеты |\n|---|---|---|---|---|")
    for r in results:
        c = r["counts"]
        L.append(f"| {r['title']} | {c['classes']} | {c['object_props']} | {c['data_props']} | {c['triples']} |")
    n = len(cqs["cqs"])
    L.append(f"\n## 2. Доля смоделированных компетентных вопросов (из {n})\n")
    L.append("| Онтология | Полностью | Частично | Нет | Доля полностью |\n|---|---|---|---|---|")
    for r in results:
        s = [c["status"] for c in r["cq"]]
        L.append(f"| {r['title']} | {s.count('полностью')} | {s.count('частично')} | {s.count('нет')} | {pct(s.count('полностью'), n)} |")
    L.append("\nПо вопросам:\n")
    L.append("| CQ | Вопрос | " + " | ".join(r["name"] for r in results) + " |\n|---|---|" + "---|" * len(results))
    for i, cq in enumerate(cqs["cqs"]):
        row = []
        for r in results:
            c = r["cq"][i]
            row.append({"полностью": "✓", "частично": "◐", "нет": "✗"}[c["status"]])
        L.append(f"| {cq['id']} | {cq['ru']} | " + " | ".join(row) + " |")
    L.append("\n✓ — смоделирован, ◐ — частично, ✗ — нет. Что именно не хватает:\n")
    for r in results:
        L.append(f"- **{r['name']}**: " + "; ".join(f"{c['id']}: {', '.join(c['missing'])}" for c in r["cq"] if c["missing"]) if any(c["missing"] for c in r["cq"]) else f"- **{r['name']}**: всё есть")
    L.append("\n## 3. Лишние элементы и дубликаты\n")
    L.append("«Лишний» — элемент, который не нужен ни одному CQ. Формат: лишних/всего (доля); «без обоснованных» — после исключения элементов, чьё присутствие объяснено (причины в поле justified файла соответствия).\n")
    L.append("| Онтология | Лишние классы | Лишние объектные свойства | Лишние свойства-данные | Слоты с дубликатами |\n|---|---|---|---|---|")
    for r in results:
        s = r["superfluous"]
        cells = [f"{len(s[k]['raw'])}/{s[k]['total']} ({pct(len(s[k]['raw']), s[k]['total'])}); без обоснованных: {len(s[k]['after_justified'])} ({pct(len(s[k]['after_justified']), s[k]['total'])})" for k in ("classes", "object_props", "data_props")]
        L.append(f"| {r['name']} | " + " | ".join(cells) + f" | {len(r['duplicates'])} |")
    L.append("")
    for r in results:
        s = r["superfluous"]
        parts = [f"{k}: {', '.join(s[k]['raw'])}" for k in s if s[k]["raw"]]
        L.append(f"- **{r['name']}**: " + ("; ".join(parts) if parts else "лишних нет") + (f"; дубликаты: {r['duplicates']}" if r["duplicates"] else ""))
    L.append("\n## 4. Ловушки (в стиле OOPS!)\n")
    allp = sorted({k for r in results for k in r["pitfalls"]} | CRITICAL)
    L.append("| Ловушка | " + " | ".join(r["name"] for r in results) + " |\n|---|" + "---|" * len(results))
    names = {"P04": "несвязанные элементы", "P05": "неверное обратное свойство", "P06": "цикл в иерархии классов", "P08": "нет меток/комментариев",
             "P10": "нет непересечения классов", "P11": "нет области определения/значений", "P13": "нет обратного свойства",
             "P19": "несколько областей определения/значений", "P29": "неверная транзитивность", "P34": "класс не объявлен", "P35": "свойство не объявлено",
             "P39": "неоднозначное пространство имён"}
    for k in allp:
        row = [str(len(r["pitfalls"].get(k, []))) for r in results]
        L.append(f"| {k}{' (критичная)' if k in CRITICAL else ''} {names.get(k, '')} | " + " | ".join(row) + " |")
    L.append("\nПримеры:\n")
    for r in results:
        for k, v in r["pitfalls"].items():
            if k in CRITICAL or k in ("P11", "P04", "P34", "P35"):
                L.append(f"- **{r['name']} / {k}**: " + "; ".join(v[:6]) + (" …" if len(v) > 6 else ""))
    L.append("\n## 5. Вывод и согласованность\n")
    L.append("Тест: цепочка провинция → область → район → месторождение → пласт → залежь, публикация о залежи; после вывода OWL 2 RL проверяются транзитивность и поднятие связи публикации вверх по иерархии.\n")
    L.append("| Онтология | «залежь входит в провинцию» | «публикация относится к району и провинции» | owlrl: согласована | HermiT: схема | HermiT: схема+тест | Типы, выведенные вне объявленной иерархии |\n|---|---|---|---|---|---|---|")
    for r in results:
        b = r["behaviour"]
        h = b.get("hermit") or {}
        hs = lambda k: ("да" if h.get(k, {}).get("consistent") else f"нет ({h.get(k, {}).get('error', '')[:50]})") if h.get(k) else "—"
        L.append(f"| {r['name']} | {'да' if b['transitive'] else 'нет'} | {'да' if b['inherit'] else 'нет'} | {'да' if b['owlrl_consistent'] else ('нет' if b['owlrl_consistent'] is not None else '—')} | {hs('tbox')} | {hs('tbox+abox')} | {len(b['pollution'])} |")
    for r in results:
        if r["behaviour"]["pollution"]:
            L.append(f"\n{r['name']} — примеры ложных типов: " + "; ".join(r["behaviour"]["pollution"][:8]))
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extra", action="append", default=[], help="имя=путь_к_ttl; рядом нужен ontology_llm/mappings/имя.json с полем file")
    ap.add_argument("--no-hermit", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "ontology_llm" / "results"))
    a = ap.parse_args()
    cqs = json.loads((ROOT / "ontology_llm" / "cq.json").read_text(encoding="utf-8"))
    names = ["manual_original", "manual_v22", "claude_cqbycq"] + [e.split("=")[0] for e in a.extra]
    results = []
    for n in names:
        cfg = json.loads((ROOT / "ontology_llm" / "mappings" / f"{n}.json").read_text(encoding="utf-8"))
        print("считаю:", n)
        results.append(evaluate(n, cfg, cqs, not a.no_hermit))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "comparison.md").write_text(report(results, cqs), encoding="utf-8")
    print("сохранено:", out / "comparison.md")


if __name__ == "__main__":
    main()
