"""
Измерение качества поиска и ответов (раздел 3.8.3 отчёта: релевантность,
полнота, наличие ссылок, отсутствие вымышленных источников).

Без измерений нельзя сказать, что система стала лучше. Скрипт работает на ВАШЕЙ
базе, поэтому цифры отражают реальное состояние, и их можно вставить в
диссертацию «до и после» изменений.

Режимы:
  retrieval  сравнивает поиск (без LLM): прежний (по склеенному тексту, один язык)
             и новый (по фрагментам, двуязычный словарь; опционально —
             английская формулировка от LLM и смысловой поиск).
             Вопросы: eval/questions.json (+ eval/generated.json, если есть).
  generate   LLM придумывает по аннотациям вопрос (по-русски и по-английски),
             ответ на который есть в конкретной публикации. Нужен LLM-ключ.
  answers    прогоняет полный чат и считает: долю ответов с верными ссылками,
             выдуманные номера и источники, язык ответа, время.
  calibrate  подбирает порог схожести EMBEDDINGS_MIN_SIM по сгенерированным вопросам.
  behavior   поведение на вопросах разных видов (eval/behavior.json): разговор («привет»),
             «что ты умеешь», вопросы вне темы, вопросы по теме без ответа в базе (честный отказ,
             выдуманные числа).

  pool       готовит файл для ручной разметки релевантности (eval/gold_template.json): для каждого вопроса
             собирает кандидатов из результатов нескольких методов поиска (метод «пулинга», как в TREC).
             Вы выставляете каждой публикации оценку grade: 0 — не по теме, 1 — частично, 2 — прямо отвечает,
             и сохраняете файл как eval/gold.json. Свои вопросы можно дописать в тот же файл.
             При наличии eval/gold.json режим retrieval считает метрики по ЭТОЙ разметке (а не по регулярным
             выражениям): Precision@5, Recall@10, nDCG@5, MRR, Hit@5 — отдельно для русских и английских вопросов.

  intent     точность определения намерения (llm.detect_intent) на eval/intent_questions.json: 24 вопроса с ожидаемой
             меткой (FIND_PUBLICATION, FIND_OBJECT, COMPARE_OBJECTS, EXPLAIN, OFFTOPIC, UNKNOWN). Печатает таблицу
             «Вопрос | Ожидалось | Получено | Верно», Accuracy, точность/полноту по меткам и матрицу ошибок.
             Нужен ключ LLM: без него detect_intent всегда возвращает FIND_PUBLICATION, и точность ничего не значит.

  scope      точность классификатора темы на eval/scope_questions.json (36 вопросов: по теме / вне темы).
             Таблица «Вопрос | Ожидалось | Получено | Верно» и Accuracy. Режимы: по умолчанию вопросы идут
             через /api/chat (в процессе, без внешнего поиска); --api http://127.0.0.1:8001 — через
             запущенный сервер; --fast — только классификатор (без поиска и ответа LLM), с близостью
             к теме и рекомендацией порогов SCOPE_SIM_LOW / SCOPE_SIM_HIGH.

Примеры:
  python -m scripts.eval_chat scope --fast
  python -m scripts.eval_chat scope --api http://127.0.0.1:8001
  python -m scripts.eval_chat retrieval
  python -m scripts.eval_chat generate --n 25
  python -m scripts.eval_chat retrieval --llm-rewrite
  python -m scripts.eval_chat retrieval --details       # где именно новый поиск хуже прежнего
  python -m scripts.eval_chat answers --n 10 --no-external
  python -m scripts.eval_chat behavior --no-external
"""
import argparse
import json
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv()

from app import embeddings, llm, models, search  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.tfidf import tfidf_cosine_scores  # noqa: E402

EVAL_DIR = ROOT / "eval"


# ----------------------------------------------------------------------------
def cyr_title(title):
    letters = [c for c in (title or "") if c.isalpha()]
    return bool(letters) and sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё") / len(letters) >= 0.5


def load_publications(db):
    pubs = db.query(models.Publication).all()
    rows = []
    for p in pubs:
        first = sorted(p.chunks, key=lambda c: (c.chunk_index, c.chunk_id))[:1]
        abstract = first[0].text if first else ""
        authors = " ".join(l.author.full_name for l in p.author_links)
        full = " ".join([p.title or "", authors, p.journal or ""] + [c.text for c in p.chunks])
        rows.append({"id": p.publication_id, "title": p.title, "doi": p.doi, "year": p.year, "abstract": abstract, "legacy_text": full,
                     "ru": cyr_title(p.title), "abs_len": len(abstract), "chunks": len(p.chunks)})
    return rows


def relevant_ids(pubs, regex):
    rx = re.compile(regex, re.IGNORECASE)
    return {p["id"] for p in pubs if rx.search(f"{p['title']} {p['abstract'][:1500]}")}


def metrics(ranked, relevant, k=5):
    top = ranked[:k]
    hit = 1.0 if any(i in relevant for i in top) else 0.0
    prec = (sum(1 for i in top if i in relevant) / len(top)) if top else 0.0
    mrr = 0.0
    for rank, i in enumerate(ranked[:10], start=1):
        if i in relevant:
            mrr = 1.0 / rank
            break
    return hit, prec, mrr


class Retrievers:
    def __init__(self, db, pubs, llm_rewrite=False):
        self.db, self.pubs, self.llm_rewrite = db, pubs, llm_rewrite
        self.corpus = [p["legacy_text"] for p in pubs]

    def legacy(self, q, lang):
        scores = tfidf_cosine_scores(self.corpus, q)
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [self.pubs[i]["id"] for i in order if scores[i] > 0][:10]

    def _new(self, queries):
        _ids, hits = search.retrieve(self.db, queries, top_k=10)
        return [h.pub.publication_id for h in hits if h.score > 0]

    def new_traced(self, q):
        """Новый поиск без эмбеддингов, с диагностикой: (id по убыванию оценки, trace)."""
        key = os.environ.pop("EMBEDDINGS_API_KEY", None)
        try:
            trace = {}
            _ids, hits = search.retrieve(self.db, [q], top_k=10, trace=trace)
            return [h.pub.publication_id for h in hits if h.score > 0], trace
        finally:
            if key:
                os.environ["EMBEDDINGS_API_KEY"] = key

    def new_lexical(self, q, lang):
        key = os.environ.pop("EMBEDDINGS_API_KEY", None)  # честное сравнение: без смыслового поиска
        try:
            return self._new([q])
        finally:
            if key:
                os.environ["EMBEDDINGS_API_KEY"] = key

    def new_llm(self, q, lang):
        key = os.environ.pop("EMBEDDINGS_API_KEY", None)
        try:
            extra = llm.generate_search_query(q, None, "en") if lang == "ru" else None
            return self._new([q] + ([extra] if extra else []))
        finally:
            if key:
                os.environ["EMBEDDINGS_API_KEY"] = key

    def new_semantic(self, q, lang):
        extra = llm.generate_search_query(q, None, "en") if (self.llm_rewrite and lang == "ru") else None
        return self._new([q] + ([extra] if extra else []))


# ----------------------------------------------------------------------------
# Эталонная разметка (gold): ручные оценки релевантности по методу пулинга
def pub_key(doi, title):
    """Устойчивый ключ публикации для файла разметки: DOI, а без него — нормализованное название
    (числовые id меняются при пересоздании базы, DOI и название — нет)."""
    d = (doi or "").strip().lower()
    for pref in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pref):
            d = d[len(pref):]
    if d:
        return "doi:" + d
    return "t:" + re.sub(r"[^a-zа-яё0-9]+", " ", (title or "").lower()).strip()[:120]


def ndcg_at_k(ranked_grades, all_grades, k=5):
    """nDCG@k: ranked_grades — оценки результатов по порядку выдачи; all_grades — все известные оценки
    по вопросу (для идеального порядка)."""
    import math
    dcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ranked_grades[:k]))
    ideal = sorted(all_grades, reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def gold_metrics(ranked_keys, grades, k=5, k_recall=10):
    """Метрики одного вопроса. grades: {ключ публикации: 0/1/2}. Релевантной считается оценка >= 1.
    Публикации, которых нет в разметке, считаются нерелевантными (оценка 0)."""
    rel = {key for key, g in grades.items() if g >= 1}
    top = ranked_keys[:k]
    hit = 1.0 if any(x in rel for x in top) else 0.0
    prec = (sum(1 for x in top if x in rel) / len(top)) if top else 0.0
    recall = (len({x for x in ranked_keys[:k_recall] if x in rel}) / len(rel)) if rel else 0.0
    mrr = next((1.0 / r for r, x in enumerate(ranked_keys[:10], start=1) if x in rel), 0.0)
    ndcg = ndcg_at_k([grades.get(x, 0) for x in ranked_keys], list(grades.values()), k)
    return {"hit": hit, "prec": prec, "recall": recall, "mrr": mrr, "ndcg": ndcg}


def load_gold():
    path = EVAL_DIR / "gold.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for item in data.get("questions", []):
        grades = {j["key"]: int(j["grade"]) for j in item.get("judgments", []) if j.get("grade") is not None}
        if grades:
            out.append({"id": item.get("id"), "q": item.get("q"), "q_en": item.get("q_en") or item.get("q"), "grades": grades})
    return out


def cmd_pool(db, args):
    """Готовит eval/gold_template.json: кандидаты для ручной оценки по каждому вопросу."""
    pubs = load_publications(db)
    if not pubs:
        print("В базе нет публикаций — сначала наполните её (python -m scripts.bulk_import).")
        return None
    by_id = {p["id"]: p for p in pubs}
    bank = json.loads((EVAL_DIR / "questions.json").read_text(encoding="utf-8"))["questions"]
    extra = EVAL_DIR / "my_questions.json"          # свои вопросы: [{"id","q","q_en"}]
    if extra.exists():
        bank = bank + json.loads(extra.read_text(encoding="utf-8"))
    R = Retrievers(db, pubs, False)
    methods = [R.legacy, R.new_lexical] + ([R.new_semantic] if embeddings.is_configured() else [])
    out = []
    for item in bank:
        pooled = []
        for fn in methods:
            for lang, text in (("ru", item["q"]), ("en", item.get("q_en") or item["q"])):
                for pid in fn(text, lang)[: args.depth]:
                    if pid not in pooled:
                        pooled.append(pid)
        out.append({"id": item["id"], "q": item["q"], "q_en": item.get("q_en"),
                    "judgments": [{"key": pub_key(by_id[i]["doi"], by_id[i]["title"]), "title": by_id[i]["title"],
                                   "year": by_id[i]["year"], "grade": None} for i in pooled]})
    path = EVAL_DIR / "gold_template.json"
    path.write_text(json.dumps({"_comment": "Для каждой публикации поставьте grade: 0 — не по теме, 1 — частично по теме, "
                                "2 — прямо отвечает на вопрос. Строки без grade (null) не учитываются. "
                                "Готовый файл сохраните как eval/gold.json.", "questions": out}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    total = sum(len(x["judgments"]) for x in out)
    print(f"Файл для разметки: {_rel(path)} — {len(out)} вопросов, {total} пар «вопрос — публикация».")
    print("Выставьте grade (0/1/2), сохраните как eval/gold.json и запустите: python -m scripts.eval_chat retrieval")
    return None


def evaluate_gold(db, pubs, gold, args):
    """Метрики поиска по ручной разметке."""
    by_id = {p["id"]: p for p in pubs}
    R = Retrievers(db, pubs, args.llm_rewrite)
    methods = [("прежний (v16: склеенный текст, один язык)", R.legacy), ("новый: фрагменты + словарь", R.new_lexical)]
    if llm.is_configured() and args.llm_rewrite:
        methods.append(("новый + английская формулировка от LLM", R.new_llm))
    if embeddings.is_configured():
        methods.append(("новый + смысловой поиск (эмбеддинги)", R.new_semantic))
    n_pairs = sum(len(g["grades"]) for g in gold)
    lines = ["## Оценка по ручной разметке (eval/gold.json)", "",
             f"Вопросов с разметкой: {len(gold)}, оценённых пар «вопрос — публикация»: {n_pairs}. "
             "Публикации, которых нет в разметке, считаются нерелевантными.", ""]
    for lang, label in (("ru", "Вопросы на русском"), ("en", "Вопросы на английском")):
        lines += [f"### {label}", "", "| Метод | Hit@5 | Precision@5 | Recall@10 | nDCG@5 | MRR |", "|---|---|---|---|---|---|"]
        for name, fn in methods:
            acc = {"hit": 0.0, "prec": 0.0, "recall": 0.0, "mrr": 0.0, "ndcg": 0.0}
            for item in gold:
                text = item["q"] if lang == "ru" else item["q_en"]
                ids = fn(text, lang)
                keys = [pub_key(by_id[i]["doi"], by_id[i]["title"]) for i in ids if i in by_id]
                for k, v in gold_metrics(keys, item["grades"]).items():
                    acc[k] += v
            n = len(gold)
            lines.append(f"| {name} | " + " | ".join(fmt(acc[k] / n) for k in ("hit", "prec", "recall", "ndcg", "mrr")) + " |")
        lines.append("")
    lines += ["Hit@5 — доля вопросов, где среди 5 первых результатов есть релевантная публикация; Precision@5 — доля "
              "релевантных среди 5 первых; Recall@10 — доля найденных релевантных среди 10 первых; nDCG@5 — качество "
              "порядка с учётом оценок 0/1/2 (1.0 — идеальный порядок); MRR — средняя величина 1/позиция первой релевантной.", ""]
    return lines


def _rel(path):
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


def fmt(x):
    return f"{x:.2f}"


def details_section(R, pubs, answerable):
    """Диагностика: по каким вопросам новый поиск хуже прежнего и почему."""
    by_id = {p["id"]: p for p in pubs}
    L = ["## Диагностика", ""]

    def first_rank(ranked, rel):
        for i, pid in enumerate(ranked[:10], start=1):
            if pid in rel:
                return i
        return None

    def p5(ranked, rel):
        top = ranked[:5]
        return (sum(1 for i in top if i in rel) / len(top)) if top else 0.0

    rows, problems = [], []
    junk = {"ru": [], "en": []}
    for lang, key in (("ru", "q"), ("en", "q_en")):
        for item, rel in answerable:
            q = item[key]
            old = R.legacy(q, lang)
            new, trace = R.new_traced(q)
            fo, fn = first_rank(old, rel), first_rank(new, rel)
            po, pn = p5(old, rel), p5(new, rel)
            rows.append((item["id"], lang, len(rel), fo, fn, po, pn))
            for pid in new[:5]:
                if pid not in rel:
                    junk[lang].append((pid, trace.get(pid, {})))
            if fn is None or (fo is not None and fn > fo + 1) or pn < po - 0.2:
                problems.append((lang, q, rel, old, new, trace, fo, fn, po, pn))

    L += ["### Таблица по всем вопросам", "",
          "Позиция — место первой релевантной публикации (— если нет среди первых 10).", "",
          "| вопрос | язык | релевантных в базе | позиция прежний | позиция новый | P@5 прежний | P@5 новый |", "|---|---|---|---|---|---|---|"]
    for qid, lang, nrel, fo, fn, po, pn in rows:
        mark = " **<**" if (fn is None and fo) or (fo and fn and fn > fo) or pn < po else ""
        L.append(f"| {qid} | {lang} | {nrel} | {fo or '—'} | {fn or '—'}{mark} | {po:.2f} | {pn:.2f} |")
    L.append("")

    L += ["### Из чего состоит «мусор» в первой пятёрке нового поиска", ""]
    L += ["Нерелевантные публикации в первой пятёрке: какие они. Это показывает причину потери точности.", "",
          "| язык вопроса | всего | на русском | без аннотации | средняя длина аннотации, симв. | оценку дал перевод | оценку дала карточка |", "|---|---|---|---|---|---|---|"]
    for lang in ("ru", "en"):
        j = junk[lang]
        n = max(len(j), 1)
        ru = sum(1 for pid, _t in j if by_id[pid]["ru"])
        noabs = sum(1 for pid, _t in j if not by_id[pid]["abs_len"])
        avg = sum(by_id[pid]["abs_len"] for pid, _t in j) / n
        tr = sum(1 for _p, t in j if "перевод" in t.get("variant", ""))
        card = sum(1 for _p, t in j if t.get("unit") == "карточка")
        L.append(f"| {lang} | {len(j)} | {ru} ({100 * ru // n}%) | {noabs} ({100 * noabs // n}%) | {avg:.0f} | {tr} ({100 * tr // n}%) | {card} ({100 * card // n}%) |")
    rel_all = [pid for _i, rel in answerable for pid in rel]
    L += ["", f"Для сравнения: средняя длина аннотации у релевантных публикаций {sum(by_id[p]['abs_len'] for p in rel_all) / max(len(rel_all), 1):.0f} симв., "
              f"без аннотации {100 * sum(1 for p in rel_all if not by_id[p]['abs_len']) // max(len(rel_all), 1)}%, "
              f"на русском {100 * sum(1 for p in rel_all if by_id[p]['ru']) // max(len(rel_all), 1)}%.", ""]

    L += ["### Проблемные вопросы", ""]
    if not problems:
        L += ["Таких вопросов нет.", ""]
    for lang, q, rel, old, new, trace, fo, fn, po, pn in problems[:14]:
        L += [f"**[{lang}] {q}** (релевантных в базе: {len(rel)}; позиция первой релевантной: прежний {fo or '—'}, новый {fn or '—'}; P@5 {po:.2f} → {pn:.2f})", ""]

        def info(pid):
            p = by_id[pid]; t = trace.get(pid, {})
            return (f"{'+' if pid in rel else '–'} {p['title'][:70]} [{'рус' if p['ru'] else 'англ'}, аннотация {p['abs_len']} симв.; "
                    f"оценка {t.get('score', 0):.2f}: {t.get('variant', '?')}, {t.get('unit', '?')}]")
        L += [f"- новый: {info(pid)}" for pid in new[:4]] or ["- новый: ничего не найдено"]
        L += [f"- прежний: {'+' if pid in rel else '–'} {by_id[pid]['title'][:70]}" for pid in old[:3]] or ["- прежний: ничего не найдено"]
        lost = [pid for pid in old[:3] if pid in rel and pid not in new[:5]]
        for pid in lost:
            L.append(f"- потеряна: {info(pid)}; в новой выдаче " + (f"на месте {new.index(pid) + 1}" if pid in new else "её нет"))
        L.append("")
    L += ["«+» — публикация релевантна вопросу (по регулярному выражению в eval/questions.json), «–» — нет.", ""]
    return L


def evaluate_retrieval(db, args):
    pubs = load_publications(db)
    if not pubs:
        print("В базе нет публикаций — оценивать нечего.")
        return None
    bank = json.loads((EVAL_DIR / "questions.json").read_text(encoding="utf-8"))["questions"]
    R = Retrievers(db, pubs, args.llm_rewrite)
    methods = [("прежний (v16: склеенный текст, один язык)", R.legacy),
               ("новый: фрагменты + словарь", R.new_lexical)]
    if llm.is_configured() and args.llm_rewrite:
        methods.append(("новый + английская формулировка от LLM", R.new_llm))
    if embeddings.is_configured():
        methods.append(("новый + смысловой поиск (эмбеддинги)", R.new_semantic))

    lines = [f"# Оценка поиска ({datetime.now():%Y-%m-%d %H:%M})", "",
             f"Публикаций в базе: {len(pubs)}. Вопросов в банке: {len(bank)}.", ""]
    gold = load_gold()
    if gold:
        lines += evaluate_gold(db, pubs, gold, args)
    else:
        lines += ["Ручной разметки нет (eval/gold.json): ниже метрики по регулярным выражениям — это грубая оценка. "
                  "Для защищаемых цифр выполните `python -m scripts.eval_chat pool` и разметьте релевантность.", ""]

    answerable = []
    for item in bank:
        rel = relevant_ids(pubs, item["expect"])
        if rel:
            answerable.append((item, rel))
    lines += [f"Вопросов, на которые в базе есть хотя бы одна подходящая публикация: {len(answerable)} из {len(bank)}. "
              "Остальные в расчёт не берутся (нельзя ругать поиск за то, чего в базе нет).", ""]
    if not answerable:
        lines.append("Подходящих публикаций в базе нет ни по одному вопросу — наполните базу (вкладка «Наполнение базы»).")

    for lang, label in (("ru", "Вопросы на русском"), ("en", "Вопросы на английском")):
        if not answerable:
            break
        lines += [f"## {label}", "", "| Метод | Hit@5 | Precision@5 | MRR |", "|---|---|---|---|"]
        for name, fn in methods:
            h = p = m = 0.0
            for item, rel in answerable:
                ranked = fn(item["q"] if lang == "ru" else item["q_en"], lang)
                a, b, c = metrics(ranked, rel)
                h, p, m = h + a, p + b, m + c
            n = len(answerable)
            lines.append(f"| {name} | {fmt(h / n)} | {fmt(p / n)} | {fmt(m / n)} |")
        lines.append("")
    lines += ["Hit@5 — доля вопросов, у которых среди 5 первых результатов есть релевантная публикация; "
              "Precision@5 — доля релевантных среди 5 первых; MRR — средняя величина 1/позиция первой релевантной.", ""]

    if getattr(args, "details", False) and answerable:
        lines += details_section(R, pubs, answerable)

    gen_path = EVAL_DIR / "generated.json"
    if gen_path.exists():
        gen = json.loads(gen_path.read_text(encoding="utf-8"))
        ids = {p["id"] for p in pubs}
        gen = [g for g in gen if g["expected_pub_id"] in ids]
        lines += [f"## Сгенерированные вопросы по аннотациям ({len(gen)}): ожидаемая публикация", ""]
        for lang, key, label in (("ru", "question_ru", "русские"), ("en", "question_en", "английские")):
            lines += [f"Вопросы {label}:", "", "| Метод | Recall@5 | MRR |", "|---|---|---|"]
            for name, fn in methods:
                r = m = 0.0
                for g in gen:
                    ranked = fn(g[key], lang)
                    exp = g["expected_pub_id"]
                    r += 1.0 if exp in ranked[:5] else 0.0
                    m += (1.0 / (ranked.index(exp) + 1)) if exp in ranked[:10] else 0.0
                lines.append(f"| {name} | {fmt(r / max(len(gen), 1))} | {fmt(m / max(len(gen), 1))} |")
            lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
def cmd_generate(db, args):
    if not llm.is_configured():
        print("Нужен ключ LLM (DEEPSEEK_API_KEY и т. п. в .env).")
        return
    client = llm._get_client()
    provider = llm._active_provider()
    pubs = [p for p in db.query(models.Publication).all()
            if p.chunks and len(sorted(p.chunks, key=lambda c: (c.chunk_index, c.chunk_id))[0].text) >= args.min_abstract]
    random.seed(args.seed)
    random.shuffle(pubs)
    out = []
    for p in pubs[: args.n]:
        abstract = sorted(p.chunks, key=lambda c: (c.chunk_index, c.chunk_id))[0].text[:1500]
        prompt = ("Ниже аннотация научной статьи. Придумай ОДИН вопрос, ответ на который есть в этой аннотации. "
                  "Не копируй название, не упоминай «статью» и «автора». Верни JSON без пояснений: "
                  '{"question_ru": "...по-русски...", "question_en": "...in English..."}\n\n'
                  f"Название: {p.title}\nАннотация: {abstract}")
        try:
            resp = client.chat.completions.create(model=provider["model"], temperature=0.3, max_tokens=300,
                                                  messages=[{"role": "user", "content": prompt}])
            text = (resp.choices[0].message.content or "").strip()
            m = re.search(r"\{.*\}", text, re.S)
            data = json.loads(m.group(0))
            out.append({"expected_pub_id": p.publication_id, "title": p.title,
                        "question_ru": data["question_ru"], "question_en": data["question_en"]})
            print(f"  [{p.publication_id}] {data['question_ru']}")
        except Exception as e:
            print(f"  [{p.publication_id}] пропущено: {type(e).__name__}")
    EVAL_DIR.mkdir(exist_ok=True)
    (EVAL_DIR / "generated.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Сохранено вопросов: {len(out)} -> eval/generated.json")


_NUM_RE = re.compile(r"\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?")


def _numbers(text, min_digits=2):
    """Числа из текста в нормализованном виде. Однозначные целые (номера пунктов,
    маркеры) не учитываются; числа от двух цифр и десятичные дроби учитываются."""
    text = re.sub(r"https?://\S+|10\.\d{4,9}/\S+", " ", text or "")
    text = re.sub(r"\[[\d,\s\-–?]+\]", " ", text)            # ссылки [3], [1, 2]
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", "", text)              # нумерация списков
    out = set()
    for m in _NUM_RE.finditer(text):
        raw = m.group(0).replace("\u00a0", "").replace(" ", "").replace(",", ".")
        digits = re.sub(r"\D", "", raw)
        if "." in raw or len(digits) >= min_digits:
            out.add(raw.rstrip("."))
    return out


def unsupported_numbers(db, resp, question):
    """Числа в ответе, которых нет ни в источниках, ни в справке из БД, ни в самом вопросе.
    Это эвристика выдумки: модель могла назвать цифру «из головы». Числа вида «20 %» и
    расчётные величины тоже попадут сюда, поэтому показатель надо читать как «подозрение»,
    а подозрительные ответы просмотреть глазами."""
    from app.services import kb_context

    pool = [question]
    ids = [x["publication_id"] for x in resp.get("sources", [])]
    if ids:
        for p in db.query(models.Publication).filter(models.Publication.publication_id.in_(ids)).all():
            pool += [p.title or "", str(p.year or ""), p.journal or ""] + [c.text for c in p.chunks]
    for e in resp.get("external_sources", []):
        pool += [e.get("title") or "", str(e.get("year") or ""), e.get("abstract") or ""]
    kb_text, _ = kb_context.build_kb_context(db, question)
    pool.append(kb_text or "")
    known = set()
    for t in pool:
        known |= _numbers(t, min_digits=1)
    return sorted(n for n in _numbers(resp.get("answer", "")) if n not in known)


_REFUSAL_RE = re.compile(
    r"нет (?:информации|данных|сведений|упоминан\w*)|не (?:найден\w*|нашл\w*|содерж\w*|удалось найти|приведен\w*|приводятся|"
    r"указан\w*|раскрыт\w*|упомина\w*|представлен\w*|располагаю)|недостаточно|отсутству\w*|"
    r"no (?:information|data)|not (?:found|mentioned|available|provided|reported)|cannot (?:be )?(?:answer|determin)|"
    r"don'?t (?:contain|have)|do not (?:contain|have)", re.IGNORECASE)


def cyr_ratio(text):
    letters = [c for c in text if c.isalpha()]
    return (sum(1 for c in letters if "а" <= c.lower() <= "я" or c.lower() == "ё") / len(letters)) if letters else 0.0


def cmd_answers(db, args):
    if not llm.is_configured():
        print("Нужен ключ LLM для оценки ответов.")
        return None
    from fastapi.testclient import TestClient
    from app.main import app

    bank = json.loads((EVAL_DIR / "questions.json").read_text(encoding="utf-8"))["questions"]
    questions = [b["q"] for b in bank]
    gen_path = EVAL_DIR / "generated.json"
    if gen_path.exists():
        questions = [g["question_ru"] for g in json.loads(gen_path.read_text(encoding="utf-8"))] + questions
    random.seed(args.seed)
    questions = questions[: args.n]
    client = TestClient(app)
    rows, times = [], []
    for q in questions:
        t = time.perf_counter()
        r = client.post("/api/chat", json={"message": q, "history": [], "use_external": not args.no_external})
        dt = time.perf_counter() - t
        if r.status_code != 200:
            print(f"  ошибка {r.status_code}: {q}")
            continue
        j = r.json()
        times.append(dt)
        warn = j["citation_warnings"]
        rows.append({
            "numbers": bool(unsupported_numbers(db, j, q)),
            "invalid": any("несуществующие" in w for w in warn),
            "unverified": any("Не удалось сопоставить" in w for w in warn),
            "valid_cit": len(j["citations"]) > 0,
            "ru": cyr_ratio(j["answer"]) >= 0.6,
            "llm": j["llm_used"], "kb": bool(j["kb_objects"]),
            "sources": len(j["sources"]) + len(j["external_sources"]),
        })
        print(f"  {dt:5.1f} с | ссылок: {len(j['citations'])} | {'ЕСТЬ ПРОБЛЕМЫ ' + ' '.join(warn)[:60] if warn else 'ссылки чистые'} | {q[:60]}")
    n = max(len(rows), 1)
    pct = lambda k: f"{100 * sum(1 for r in rows if r[k]) / n:.0f}%"
    lines = [f"# Оценка ответов чат-бота ({datetime.now():%Y-%m-%d %H:%M})", "",
             f"Вопросов: {len(rows)}; внешний поиск: {'выключен' if args.no_external else 'включён'}.", "",
             "| Показатель | Значение |", "|---|---|",
             f"| Ответ сформирован LLM | {pct('llm')} |",
             f"| В ответе есть хотя бы одна проверенная ссылка [n] | {pct('valid_cit')} |",
             f"| Были выдуманные номера источников (удалены) | {pct('invalid')} |",
             f"| В списке литературы есть строки, не найденные среди источников | {pct('unverified')} |",
             f"| В ответе есть числа, которых нет ни в источниках, ни в вопросе (подозрение на выдумку) | {pct('numbers')} |",
             f"| Ответ на русском языке | {pct('ru')} |",
             f"| Использована справка из БД | {pct('kb')} |",
             f"| Среднее число источников в ответе | {statistics.mean(r['sources'] for r in rows):.1f} |" if rows else "| — | — |",
             f"| Время ответа, среднее / медиана, с | {statistics.mean(times):.1f} / {statistics.median(times):.1f} |" if times else "| — | — |"]
    return "\n".join(lines)


def cmd_behavior(db, args):
    from fastapi.testclient import TestClient
    from app.main import app

    spec = json.loads((EVAL_DIR / "behavior.json").read_text(encoding="utf-8"))
    client = TestClient(app)
    use_ext = not args.no_external
    llm_on = llm.is_configured()
    results = {}

    def ask(q):
        t = time.perf_counter()
        r = client.post("/api/chat", json={"message": q, "history": [], "use_external": use_ext})
        return r.json(), time.perf_counter() - t, r.status_code

    def has_sources(j):
        return bool(j["sources"] or j["external_sources"] or j["citations"])

    checks = {
        "smalltalk": lambda j, dt, q: (
            [] if (j["intent"] == "smalltalk" and not has_sources(j) and "не найдено" not in j["answer"].lower() and dt < 3.0)
            else [f"intent={j['intent']}", "есть источники" if has_sources(j) else "", f"{dt:.1f} с" if dt >= 3.0 else ""]),
        "capabilities": lambda j, dt, q: (
            [] if (j["intent"] == "capabilities" and not has_sources(j)
                   and re.search(r"публикац|publications", j["answer"]) and re.search(r"Ограничения|Limits", j["answer"]))
            else [f"intent={j['intent']}", "нет описания возможностей/ограничений" if not re.search(r"Ограничения|Limits", j["answer"]) else ""]),
        "offtopic": lambda j, dt, q: (
            [] if (j["intent"] == "offtopic" and not has_sources(j)
                   and (j.get("is_offtopic") or re.search(r"не относится к теме|Не удалось связать|outside the topic|couldn't connect", j["answer"])))
            else [f"intent={j['intent']}", "искал источники" if has_sources(j) else ""]),
    }

    lines = [f"# Поведение чат-бота на вопросах разных видов ({datetime.now():%Y-%m-%d %H:%M})", "",
             f"LLM: {'подключён' if llm_on else 'не подключён'}; внешний поиск: {'включён' if use_ext else 'выключен'}.", "",
             "| Вид вопросов | Вопросов | Прошли | Доля | Что проверяется |", "|---|---|---|---|---|"]
    notes, examples, failures = [], [], []
    descr = {
        "smalltalk": "ответ мгновенный, без поиска и источников, не «ничего не найдено»",
        "capabilities": "описание возможностей и ограничений, без источников",
        "offtopic": "вежливо объясняет, что вопрос вне темы; публикации не ищет",
        "unanswerable": "нет выдуманных чисел; признаёт отсутствие информации или даёт ссылку на источник",
    }
    for cat in ("smalltalk", "capabilities", "offtopic", "unanswerable"):
        qs = spec.get(cat, [])
        passed = 0
        refused = suspicious = 0
        for q in qs:
            j, dt, code = ask(q)
            if code != 200:
                failures.append((cat, q, [f"HTTP {code}"], ""))
                continue
            if cat == "unanswerable":
                nums = unsupported_numbers(db, j, q)
                is_refusal = bool(_REFUSAL_RE.search(j["answer"]))
                cited = bool(j["citations"])
                refused += is_refusal
                suspicious += bool(nums)
                reasons = []
                if nums:
                    reasons.append("числа без источника: " + ", ".join(nums[:5]))
                if not is_refusal and not cited:
                    reasons.append("не признал отсутствие информации и не дал ссылок")
                if j["intent"] != "domain":
                    reasons.append(f"intent={j['intent']}")
            else:
                reasons = [r for r in checks[cat](j, dt, q) if r]
            if not reasons:
                passed += 1
            else:
                failures.append((cat, q, reasons, j["answer"][:140].replace("\n", " ")))
            if len(examples) < 8 and cat in ("smalltalk", "capabilities", "offtopic") and not any(e[0] == cat for e in examples):
                examples.append((cat, q, j["answer"]))
            if cat == "unanswerable":
                notes.append((q, [x["title"][:60] for x in j["sources"][:2]], is_refusal, bool(nums), cited))
        n = max(len(qs), 1)
        results[cat] = (len(qs), passed)
        lines.append(f"| {cat} | {len(qs)} | {passed} | {100 * passed // n}% | {descr[cat]} |")
        if cat == "unanswerable" and qs:
            lines.append(f"| (из них признали отсутствие информации) | {len(qs)} | {refused} | {100 * refused // n}% | доля честных отказов |")
            lines.append(f"| (из них с числами без источника) | {len(qs)} | {suspicious} | {100 * suspicious // n}% | подозрение на выдумку, меньше — лучше |")

    if failures:
        lines += ["", "## Не прошли", ""]
        for cat, q, reasons, ans in failures:
            lines.append(f"- [{cat}] **{q}**: {'; '.join(reasons)}" + (f"\n  ответ: {ans}…" if ans else ""))
    if notes:
        lines += ["", "## Вопросы без ответа: что нашлось в локальной базе", "",
                  "Чтобы оценить, можно ли было ответить: если в базе нашлось что-то по делу, бот вправе был дать ответ со ссылкой.", "",
                  "| вопрос | ближайшие публикации в базе | отказ | числа без источника | ссылки |", "|---|---|---|---|---|"]
        for q, titles, refusal, nums, cited in notes:
            lines.append(f"| {q[:70]} | {'; '.join(titles) or '—'} | {'да' if refusal else 'нет'} | {'ДА' if nums else 'нет'} | {'да' if cited else 'нет'} |")
    if examples:
        lines += ["", "## Примеры ответов", ""]
        for cat, q, ans in examples:
            lines += [f"**{cat}: «{q}»**", "", ans, ""]
    if not llm_on:
        lines += ["", "Замечание: LLM не подключён, поэтому вопросы «unanswerable» отработали по шаблонному ответу "
                      "(он перечисляет найденное и не признаёт отсутствие информации). Для честной оценки подключите ключ LLM."]
    return "\n".join(lines)


def cmd_calibrate(db, args):
    if not embeddings.is_configured():
        print("Сервис эмбеддингов не настроен (EMBEDDINGS_API_KEY).")
        return
    gen_path = EVAL_DIR / "generated.json"
    if not gen_path.exists():
        print("Сначала: python -m scripts.eval_chat generate")
        return
    embeddings.ensure_embeddings(db, None, 500)
    gen = json.loads(gen_path.read_text(encoding="utf-8"))
    rel, non = [], []
    for g in gen:
        sims = embeddings.similarities(db, [g["question_ru"]])
        for pid, s in sims.items():
            (rel if pid == g["expected_pub_id"] else non).append(s)
    if not rel or not non:
        print("Недостаточно данных для калибровки.")
        return
    q = lambda xs, p: sorted(xs)[min(len(xs) - 1, int(p * len(xs)))]
    lo_rel, hi_non = q(rel, 0.10), q(non, 0.90)
    print(f"Схожесть с ОЖИДАЕМОЙ публикацией: медиана {statistics.median(rel):.2f}, 10-й перцентиль {lo_rel:.2f}")
    print(f"Схожесть с ПРОЧИМИ публикациями : медиана {statistics.median(non):.2f}, 90-й перцентиль {hi_non:.2f}")
    rec = (lo_rel + hi_non) / 2
    print(f"\nРекомендуемый EMBEDDINGS_MIN_SIM ≈ {rec:.2f}" + ("  (классы пересекаются — порог даст ошибки обоих видов)" if lo_rel <= hi_non else ""))


def _percentile(values, q):
    values = sorted(values)
    if not values:
        return None
    k = (len(values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def cmd_intent(db, args):
    """Точность определения намерения: каждый вопрос идёт в llm.detect_intent (кэш очищен, истории нет)."""
    from app.services import routing

    path = EVAL_DIR / "intent_questions.json"
    items = json.loads(path.read_text(encoding="utf-8"))["questions"]
    if not llm.is_configured():
        print("LLM не настроен (нет ключа в .env): detect_intent вернёт FIND_PUBLICATION на любой вопрос, "
              "поэтому точность посчитать нельзя. Задайте DEEPSEEK_API_KEY и повторите.")
        return None
    llm.clear_caches()
    rows = []
    for item in items:
        got = llm.detect_intent(item["q"], None)
        rows.append((item["q"], item["expected"], got, got == item["expected"]))
    n = len(rows)
    right = sum(1 for r in rows if r[3])
    labels = list(llm.INTENTS)
    lines = [f"# Точность определения намерений ({datetime.now():%Y-%m-%d %H:%M})", "",
             f"Провайдер: {llm.active_provider_label()}. Вопросов: {n}. **Accuracy: {right}/{n} = {right / n:.0%}**", "",
             "| Вопрос | Ожидалось | Получено | Верно |", "|---|---|---|---|"]
    for q, exp, got, ok in rows:
        lines.append(f"| {q} | {exp} | {got} | {'да' if ok else '**нет**'} |")
    lines += ["", "## По меткам", "", "| Метка | Вопросов | Предсказано | Точность (precision) | Полнота (recall) |", "|---|---|---|---|---|"]
    for lab in labels:
        exp_n = sum(1 for r in rows if r[1] == lab)
        pred_n = sum(1 for r in rows if r[2] == lab)
        tp = sum(1 for r in rows if r[1] == lab and r[2] == lab)
        prec = f"{tp / pred_n:.2f}" if pred_n else "—"
        rec = f"{tp / exp_n:.2f}" if exp_n else "—"
        lines.append(f"| {lab} | {exp_n} | {pred_n} | {prec} | {rec} |")
    lines += ["", "## Матрица ошибок (строки — ожидалось, столбцы — получено)", "",
              "| | " + " | ".join(labels) + " |", "|---|" + "---|" * len(labels)]
    for exp in labels:
        counts = [sum(1 for r in rows if r[1] == exp and r[2] == got) for got in labels]
        if any(counts):
            lines.append(f"| {exp} | " + " | ".join(str(c) if c else "" for c in counts) + " |")
    cmp_items = [r for r in rows if r[1] == "COMPARE_OBJECTS"]
    split = sum(1 for r in cmp_items if routing.split_compare(r[0]))
    lines += ["", f"Просьбы о сравнении, которые правила разделили на два объекта без LLM: {split} из {len(cmp_items)}."]
    return "\n".join(lines)


def cmd_scope(db, args):
    """Точность классификатора темы: каждый вопрос из eval/scope_questions.json получает ответ
    «domain» или «offtopic», сравнивается с ожидаемым; в конце — Accuracy и ошибки двух видов."""
    import urllib.request

    from app import scope_embed
    from app.services import intent as intent_mod

    spec = json.loads((EVAL_DIR / "scope_questions.json").read_text(encoding="utf-8"))
    questions = spec["questions"]
    rows = []

    client = None
    if not args.fast and not args.api:
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)

    def via_chat(q):
        body = {"message": q, "history": [], "use_external": False}
        if args.api:
            req = urllib.request.Request(args.api.rstrip("/") + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                j = json.loads(r.read().decode("utf-8"))
        else:
            j = client.post("/api/chat", json=body).json()
        return "offtopic" if (j.get("is_offtopic") or j.get("intent") == "offtopic") else "domain"

    def via_classifier(q):
        scope = llm.classify_scope(q, None)
        if scope is None:  # так же, как в чате: без решения и без терминов предметной области — вне темы
            scope = "domain" if intent_mod.looks_domain(q) else "offtopic"
        return scope

    for item in questions:
        q, expected = item["question"], item["expected"]
        t = time.perf_counter()
        try:
            got = via_classifier(q) if args.fast else via_chat(q)
        except Exception as e:  # noqa: BLE001
            got = f"ошибка: {type(e).__name__}"
        dt = (time.perf_counter() - t) * 1000
        sim = scope_embed.similarity(q) if args.fast else None
        rows.append({"q": q, "expected": expected, "got": got, "ok": got == expected, "ms": dt, "sim": sim})

    total = len(rows)
    correct = sum(r["ok"] for r in rows)
    false_refusals = [r for r in rows if r["expected"] == "domain" and r["got"] == "offtopic"]
    leaks = [r for r in rows if r["expected"] == "offtopic" and r["got"] == "domain"]
    mode = f"через API {args.api}" if args.api else ("только классификатор" if args.fast else "через /api/chat (в процессе)")
    show_sim = any(r["sim"] is not None for r in rows)

    lines = [f"# Точность классификатора темы ({mode})", "",
             f"LLM: {llm.active_provider_label() or 'не настроен'}; эмбеддинги: {embeddings.model_name() or 'не настроены'}", "",
             "| Вопрос | Ожидалось | Получено | Верно |" + (" Близость |" if show_sim else "") + " мс |",
             "|---|---|---|---|" + ("---|" if show_sim else "") + "---|"]
    for r in rows:
        sim = f" {r['sim']:.3f} |" if show_sim and r["sim"] is not None else (" — |" if show_sim else "")
        lines.append(f"| {r['q']} | {r['expected']} | {r['got']} | {'Да' if r['ok'] else 'Нет'} |{sim} {r['ms']:.0f} |")
    lines += ["", f"**Accuracy: {correct}/{total} = {100 * correct / total:.1f}%**", "",
              f"- Ложные отказы (по теме, но отказано): {len(false_refusals)}" + (": " + "; ".join(r['q'] for r in false_refusals) if false_refusals else ""),
              f"- Пропущенный оффтоп (вне темы, но принят): {len(leaks)}" + (": " + "; ".join(r['q'] for r in leaks) if leaks else "")]

    dom = [r["sim"] for r in rows if r["expected"] == "domain" and r["sim"] is not None]
    off = [r["sim"] for r in rows if r["expected"] == "offtopic" and r["sim"] is not None]
    if dom and off:
        high = _percentile(dom, 0.10)   # 90 % вопросов по теме выше этого значения
        low = _percentile(off, 0.90)    # 90 % вопросов вне темы ниже этого значения
        lines += ["", "## Пороги для .env",
                  f"- По теме: медиана {statistics.median(dom):.3f}, 10-й перцентиль {high:.3f}",
                  f"- Вне темы: медиана {statistics.median(off):.3f}, 90-й перцентиль {low:.3f}"]
        if low < high:
            lines.append(f"\nРекомендуется: `SCOPE_SIM_LOW={low:.2f}` и `SCOPE_SIM_HIGH={high:.2f}` (между ними решают ключевые слова и LLM).")
        else:
            lines.append(f"\nКлассы пересекаются (LOW {low:.2f} ≥ HIGH {high:.2f}): попробуйте `SCOPE_METHOD=top3` или добавьте эталонные фразы в `scope_embed.EXTRA_ANCHORS`.")
    elif args.fast:
        lines += ["", "Эмбеддинги не настроены (EMBEDDINGS_API_KEY) — близость не считалась; работают ключевые слова и LLM."]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["retrieval", "generate", "answers", "calibrate", "behavior", "scope", "pool", "intent"])
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--depth", type=int, default=10, help="pool: сколько первых результатов каждого метода брать в разметку")
    ap.add_argument("--min-abstract", type=int, default=300, help="generate: минимальная длина аннотации, символов")
    ap.add_argument("--llm-rewrite", action="store_true", help="добавить английскую формулировку запроса от LLM")
    ap.add_argument("--no-external", action="store_true", help="для answers/behavior: без внешнего поиска")
    ap.add_argument("--details", action="store_true", help="для retrieval: показать вопросы, на которых новый поиск хуже прежнего")
    ap.add_argument("--fast", action="store_true", help="для scope: только классификатор темы, без поиска и ответа LLM")
    ap.add_argument("--api", default=None, help="для scope: адрес запущенного сервера, например http://127.0.0.1:8001")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        report = None
        if args.mode == "retrieval":
            report = evaluate_retrieval(db, args)
        elif args.mode == "generate":
            cmd_generate(db, args)
        elif args.mode == "answers":
            report = cmd_answers(db, args)
        elif args.mode == "calibrate":
            cmd_calibrate(db, args)
        elif args.mode == "behavior":
            report = cmd_behavior(db, args)
        elif args.mode == "scope":
            report = cmd_scope(db, args)
        elif args.mode == "pool":
            cmd_pool(db, args)
        elif args.mode == "intent":
            report = cmd_intent(db, args)
        if report:
            print("\n" + report)
            EVAL_DIR.mkdir(exist_ok=True)
            path = EVAL_DIR / f"report-{args.mode}-{datetime.now():%Y%m%d-%H%M}.md"
            path.write_text(report, encoding="utf-8")
            print(f"\nОтчёт сохранён: {_rel(path)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
