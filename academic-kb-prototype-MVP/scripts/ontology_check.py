"""
Проверка онтологии: размер, согласованность, сверка с реляционной базой и (по желанию) со стандартным
рассуждателем HermiT.

    python -m scripts.ontology_check                       # сводка и сверка с реляционной базой
    python -m scripts.ontology_check --export onto.owl     # сохранить файл для Protégé
    python -m scripts.ontology_check --hermit              # сверить вывод приложения с HermiT

Для --hermit нужны: pip install owlready2 и установленная Java (HermiT входит в owlready2). Сравниваются
три вещи на ВСЕЙ онтологии: связи «входит в» (транзитивность, обратные свойства), связи публикаций с
объектами (цепочка свойств) и членство публикаций в определяемых классах (автоклассификация).
Совпадение означает, что чисто-питоновский вывод OWL 2 RL, которым пользуется приложение, даёт тот же
результат, что и HermiT из Protégé.
"""
import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rdflib import OWL, RDF

from app.database import SessionLocal
from app.ontology import queries, reasoner
from app.ontology import vocab as V


def hermit_compare(state) -> bool:
    try:
        from owlready2 import get_ontology, sync_reasoner_hermit
    except ImportError:
        print("Не установлен owlready2: pip install owlready2")
        return False
    body, _mime, _name = queries.export(state, "owl", inferred=False)
    path = Path(tempfile.mkdtemp()) / "oilgas_ontology.owl"
    path.write_bytes(body)
    onto = get_ontology(path.as_uri()).load()
    t = time.time()
    try:
        with onto:
            sync_reasoner_hermit(infer_property_values=True, debug=0)
    except Exception as e:
        print(f"HermiT не отработал ({type(e).__name__}: {str(e)[:200]}). Проверьте, что установлена Java.")
        return False
    print(f"HermiT отработал за {time.time() - t:.1f} с")
    world = onto.world
    get = lambda iri: world[str(iri)]

    def values(ind, prop_name):
        return {v.iri for v in getattr(ind, prop_name)}

    ok = True
    # 1. «входит в» у каждого объекта
    bad = total = 0
    for obj in state.reg.objects.values():
        ind = get(obj["iri"])
        mine = {str(o) for o in state.full.objects(obj["iri"], V.P_IN)}
        theirs = values(ind, "входитВ")
        total += 1
        if mine != theirs:
            bad += 1
            if bad <= 3:
                print(f"   расхождение входитВ у «{obj['name']}»: приложение {len(mine)}, HermiT {len(theirs)}")
    print(f"«входит в» (транзитивность, обратные свойства): совпало {total - bad} из {total} объектов")
    ok &= bad == 0

    # 2. «относится к объекту» у каждой публикации
    bad = total = 0
    for pid, iri in state.reg.publications.items():
        ind = get(iri)
        mine = {str(o) for o in state.full.objects(iri, V.P_ABOUT)}
        theirs = values(ind, "относитсяКОбъекту")
        total += 1
        if mine != theirs:
            bad += 1
            if bad <= 3:
                print(f"   расхождение относитсяКОбъекту у публикации {pid}: приложение {len(mine)}, HermiT {len(theirs)}")
    print(f"«относится к объекту» (цепочка свойств): совпало {total - bad} из {total} публикаций")
    ok &= bad == 0

    # 3. членство в определяемых классах. owlready2 для объекта возвращает только самые КОНКРЕТНЫЕ
    # классы: класс «Публикация района X» лежит ниже «Публикации области Y», а тот ниже «Публикации
    # провинции Z» (район входит в область), поэтому классы области и провинции у публикации
    # унаследованные. Сравниваем по всем классам, включая предков в иерархии, которую вывел HermiT.
    from rdflib import URIRef
    defined = sorted({str(s) for s, p, o in state.explicit if p == OWL.equivalentClass})
    bad = checked = 0
    for pid, iri in state.reg.publications.items():
        ind = get(iri)
        theirs = set()
        for c in ind.is_a:
            if hasattr(c, "iri"):
                theirs |= {a.iri for a in c.ancestors()}
        for cls in defined:
            mine = (iri, RDF.type, URIRef(cls)) in state.full
            checked += 1
            if mine != (cls in theirs):
                bad += 1
                if bad <= 3:
                    print(f"   расхождение по классу {V.short(cls)} у публикации {pid}")
    print(f"Автоклассификация ({len(defined)} определяемых классов): совпало {checked - bad} из {checked} проверок")
    ok &= bad == 0

    # иерархия определяемых классов, которую HermiT вывел сам (в базе она не записана)
    areas = [c for c in defined if "Публикация_района_" in c or "Публикация_области_" in c or "Публикация_провинции_" in c]
    shown = 0
    for cls in areas:
        c = get(cls)
        parents = [V.short(a.iri) for a in c.ancestors() if a.iri in areas and a.iri != cls]
        if parents and shown < 2:
            print(f"   HermiT вывел: {V.short(cls)} ⊑ {', '.join(parents)}")
            shown += 1
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", metavar="ФАЙЛ", help="сохранить онтологию (RDF/XML) для Protégé")
    ap.add_argument("--inferred", action="store_true", help="в экспорт добавить выведенные факты")
    ap.add_argument("--hermit", action="store_true", help="сверить вывод приложения с HermiT")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        st = reasoner.get_state(db)
        s = queries.summary(st)
        print(f"Онтология построена за {s['build_seconds']} с (из них вывод {s['closure_seconds']} с)")
        print(f"Классов: {s['classes']} (определяемых: {s['defined_classes']}); объектных свойств: {s['object_properties']}; "
              f"свойств-данных: {s['data_properties']}; экземпляров: {s['individuals_total']}")
        print(f"Фактов утверждено: {s['abox_triples']} (всего вместе с аксиомами {s['triples_asserted']}); выведено значимых: {s['inferred_meaningful']}")
        for k, v in sorted(s["inferred_by_kind"].items(), key=lambda kv: -kv[1]):
            print(f"   выведено {v:6d}: {k}")
        print("Согласованность:", "да" if s["consistent"] else "НЕТ: " + "; ".join(s["errors"]))
        v = queries.validate_against_sql(st, db)
        print("Сверка с реляционной базой (рекурсивные запросы):")
        for r in v["rows"]:
            print(f"   {r['level']:14} совпало {r['matching']} из {r['objects']}" + (f"; расхождения: {r['mismatches']}" if r["mismatches"] else ""))
        ok = s["consistent"] and v["all_match"]
        if args.export:
            body, _m, _n = queries.export(st, "owl", args.inferred)
            Path(args.export).write_bytes(body)
            print(f"Файл сохранён: {args.export} (откройте в Protégé и запустите HermiT)")
        if args.hermit:
            print("\nСверка с HermiT:")
            ok &= hermit_compare(st)
        print("\nИТОГ:", "всё совпало" if ok else "ЕСТЬ РАСХОЖДЕНИЯ")
        return 0 if ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
