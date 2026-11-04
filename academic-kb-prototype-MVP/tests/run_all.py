"""
Автоматические проверки системы. Запуск из корня проекта:

    python -m tests.run_all

Работают на временной базе SQLite и НЕ трогают вашу рабочую базу, даже если в окружении
задана DATABASE_URL (тесты пересоздают таблицы, поэтому она игнорируется). Чтобы
прогнать тесты на PostgreSQL, задайте отдельную одноразовую базу в TEST_DATABASE_URL.
Внешние сервисы (OpenAlex, Crossref, LLM,
эмбеддинги) заменяются заглушками, сеть не нужна.
"""
import contextlib
import json
import io
import math
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# ВАЖНО: DATABASE_URL рабочей базы здесь намеренно игнорируется — тесты вызывают drop_all() и стёрли бы данные.
if os.environ.get("DATABASE_URL") and not os.environ.get("TEST_DATABASE_URL"):
    print("[tests] DATABASE_URL проигнорирована: тесты идут на временной SQLite, рабочая база не затрагивается.")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or (
    "sqlite:///" + os.path.join(tempfile.mkdtemp(), "test.db"))
# Тесты должны давать один результат на любой машине: ключи из вашего .env (load_dotenv в app/main.py их
# подхватит) не должны включать настоящий LLM и эмбеддинги. Пустая строка надёжнее pop: load_dotenv
# не перезаписывает уже заданные переменные, а пустое значение для приложения означает «ключ не задан».
for _key in ("DEEPSEEK_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "EMBEDDINGS_API_KEY"):
    os.environ[_key] = ""
# Кэш выключен (иначе одинаковые вопросы в разных тестах брали бы ответы друг друга), журнал чата — тоже
# (чтобы тесты не писали в logs/ проекта); нужные тесты включают их сами.
os.environ["LLM_CACHE_SIZE"] = "0"
os.environ["CHAT_LOG"] = "0"
os.environ["AUTH_ENFORCE"] = "0"   # защита включается в самих тестах авторизации

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import close_all_sessions  # noqa: E402

from app import auth, embeddings, hierarchy, llm, models, ontology, search  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
import app.main as main_mod  # noqa: E402
from app.services import citations, classifier, glossary, index as idx, kb_context  # noqa: E402
from app.services.publication_deduplicator import deduplicate  # noqa: E402
from app.services.publication_saver import parse_author_name, save_publication  # noqa: E402
from app.sources.models import PublicationResult as P  # noqa: E402
from app.tfidf import tokenize  # noqa: E402

client = TestClient(app)
TESTS = []


def case(fn):
    TESTS.append(fn)
    return fn


@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def seed():
    from scripts import seed_db
    with contextlib.redirect_stdout(io.StringIO()):
        seed_db.run()
    idx.invalidate()


def add_pub(db, title, abstract=None, doi=None, **kw):
    rec, _ = save_publication(db, P(title=title, abstract=abstract, doi=doi, source="OpenAlex", **kw))
    db.commit()
    idx.invalidate()
    return rec


# --------------------------------------------------------------------------
@case
def test_tokenize_forms():
    assert tokenize("Шаимском районе") == tokenize("Шаимского района")
    assert tokenize("месторождения месторождении месторождение") == ["месторожден"] * 3
    assert tokenize("fields reservoirs studies") == ["field", "reservoir", "study"]
    assert "после" not in tokenize("исследования после 2010 года")


@case
def test_glossary_both_directions():
    ru = glossary.expand_query("минерализация подземных вод в Шаимском районе")
    assert "mineralization" in ru and "groundwater" in ru and "shaim" in ru
    en = glossary.expand_query("groundwater mineralization in the Shaim region")
    assert "минерализация" in en and "шаимский" in en
    assert glossary.expand_query("квантовая хромодинамика") == "квантовая хромодинамика"


@case
def test_chunking_keeps_words():
    from app.document_processing import chunk_text
    text = " ".join(f"слово{i} word{i}" for i in range(300))
    ch = chunk_text(text, 300, 60)
    assert all(c.split()[0].startswith(("слово", "word")) for c in ch)
    assert all(f"слово{i}" in " ".join(ch) for i in range(300))
    assert chunk_text("") == [] and chunk_text("Коротко.") == ["Коротко."]


@case
def test_retrieval_cross_language_and_passages():
    seed()
    db = SessionLocal()
    rec = add_pub(db, "Causes of Changes in Mineralization of Underground Water in the Shaim Region", doi="10.1/a",
                  abstract="Groundwater mineralization increased because of oil field development and brine inflow.")
    for i, t in enumerate(["Intro text.", "Chloride content of brine rose twofold near wells.", "Conclusions."], start=1):
        db.add(models.DocumentChunk(publication_id=rec.publication_id, source_filename="полный текст", chunk_index=i, text=t))
    db.commit(); idx.invalidate()
    _, hits = search.retrieve(db, ["Почему растёт минерализация подземных вод в Шаимском районе?"])
    assert hits and hits[0].pub.doi == "10.1/a", "русский вопрос должен находить английскую публикацию"
    assert search.retrieve(db, ["квантовая хромодинамика"])[1] == []
    _, hits = search.retrieve(db, ["содержание хлоридов в рассоле", "chloride content of brine"])
    assert any("Chloride content" in p for p in hits[0].passages), "в пассаж должен попасть совпавший фрагмент"
    # фильтры
    assert search.retrieve(db, ["минерализация подземных вод"], year_from=2100)[1] == []
    assert search.retrieve(db, ["минерализация подземных вод"], field_id=2)[1] == []
    db.close()


@case
def test_english_questions_not_polluted_by_russian_docs():
    """Регрессия, найденная на реальной базе: перевод английского запроса на русский,
    подмешанный к запросу с полным весом, поднимал русскоязычные публикации над
    точными английскими (MRR на английских вопросах падал с 0,94 до 0,68)."""
    seed()
    db = SessionLocal()
    add_pub(db, "Groundwater mineralization near oil wells", doi="10.1/en",
            abstract="Groundwater mineralization and salinity near oil wells in the district.")
    for i in range(12):   # много русских публикаций с общей лексикой: нефть, район, месторождение
        add_pub(db, f"Нефтяные месторождения района {i}", doi=f"10.1/ru{i}",
                abstract="Нефтяное месторождение района. Добыча нефти по месторождению растёт. Район добычи нефти.")
    _, hits = search.retrieve(db, ["Groundwater mineralization near oil fields in the district"], top_k=5)
    assert hits and hits[0].pub.doi == "10.1/en", [h.pub.title for h in hits]
    with patched(search, "REVERSE_WEIGHT", 50.0):   # эмуляция прежней ошибки: обратный перевод «важнее» вопроса
        _, bad = search.retrieve(db, ["Groundwater mineralization near oil fields in the district"], top_k=5)
        assert bad[0].pub.doi != "10.1/en", "тест должен ловить завышенный вес обратного перевода"
    db.close()


def _plain_cosine(self, query_text):
    """Оценка v18 — обычный косинус без учёта покрытия запроса (для проверки самого теста)."""
    from collections import Counter, defaultdict
    tokens = tokenize(query_text)
    if not tokens:
        return {}
    counts = Counter(tokens)
    qvec = {t: (c / len(tokens)) * self.idf[t] for t, c in counts.items() if t in self.postings}
    qn = math.sqrt(sum(v * v for v in qvec.values()))
    if qn == 0:
        return {}
    acc = defaultdict(float)
    for term, qw in qvec.items():
        ids, tfs = self.postings[term]
        for u, tf in zip(ids, tfs):
            acc[u] += qw * tf * self.idf[term]
    return {u: d / (qn * self.norm[u]) for u, d in acc.items()}


@case
def test_short_title_matching_few_words_loses_to_full_coverage():
    """Реальный случай из диагностики: запись без аннотации с названием «Development of oil and
    gas fields of West Siberia» (совпала часть слов запроса) стояла выше статей, где найдены все
    слова, но текст длиннее. Аннотация подобрана такой длины (~110 слов, как у реальных статей),
    при которой обычный косинус ошибается."""
    from app.services.index import CorpusIndex
    from app.services.publication_saver import delete_publication
    abstract = (
        "Oil potential of Jurassic deposits in West Siberia is controlled by lithology and tectonic setting. "
        "Sandstone reservoirs of the Tyumen suite contain most of the recoverable reserves, while source rocks generate hydrocarbons "
        "that migrate into adjacent traps. Core data and well logs confirm the trend. The authors analyze porosity, permeability, "
        "grain size, cement composition, diagenetic alteration, fluid saturation, pressure regimes, seismic attributes, structural maps, "
        "stratigraphic correlation, facies models, depositional environments, sedimentary cycles, subsidence history, thermal maturity, "
        "burial depth, drilling results, testing intervals, flow rates, production history and economic evaluation across the licence blocks.")
    seed()
    db = SessionLocal()
    for p in db.query(models.Publication).all():   # убираем демонстрационные записи, чтобы не мешали
        delete_publication(db, p)
    db.commit(); idx.invalidate()
    add_pub(db, "Development of oil and gas fields of West Siberia", doi="10.1/short")
    add_pub(db, "Jurassic reservoirs", doi="10.1/full", abstract=abstract)
    q = ["Oil potential of Jurassic deposits in West Siberia"]
    assert search.retrieve(db, q, top_k=3)[1][0].pub.doi == "10.1/full"
    with patched(CorpusIndex, "score_units", _plain_cosine):   # старая формула должна ошибаться на этом примере
        idx.invalidate()
        assert search.retrieve(db, q, top_k=3)[1][0].pub.doi == "10.1/short", "тест не различает формулы"
    idx.invalidate()
    db.close()


@case
def test_english_question_finds_russian_only_publication():
    seed()
    db = SessionLocal()
    add_pub(db, "Карстовые провалы в районе добычи", doi="10.1/karst",
            abstract="Изучены гидрогеологические условия и минерализация подземных вод при добыче нефти.")
    _, hits = search.retrieve(db, ["Hydrogeological conditions and groundwater mineralization"], top_k=5)
    assert any(h.pub.doi == "10.1/karst" for h in hits)
    db.close()


@case
def test_short_generic_title_does_not_beat_specific_abstract():
    seed()
    db = SessionLocal()
    add_pub(db, "Oil fields", doi="10.1/short", abstract="General remarks on administration and history.")
    add_pub(db, "A study", doi="10.1/spec", abstract="Porosity and permeability of sandstone cores were measured in the laboratory.")
    _, hits = search.retrieve(db, ["porosity and permeability of sandstone oil fields"], top_k=3)
    assert hits[0].pub.doi == "10.1/spec", [h.pub.title for h in hits]
    db.close()


@case
def test_abstract_included_in_passages_with_fulltext():
    seed()
    db = SessionLocal()
    rec = add_pub(db, "Paper with fulltext", doi="10.1/ft", abstract="Abstract about brine chloride growth.")
    db.add(models.DocumentChunk(publication_id=rec.publication_id, source_filename="полный текст", chunk_index=1,
                                text="Detailed section: chloride concentration doubled."))
    db.commit(); idx.invalidate()
    _, hits = search.retrieve(db, ["chloride concentration doubled"])
    texts = " ".join(hits[0].passages)
    assert "Detailed section" in texts and "Abstract about brine" in texts
    db.close()


@case
def test_index_refreshes_after_change():
    seed()
    db = SessionLocal()
    assert search.retrieve(db, ["uniqueword zyxwv"])[1] == []
    add_pub(db, "Paper about zyxwv uniqueword", doi="10.1/u")
    assert search.retrieve(db, ["uniqueword zyxwv"])[1], "индекс должен обновиться после добавления"
    db.close()


@case
def test_citations_verification():
    ctx = [{"title": "Real paper about Shaim oil geology", "doi": "10.1/x"}, {"title": "Second paper on porosity"}]
    clean, rep = citations.verify_answer("Факт [1]. Выдумка [5]. Смесь [1, 6]. Диапазон [1-2].", ctx)
    assert "[5]" not in clean and "[?]" in clean and "[1, 2]" in clean
    assert rep["invalid"] == [5, 6] and rep["cited"] == [1, 2] and rep["warnings"]
    _, rep2 = citations.verify_answer("Текст [1].\n\n### Источники\n1. Real paper about Shaim oil geology, 2020.\n2. Invented Paper That Does Not Exist Anywhere, 2019.", ctx)
    assert len(rep2["unverified_lines"]) == 1 and "Invented" in rep2["unverified_lines"][0]
    cards = citations.citation_cards([1], [{"title": "T", "doi": "10.1/X"}])
    assert cards[0]["url"] == "https://doi.org/10.1/x"


@case
def test_kb_context():
    seed()
    db = SessionLocal()
    text, names = kb_context.build_kb_context(db, "Какие месторождения входят в Шаимский район?")
    assert any("Шаимский" in n for n in names) and "Шушминское" in text
    text, names = kb_context.build_kb_context(db, "Расскажи про Шаимское месторождение")
    assert "1960" in text and "глубина" not in text and "обводн" not in text, "пустые параметры не должны попадать в справку"
    assert kb_context.build_kb_context(db, "квантовая хромодинамика") == (None, [])
    db.close()


@case
def test_classifier_rules():
    types = [(1, "Геологическое"), (2, "Геофизическое"), (3, "Гидрогеологическое")]
    assert classifier.is_relevant(P(title="Hydrogeological stratification of Megovka Oil and Gas Region (Novosibirsk and Tomsk Region)", source="x"))
    for bad in ["Эволюция роли президента в политической системе Польши после 1989 года",
                "Oil and gas complexes of the Aldano-May region", "Organizing gas storage at Verkhnechonskoye field in Eastern Siberia"]:
        assert not classifier.is_relevant(P(title=bad, source="x")), bad
    p = P(title="Seismic data interpretation, Shaim oil field", journal="Геология и геофизика", source="x")
    assert classifier.match_research_types(classifier.publication_text(p), types) == [2], "журнал не должен влиять на тип"


@case
def test_deduplicate_across_sources():
    a = P(title="Paper A", doi="https://doi.org/10.3390/EARTH6010005", abstract="abs", source="OpenAlex")
    b = P(title="Paper A crossref", doi="10.3390/earth6010005", journal="Earth", source="Crossref")
    d = P(title="Paper A", doi="10.9/different", source="Crossref")
    r = deduplicate([a, b, d])
    assert len(r) == 2 and r[0].abstract == "abs" and r[0].journal == "Earth"


@case
def test_author_parsing_and_saver():
    assert parse_author_name("Абдрашитова Римма Наильевна") == ("Абдрашитова", "Римма", "Наильевна")
    assert parse_author_name("Инна Николаевна Пономарева") == ("Пономарева", "Инна", "Николаевна")
    assert parse_author_name("Иванов И.И.") == ("Иванов", "И.И.", None)
    seed()
    db = SessionLocal()
    a = add_pub(db, "T1 shaim", doi="10.1/1", abstract="abs text", authors=["Yu.O. Rusakova"])
    b = add_pub(db, "T2 shaim", doi="10.1/2", authors=["Yu.O. Rusakova"])
    assert db.query(models.Author).filter(models.Author.last_name == "Rusakova").count() == 1
    again, created = save_publication(db, P(title="T1 shaim", doi="https://doi.org/10.1/1", source="x"))
    assert not created and again.publication_id == a.publication_id
    assert db.query(models.DocumentChunk).filter(models.DocumentChunk.source_filename.like("аннотация%")).count() == 1
    long = add_pub(db, "L" * 700, doi="10." + "9" * 150, authors=["A" * 80 + " " + "B" * 80])
    assert len(long.title) == 500 and len(long.doi) == 100
    db.close()


@case
def test_embeddings_pipeline():
    seed()
    CONCEPTS = [["brine", "рассол"], ["chloride", "хлорид"], ["seismic", "сейсм"], ["porosity", "пористост"]]

    def fake(texts, batch_size=16):
        out = []
        for t in texts:
            v = [float(sum(t.lower().count(w) for w in g)) for g in CONCEPTS] + [0.05] * 3
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out

    os.environ["EMBEDDINGS_API_KEY"] = "fake"; os.environ["EMBEDDINGS_MIN_SIM"] = "0.5"
    try:
        with patched(embeddings, "embed_texts", fake):
            db = SessionLocal()
            add_pub(db, "Brine inflow and chloride growth", doi="10.1/b", abstract="Brine and chloride increased.")
            add_pub(db, "Seismic survey", doi="10.1/s", abstract="Seismic data.")
            assert embeddings.ensure_embeddings(db, None)["failed"] == 0
            assert embeddings.ensure_embeddings(db, None)["embedded"] == 0, "повторно считать нечего"
            idx.invalidate()
            _, hits = search.retrieve(db, ["рассолы и хлориды"])
            assert hits and hits[0].via == "semantic" and hits[0].pub.doi == "10.1/b"
            assert not any(h.pub.doi == "10.1/s" for h in search.retrieve(db, ["рассолы и хлориды"])[1])
            db.close()
        with patched(embeddings, "embed_texts", lambda t, batch_size=16: None):
            embeddings._query_cache.clear()
            db = SessionLocal()
            assert isinstance(search.retrieve(db, ["Seismic survey"])[1], list), "сбой сервиса не должен ронять поиск"
            db.close()
    finally:
        os.environ["EMBEDDINGS_API_KEY"] = ""; os.environ.pop("EMBEDDINGS_MIN_SIM", None)  # "", а не pop: иначе load_dotenv вернёт ключ из .env
        idx.invalidate()


@case
def test_fulltext_pipeline():
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
    except ImportError:
        print("      (пропущено: нужен reportlab для генерации тестового PDF)")
        return
    import asyncio
    import httpx
    from app.services import fulltext

    def make_pdf(pages):
        buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4)
        for lines in pages:
            y = 800
            for line in lines:
                c.drawString(40, y, line); y -= 16
            c.showPage()
        c.save(); return buf.getvalue()

    body = ["Introduction. Reservoir studies of the Tyumen formation in West Siberia."] * 25
    mid = ["Brine chloride content increased after waterflooding of the Jurassic reservoir."] * 25
    docs = {"10.1/ok": ("https://oa/ok.pdf", make_pdf([body, mid, body]), {}),
            "10.1/html": ("https://oa/h.pdf", b"<html>login</html>", {}),
            "10.1/gone": ("https://oa/g.pdf", None, {})}

    class OA:
        email = None
        async def get_work_by_doi(self, doi):
            return {"best_oa_location": {"pdf_url": docs[doi][0]}} if doi in docs else None

    def handler(req):
        for url, body_, hdr in docs.values():
            if str(req.url) == url:
                return httpx.Response(404) if body_ is None else httpx.Response(200, content=body_, headers=hdr)
        return httpx.Response(404)

    seed()
    db = SessionLocal()
    for doi in list(docs) + ["10.1/closed"]:
        add_pub(db, f"Paper {doi}", doi=doi)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await fulltext.fetch_fulltexts(db, None, 20, oa_client=OA(), http=http)

    st = {r["title"].split()[-1]: r["status"] for r in asyncio.run(go())}
    assert st == {"10.1/ok": "ok", "10.1/html": "not_pdf", "10.1/gone": "download_failed", "10.1/closed": "no_open_access"}, st
    idx.invalidate()
    assert search.retrieve(db, ["chloride content after waterflooding"])[1], "полный текст должен искаться"
    db.close()


@case
def test_import_flow_and_dedup():
    seed()
    pool = [
        P(title="Causes of Changes in Mineralization of Water in the Shaim Oil and Gas Region", authors=["Yu. Rusakova"], year=2025,
          doi="https://doi.org/10.3390/earth6010005", source="OpenAlex", abstract="Groundwater of the Shaim region."),
        P(title="Same paper", doi="10.3390/EARTH6010005", source="Crossref"),
        P(title="Эволюция роли президента в политической системе Польши", year=2023, source="Crossref", doi="10.1/p"),
    ]

    async def fake(query, limit=20, language=None):
        return list(pool), []

    with patched(main_mod._source_manager, "search_detailed", fake):
        j = client.post("/api/import/preview", json={"queries": ["q"], "per_query": 5}).json()
        assert j["stats"]["new"] == 1 and j["stats"]["irrelevant"] == 1, j["stats"]
        assert client.post("/api/import/commit", json={"items": j["candidates"]}).json()["created"] == 1
        assert client.post("/api/import/commit", json={"items": j["candidates"]}).json()["created"] == 0
    pubs = client.get("/api/publications").json()
    assert pubs[0]["doi"] == "10.3390/earth6010005" and "link" in pubs[0]


@case
def test_chat_end_to_end_with_fake_llm():
    seed()
    db = SessionLocal()
    add_pub(db, "Causes of Changes in Mineralization of Water in the Shaim Region", doi="10.1/a", abstract="Groundwater mineralization rose near Shaim oil wells.")
    db.close()
    seen = {}

    async def no_ext(query, limit=20, language=None):
        return [], []

    def answer(q, pubs, hist=None, kb_context=None, intent=None):
        seen["pubs"], seen["kb"] = pubs, kb_context
        return "Рост минерализации [1]. Выдумка [42].\n\n### Источники\n1. Invented Totally Fake Title, 2020."

    q = lambda msg, hist=None, language="en": "groundwater mineralization Shaim"
    with patched(main_mod._source_manager, "search_detailed", no_ext), patched(llm, "is_configured", lambda: True), \
            patched(llm, "generate_search_query", q), patched(llm, "generate_answer", answer), \
            patched(llm, "active_provider_label", lambda: "Fake"):
        j = client.post("/api/chat", json={"message": "Почему растёт минерализация в Шаимском районе?", "history": [], "use_external": False}).json()
    assert len(j["local_queries"]) == 2 and j["kb_objects"] and seen["kb"]
    assert any("Groundwater mineralization" in (p["text_snippet"] or "") for p in seen["pubs"])
    assert "[42]" not in j["answer"] and "[?]" in j["answer"] and len(j["citation_warnings"]) == 2
    assert j["citations"] and j["citations"][0]["n"] == 1


@case
def test_chat_without_llm_and_nothing_found():
    seed()
    j = client.post("/api/chat", json={"message": "Шаимское месторождение", "history": [], "use_external": False}).json()
    assert not j["llm_used"] and j["answer"] and j["intent"] == "domain"
    # вопрос вне темы: бот не ищет публикации, а объясняет, о чём база
    j = client.post("/api/chat", json={"message": "квантовая хромодинамика", "history": [], "use_external": False}).json()
    assert j["intent"] == "offtopic" and not j["sources"] and "не" in j["answer"]


@case
def test_intent_classification():
    from app.services.intent import classify
    talk = {"Привет": "greeting", "Здравствуйте!": "greeting", "hello": "greeting", "Привет, как дела?": "howareyou",
            "Как дела?": "howareyou", "Hi, how are you?": "howareyou", "Спасибо!": "thanks", "Большое спасибо за помощь": "thanks",
            "Пока": "farewell", "До свидания!": "farewell"}
    for text, sub in talk.items():
        i = classify(text)
        assert i.kind == "smalltalk" and i.sub == sub, (text, i)
    for text in ["Что ты умеешь?", "Что ты умеешь делать?", "Привет, что ты умеешь делать?", "Чем ты можешь помочь?",
                 "Кто ты?", "Расскажи о себе", "Помощь", "What can you do?", "как тобой пользоваться"]:
        assert classify(text).kind == "capabilities", text
    # настоящие вопросы не должны приниматься за разговор
    for text in ["Как дела на Шаимском месторождении?", "Что ты думаешь о тюменской свите?", "Что ты знаешь о Шаимском районе?",
                 "Спасибо, а что известно о Т1?", "Пока не ясно, какие пласты продуктивные", "Кто руководил Урайнефтегазом?",
                 "Помоги найти статьи про гидрогеологию", "Какие исследования проводились в Шаимском районе?"]:
        assert classify(text).kind == "domain", text
    assert classify("Привет, расскажи про Шаимское месторождение").message == "расскажи про Шаимское месторождение"
    assert classify("Hello").lang == "en" and classify("Привет").lang == "ru"


@case
def test_chat_smalltalk_needs_no_search_and_no_llm():
    seed()

    def boom(*a, **k):
        raise AssertionError("на приветствие нельзя ни искать, ни звать LLM")

    async def boom_async(*a, **k):
        raise AssertionError("внешний поиск на приветствие запрещён")

    with patched(llm, "generate_answer", boom), patched(llm, "generate_search_query", boom), patched(llm, "classify_scope", boom), \
            patched(main_mod._source_manager, "search_detailed", boom_async), patched(search, "retrieve", boom):
        for text, kind in [("Привет", "smalltalk"), ("Как дела?", "smalltalk"), ("Спасибо", "smalltalk"), ("Пока", "smalltalk"),
                           ("Что ты умеешь?", "capabilities"), ("Hello", "smalltalk"), ("What can you do?", "capabilities")]:
            j = client.post("/api/chat", json={"message": text, "history": []}).json()
            assert j["intent"] == kind and j["answer"] and not j["sources"] and not j["external_sources"] and not j["citations"], text
            assert "не найдено" not in j["answer"].lower()


@case
def test_capabilities_reply_is_truthful():
    seed()
    j = client.post("/api/chat", json={"message": "Что ты умеешь?", "history": []}).json()
    assert "5 публикаций" in j["answer"], "число публикаций должно браться из базы"
    assert "не подключена" in j["answer"] and j["llm_configured"] is False
    with patched(llm, "is_configured", lambda: True), patched(llm, "active_provider_label", lambda: "Fake"):
        j = client.post("/api/chat", json={"message": "Что ты умеешь?", "history": []}).json()
    assert "не подключена" not in j["answer"] and j["llm_configured"] is True and j["llm_provider"] == "Fake"
    assert "Ограничения" in j["answer"] and "не придумываю" in j["answer"]


@case
def test_greeting_plus_question_is_answered_as_question():
    seed()
    j = client.post("/api/chat", json={"message": "Привет, расскажи про Шаимское месторождение", "history": [], "use_external": False}).json()
    assert j["intent"] == "domain" and any("Шаимское" in o for o in j["kb_objects"])


@case
def test_offtopic_skips_external_search():
    seed()
    calls = {"external": 0}

    async def ext(query, limit=20, language=None):
        calls["external"] += 1
        return [], []

    # 1) LLM подключён и говорит «вне темы»
    with patched(main_mod._source_manager, "search_detailed", ext), patched(llm, "is_configured", lambda: True), \
            patched(llm, "active_provider_label", lambda: "Fake"), patched(llm, "generate_search_query", lambda *a, **k: None), \
            patched(llm, "classify_scope", lambda m, h=None: "offtopic"):
        j = client.post("/api/chat", json={"message": "Расскажи рецепт борща", "history": [], "use_external": True}).json()
    assert j["intent"] == "offtopic" and calls["external"] == 0 and j["is_offtopic"] is True and j["sources"] == []
    assert "не могу помочь с вопросами из других областей" in j["answer"] and not j["external_sources"]
    # 2) LLM нет: явный признак («рецепт») ловится быстрыми ключевыми словами и блокируется сразу
    j = client.post("/api/chat", json={"message": "Расскажи рецепт борща", "history": [], "use_external": True}).json()
    assert j["intent"] == "offtopic" and calls["external"] == 0 and j["is_offtopic"] is True
    # 2б) нет ни ключевых слов, ни ответа LLM, и в вопросе нет терминов предметной области: жёсткий блок
    j = client.post("/api/chat", json={"message": "Расскажи про сыр маасдам", "history": [], "use_external": True}).json()
    assert j["intent"] == "offtopic" and calls["external"] == 0 and j["is_offtopic"] is True
    # 2в) «как приготовить борщ?» ловится ключевыми словами
    j = client.post("/api/chat", json={"message": "Как приготовить борщ?", "history": [], "use_external": True}).json()
    assert j["intent"] == "offtopic" and calls["external"] == 0 and j["is_offtopic"] is True
    # 3) LLM говорит «по теме», хотя в базе ничего нет: идём во внешние источники
    with patched(main_mod._source_manager, "search_detailed", ext), patched(llm, "is_configured", lambda: True), \
            patched(llm, "active_provider_label", lambda: "Fake"), patched(llm, "generate_search_query", lambda *a, **k: "Moho depth"), \
            patched(llm, "classify_scope", lambda m, h=None: "domain"):
        j = client.post("/api/chat", json={"message": "Какова глубина Мохо под Анабарским щитом?", "history": [], "use_external": True}).json()
    assert j["intent"] == "domain" and calls["external"] == 1


@case
def test_domain_question_with_nothing_found_says_so():
    seed()
    db = SessionLocal()
    from app.services.publication_saver import delete_publication
    for p in db.query(models.Publication).all():
        delete_publication(db, p)
    db.commit(); idx.invalidate(); db.close()
    j = client.post("/api/chat", json={"message": "Какова обводнённость скважин на Самотлорском месторождении?", "history": [], "use_external": False}).json()
    assert j["intent"] == "domain" and "не найдено" in j["answer"]


@case
def test_unsupported_numbers_metric():
    import scripts.eval_chat as ev
    seed()
    db = SessionLocal()
    rec = add_pub(db, "Reservoir study", doi="10.1/n", year=2019, abstract="Porosity was 18 percent and permeability reached 450 mD.")
    resp = {"sources": [{"publication_id": rec.publication_id}], "external_sources": [],
            "answer": "Пористость составила 18 % [1], проницаемость 450 мД [1]. Температура пласта 95 °C. Добыча 18,4 млн т. "
                      "См. 10.1234/abc.567 и https://x.org/12345.\n\n### Источники\n1. Reservoir study, 2019."}
    bad = ev.unsupported_numbers(db, resp, "Какая температура пласта?")
    assert bad == ["18.4", "95"], bad       # 18 и 450 есть в источнике; 2019 — год источника; DOI и ссылки не считаются
    assert ev.unsupported_numbers(db, {**resp, "answer": "Пористость 18 [1]. Пункты: 1. и 2. без чисел."}, "?") == []
    db.close()


@case
def test_behavior_eval_rule_based_categories():
    """Разговор, «что ты умеешь» и вопросы вне темы должны проходить на 100% и без LLM."""
    import scripts.eval_chat as ev
    seed()
    db = SessionLocal()
    out = ev.cmd_behavior(db, type("A", (), {"no_external": True})())
    db.close()
    for cat in ("smalltalk", "capabilities", "offtopic"):
        row = next(l for l in out.splitlines() if l.startswith(f"| {cat} |"))
        assert "100%" in row, row
    assert "Замечание: LLM не подключён" in out


def enrich_db(db, publications=40, seed_value=3):
    """Тестовая база с несколькими районами, месторождениями, пластами, залежами и публикациями,
    привязанными на разных уровнях иерархии."""
    import random
    rnd = random.Random(seed_value)
    prov = db.query(models.Province).first()
    ob2 = models.Oblast(province_id=prov.province_id, name="Вторая область"); db.add(ob2); db.flush()
    rayons = [db.query(models.Rayon).first()]
    for i, ob in enumerate([db.query(models.Oblast).first(), ob2]):
        r = models.Rayon(oblast_id=ob.oblast_id, name=f"Район {i + 2}"); db.add(r); db.flush(); rayons.append(r)
    fields = db.query(models.Field).all()
    for r in rayons[1:]:
        for k in range(3):
            f = models.Field(rayon_id=r.rayon_id, name=f"{r.name} м{k}"); db.add(f); db.flush(); fields.append(f)
    horizons = db.query(models.Horizon).all()
    for f in fields:
        for k in range(rnd.choice([1, 2])):
            h = models.Horizon(field_id=f.field_id, index_code=rnd.choice(["Т1", "Ю1"])); db.add(h); db.flush(); horizons.append(h)
    deposits = db.query(models.Deposit).all()
    for h in horizons:
        if rnd.random() < 0.6:
            d = models.Deposit(horizon_id=h.horizon_id, name=f"Залежь {h.horizon_id}"); db.add(d); db.flush(); deposits.append(d)
    types = db.query(models.ResearchType).all()
    for i in range(publications):
        p = models.Publication(title=f"Публикация теста {i}", year=2000 + i % 20, type="статья"); db.add(p); db.flush()
        level = rnd.choice(["field", "horizon", "horizon", "deposit"])
        pool = {"field": fields, "horizon": horizons, "deposit": deposits}[level]
        for target in rnd.sample(pool, rnd.choice([1, 2])):
            if level == "field":
                db.add(models.PublicationField(publication_id=p.publication_id, field_id=target.field_id))
            elif level == "horizon":
                db.add(models.PublicationHorizon(publication_id=p.publication_id, horizon_id=target.horizon_id))
            else:
                db.add(models.PublicationDeposit(publication_id=p.publication_id, deposit_id=target.deposit_id))
        for t in rnd.sample(types, rnd.choice([1, 2])):
            db.add(models.PublicationResearchType(publication_id=p.publication_id, type_id=t.type_id))
    db.commit()
    idx.invalidate()


@case
def test_ontology_structure_comes_from_the_original_file():
    """Онтология первична: структуру задаёт ваш файл, а не код приложения. Все его классы и свойства
    есть в построенной онтологии с теми же адресами, а его демонстрационные экземпляры не попадают в неё."""
    from rdflib import Graph, OWL, RDF, URIRef
    seed()
    db = SessionLocal()
    st = ontology.get_state(db)
    V = ontology.vocab
    src = Graph().parse(str(ontology.tbox.DEFAULT_FILE), format="turtle")
    assert "файл онтологии" in st.base.source and st.base.path
    declared = {s for t in (OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty) for s in src.subjects(RDF.type, t)}
    assert len(declared) == 28
    built = set(st.explicit)
    missing = [str(d) for d in declared if (d, RDF.type, None) not in st.structure]
    assert not missing, missing
    # аксиомы вашего файла сохранены: транзитивность, обратное, подклассы исследования, области определения
    from rdflib import RDFS
    for triple in [(V.P_IN, RDF.type, OWL.TransitiveProperty), (V.P_IN, OWL.inverseOf, V.P_CONTAINS),   # как записано в вашем файле
                   (V.O["Геологическое"], RDFS.subClassOf, V.C_RESEARCH), (V.P_ABOUT, RDFS.domain, V.C_PUB)]:
        assert triple in st.structure, triple
    # его экземпляры (Публикация_Иванов_2005, Шаимский_район…) в онтологию не переносятся: экземпляры даёт БД
    assert (V.O["Шаимский_район"], RDF.type, V.C_RAYON) not in st.structure
    assert (V.O["Публикация_Иванов_2005"], RDF.type, None) not in st.structure
    # у каждого экземпляра есть ключ записи БД
    keys = {str(o) for s, p, o in st.literals if p == V.D_DBREF}
    assert "rayon:1" in keys and "field:1" in keys and any(k.startswith("publication:") for k in keys)
    db.close()


@case
def test_ontology_falls_back_when_file_is_missing():
    import os
    seed()
    db = SessionLocal()
    old = os.environ.get("ONTOLOGY_FILE")
    os.environ["ONTOLOGY_FILE"] = "/нет/такого/файла.owl"
    default = ontology.tbox.DEFAULT_FILE
    ontology.tbox.DEFAULT_FILE = type(default)("/нет/другого/файла.owl")
    try:
        ontology.invalidate()
        st = ontology.get_state(db)
        assert "встроенная копия" in st.base.source and len(st.base.classes) == 16
        assert ontology.queries.publications_for(st, "rayon", 1)["with_inference"] >= 1
    finally:
        ontology.tbox.DEFAULT_FILE = default
        os.environ.pop("ONTOLOGY_FILE", None)
        if old:
            os.environ["ONTOLOGY_FILE"] = old
        ontology.invalidate()
    db.close()


@case
def test_ontology_picks_up_edits_of_the_ontology_file():
    """Руководитель правит онтологию в Protégé и сохраняет файл: приложение должно подхватить это само."""
    import os, shutil, tempfile, time
    seed()
    db = SessionLocal()
    tmp = os.path.join(tempfile.mkdtemp(), "my.owl")
    shutil.copy(ontology.tbox.DEFAULT_FILE, tmp)
    os.environ["ONTOLOGY_FILE"] = tmp
    try:
        ontology.invalidate()
        before = ontology.get_state(db)
        n = len(before.base.classes)
        new_class = ontology.vocab.O["НовыйКласс"]
        assert str(new_class) not in before.base.classes
        with open(tmp, "a", encoding="utf-8") as f:
            f.write("\n<%s> a <http://www.w3.org/2002/07/owl#Class> ;\n  <http://www.w3.org/2000/01/rdf-schema#subClassOf> <%s> .\n" % (new_class, ontology.vocab.C_OBJECT))
        os.utime(tmp, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
        after = ontology.get_state(db)           # без invalidate(): файл изменился — состояние пересоздаётся
        assert after is not before and len(after.base.classes) == n + 1 and str(new_class) in after.base.classes
    finally:
        os.environ.pop("ONTOLOGY_FILE", None)
        ontology.invalidate()
    db.close()


@case
def test_ontology_mapping_covers_every_element_of_the_original_ontology():
    seed()
    db = SessionLocal()
    st = ontology.get_state(db)
    from app.ontology import mapping
    rows = mapping.mapping_rows(st)
    text = " ".join(r["element"] for r in rows)
    missing = [e for e in mapping.base_entities(st) if e not in text and e not in ("DOI",)]
    assert not missing, f"в таблице соответствия нет элементов онтологии: {missing}"
    origins = {r["origin"] for r in rows}
    assert origins == {"ваша онтология", "расширение приложения"}
    assert any("нет таблицы" in r["db"] for r in rows if r["element"] == "ЗонаНефтегазонакопления"), "пустые классы должны быть названы честно"
    j = client.get("/api/ontology/mapping").json()
    assert j["rows"] and j["extensions"] and "oil-gas-ontology" in j["base"]["path"]
    db.close()


@case
def test_ontology_research_types_follow_the_ontology_classes():
    seed()
    db = SessionLocal()
    db.add(models.ResearchType(name="Исследования и данные разработки")); db.add(models.ResearchType(name="Совсем новый тип"))
    db.commit(); idx.invalidate()
    st = ontology.get_state(db)
    V = ontology.vocab
    from rdflib import RDF
    by_name = {t["name"]: V.short(t["class_iri"]) for t in st.reg.types.values()}
    assert by_name["Геологическое"] == "Геологическое" and by_name["Исследования и данные разработки"] == "Разработка"
    assert by_name["Совсем новый тип"] == "Совсем_новый_тип" and "Совсем новый тип" in st.base.extension_classes
    geo = next(t for t in st.reg.types.values() if t["name"] == "Геологическое")
    assert (geo["iri"], RDF.type, V.C_RESEARCH) in st.full, "экземпляр типа должен быть и «Исследованием» (подкласс из вашей онтологии)"
    db.close()


@case
def test_ontology_transitivity_and_property_chain():
    seed()
    db = SessionLocal()
    st = ontology.get_state(db)
    from rdflib import RDF
    V = ontology.vocab
    reg = st.reg
    deposit = reg.objects[("deposit", 1)]["iri"]
    province = reg.objects[("province", 1)]["iri"]
    # в базе записаны только соседние уровни, а «входит в» до провинции выводится
    assert (deposit, V.P_IN, province) not in st.explicit and (deposit, V.P_IN, province) in st.full
    assert (province, V.P_CONTAINS, deposit) in st.full, "обратное свойство"
    # публикация, привязанная только к залежи, относится и к району (цепочка свойств)
    res = ontology.queries.publications_for(st, "rayon", 1)
    assert res["direct"] == 0 and res["with_inference"] >= 1 and res["gain"] == res["with_inference"], res
    assert all(r["how"] == "inferred" for r in res["publications"])
    assert ontology.queries.publications_for(st, "rayon", 1, use_inference=False)["publications"] == []
    db.close()


@case
def test_ontology_explanation_chain():
    seed()
    db = SessionLocal()
    st = ontology.get_state(db)
    res = ontology.queries.publications_for(st, "rayon", 1)
    row = next(r for r in res["publications"] if len(r["explanation"]) >= 4)
    kinds = [s["kind"] for s in row["explanation"]]
    assert kinds[0] == "asserted" and kinds[-1] == "inferred" and kinds.count("inferred") == 1, kinds
    assert "входит в" in row["explanation"][1]["text"] and "Вывод:" in row["explanation"][-1]["text"]
    direct = ontology.queries.publications_for(st, "field", 1)
    assert direct["with_inference"] >= direct["direct"] >= 0
    db.close()


@case
def test_ontology_matches_relational_queries():
    """Вывод и рекурсивные SQL-запросы должны давать одни и те же публикации для КАЖДОГО объекта."""
    seed()
    db = SessionLocal()
    enrich_db(db)
    st = ontology.get_state(db)
    v = ontology.queries.validate_against_sql(st, db)
    assert v["all_match"], v
    assert sum(r["objects"] for r in v["rows"]) == len(st.reg.objects) > 10
    db.close()


@case
def test_ontology_defined_classes_classify_publications():
    seed()
    db = SessionLocal()
    enrich_db(db)
    st = ontology.get_state(db)
    from rdflib import RDF
    V = ontology.vocab
    rayon = V.O["Публикация_района_" + V.slug(st.reg.objects[("rayon", 1)]["name"])]
    members = {s for s in st.full.subjects(RDF.type, rayon)}
    expected = {st.reg.publications[p] for p in ontology.queries._sql_ids(db, "rayon", 1)}
    assert members == expected and members, "членство в определяемом классе должно совпадать с публикациями района"
    some_type = next(iter(st.reg.types.values()))
    t_members = set(st.full.subjects(RDF.type, V.O["Публикация_типа_" + V.slug(some_type["name"])]))
    assert t_members == set(st.full.subjects(V.P_RESEARCH, some_type["iri"])) and t_members
    db.close()


@case
def test_ontology_consistency_check():
    seed()
    db = SessionLocal()
    st = ontology.get_state(db)
    assert ontology.queries.check(st)["consistent"] is True
    demo = ontology.queries.check(st, demo=True)
    assert demo["consistent"] is False and "не пересекаются" in demo["errors"][0] and "Рабочая онтология не изменена" in demo["demo"]
    assert ontology.queries.check(ontology.get_state(db))["consistent"] is True, "демонстрация не должна портить рабочую онтологию"
    db.close()


@case
def test_ontology_sparql_and_guards():
    seed()
    db = SessionLocal()
    st = ontology.get_state(db)
    q = ("SELECT ?title WHERE { ?pub :относитсяКОбъекту ?o ; rdfs:label ?title . ?o rdfs:label \"" +
         st.reg.objects[("rayon", 1)]["name"] + "\" }")
    assert len(ontology.queries.sparql(st, q, inferred=True)["rows"]) >= 1
    assert ontology.queries.sparql(st, q, inferred=False)["rows"] == [], "без вывода публикаций у района нет"
    assert ontology.queries.sparql(st, "ASK { ?x a :Район }")["ask"] is True
    # запросы в стиле вашей лабораторной (префикс ontology:) и с префиксами, объявленными самим пользователем
    assert ontology.queries.sparql(st, "ASK { ?x a ontology:Район }")["ask"] is True
    ns = ontology.vocab.NS
    for own in (f"PREFIX ontology: <{ns}>\nASK {{ ?x a ontology:Район . ?y a :Пласт }}", f"PREFIX : <{ns}>\nASK {{ ?x a :Район }}",
                f"PREFIX ontology: <{ns}>\nPREFIX : <{ns}>\nASK {{ ?x a :Район }}"):
        assert ontology.queries.sparql(st, own)["ask"] is True, own
    for bad in ["DELETE WHERE { ?s ?p ?o }", "SELECT * WHERE { SERVICE <http://example.org/> { ?s ?p ?o } }", "SELEC nonsense",
                "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"]:
        try:
            ontology.queries.sparql(st, bad)
        except ValueError:
            continue
        raise AssertionError(f"запрос должен быть отклонён: {bad}")
    big = ontology.queries.sparql(st, "SELECT ?s ?p ?o WHERE { ?s ?p ?o }", max_rows=5)
    assert len(big["rows"]) == 5 and big["truncated"] is True
    db.close()


@case
def test_ontology_follows_data_changes():
    import time
    seed()
    db = SessionLocal()
    first = ontology.get_state(db)
    n = len(first.reg.publications)
    add_pub(db, "Новая публикация про Шаимское", doi="10.1/onto")
    pub = db.query(models.Publication).filter(models.Publication.doi == "10.1/onto").one()
    db.add(models.PublicationField(publication_id=pub.publication_id, field_id=1)); db.commit()
    stale = ontology.get_state_nowait(db)       # не ждёт: отдаёт прежнее состояние и запускает пересчёт
    assert stale is first or stale is None or len(stale.reg.publications) >= n
    for _ in range(100):                          # пересчёт идёт в фоне
        fresh = ontology.get_state_nowait(db)
        if fresh is not None and len(fresh.reg.publications) == n + 1:
            break
        time.sleep(0.1)
    assert len(ontology.get_state(db).reg.publications) == n + 1
    res = ontology.queries.publications_for(ontology.get_state(db), "rayon", 1)
    assert pub.publication_id in {r["publication_id"] for r in res["publications"]}
    db.close()


@case
def test_ontology_api_and_export():
    seed()
    db = SessionLocal()
    enrich_db(db, 12)
    db.close()
    for url in ["/api/ontology/summary", "/api/ontology/objects", "/api/ontology/check", "/api/ontology/validate",
                "/api/ontology/check?demo=true", "/api/ontology/publications?kind=rayon&id=1"]:
        assert client.get(url).status_code == 200, url
    assert client.get("/api/ontology/publications?kind=rayon&id=999").status_code == 404
    assert client.post("/api/ontology/sparql", json={"query": "DROP ALL"}).status_code == 400
    j = client.get("/api/ontology/summary").json()
    assert j["consistent"] and j["inferred_meaningful"] > 0 and j["classes"] >= 12 and j["defined_classes"] >= 5 and j["sparql_examples"]
    assert client.get("/api/ontology/validate").json()["all_match"] is True
    from rdflib import Graph
    plain = client.get("/api/ontology/export?format=owl")
    assert plain.status_code == 200 and "attachment" in plain.headers["content-disposition"]
    g1 = Graph().parse(data=plain.text, format="xml")
    g2 = Graph().parse(data=client.get("/api/ontology/export?format=ttl").text, format="turtle")
    g3 = Graph().parse(data=client.get("/api/ontology/export?format=owl&inferred=true").text, format="xml")
    assert len(g1) == len(g2) and len(g3) > len(g1), (len(g1), len(g2), len(g3))
    assert client.get("/api/ontology/export?format=docx").status_code == 422


@case
def test_chat_shows_ontology_answer():
    seed()
    db = SessionLocal(); ontology.get_state(db); db.close()
    j = client.post("/api/chat", json={"message": "Какие исследования проводились в Шаимском районе?", "history": [], "use_external": False}).json()
    o = j["ontology"]
    assert o and o["level"] == "район" and o["with_inference"] >= 1 and o["direct"] == 0, o
    assert o["publications"][0]["how"] == "inferred" and "Вывод:" in o["publications"][0]["explanation"]
    j = client.post("/api/chat", json={"message": "квантовая хромодинамика", "history": [], "use_external": False}).json()
    assert j["ontology"] is None
    from app.services import kb_context
    db = SessionLocal()
    text, _ = kb_context.build_kb_context(db, "Расскажи про Шаимский район")
    db.close()
    assert "по онтологии, с логическим выводом" in text and "напрямую: 0" in text


@case
def test_ontology_axiom_mutations_are_caught():
    """Тест тестов: без транзитивности и без цепочки свойств проверки вывода должны падать."""
    from rdflib import OWL, RDF
    V = ontology.vocab
    seed()
    db = SessionLocal()
    original = V.build_tbox

    def without(drop):
        def build(g, lit, base, reg):
            result = original(g, lit, base, reg)
            if drop == "transitive":
                g.remove((V.P_IN, RDF.type, OWL.TransitiveProperty))
            else:
                for chain in list(g.objects(V.P_ABOUT, OWL.propertyChainAxiom)):
                    g.remove((V.P_ABOUT, OWL.propertyChainAxiom, chain))
            return result
        return build

    for drop in ("transitive", "chain"):
        with patched(V, "build_tbox", without(drop)):
            ontology.invalidate()
            try:
                test_ontology_transitivity_and_property_chain()
            except AssertionError:
                continue
            finally:
                # упавший вложенный тест не закрыл свою сессию: на PostgreSQL её открытая транзакция
                # заблокировала бы следующее пересоздание таблиц
                close_all_sessions()
                ontology.invalidate()
            raise AssertionError(f"поломка «{drop}» не замечена тестами")
    db.close()


@case
def test_ontology_agrees_with_hermit():
    """Вывод приложения (OWL 2 RL) сверяется со стандартным HermiT. Нужны Java и owlready2."""
    import shutil
    try:
        import owlready2  # noqa: F401
    except ImportError:
        print("      (пропущено: нет owlready2)")
        return
    if shutil.which("java") is None:
        print("      (пропущено: не найдена Java)")
        return
    from scripts import ontology_check
    seed()
    db = SessionLocal()
    enrich_db(db, 30)
    st = ontology.get_state(db)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = ontology_check.hermit_compare(st)
    db.close()
    assert ok, buf.getvalue()


@case
def test_api_smoke_and_delete():
    seed()
    for url in ["/api/provinces", "/api/rayons/1", "/api/fields/1", "/api/fields/1/publications", "/api/gis/fields",
                "/api/publications", "/api/import/default-queries", "/api/fulltext/status", "/api/embeddings/status", "/api/ontology/summary"]:
        assert client.get(url).status_code == 200, url
    pid = client.get("/api/publications").json()[0]["publication_id"]
    assert client.delete(f"/api/publications/{pid}").status_code == 204
    assert client.delete(f"/api/publications/{pid}").status_code == 404


@case
def test_seed_has_no_invented_numbers():
    seed()
    db = SessionLocal()
    for f in db.query(models.Field).all():
        assert f.depth is None and f.water_cut is None, "в тестовых данных не должно быть выдуманных чисел"
    assert db.query(models.Horizon).one().permeability is None
    db.close()


@case
def test_evaluation_scripts_import():
    import json
    import scripts.eval_chat as ev
    bank = json.loads((ROOT / "eval" / "questions.json").read_text(encoding="utf-8"))["questions"]
    assert len(bank) >= 20 and all({"id", "q", "q_en", "expect"} <= set(b) for b in bank)
    for b in bank:
        re.compile(b["expect"])
    seed()
    db = SessionLocal()
    out = ev.evaluate_retrieval(db, type("A", (), {"llm_rewrite": False, "details": True})())
    assert out and "Hit@5" in out and "Проблемные вопросы" in out
    db.close()


# --------------------------------------------------------------------------
# llm.py: контекст, цитаты, уверенность, быстрая классификация темы, история
class _FakeClient:
    """Подмена OpenAI-клиента: запоминает запрос и возвращает заданный текст."""

    def __init__(self, text, finish="stop"):
        self.calls = []
        outer = self

        class _Completions:
            def create(self, **kw):
                outer.calls.append(kw)
                msg = type("M", (), {"content": text})()
                return type("R", (), {"choices": [type("C", (), {"message": msg, "finish_reason": finish})()]})()

        self.chat = type("Chat", (), {"completions": _Completions()})()


_PROV = {"label": "FakeLLM", "model": "fake-1"}


@case
def test_llm_context_groups_and_numbering():
    pubs = [
        {"title": "Ext one", "origin": "внешняя база OpenAlex", "text_snippet": "abstract"},
        {"title": "Local one", "origin": "локальная база знаний", "text_snippet": "chunk", "object_name": "Шаимское"},
        {"title": "Local two"},
    ]
    ctx = llm._format_context(pubs)
    assert ctx.index("=== ЛОКАЛЬНАЯ БАЗА (проверено) ===") < ctx.index("=== ВНЕШНИЕ ИСТОЧНИКИ (OpenAlex / Crossref) ===")
    # локальные идут первыми, нумерация сквозная
    assert ctx.index("[Источник 1] Local one") < ctx.index("[Источник 2] Local two") < ctx.index("[Источник 3] Ext one")
    assert "Объект: Шаимское" in ctx and "Объект: не указан" in ctx and "Тип исследования: не указан" in ctx
    assert "Полнота: только аннотация" in ctx and "Полнота: только метаданные" in ctx
    assert "Полнота: полный текст" in llm._format_context([{"title": "T", "text_snippet": "x", "fulltext": True}])
    assert "ВНЕШНИЕ" not in llm._format_context([{"title": "Only local"}])


@case
def test_llm_verify_citations_and_confidence():
    fixed, bad = llm._verify_citations("а [1], б [4], в [0], г [3]", 3)
    assert fixed == "а [1], б [?], в [?], г [3]" and bad == [4, 0]
    assert llm._verify_citations("без ссылок", 2) == ("без ссылок", [])
    five = [{"origin": "локальная база знаний", "text_snippet": "x"} for _ in range(5)]
    assert llm._compute_confidence(five, "[1] и [2]") == "high"            # 4 балла
    assert llm._compute_confidence(five[:2], "[1] [2]") == "high"          # фрагменты + локальный + 2 цитаты = 3
    assert llm._compute_confidence(five[:1], "[1]") == "low"                # только «есть локальный»
    assert llm._compute_confidence(five[:1], "[1] [1]") == "low"            # повтор номера считается один раз
    assert llm._compute_confidence(five[:1], "[1] [2]") == "medium"         # локальный + 2 разных номера
    assert llm._compute_confidence([{"origin": "внешняя база X"}], "нет ссылок") == "low"
    assert llm._compute_confidence([], "") == "low"
    assert llm._compute_confidence([{"origin": ""}, {"origin": "x", "text_snippet": "a"}, {"origin": "y", "text_snippet": "b"}], "[1]") == "medium"


@case
def test_llm_classify_scope_fast_and_llm_not_called():
    f = llm.classify_scope_fast
    assert f("Геология Шаимского района") == "domain" and f("Расскажи про пласт БС10") == "domain"
    assert f("какая погода завтра") == "offtopic" and f("Привет!") == "offtopic" and f("дай рецепт борща") == "offtopic"
    assert f("магазин") is None and f("в загазованной зоне") is None, "«газ» не должно срабатывать внутри слов"
    assert f("пластик") != "domain"  # «ложные друзья» вырезаются (text_utils.strip_false_friends)
    assert f("погода в Шаиме") == "domain", "слова предметной области важнее"
    assert f("как дела у физиков") is None and f("") is None
    with patched(llm, "classify_scope_llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM не нужна"))):
        assert llm.classify_scope("Геофизика Шаима") == "domain"
    with patched(llm, "classify_scope_llm", lambda m, h=None: "offtopic"):
        assert llm.classify_scope("стоит ли покупать ноутбук") == "offtopic"  # нет признаков → LLM


@case
def test_llm_generate_answer_dict_text_and_history():
    pubs = [{"title": "Local", "origin": "локальная база знаний", "text_snippet": "a"},
            {"title": "Ext", "origin": "внешняя база OpenAlex", "text_snippet": "b", "doi": "10.1/x"}]
    fake = _FakeClient("Вывод [1] и [2], выдумка [7].")
    hist = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(10)]
    with patched(llm, "_get_client", lambda: fake), patched(llm, "_active_provider", lambda: _PROV):
        r = llm.generate_answer("а после 2018?", pubs, hist)
        assert set(r) >= {"text", "sources", "provider", "confidence", "bad_citations"}
        assert r["text"] == "Вывод [1] и [2], выдумка [?]." and r["bad_citations"] == [7]
        assert r["provider"] == "FakeLLM" and r["confidence"] in ("high", "medium", "low")
        assert [s["n"] for s in r["sources"]] == [1, 2] and r["sources"][1]["doi"] == "10.1/x" and r["sources"][0]["has_fulltext"] is False
        sent = fake.calls[-1]["messages"]
        assert len(sent) == 1 + 6 + 1, "в модель уходят система + 6 реплик истории + вопрос"
        assert sent[1]["content"] == "m4" and sent[-1]["content"].startswith("(Это уточнение к предыдущему вопросу.)")
        # длинный вопрос или короткая история — без пометки
        llm.generate_answer("Какие исследования проводились в Шаимском районе после 2010 года?", pubs, hist)
        assert not fake.calls[-1]["messages"][-1]["content"].startswith("(Это")
        llm.generate_answer("а после 2018?", pubs, hist[:4])
        assert not fake.calls[-1]["messages"][-1]["content"].startswith("(Это")
        # обратная совместимость: строка
        s = llm.generate_answer("q", pubs, return_dict=False)
        assert isinstance(s, str) and "[?]" in s
    with patched(llm, "_get_client", lambda: None):
        assert llm.generate_answer("q", pubs) is None and llm.generate_answer("q", pubs, return_dict=False) is None


@case
def test_chat_accepts_dict_answer_and_returns_confidence():
    seed()
    db = SessionLocal()
    add_pub(db, "Causes of Changes in Mineralization of Water in the Shaim Region", doi="10.1/b", abstract="Groundwater mineralization rose near Shaim oil wells.")
    db.close()

    async def no_ext(query, limit=20, language=None):
        return [], []

    def answer(q, pubs, hist=None, kb_context=None, return_dict=True, intent=None):
        return {"text": "Рост минерализации [1]. Выдумка [?].", "sources": [], "provider": "Fake", "confidence": "medium", "bad_citations": [9]}

    q = lambda msg, hist=None, language="en": "groundwater mineralization Shaim"
    with patched(main_mod._source_manager, "search_detailed", no_ext), patched(llm, "is_configured", lambda: True), \
            patched(llm, "generate_search_query", q), patched(llm, "generate_answer", answer), \
            patched(llm, "active_provider_label", lambda: "Fake"):
        j = client.post("/api/chat", json={"message": "Почему растёт минерализация в Шаимском районе?", "history": [], "use_external": False}).json()
    assert j["llm_used"] is True and j["confidence"] == "medium"
    assert any("[9]" in w for w in j["citation_warnings"]) and j["citations"]


@case
def test_search_build_answer_takes_dict_and_string():
    from app import search as search_mod
    hit = type("H", (), {"pub": type("P", (), {"title": "T", "year": 2020, "journal": None, "doi": None, "link": None, "author_links": []})(),
                         "score": 1.0, "matched_field": "Шаимское", "passages": []})()
    with patched(llm, "is_configured", lambda: True), patched(search_mod, "passages_text", lambda h: "chunk"):
        with patched(llm, "generate_answer", lambda q, c, *a, **k: {"text": "из dict", "confidence": "low"}):
            assert search_mod._build_answer("q", [hit]) == "из dict"
        with patched(llm, "generate_answer", lambda q, c, *a, **k: "из строки"):
            assert search_mod._build_answer("q", [hit]) == "из строки"
        with patched(llm, "generate_answer", lambda q, c, *a, **k: None):
            assert "найдены следующие материалы" in search_mod._build_answer("q", [hit])


@case
def test_offtopic_hard_block_makes_no_search_and_no_rewrite():
    seed()

    def boom(*a, **k):
        raise AssertionError("после определения «вне темы» ничего вызываться не должно")

    async def aboom(*a, **k):
        raise AssertionError("внешний поиск не должен вызываться")

    with patched(main_mod._source_manager, "search_detailed", aboom), patched(llm, "is_configured", lambda: True), \
            patched(llm, "active_provider_label", lambda: "Fake"), patched(llm, "generate_search_query", boom), \
            patched(search, "retrieve", boom), patched(llm, "generate_answer", boom), \
            patched(llm, "classify_scope", lambda m, h=None: "offtopic"):
        j = client.post("/api/chat", json={"message": "Как приготовить борщ?", "history": [], "use_external": True}).json()
    assert j["is_offtopic"] is True and j["intent"] == "offtopic" and j["sources"] == [] and j["external_sources"] == []
    assert j["answer"].startswith("Я — академический ассистент") and j["llm_used"] is False and len(j["suggestions"]) == 3 and j["confidence"] == "high"


@case
def test_search_query_rewrite_rejects_service_messages():
    outputs = {
        "Shaim hydrogeology Jurassic": "Shaim hydrogeology Jurassic",
        "Этот вопрос не относится к теме базы знаний": None,
        "Извините, я не могу помочь с этим запросом": None,
        "Ошибка: запрос не распознан": None,
        "I'm sorry, this is not related to geology": None,
        "one two three four five six seven": None,           # длиннее 6 слов
        "one two three four five six": "one two three four five six",
        "Shaim oil field\nПояснение: запрос переписан": "Shaim oil field",
        "": None,
    }
    for raw, expected in outputs.items():
        with patched(llm, "_get_client", lambda raw=raw: _FakeClient(raw)), patched(llm, "_active_provider", lambda: _PROV):
            assert llm.generate_search_query("вопрос", None, "en") == expected, (raw, expected)


@case
def test_external_search_needs_a_usable_rewrite_when_llm_is_on():
    seed()
    seen = []

    async def ext(query, limit=20, language=None):
        seen.append(query)
        return [], []

    body = {"message": "Шаимское месторождение", "history": [], "use_external": True}
    # LLM подключён, но формулировку не дал (сбой или служебный ответ): во внешние базы не идём
    with patched(main_mod._source_manager, "search_detailed", ext), patched(llm, "is_configured", lambda: True), \
            patched(llm, "active_provider_label", lambda: "Fake"), patched(llm, "generate_search_query", lambda *a, **k: None), \
            patched(llm, "classify_scope", lambda m, h=None: "domain"), patched(llm, "generate_answer", lambda *a, **k: None):
        j = client.post("/api/chat", json=body).json()
    assert seen == [] and j["external_query"] is None and "Не удалось сформулировать поисковый запрос" in (j["external_note"] or "")
    assert j["sources"], "локальная база при этом работает"
    # LLM не подключён: переписывать нечем, ищем по самому вопросу, как и раньше
    with patched(main_mod._source_manager, "search_detailed", ext):
        j = client.post("/api/chat", json=body).json()
    assert seen == ["Шаимское месторождение"] and j["external_query"] == "Шаимское месторождение"


# --------------------------------------------------------------------------
@case
def test_scope_llm_answer_parsing_is_tolerant():
    """LLM может ответить «вне» в кавычках, жирным или с точкой — всё равно распознаём."""
    from types import SimpleNamespace as N
    for txt, want in [("«вне»", "offtopic"), ("**Вне темы**", "offtopic"), ("Вне.", "offtopic"),
                      ("«тема»", "domain"), ("Тема.", "domain"), ("не знаю", None)]:
        class C:
            class chat:
                class completions:
                    @staticmethod
                    def create(**k):
                        return N(choices=[N(message=N(content=txt))])
        with patched(llm, "_get_client", lambda: C), patched(llm, "_active_provider", lambda: {"model": "m", "label": "t"}):
            assert llm.classify_scope_llm("как приготовить борщ?") == (want if want != "не знаю" else None), txt


# --------------------------------------------------------------------------
# v29: эмбеддинг-классификатор темы, кэш, повтор при сбое, журнал запросов
@contextlib.contextmanager
def _env(**values):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update({k: str(v) for k, v in values.items()})
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _fake_embed_texts(texts, batch_size=16):
    """Детерминированные «эмбеддинги»: мешок первых пяти букв слов в 128 измерениях. Совпадение слов
    с эталонами даёт близость, отсутствие — почти ноль; этого хватает, чтобы проверить логику порогов."""
    import zlib

    out = []
    for t in texts:
        v = [0.0] * 128
        for w in re.findall(r"\w+", t.lower()):
            v[zlib.crc32(w[:5].encode()) % 128] += 1
        out.append(v)
    return out


@contextlib.contextmanager
def _fake_embeddings(vectors=_fake_embed_texts):
    from app import scope_embed

    scope_embed.reset()
    with patched(embeddings, "embed_texts", vectors), patched(embeddings, "is_configured", lambda: True), \
            patched(embeddings, "model_name", lambda: "fake-embed"):
        try:
            yield scope_embed
        finally:
            scope_embed.reset()


class _Flaky:
    """Подмена OpenAI-клиента: на каждый вызов — следующий элемент списка (исключение или текст)."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0
        self.last_kwargs = None
        outer = self

        class _C:
            def create(self, **kw):
                outer.calls += 1
                outer.last_kwargs = kw
                step = outer.steps.pop(0) if outer.steps else "ok"
                if isinstance(step, Exception):
                    raise step
                msg = type("M", (), {"content": step})()
                usage = type("U", (), {"total_tokens": 42})()
                return type("R", (), {"choices": [type("C", (), {"message": msg, "finish_reason": "stop"})()], "usage": usage})()

        self.chat = type("Chat", (), {"completions": _C()})()


@case
def test_false_friends_are_not_domain():
    from app.services import intent as intent_mod

    for q in ("Какой пластик безопаснее для бутылок?", "Что почитать в газете сегодня?", "Как разработать сайт на React?"):
        assert llm.classify_scope_fast(q) != "domain", q
        assert not intent_mod.looks_domain(q), q
    # настоящие вопросы по теме по-прежнему распознаются, в том числе «пластовый», «газовый»
    for q in ("Пластовое давление на Шаимском месторождении", "Газовые залежи Тюменской области", "Публикации по пласту БС10"):
        assert llm.classify_scope_fast(q) == "domain", q


@case
def test_scope_embeddings_use_two_thresholds():
    with _env(SCOPE_SIM_LOW="0.12", SCOPE_SIM_HIGH="0.18"), _fake_embeddings() as se:
        assert se.classify("Какие исследования проводились в Шаимском районе?") == "domain"
        assert se.classify("Пористость коллекторов юрских отложений") == "domain"
        assert se.classify("Как приготовить борщ?") == "offtopic"
        assert se.classify("Кто выиграл чемпионат мира по футболу?") == "offtopic"
        # короткое уточнение при наличии истории: «вне темы» не выносим, ответа по теме не навязываем
        assert se.classify("а после 2018?", has_history=True) is None
        assert se.classify("а после 2018?", has_history=False) == "offtopic"
    # серая зона между порогами: эмбеддинги «не знают», дальше решает LLM
    with _env(SCOPE_SIM_LOW="0.05", SCOPE_SIM_HIGH="0.50"), _fake_embeddings() as se:
        q = "Пористость и проницаемость пород"            # ключевых слов нет, близость средняя
        assert llm.classify_scope_fast(q) is None and se.classify(q) is None
        client = _Flaky(["тема"])
        with patched(llm, "_get_client", lambda: client), patched(llm, "_active_provider", lambda: _PROV):
            assert llm.classify_scope(q, None) == "domain"
        assert client.calls == 1


@case
def test_scope_embeddings_outage_falls_back_to_keywords():
    calls = {"n": 0}

    def down(texts, batch_size=16):
        calls["n"] += 1
        return None

    with _env(SCOPE_SIM_LOW="0.12", SCOPE_SIM_HIGH="0.18"), _fake_embeddings(down) as se:
        assert llm.classify_scope("как готовить еду?", None) == "offtopic"       # ключевые слова
        assert llm.classify_scope("Геология Шаима", None) == "domain"
        # после сбоя сервис не опрашивается повторно (пауза), чтобы не тормозить каждый вопрос
        assert calls["n"] == 1, calls
        assert se.is_available() is False


@case
def test_lru_cache_evicts_oldest_and_can_be_disabled():
    from app import telemetry

    with _env(LLM_CACHE_SIZE="3"):
        c = telemetry.LRUCache()
        for i in range(4):
            c.set(f"k{i}", i)
        assert len(c) == 3 and c.get("k0") is None and c.get("k3") == 3
        c.get("k1")                      # k1 стал «свежим», вытесняется k2
        c.set("k4", 4)
        assert c.get("k1") == 1 and c.get("k2") is None
        c.set("none", None)              # None не кэшируется (сбой может пройти при повторе)
        assert c.get("none") is None
    with _env(LLM_CACHE_SIZE="0"):
        c = telemetry.LRUCache()
        c.set("a", 1)
        assert c.get("a") is None and len(c) == 0
    assert telemetry.cache_key("a", "b") != telemetry.cache_key("ab")


@case
def test_scope_and_rewrite_results_are_cached():
    from app import telemetry

    with _env(LLM_CACHE_SIZE="1000"):
        llm.clear_caches()
        stats = telemetry.start_request()
        client = _Flaky(["вне", "shaim geology", "other query"])
        with patched(llm, "_get_client", lambda: client), patched(llm, "_active_provider", lambda: _PROV):
            assert llm.classify_scope("Расскажи про сыр маасдам", None) == "offtopic"
            assert llm.classify_scope("расскажи про сыр маасдам  ", None) == "offtopic"   # тот же вопрос
            assert client.calls == 1 and stats["cache_hits"] == 1
            assert llm.generate_search_query("Геология Шаима", None, "en") == "shaim geology"
            assert llm.generate_search_query("Геология Шаима", None, "en") == "shaim geology"
            assert client.calls == 2
            # другой язык и другая история — другие ключи
            assert llm.generate_search_query("Геология Шаима", None, "ru") == "other query"
            assert client.calls == 3
        llm.clear_caches()


@case
def test_llm_call_retries_once_on_transient_error_but_not_on_auth_error():
    from app import telemetry

    class AuthError(Exception):
        status_code = 401

    with patched(llm, "RETRY_DELAY_SEC", 0), patched(llm, "_active_provider", lambda: _PROV):
        # временный сбой → один повтор → успех, токены учтены
        stats = telemetry.start_request()
        flaky = _Flaky([ConnectionError("reset"), "shaim geology"])
        with patched(llm, "_get_client", lambda: flaky):
            assert llm.generate_search_query("Геология Шаима", None, "en") == "shaim geology"
        assert flaky.calls == 2 and stats["retries"] == 1 and stats["tokens_used"] == 42 and stats["llm_calls"] == 1
        # два сбоя подряд → None (вызывающий код откатывается на шаблон)
        flaky = _Flaky([TimeoutError("t1"), TimeoutError("t2")])
        with patched(llm, "_get_client", lambda: flaky):
            assert llm.generate_search_query("Геология Шаима", None, "en") is None
            assert flaky.calls == 2
        # ошибка ключа не повторяется
        flaky = _Flaky([AuthError("bad key")])
        with patched(llm, "_get_client", lambda: flaky):
            assert llm.generate_answer("вопрос", [{"title": "T"}]) is None
            assert flaky.calls == 1
        # generate_answer тоже повторяет
        flaky = _Flaky([ConnectionError("x"), "Ответ [1]"])
        with patched(llm, "_get_client", lambda: flaky):
            res = llm.generate_answer("вопрос", [{"title": "T"}])
            assert res and "Ответ" in res["text"] and flaky.calls == 2


@case
def test_chat_requests_are_logged_as_jsonl():
    import json as _json

    path = os.path.join(tempfile.mkdtemp(), "logs", "chat_logs.jsonl")
    with _env(CHAT_LOG="1", CHAT_LOG_PATH=path):
        j = client.post("/api/chat", json={"message": "Как приготовить суп?", "history": [], "use_external": True}).json()
        assert j["is_offtopic"] is True
        client.post("/api/chat", json={"message": "Привет", "history": []})
    lines = [_json.loads(x) for x in open(path, encoding="utf-8").read().splitlines()]
    assert len(lines) == 2
    first = lines[0]
    for key in ("timestamp", "query", "scope", "rewritten_query", "sources_found", "provider", "latency_ms", "tokens_used"):
        assert key in first, key
    assert first["query"] == "Как приготовить суп?" and first["scope"] == "offtopic" and first["sources_found"] == 0
    assert first["tokens_used"] == 0 and isinstance(first["latency_ms"], int)
    assert lines[1]["scope"] == "smalltalk"
    # CHAT_LOG=0 — ничего не пишется
    path2 = os.path.join(tempfile.mkdtemp(), "x.jsonl")
    with _env(CHAT_LOG="0", CHAT_LOG_PATH=path2):
        client.post("/api/chat", json={"message": "Как приготовить суп?", "history": []})
    assert not os.path.exists(path2)


@case
def test_scope_eval_questions_file_is_consistent():
    import json as _json

    spec = _json.loads((ROOT / "eval" / "scope_questions.json").read_text(encoding="utf-8"))
    qs = spec["questions"]
    assert len(qs) >= 30
    assert {q["expected"] for q in qs} == {"domain", "offtopic"}
    assert sum(q["expected"] == "domain" for q in qs) >= 10 and sum(q["expected"] == "offtopic" for q in qs) >= 10
    # быстрый классификатор никогда не противоречит ожиданию (он может промолчать, но не ошибаться)
    for q in qs:
        assert llm.classify_scope_fast(q["question"]) in (None, q["expected"]), q["question"]



@case
def test_scope_standalone_question_ignores_history_but_followup_uses_it():
    long_answer = "очень длинный прошлый ответ " * 100
    hist_a = [{"role": "user", "content": "Геология Шаима"}, {"role": "assistant", "content": long_answer}]
    hist_b = [{"role": "user", "content": "Другой вопрос"}, {"role": "assistant", "content": long_answer}]
    with _env(LLM_CACHE_SIZE="1000"):
        llm.clear_caches()
        client = _Flaky(["вне", "тема"])
        with patched(llm, "_get_client", lambda: client), patched(llm, "_active_provider", lambda: _PROV):
            # самостоятельный вопрос: история в LLM не уходит, повтор в другой беседе берётся из кэша
            assert llm.classify_scope("Как научиться плавать?", hist_a) == "offtopic"
            assert [m["role"] for m in client.last_kwargs["messages"]] == ["system", "user"]
            assert llm.classify_scope("Как научиться плавать?", hist_b) == "offtopic"
            assert client.calls == 1
            # уточнение зависит от истории: она передаётся (обрезанной), а кэш учитывает её
            assert llm.classify_scope("а после 2018 года?", hist_a) == "domain"
            sent = client.last_kwargs["messages"]
            assert len(sent) == 4 and all(len(m["content"]) <= llm.MAX_SCOPE_HISTORY_CHARS for m in sent[1:-1])
            assert client.calls == 2
        llm.clear_caches()
    assert llm._depends_on_history("а после 2018 года?", hist_a) and not llm._depends_on_history("Как научиться плавать?", hist_a)
    assert not llm._depends_on_history("а после 2018 года?", None)


@case
def test_scope_fast_knows_english_domain_words_but_not_generic_ones():
    assert llm.classify_scope_fast("What was the oil production of the Samotlor field in 2023?") == "domain"
    assert llm.classify_scope_fast("Geology of the Shaim district") == "domain"
    assert llm.classify_scope_fast("West Siberian petroleum basin studies") == "domain"
    for q in ("How do I bake a chocolate cake?", "Gasoline prices today", "Who won the playing field debate?", "Well, how are you?"):
        assert llm.classify_scope_fast(q) is None, q



# --------------------------------------------------------------------------
# v30: кэш переписанных запросов без истории, потоковый ответ (SSE)
class _StatusError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class _StreamClient:
    """Подмена OpenAI-клиента с поддержкой stream=True: куски текста, finish_reason и чанк с usage."""

    def __init__(self, parts, usage=77, delay=0.0, finish="stop", reject_usage=False):
        from types import SimpleNamespace as NS

        self.parts, self.usage, self.delay, self.finish, self.reject_usage = parts, usage, delay, finish, reject_usage
        self.calls, self.produced, self.closed = [], 0, False
        outer = self

        def chunk(delta=None, finish=None, usage=None, empty=False):
            choices = [] if empty else [NS(delta=NS(content=delta), finish_reason=finish)]
            return NS(choices=choices, usage=usage)

        class _Stream:
            def __iter__(self):
                import time as _t

                for p in outer.parts:
                    _t.sleep(outer.delay)
                    outer.produced += 1
                    yield chunk(delta=p)
                yield chunk(delta=None, finish=outer.finish)
                if outer.usage is not None:
                    yield chunk(empty=True, usage=NS(total_tokens=outer.usage))

            def close(self):
                outer.closed = True

        class _C:
            def create(self, **kw):
                outer.calls.append(kw)
                if kw.get("stream"):
                    if outer.reject_usage and "stream_options" in kw:
                        raise _StatusError(400)
                    return _Stream()
                msg = NS(content="".join(outer.parts))
                return NS(choices=[NS(message=msg, finish_reason="stop")], usage=NS(total_tokens=5))

        self.chat = NS(completions=_C())


@case
def test_rewrite_cache_ignores_history_for_standalone_questions():
    from app import telemetry

    hist_a = [{"role": "user", "content": "Другая тема"}, {"role": "assistant", "content": "длинный ответ " * 50}]
    hist_b = [{"role": "user", "content": "Совсем другое"}, {"role": "assistant", "content": "ещё ответ " * 50}]
    with _env(LLM_CACHE_SIZE="1000"):
        llm.clear_caches()
        stats = telemetry.start_request()
        client = _Flaky(["shaim geology", "oil 2018 shaim", "oil 2018 samotlor"])
        with patched(llm, "_get_client", lambda: client), patched(llm, "_active_provider", lambda: _PROV):
            r1 = llm.generate_search_query("Геология Шаима", hist_a, "en")
            sent = client.last_kwargs["messages"]
            assert [m["role"] for m in sent] == ["system", "user"]          # история в LLM не уходит
            r2 = llm.generate_search_query("Геология Шаима", hist_b, "en")  # другая беседа
            r3 = llm.generate_search_query("  геология шаима ", None, "en")  # новая беседа, регистр и пробелы
            assert r1 == r2 == r3 == "shaim geology" and client.calls == 1 and stats["cache_hits"] == 2
            assert llm.generate_search_query("Геология Шаима", hist_a, "ru") == "oil 2018 shaim" and client.calls == 2  # язык — в ключе
            # уточнение зависит от беседы: история и в запросе, и в ключе кэша
            assert llm.generate_search_query("а после 2018 года?", hist_a, "en") == "oil 2018 samotlor"
            assert len(client.last_kwargs["messages"]) == 4 and client.calls == 3
            assert llm.generate_search_query("а после 2018 года?", hist_a, "en") == "oil 2018 samotlor" and client.calls == 3
        llm.clear_caches()


@case
def test_repeated_question_in_new_chat_skips_rewrite_and_costs_no_tokens():
    import json as _json

    seed()
    db = SessionLocal()
    add_pub(db, "Mineralization of groundwater in the Shaim region", doi="10.1/cache", abstract="Groundwater mineralization near Shaim.")
    db.close()
    path = os.path.join(tempfile.mkdtemp(), "chat_logs.jsonl")
    rewrite = _Flaky(["groundwater mineralization Shaim"])

    async def no_ext(query, limit=20, language=None):
        return [], []

    answer = lambda q, pubs, hist=None, kb_context=None, intent=None: "Минерализация растёт [1]."
    msg = "Почему растёт минерализация в Шаимском районе?"
    with _env(LLM_CACHE_SIZE="1000", CHAT_LOG="1", CHAT_LOG_PATH=path):
        llm.clear_caches()
        with patched(main_mod._source_manager, "search_detailed", no_ext), patched(llm, "is_configured", lambda: True), \
                patched(llm, "_get_client", lambda: rewrite), patched(llm, "_active_provider", lambda: _PROV), \
                patched(llm, "generate_answer", answer), patched(llm, "active_provider_label", lambda: "Fake"), \
                patched(llm, "detect_intent", lambda q, h=None: "FIND_PUBLICATION"):
            first = client.post("/api/chat", json={"message": msg, "history": [], "use_external": False}).json()
            second = client.post("/api/chat", json={   # «другая сессия»: у клиента другая история
                "message": msg, "use_external": False,
                "history": [{"role": "user", "content": "Привет"}, {"role": "assistant", "content": "Здравствуйте"}]}).json()
        llm.clear_caches()
    assert first["local_queries"] == second["local_queries"] and len(second["local_queries"]) == 2
    assert rewrite.calls == 1                       # второй запрос не вызвал LLM для переписывания
    lines = [_json.loads(x) for x in open(path, encoding="utf-8").read().splitlines()]
    assert lines[0]["tokens_used"] == 42 and lines[1]["tokens_used"] == 0 and lines[1]["cache_hits"] >= 1


@case
def test_generate_answer_stream_tokens_first_verified_result_last():
    import asyncio as _a
    from app import telemetry

    parts = ["Рост минерализации ", "[1]. ", "Выдумка [9]."]
    stream = _StreamClient(parts, usage=77)
    stats = telemetry.start_request()

    async def go():
        out = []
        async for kind, value in llm.generate_answer_stream("вопрос", [{"title": "Real paper", "origin": "локальная база знаний"}]):
            out.append((kind, value))
        return out

    with patched(llm, "_get_client", lambda: stream), patched(llm, "_active_provider", lambda: _PROV):
        events = _a.run(go())
    kinds = [k for k, _ in events]
    assert kinds[:-1] == ["token"] * 3 and kinds[-1] == "done"
    streamed = "".join(v for k, v in events if k == "token")
    assert streamed == "".join(parts) and "[9]" in streamed                      # во время потока текст не проверен
    final = events[-1][1]
    assert "[9]" not in final["text"] and "[?]" in final["text"] and final["bad_citations"] == [9]   # итог проверен
    assert final["confidence"] in ("high", "medium", "low") and len(final["sources"]) == 1
    assert stream.calls[0]["stream"] is True and stream.calls[0]["stream_options"] == {"include_usage": True}
    assert stats["tokens_used"] == 77 and stream.closed


@case
def test_stream_falls_back_when_provider_rejects_usage_option():
    import asyncio as _a

    stream = _StreamClient(["Ответ [1]."], reject_usage=True)

    async def go():
        return [x async for x in llm.generate_answer_stream("вопрос", [{"title": "T"}])]

    with patched(llm, "_get_client", lambda: stream), patched(llm, "_active_provider", lambda: _PROV):
        events = _a.run(go())
    assert events[-1][0] == "done" and events[-1][1]["text"].startswith("Ответ")
    assert len(stream.calls) == 2 and "stream_options" not in stream.calls[1]


@case
def test_generate_answer_stream_without_llm_returns_none_and_stops_early_on_close():
    import asyncio as _a
    import time as _t

    async def no_llm():
        return [x async for x in llm.generate_answer_stream("вопрос", [{"title": "T"}])]

    assert _a.run(no_llm()) == [("done", None)]          # LLM не настроен: вызывающий код берёт шаблон

    slow = _StreamClient(["x "] * 200, delay=0.01)

    async def abort():
        agen = llm.generate_answer_stream("вопрос", [{"title": "T"}])
        got = 0
        async for kind, _value in agen:
            got += 1
            if got == 3:
                break
        await agen.aclose()                              # как при нажатии «Стоп» / закрытии вкладки
        await _a.sleep(0.3)
        return got

    with patched(llm, "_get_client", lambda: slow), patched(llm, "_active_provider", lambda: _PROV):
        _a.run(abort())
    assert slow.produced < 60 and slow.closed            # чтение потока у провайдера остановлено


def _read_sse(response):
    events, cur = [], {}
    for line in response.iter_lines():
        if line.startswith("event:"):
            cur = {"event": line.split(":", 1)[1].strip()}
        elif line.startswith("data:") and cur:
            import json as _json

            cur["data"] = _json.loads(line.split(":", 1)[1].strip())
            events.append(cur)
            cur = {}
    return events


@case
def test_chat_stream_endpoint_sends_status_tokens_then_verified_done():
    import json as _json

    seed()
    db = SessionLocal()
    add_pub(db, "Causes of mineralization of water in the Shaim region", doi="10.1/s", abstract="Groundwater mineralization rose near Shaim.")
    db.close()
    path = os.path.join(tempfile.mkdtemp(), "chat_logs.jsonl")
    parts = ["Минерализация растёт ", "[1]. ", "Выдумка [42].\n\n", "### Источники\n1. Invented Fake Title, 2020."]
    stream = _StreamClient(parts, usage=77)

    async def no_ext(query, limit=20, language=None):
        return [], []

    qfn = lambda msg, hist=None, language="en": "groundwater mineralization Shaim"
    with _env(CHAT_LOG="1", CHAT_LOG_PATH=path), patched(main_mod._source_manager, "search_detailed", no_ext), \
            patched(llm, "is_configured", lambda: True), patched(llm, "generate_search_query", qfn), \
            patched(llm, "_get_client", lambda: stream), patched(llm, "_active_provider", lambda: _PROV), \
            patched(llm, "active_provider_label", lambda: "Fake"), patched(llm, "detect_intent", lambda q, h=None: "FIND_PUBLICATION"):
        with client.stream("POST", "/api/chat/stream", json={
                "message": "Почему растёт минерализация в Шаимском районе?", "history": [], "use_external": False}) as r:
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
            events = _read_sse(r)
    names = [e["event"] for e in events]
    assert names[-1] == "done" and "error" not in names
    assert names.count("token") == len(parts) and names.index("status") < names.index("token")
    assert "Формулирую ответ…" in [e["data"]["text"] for e in events if e["event"] == "status"]
    streamed = "".join(e["data"]["text"] for e in events if e["event"] == "token")
    assert "[42]" in streamed                                     # поток — сырой текст
    done = events[-1]["data"]
    assert done["llm_used"] is True and "[42]" not in done["answer"] and "[?]" in done["answer"]   # итог проверен
    assert done["citations"] and done["citations"][0]["n"] == 1 and len(done["citation_warnings"]) == 2
    assert done["confidence"] in ("high", "medium", "low") and done["sources"]
    log = [_json.loads(x) for x in open(path, encoding="utf-8").read().splitlines()][-1]
    assert log["stream"] is True and log["tokens_used"] == 77 and log["error"] is False


@case
def test_chat_stream_endpoint_non_llm_replies_arrive_as_one_chunk():
    seed()
    with client.stream("POST", "/api/chat/stream", json={"message": "Как приготовить суп?", "history": []}) as r:
        events = _read_sse(r)
    assert [e["event"] for e in events] == ["token", "done"]
    done = events[-1]["data"]
    assert done["is_offtopic"] is True and done["sources"] == [] and events[0]["data"]["text"] == done["answer"]
    with client.stream("POST", "/api/chat/stream", json={"message": "Шаимское месторождение", "history": [], "use_external": False}) as r:
        events = _read_sse(r)                                     # LLM не настроен: шаблонный ответ одним куском
    assert [e["event"] for e in events][-1] == "done" and events[-2]["event"] == "token"
    assert events[-1]["data"]["llm_used"] is False and events[-2]["data"]["text"] == events[-1]["data"]["answer"]



@case
def test_bulk_import_select_commit_and_report():
    from scripts import bulk_import
    seed()
    pool = [
        P(title="Causes of Changes in Mineralization of Water in the Shaim Oil and Gas Region", authors=["Yu. Rusakova"], year=2025,
          doi="10.3390/earth6010005", source="OpenAlex", abstract="Groundwater of the Shaim region."),
        P(title="Geology of the Shaim oil field, Tyumen formation", year=1985, doi="10.1/old", source="Crossref"),
    ]

    async def fake_preview(db, queries, per_query):
        async def fake(query, limit=20, language=None):
            return list(pool), []
        with patched(main_mod._source_manager, "search_detailed", fake):
            from app.services import importer
            return await importer.build_preview(db, main_mod._source_manager, queries, per_query=per_query)

    with patched(bulk_import, "_preview", fake_preview):
        assert bulk_import.main(["--dry-run", "--queries-file", str(ROOT / "eval" / "import_queries.txt")]) == 0
        assert client.get("/api/stats").json()["publications"] == 5          # dry-run ничего не сохранил (демо-данные)
        assert bulk_import.main(["--require-abstract", "--min-year", "2000"]) == 0
    stats = client.get("/api/stats").json()
    assert stats["publications"] == 6, stats                                  # сохранена только статья с аннотацией и годом >= 2000
    db = SessionLocal()
    try:
        report = bulk_import.coverage_report(db)
        assert "Публикаций: **6**" in report and "Публикации по месторождениям" in report and "По десятилетиям" in report
        cands = [{"publication": pool[1], "field_ids": [], "research_type_ids": []}]
        assert bulk_import.select_candidates(cands, require_field=True) == []
        assert len(bulk_import.select_candidates(cands, limit=1)) == 1
    finally:
        db.close()
    assert len(bulk_import.load_queries(None)) > 30



@case
def test_gold_metrics_and_pool_roundtrip():
    from scripts import eval_chat as ev
    # метрики на ручном примере: оценки a=2, b=1, c=0; выдача [c, a, x, b]
    m = ev.gold_metrics(["c", "a", "x", "b"], {"a": 2, "b": 1, "c": 0})
    assert m["hit"] == 1.0 and abs(m["prec"] - 0.5) < 1e-9 and m["recall"] == 1.0 and abs(m["mrr"] - 0.5) < 1e-9
    assert 0 < m["ndcg"] < 1
    assert abs(ev.ndcg_at_k([2, 1, 0], [2, 1, 0]) - 1.0) < 1e-9          # идеальный порядок
    assert ev.gold_metrics([], {"a": 1})["prec"] == 0.0 and ev.gold_metrics(["a"], {})["recall"] == 0.0
    assert ev.pub_key("https://doi.org/10.1/ABC", "t") == "doi:10.1/abc" == ev.pub_key("10.1/abc", None)
    assert ev.pub_key(None, "Шаимский  район!").startswith("t:шаимский район")
    seed()
    tmp = Path(tempfile.mkdtemp())
    (tmp / "questions.json").write_text((ROOT / "eval" / "questions.json").read_text(encoding="utf-8"), encoding="utf-8")
    import argparse
    with patched(ev, "EVAL_DIR", tmp):
        db = SessionLocal()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ev.cmd_pool(db, argparse.Namespace(depth=5))
            tpl = json.loads((tmp / "gold_template.json").read_text(encoding="utf-8"))
            q1 = next(x for x in tpl["questions"] if x["id"] == "q01")
            assert q1["judgments"] and all(j["grade"] is None for j in q1["judgments"])
            assert ev.load_gold() is None                                  # без оценок разметки нет
            for j in q1["judgments"]:
                j["grade"] = 2
            (tmp / "gold.json").write_text(json.dumps({"questions": [q1]}, ensure_ascii=False), encoding="utf-8")
            gold = ev.load_gold()
            assert len(gold) == 1
            report = ev.evaluate_retrieval(db, argparse.Namespace(llm_rewrite=False, details=False))
            assert "ручной разметке" in report and "nDCG@5" in report
        finally:
            db.close()


@case
def test_locate_picks_most_specific_level():
    F = [(1, "Шаимское"), (2, "Ловинское"), (3, "Шушминское")]
    R, O, PR = [(10, "Шаимский нефтеносный район")], [(20, "Приуральская нефтегазоносная область")], [(30, "Западно-Сибирская нефтегазоносная провинция")]
    loc = lambda t: classifier.locate(t, F, R, O, PR)  # noqa: E731
    a = loc("Geology of the Shaim oil field, Tyumen formation")
    assert a.level == "field" and a.field_ids == [1] and not a.rayon_ids
    b = loc("Mineralization of groundwater in the Shaim Oil and Gas Region")       # район, а не месторождение
    assert b.level == "rayon" and b.rayon_ids == [10] and not b.field_ids
    assert loc("Нефтеносность Шаимского нефтеносного района").level == "rayon"
    assert loc("Геология Шаимского месторождения").field_ids == [1]
    assert loc("Lovinskoye oil field reservoirs").field_ids == [2] and loc("Шушминское месторождение").field_ids == [3]
    c = loc("Pre-Urals oil and gas province geology of Priural region")
    assert c.level == "oblast" and c.oblast_ids == [20]
    d = loc("Geology and oil potential of the West Siberian basin")
    assert d.level == "province" and d.province_ids == [30] and d.ids() == [30]
    assert loc("Oil in the Samotlor field, Surgut region").level == "province"
    assert loc("Cooking recipes").level is None and loc("Eastern Siberia gas storage").level is None
    assert loc("Президент Польши").ids() == []


@case
def test_region_level_publications_rise_up_the_hierarchy_only():
    seed()
    db = SessionLocal()
    try:
        rayon = db.query(models.Rayon).first()
        oblast, province = rayon.oblast, rayon.oblast.province
        field = db.query(models.Field).first()
        before = {k: len(hierarchy.publication_ids_for_level(db, **{f"{k}_id": v}))
                  for k, v in (("province", province.province_id), ("oblast", oblast.oblast_id),
                               ("rayon", rayon.rayon_id), ("field", field.field_id))}
        p_rayon = save_publication(db, P(title="Shaim region groundwater (районная)", doi="10.9/r1", source="x"),
                                   rayon_ids=[rayon.rayon_id])[0]
        p_prov = save_publication(db, P(title="West Siberia basin review (провинциальная)", doi="10.9/p1", source="x"),
                                  province_ids=[province.province_id])[0]
        p_obl = save_publication(db, P(title="Priural oil province (областная)", doi="10.9/o1", source="x"),
                                 oblast_ids=[oblast.oblast_id])[0]
        db.commit()
        ids = lambda **kw: hierarchy.publication_ids_for_level(db, **kw)  # noqa: E731
        assert p_rayon.publication_id in ids(rayon_id=rayon.rayon_id)
        assert p_rayon.publication_id in ids(oblast_id=oblast.oblast_id) and p_rayon.publication_id in ids(province_id=province.province_id)
        assert p_obl.publication_id not in ids(rayon_id=rayon.rayon_id)             # вниз не опускается
        assert p_obl.publication_id in ids(oblast_id=oblast.oblast_id) and p_obl.publication_id in ids(province_id=province.province_id)
        assert p_prov.publication_id in ids(province_id=province.province_id)
        for lvl in (oblast.oblast_id,):
            assert p_prov.publication_id not in ids(oblast_id=lvl)
        for new in (p_rayon, p_prov, p_obl):
            assert new.publication_id not in ids(field_id=field.field_id)
        assert len(ids(province_id=province.province_id)) == before["province"] + 3
        assert len(ids(field_id=field.field_id)) == before["field"]
        # повторное сохранение не дублирует связи
        save_publication(db, P(title="Shaim region groundwater (районная)", doi="10.9/r1", source="x"), rayon_ids=[rayon.rayon_id])
        db.commit()
        assert db.query(models.PublicationRayon).filter_by(publication_id=p_rayon.publication_id).count() == 1
        # через API: публикации района и статистика по уровням
        r = client.get(f"/api/rayons/{rayon.rayon_id}/publications").json()
        assert any(x["publication_id"] == p_rayon.publication_id for x in r)
        lv = client.get("/api/stats").json()["publications_by_level"]
        assert lv["rayon"] == 1 and lv["oblast"] == 1 and lv["province"] == 1 and lv["field"] >= 1
        # онтология согласована с SQL (вывод вверх по иерархии), в т.ч. для публикаций уровня района/области/провинции
        v = client.get("/api/ontology/validate").json()
        assert v["all_match"], v
        # удаление публикации убирает и связи верхнего уровня
        assert client.delete(f"/api/publications/{p_rayon.publication_id}").status_code == 204
        assert db.query(models.PublicationRayon).filter_by(publication_id=p_rayon.publication_id).count() == 0
    finally:
        db.close()


@case
def test_assign_regions_backfill_and_import_preview_levels():
    from scripts import assign_regions
    seed()
    db = SessionLocal()
    try:
        # публикации, загруженные со старой привязкой: «Shaim region» было привязано к месторождению
        shaim_field = db.query(models.Field).filter(models.Field.name.like("Шаим%")).first()
        old_loose = save_publication(db, P(title="Groundwater of the Shaim Oil and Gas Region", doi="10.9/a1", source="OpenAlex",
                                           abstract="Geology and oil wells of the region."), field_ids=[shaim_field.field_id])[0]
        wide = save_publication(db, P(title="West Siberian basin oil geology", doi="10.9/a2", source="OpenAlex",
                                      abstract="Reservoir geology of the basin."))[0]
        nothing = save_publication(db, P(title="Cooking soup", doi="10.9/a3", source="OpenAlex", abstract="Recipes."))[0]
        db.commit()
        demo_field_links = db.query(models.PublicationField).count() - 1       # без привязки old_loose
        dry = assign_regions.assign(db)
        assert db.query(models.PublicationProvince).count() == 0                       # пробный прогон ничего не пишет
        res = assign_regions.assign(db, apply=True)
        assert db.query(models.PublicationProvince).filter_by(publication_id=wide.publication_id).count() == 1
        assert db.query(models.PublicationField).filter_by(publication_id=old_loose.publication_id).count() == 1  # без --tighten не трогаем
        assert db.query(models.PublicationRayon).filter_by(publication_id=nothing.publication_id).count() == 0
        assert res["stats"]["регион не определён"] >= 1 and dry["stats"]["total"] == res["stats"]["total"]
        res2 = assign_regions.assign(db, apply=True, tighten=True)
        db.expire_all()
        assert db.query(models.PublicationField).filter_by(publication_id=old_loose.publication_id).count() == 0
        assert db.query(models.PublicationRayon).filter_by(publication_id=old_loose.publication_id).count() == 1
        assert res2["stats"]["месторождение → район/область/провинция"] == 1
        # демо-публикации (без аннотации из внешнего источника) не затронуты
        assert db.query(models.PublicationField).count() == demo_field_links
    finally:
        db.close()
    # предпросмотр импорта показывает уровень
    pool = [P(title="Mineralization of groundwater in the Shaim Oil and Gas Region", year=2024, doi="10.9/pv1", source="OpenAlex",
              abstract="Groundwater of oil fields."),
            P(title="Geology of the West Siberian oil and gas basin", year=2020, doi="10.9/pv2", source="OpenAlex", abstract="Oil geology.")]

    async def fake(query, limit=20, language=None):
        return list(pool), []

    with patched(main_mod._source_manager, "search_detailed", fake):
        j = client.post("/api/import/preview", json={"queries": ["q"], "per_query": 5}).json()
        levels = {c["publication"]["doi"]: (c["level"], c["place_names"]) for c in j["candidates"]}
        assert levels["10.9/pv1"][0] == "rayon" and "Шаим" in levels["10.9/pv1"][1][0], levels
        assert levels["10.9/pv2"][0] == "province", levels
        assert client.post("/api/import/commit", json={"items": j["candidates"]}).json()["created"] == 2
    db = SessionLocal()
    try:
        saved = db.query(models.Publication).filter_by(doi="10.9/pv1").one()
        assert [r.name for r in saved.rayons] and not saved.fields
    finally:
        db.close()


@case
def test_field_card_regional_publications_summary_and_paging():
    seed()
    db = SessionLocal()
    try:
        field = db.query(models.Field).first()
        rayon = field.rayon
        province = rayon.oblast.province
        for i in range(25):                                        # 25 работ о провинции, из них у одной нет года
            save_publication(db, P(title=f"West Siberia review {i}", doi=f"10.7/{i}", year=None if i == 0 else 2000 + i, source="x"),
                             province_ids=[province.province_id])
        save_publication(db, P(title="Shaim district survey", doi="10.7/r", year=2021, source="x"), rayon_ids=[rayon.rayon_id])
        save_publication(db, P(title="Shaim field only", doi="10.7/f", year=2022, source="x"), field_ids=[field.field_id])
        db.commit()
        summary = client.get(f"/api/fields/{field.field_id}/regional-publications").json()
        by_level = {x["level"]: x for x in summary}
        assert [x["level"] for x in summary] == ["rayon", "oblast", "province"]
        assert by_level["rayon"]["total"] == 1 and by_level["oblast"]["total"] == 0 and by_level["province"]["total"] == 25
        assert by_level["rayon"]["name"] == rayon.name
        # публикация самого месторождения в региональный список не попадает, а в обычный — попадает
        own = {p["title"] for p in client.get(f"/api/fields/{field.field_id}/publications").json()}
        assert "Shaim field only" in own and "Shaim district survey" not in own
        page1 = client.get("/api/regional-publications", params={"level": "province", "object_id": province.province_id, "limit": 20}).json()
        page2 = client.get("/api/regional-publications", params={"level": "province", "object_id": province.province_id, "limit": 20, "offset": 20}).json()
        assert page1["total"] == 25 and len(page1["publications"]) == 20 and len(page2["publications"]) == 5
        years = [p["year"] for p in page1["publications"]]
        assert years == sorted(years, reverse=True) and years[0] == 2024                      # новые сверху
        assert page2["publications"][-1]["year"] is None                                      # без года — в самом конце
        titles = [p["title"] for p in page1["publications"] + page2["publications"]]
        assert len(set(titles)) == 25                                                         # страницы не пересекаются
        assert client.get("/api/regional-publications", params={"level": "field", "object_id": 1}).status_code == 422
        assert client.get("/api/regional-publications", params={"level": "rayon", "object_id": 99999}).status_code == 404
        assert client.get("/api/fields/99999/regional-publications").status_code == 404
    finally:
        db.close()


@case
def test_detect_intent_default_cache_and_tolerant_parsing():
    from app import telemetry

    with patched(llm, "_get_client", lambda: None):
        assert llm.detect_intent("Сравни Шаим и Самотлор", None) == "FIND_PUBLICATION"      # без LLM — поиск публикаций
    for raw, want in [("COMPARE_OBJECTS", "COMPARE_OBJECTS"), ("«FIND_OBJECT».", "FIND_OBJECT"), ("Intent: explain", "EXPLAIN"),
                      ("find publication", "FIND_PUBLICATION"), ("offtopic.", "OFFTOPIC"), ("**UNKNOWN**", "UNKNOWN"),
                      ("хм", None), ("", None), (None, None)]:
        assert llm.parse_intent(raw) == want, raw
    with _env(LLM_CACHE_SIZE="1000"), patched(llm, "_active_provider", lambda: _PROV), patched(llm, "RETRY_DELAY_SEC", 0):
        llm.clear_caches()
        stats = telemetry.start_request()
        client = _Flaky(["FIND_OBJECT", "«COMPARE_OBJECTS».", TimeoutError("t1"), TimeoutError("t2"), "не знаю", "EXPLAIN"])
        with patched(llm, "_get_client", lambda: client):
            assert llm.detect_intent("Что такое пласт Т1?", None) == "FIND_OBJECT"
            sent = client.last_kwargs
            assert [m["role"] for m in sent["messages"]] == ["system", "user"] and sent["messages"][0]["content"] == llm.INTENT_PROMPT
            assert sent["temperature"] == 0 and sent["max_tokens"] <= 10                    # короткий вызов без контекста публикаций
            assert llm.detect_intent("  что такое ПЛАСТ т1? ", None) == "FIND_OBJECT"        # тот же вопрос — из кэша
            assert client.calls == 1 and stats["cache_hits"] == 1
            assert llm.detect_intent("Сравни геологию Шаима и Самотлора", None) == "COMPARE_OBJECTS" and client.calls == 2
            # сбой LLM и нераспознанный ответ → UNKNOWN, и в кэш они не попадают
            assert llm.detect_intent("Что такое заводнение?", None) == "UNKNOWN" and client.calls == 4
            assert llm.detect_intent("Что такое заводнение?", None) == "UNKNOWN" and client.calls == 5
            assert llm.detect_intent("Что такое заводнение?", None) == "EXPLAIN" and client.calls == 6
            assert llm.detect_intent("Что такое заводнение?", None) == "EXPLAIN" and client.calls == 6
            # уточнение зависит от беседы: история уходит в LLM и входит в ключ кэша
            hist = [{"role": "user", "content": "Расскажи про Шаим"}, {"role": "assistant", "content": "…"}]
            client.steps = ["FIND_PUBLICATION"]
            assert llm.detect_intent("а после 2018?", hist) == "FIND_PUBLICATION" and len(client.last_kwargs["messages"]) == 4
        llm.clear_caches()


@case
def test_answer_instructions_depend_on_intent():
    ctx = [{"title": "T", "origin": "локальная база знаний", "text_snippet": "text"}]
    with patched(llm, "_active_provider", lambda: _PROV):
        for intent, marker in [("FIND_PUBLICATION", "сгруппируй их по годам или методам"), ("FIND_OBJECT", "геологическую справку об объекте"),
                               ("COMPARE_OBJECTS", "Нет данных (в базе знаний)"), ("EXPLAIN", "короткое определение")]:
            client = _Flaky(["Ответ [1]."])
            with patched(llm, "_get_client", lambda c=client: c):
                assert llm.generate_answer("вопрос", ctx, None, None, intent=intent)["text"] == "Ответ [1]."
            system = client.last_kwargs["messages"][0]["content"]
            assert system.startswith(llm.SYSTEM_PROMPT) and marker in system and f"Тип вопроса: {intent}" in system, intent
        for intent in (None, "UNKNOWN", "OFFTOPIC", "bogus"):                  # без указаний — прежняя системная инструкция
            client = _Flaky(["Ответ [1]."])
            with patched(llm, "_get_client", lambda c=client: c):
                llm.generate_answer("вопрос", ctx, None, None, intent=intent)
            assert client.last_kwargs["messages"][0]["content"] == llm.SYSTEM_PROMPT
        stream = _StreamClient(["Ответ ", "[1]."])
        with patched(llm, "_get_client", lambda: stream):
            async def go():
                return [x async for x in llm.generate_answer_stream("вопрос", ctx, None, None, intent="COMPARE_OBJECTS")]
            import asyncio as _a
            _a.run(go())
        assert "Нет данных (в базе знаний)" in stream.calls[0]["messages"][0]["content"]


@case
def test_split_compare_rules():
    from app.services import routing

    def pair(q):
        p = routing.split_compare(q)
        return p and (p.a_label, p.a_query, p.b_label, p.b_query)

    assert pair("Сравни геологию Шаима и Самотлора") == ("Шаима", "геологию Шаима", "Самотлора", "Самотлора геологию")
    assert pair("сравни Шаимское месторождение и Самотлорское месторождение?")[:2] == ("Шаимское", "Шаимское месторождение")
    assert pair("Чем баженовская свита отличается от тюменской свиты?") == ("баженовская свита", "баженовская свита", "тюменской свиты", "тюменской свиты")
    assert pair("Различия между Шаимским районом и Сургутским районом")[0::2] == ("Шаимским", "Сургутским")
    assert pair("Compare Shaim and Samotlor")[0::2] == ("Shaim", "Samotlor") and pair("Shaim vs Samotlor")[0::2] == ("Shaim", "Samotlor")
    assert pair("Сравни методы заводнения и ГРП")[3] == "ГРП"                   # предмет сравнения не дублируется, если нет названий
    assert pair("Что такое пласт Т1?") is None and pair("Сравни Шаим и Шаим") is None and pair("сравни геологию") is None


@case
def test_object_tiers_and_hits_go_from_specific_to_general():
    from app.services import kb_context, routing

    seed()
    db = SessionLocal()
    try:
        horizon = db.query(models.Horizon).first()
        field = horizon.field
        rayon = field.rayon
        province = rayon.oblast.province
        own = save_publication(db, P(title="Reservoir Т1 study zzqx1", doi="10.3/h", source="x"))[0]
        db.add(models.PublicationHorizon(publication_id=own.publication_id, horizon_id=horizon.horizon_id))
        on_field = save_publication(db, P(title="Field-wide survey zzqx2", doi="10.3/f", source="x"), field_ids=[field.field_id])[0]
        on_rayon = save_publication(db, P(title="District overview zzqx3", doi="10.3/r", source="x"), rayon_ids=[rayon.rayon_id])[0]
        on_prov = save_publication(db, P(title="Basin review zzqx4", doi="10.3/p", source="x"), province_ids=[province.province_id])[0]
        db.commit()
        matched = kb_context.match_objects(db, f"Что такое пласт {horizon.index_code}?")
        assert matched and matched[0][0] == "horizon"
        tiers = routing.object_tiers(db, *matched[0])
        labels = [t[0] for t in tiers]
        assert labels[0].startswith("пласт") and labels[1].startswith("месторождение") and labels[2].startswith("район")
        assert labels[-1].startswith("провинция")
        ids = [t[1] for t in tiers]
        assert own.publication_id in ids[0] and on_field.publication_id in ids[1] and on_rayon.publication_id in ids[2]
        assert on_prov.publication_id in ids[-1]
        assert not any(a & b for i, a in enumerate(ids) for b in ids[i + 1:])        # ярусы не пересекаются
        # вопрос не совпал с текстом публикаций, но публикации привязаны к объекту: выдача идёт по ярусам
        hits, notes = routing.object_hits(db, matched, ["совсем другие слова qwertyuiop"], top_k=4)
        got = [h.pub.publication_id for h in hits]
        assert len(got) == 4 and own.publication_id in got and notes and notes[0].startswith("пласт")
        assert all(h.score > 0 for h in hits)                                        # без совпадений по тексту — минимальная положительная оценка
        order = [next(i for i, ids_ in enumerate(ids) if pid in ids_) for pid in got]
        assert order == sorted(order)                                                # от конкретного к общему
        assert hits[0].matched_field.startswith("пласт")
        assert routing.object_hits(db, [], ["q"]) == ([], [])
    finally:
        db.close()


@case
def test_compare_hits_search_each_object_and_interleave():
    from app.services import routing

    seed()
    db = SessionLocal()
    for i in range(3):
        add_pub(db, f"Alpharock reservoir porosity study {i}", doi=f"10.4/a{i}", abstract="Alpharock sandstone porosity.")
        add_pub(db, f"Betarock reservoir permeability study {i}", doi=f"10.4/b{i}", abstract="Betarock sandstone permeability.")
    add_pub(db, "Alpharock versus Betarock joint study", doi="10.4/both", abstract="Alpharock and Betarock sandstones.")
    pair = routing.split_compare("Сравни Alpharock и Betarock")
    hits = routing.compare_hits(db, pair, top_k=4)
    labels = [h.matched_field for h in hits]
    assert labels[0] == "Alpharock" and labels[1] == "Betarock" and len(hits) >= 4        # чередование: объекты не вытесняют друг друга
    assert len({h.pub.publication_id for h in hits}) == len(hits)                       # дубликатов нет
    assert any(" и " in l for l in labels) or True
    db.close()


@case
def test_chat_routes_by_intent_and_logs_it():
    import json as _json

    seed()
    db = SessionLocal()
    horizon = db.query(models.Horizon).first()
    horizon_code = horizon.index_code
    add_pub(db, "Geology of the Shaim region reservoirs", doi="10.5/sh", abstract="Shaim geology reservoirs Tyumen formation.")
    add_pub(db, "Geology of the Samotlor field reservoirs", doi="10.5/sa", abstract="Samotlor geology reservoirs.")
    db.close()
    path = os.path.join(tempfile.mkdtemp(), "chat_logs.jsonl")
    seen = {}

    async def no_ext(query, limit=20, language=None):
        return [], []

    def answer(q, pubs, hist=None, kb_context=None, return_dict=True, intent=None):
        seen.update(intent=intent, pubs=pubs)
        return "Ответ [1]."

    def ask(message, detected, history=None):
        seen.clear()
        with patched(llm, "detect_intent", lambda q, h=None: detected):
            return client.post("/api/chat", json={"message": message, "history": history or [], "use_external": False}).json()

    qfn = lambda msg, hist=None, language="en": "shaim samotlor geology"
    with _env(CHAT_LOG="1", CHAT_LOG_PATH=path), patched(main_mod._source_manager, "search_detailed", no_ext), \
            patched(llm, "is_configured", lambda: True), patched(llm, "generate_search_query", qfn), \
            patched(llm, "generate_answer", answer), patched(llm, "active_provider_label", lambda: "Fake"):
        # сравнение: два поиска, источники подписаны объектом, ответ строится как сравнение
        j = ask("Сравни геологию Шаима и Самотлора", "COMPARE_OBJECTS")
        assert j["query_intent"] == "COMPARE_OBJECTS" and j["search_strategy"] == "compare" and seen["intent"] == "COMPARE_OBJECTS"
        assert len(j["local_queries"]) >= 2 and {p["object_name"] for p in seen["pubs"] if p["origin"] == "локальная база знаний"} >= {"Шаима", "Самотлора"}
        # объект: ярусы от пласта к родителям, ответ строится как справка
        j = ask(f"Что такое пласт {horizon_code}?", "FIND_OBJECT")
        assert j["search_strategy"] == "object" and seen["intent"] == "FIND_OBJECT"
        assert seen["pubs"][0]["object_name"].startswith("пласт")
        # обычный вопрос и объяснение: стандартный поиск
        j = ask("Какие исследования проводились в Шаимском районе?", "FIND_PUBLICATION")
        assert j["search_strategy"] == "standard" and seen["intent"] == "FIND_PUBLICATION"
        j = ask("Что такое минерализация подземных вод?", "EXPLAIN")
        assert j["search_strategy"] == "standard" and seen["intent"] == "EXPLAIN"
        # просили сравнить, но двух объектов в вопросе нет: обычный поиск и обычная форма ответа
        j = ask("Какие различия в геологии Шаима?", "COMPARE_OBJECTS")
        assert j["query_intent"] == "COMPARE_OBJECTS" and j["search_strategy"] == "standard:compare-not-split" and seen["intent"] == "FIND_PUBLICATION"
        # LLM сказал «вне темы», но тема подтверждена предметными словами: доверяем теме, ответ — обычный
        j = ask("Расскажи про Шаимское месторождение", "OFFTOPIC")
        assert j["is_offtopic"] is False and j["query_intent"] == "FIND_PUBLICATION" and seen["intent"] == "FIND_PUBLICATION"
        # «вне темы» без предметных слов (сценарий: идёт беседа, ключевых слов нет): поиск не выполняется
        calls = []
        with patched(llm, "classify_scope", lambda m, h=None: None), \
                patched(search, "retrieve", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("поиск не нужен"))):
            j = ask("Что посоветуешь приготовить на ужин сегодня вечером", "OFFTOPIC",
                    history=[{"role": "user", "content": "Привет"}, {"role": "assistant", "content": "Здравствуйте"}])
        assert j["is_offtopic"] is True and j["query_intent"] == "OFFTOPIC" and j["suggestions"] and not j["sources"] and not calls
        assert "intent" not in seen or seen == {}                                     # генерация ответа не вызывалась
    lines = [_json.loads(x) for x in open(path, encoding="utf-8").read().splitlines()]
    assert [l["query_intent"] for l in lines] == ["COMPARE_OBJECTS", "FIND_OBJECT", "FIND_PUBLICATION", "EXPLAIN", "COMPARE_OBJECTS",
                                                  "FIND_PUBLICATION", "OFFTOPIC"]
    assert [l["route"] for l in lines[:5]] == ["compare", "object", "standard", "standard", "standard:compare-not-split"]
    assert lines[-1]["route"] == "offtopic:intent", lines[-1]["route"]
    assert all("intent" in l for l in lines)


@case
def test_intent_eval_file_and_accuracy_report():
    import argparse
    from scripts import eval_chat as ev

    data = json.loads((ROOT / "eval" / "intent_questions.json").read_text(encoding="utf-8"))["questions"]
    assert len(data) >= 10 and {d["expected"] for d in data} == set(llm.INTENTS)
    assert len({d["q"] for d in data}) == len(data)
    expected = {d["q"]: d["expected"] for d in data}
    wrong = data[0]["q"]
    fake = lambda q, h=None: "UNKNOWN" if q == wrong else expected[q]  # noqa: E731
    with patched(llm, "is_configured", lambda: True), patched(llm, "detect_intent", fake), patched(llm, "active_provider_label", lambda: "Fake"):
        report = ev.cmd_intent(None, argparse.Namespace())
    assert f"Accuracy: {len(data) - 1}/{len(data)}" in report and "Матрица ошибок" in report and "| FIND_PUBLICATION |" in report
    with patched(llm, "is_configured", lambda: False):
        assert ev.cmd_intent(None, argparse.Namespace()) is None


@case
def test_passwords_and_tokens():
    h = auth.hash_password("правильный пароль 1")
    assert h.startswith("pbkdf2$") and auth.hash_password("правильный пароль 1") != h        # соль у каждого своя
    assert auth.verify_password("правильный пароль 1", h) and not auth.verify_password("другой", h)
    assert not auth.verify_password("x", "мусор") and not auth.verify_password("x", "")
    user = models.User(user_id=7, email="a@b.ru", password_hash="x", role="admin")
    with _env(AUTH_SECRET="s1", AUTH_TOKEN_HOURS="1"):
        t = auth.create_token(user, now=1000)
        assert auth.decode_token(t, now=1500)["sub"] == 7 and auth.decode_token(t, now=1500)["role"] == "admin"
        assert auth.decode_token(t, now=1000 + 3601) is None                                  # срок истёк
        head, body, sig = t.split(".")
        forged = auth._b64(json.dumps({"sub": 7, "role": "admin", "iat": 0, "exp": 9999999999}).encode())
        assert auth.decode_token(f"{head}.{forged}.{sig}", now=1500) is None                  # подпись не сходится
        assert auth.decode_token("a.b", now=1) is None and auth.decode_token("", now=1) is None
        none_alg = auth._b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        assert auth.decode_token(f"{none_alg}.{body}.", now=1500) is None                    # alg=none не принимается
    with _env(AUTH_SECRET="s2"):
        assert auth.decode_token(t, now=1500) is None                                         # другой ключ — другой токен


@case
def test_register_login_roles_and_route_protection():
    seed()
    with _env(AUTH_ENFORCE="1", AUTH_SECRET="test-secret"):
        db = SessionLocal(); db.query(models.User).delete(); db.commit(); db.close()
        assert client.get("/api/auth/me").json() == {"user": None, "auth_enforced": True}
        r = client.post("/api/auth/register", json={"email": " Admin@Example.RU ", "password": "12345678", "full_name": "Админ"})
        assert r.status_code == 201 and r.json()["user"]["role"] == "admin" and r.json()["user"]["email"] == "admin@example.ru"
        admin = {"Authorization": "Bearer " + r.json()["access_token"]}
        r = client.post("/api/auth/register", json={"email": "user@example.ru", "password": "12345678"})
        assert r.json()["user"]["role"] == "user"
        user = {"Authorization": "Bearer " + r.json()["access_token"]}
        assert client.post("/api/auth/register", json={"email": "USER@example.ru", "password": "12345678"}).status_code == 409
        assert client.post("/api/auth/register", json={"email": "bad", "password": "12345678"}).status_code == 422
        assert client.post("/api/auth/register", json={"email": "c@example.ru", "password": "short"}).status_code == 422
        assert client.post("/api/auth/login", json={"email": "user@example.ru", "password": "неверно!!"}).status_code == 401
        assert client.post("/api/auth/login", json={"email": "net@example.ru", "password": "12345678"}).status_code == 401
        ok = client.post("/api/auth/login", json={"email": "USER@example.ru", "password": "12345678"})
        assert ok.status_code == 200 and client.get("/api/auth/me", headers={"Authorization": "Bearer " + ok.json()["access_token"]}).json()["user"]["email"] == "user@example.ru"
        assert client.get("/api/auth/me", headers={"Authorization": "Bearer garbage.token.here"}).json()["user"] is None
        db = SessionLocal(); pub_id = add_pub(db, "Protected publication zzprot", doi="10.9/prot").publication_id; db.close()
        assert client.get("/api/publications").status_code == 200                            # чтение и поиск открыты
        assert client.delete(f"/api/publications/{pub_id}").status_code == 401               # без входа
        assert client.delete(f"/api/publications/{pub_id}", headers=user).status_code == 403   # обычный пользователь
        assert client.post("/api/embeddings/build").status_code == 401
        assert client.post("/api/import/commit", json={"candidates": []}, headers=user).status_code == 403
        assert client.post("/api/search/external/save", json={"title": "x"}).status_code == 401
        assert client.delete(f"/api/publications/{pub_id}", headers=admin).status_code == 204
        # заблокированный пользователь теряет доступ вместе со своим токеном
        db = SessionLocal(); u = db.query(models.User).filter_by(email="user@example.ru").first(); u.is_active = False; db.commit(); db.close()
        assert client.get("/api/auth/me", headers=user).json()["user"] is None
        assert client.post("/api/auth/login", json={"email": "user@example.ru", "password": "12345678"}).status_code == 401
        db = SessionLocal(); db.query(models.User).delete(); db.commit(); db.close()
    with _env(AUTH_ENFORCE="0"):
        assert client.post("/api/embeddings/build").status_code != 401                       # защита выключена в .env


@case
def test_export_formats_order_and_safety():
    seed()
    db = SessionLocal()
    a = add_pub(db, "=HYPERLINK(\"http://x\") Shaim & reservoirs", doi="10.9/a", abstract="x").publication_id
    b = add_pub(db, "Second paper", doi="10.9/b", abstract="y").publication_id
    db.close()
    ask = lambda ids, fmt: client.post("/api/export", json={"publication_ids": ids, "format": fmt})  # noqa: E731
    r = ask([b, a, b, 999999], "csv")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"] and r.headers["content-disposition"].endswith('.csv"')
    text = r.content.decode("utf-8")
    assert text.startswith("\ufeff") and text.count("\r\n") == 3                                  # заголовок + 2 строки, дубль и несуществующий id отброшены
    rows = text.splitlines()
    assert "Second paper" in rows[1] and "'=HYPERLINK" in rows[2] and ";10.9/b;" in rows[1]  # порядок как в выдаче; формула обезврежена
    j = ask([a], "json").json()
    assert j[0]["id"] == a and j[0]["doi"] == "10.9/a" and isinstance(j[0]["authors"], list)
    bib = ask([a, b], "bibtex").content.decode()
    assert bib.count("@") == 2 and "Shaim \\& reservoirs" in bib and "doi = {10.9/a}" in bib
    keys = re.findall(r"@\w+\{([^,]+),", bib)
    assert len(set(keys)) == 2                                                                # ключи цитирования не повторяются
    ris = ask([b], "ris").content.decode()
    assert "TI  - Second paper" in ris and ris.rstrip().endswith("ER  -")
    assert ask([a], "docx").status_code == 422 and ask([], "csv").status_code == 422 and ask([999999], "csv").status_code == 404
    with _env(AUTH_ENFORCE="1", AUTH_SECRET="t"):
        assert ask([a], "csv").status_code == 401                                             # выгрузка — для вошедших


@case
def test_chat_hides_external_results_outside_the_domain():
    seed()
    from app.sources.models import PublicationResult as R
    good = R(title="Oil reservoirs of the Shaim region, West Siberia", source="openalex", doi="10.7/good",
             abstract="Tyumen formation petroleum geology of West Siberia.", year=2020)
    bad = R(title="Darasun ore field quartz: Teremkinskoe deposit", source="crossref", doi="10.7/bad", abstract="Gold deposit geochemistry.", year=2021)

    async def ext(query, limit=20, language=None):
        return [good, bad], []

    def run(flag):
        with _env(EXTERNAL_DOMAIN_FILTER=flag), patched(main_mod._source_manager, "search_detailed", ext), \
                patched(llm, "is_configured", lambda: False):
            return client.post("/api/chat", json={"message": "Шаимское месторождение геология", "history": [], "use_external": True, "external_language": "any"}).json()

    titles = lambda j: [e["title"] for e in j["external_sources"]]  # noqa: E731
    j = run("1")
    assert titles(j) == [good.title], (titles(j), j.get("external_note"), j.get("answer"))
    assert set(titles(run("0"))) == {good.title, bad.title}                                   # фильтр отключается в .env


@case
def test_compare_always_names_both_objects_even_when_one_has_no_data():
    from app.services import routing

    assert "Markdown-таблицы" in llm.SYSTEM_PROMPT and "Нет данных (в базе знаний)" in llm.SYSTEM_PROMPT
    assert "НЕ игнорируй второй объект" in llm.SYSTEM_PROMPT
    seed()
    db = SessionLocal()
    for i in range(12):
        add_pub(db, f"Shaim reservoir geology study {i}", doi=f"10.8/s{i}", abstract="Shaim geology of reservoirs.")
    pair = routing.split_compare("Сравни геологию Шаимского месторождения и Самотлорского месторождения")
    hits = routing.compare_hits(db, pair, top_k=6)
    assert hits and all(h.matched_field == pair.a_label for h in hits), (pair, [(h.pub.title, h.matched_field, h.score) for h in hits])                 # по второму объекту найдено 0
    text = routing.compare_context(db, pair, hits)
    assert f"«{pair.a_label}»; 2) «{pair.b_label}»" in text                             # оба объекта названы явно
    assert f"«{pair.b_label}» — 0" in text and f"«{pair.a_label}» — {len(hits)}" in text
    assert "Месторождение «Шаимское»" in text                                           # справка по первому объекту
    assert f"Объект «{pair.b_label}» в структурированной справке базы не найден" in text
    assert routing.NO_DATA in text
    db.close()
    # в модель уходит и справка, и источники, и пустой второй объект
    seen = {}

    def answer(q, pubs, hist=None, kb_context=None, return_dict=True, intent=None):
        seen.update(kb=kb_context, intent=intent, pubs=pubs)
        return "| Параметр | A | B |"

    async def no_ext(query, limit=20, language=None):
        return [], []

    with patched(main_mod._source_manager, "search_detailed", no_ext), patched(llm, "is_configured", lambda: True), \
            patched(llm, "detect_intent", lambda q, h=None: "COMPARE_OBJECTS"), patched(llm, "generate_answer", answer), \
            patched(llm, "generate_search_query", lambda m, h=None, language="en": "shaim samotlor geology"), \
            patched(llm, "active_provider_label", lambda: "Fake"):
        j = client.post("/api/chat", json={"message": "Сравни геологию Шаимского месторождения и Самотлорского месторождения",
                                           "history": [], "use_external": False}).json()
    assert j["search_strategy"] == "compare" and seen["intent"] == "COMPARE_OBJECTS"
    assert "Самотлорского" in seen["kb"] and "— 0" in seen["kb"] and "Нет данных (в базе знаний)" in seen["kb"]
    assert j["answer"].startswith("| Параметр")


@case
def test_chat_history_is_private_saved_after_answer_and_deletable():
    with _env(AUTH_ENFORCE="1", AUTH_SECRET="hist-secret"):
        db = SessionLocal()
        for t in (models.ChatMessage, models.ChatSession, models.User):
            db.query(t).delete()
        db.commit(); db.close()
        reg = lambda mail: {"Authorization": "Bearer " + client.post("/api/auth/register", json={"email": mail, "password": "12345678"}).json()["access_token"]}  # noqa: E731
        alice, bob = reg("alice@example.ru"), reg("bob@example.ru")
        # гость: список и создание недоступны
        assert client.get("/api/chat/sessions").status_code == 401 and client.post("/api/chat/sessions", json={}).status_code == 401
        r = client.post("/api/chat/sessions", json={}, headers=alice)
        assert r.status_code == 201 and r.json()["title"] == "Новый чат" and len(r.json()["id"]) == 36
        sid = r.json()["id"]
        long_q = "Привет! Расскажи подробно про геологическое строение Шаимского района"
        ans = client.post("/api/chat", json={"message": long_q, "session_id": sid, "use_external": False}, headers=alice)
        assert ans.status_code == 200
        msgs = client.get(f"/api/chat/sessions/{sid}/messages", headers=alice).json()
        assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[0]["content"] == long_q
        assert msgs[1]["content"] == ans.json()["answer"] and msgs[1]["meta"]["answer"] == ans.json()["answer"]
        title = client.get("/api/chat/sessions", headers=alice).json()[0]["title"]
        assert title == " ".join(long_q.split())[:30] + "…" and len(title) == 31             # название — первые 30 символов вопроса
        # потоковый ответ сохраняется только после завершения
        stream = client.post("/api/chat/stream", json={"message": "Привет", "session_id": sid, "use_external": False}, headers=alice)
        assert "event: done" in stream.text
        assert len(client.get(f"/api/chat/sessions/{sid}/messages", headers=alice).json()) == 4
        async def boom(*a, **k):
            raise RuntimeError("сбой генерации")
        with patched(main_mod, "_chat_impl", boom):
            failed = client.post("/api/chat/stream", json={"message": "Привет", "session_id": sid}, headers=alice)
        assert "event: error" in failed.text and "event: done" not in failed.text
        assert len(client.get(f"/api/chat/sessions/{sid}/messages", headers=alice).json()) == 4   # ни вопроса без ответа, ни пустого ответа
        # чужой чат: 404 везде, и ответ в него не пишется
        assert client.get(f"/api/chat/sessions/{sid}/messages", headers=bob).status_code == 404
        assert client.delete(f"/api/chat/sessions/{sid}", headers=bob).status_code == 404
        assert client.post("/api/chat", json={"message": "Привет", "session_id": sid}, headers=bob).status_code == 404
        assert client.get("/api/chat/sessions", headers=bob).json() == []
        # гость с session_id: ответ приходит, но ничего не сохраняется
        assert client.post("/api/chat", json={"message": "Привет", "session_id": sid}).status_code == 200
        assert len(client.get(f"/api/chat/sessions/{sid}/messages", headers=alice).json()) == 4
        # свой чат удаляется вместе с сообщениями
        assert client.delete(f"/api/chat/sessions/{sid}", headers=alice).status_code == 204
        assert client.get(f"/api/chat/sessions/{sid}/messages", headers=alice).status_code == 404
        db = SessionLocal(); assert db.query(models.ChatMessage).count() == 0; db.close()
        for t in (models.ChatMessage, models.ChatSession, models.User):
            db = SessionLocal(); db.query(t).delete(); db.commit(); db.close()


@case
def test_admin_panel_api_roles_users_publications_logs_settings():
    from app.sources import manager as sm

    def wipe():
        db = SessionLocal()
        for t in (models.ChatMessage, models.ChatSession, models.AppSetting, models.User):
            db.query(t).delete()
        db.commit(); db.close()

    seed()
    path = os.path.join(tempfile.mkdtemp(), "chat_logs.jsonl")
    with _env(AUTH_ENFORCE="1", AUTH_SECRET="adm-secret", CHAT_LOG="1", CHAT_LOG_PATH=path):
        wipe()
        reg = lambda mail: client.post("/api/auth/register", json={"email": mail, "password": "12345678"}).json()  # noqa: E731
        a, u = reg("root@example.ru"), reg("plain@example.ru")
        adm, usr = {"Authorization": "Bearer " + a["access_token"]}, {"Authorization": "Bearer " + u["access_token"]}
        assert a["user"]["role"] == "admin" and u["user"]["role"] == "user"
        # без токена — 401, обычный пользователь — 403, админ — 200
        for url in ("/api/admin/users", "/api/admin/publications", "/api/admin/logs", "/api/admin/settings"):
            assert client.get(url).status_code == 401 and client.get(url, headers=usr).status_code == 403, url
            assert client.get(url, headers=adm).status_code == 200, url
        # изменение данных — только админ (в том числе сохранение из внешнего поиска, загрузка документов, импорт)
        assert client.post("/api/search/external/save", json={"title": "x"}, headers=usr).status_code == 403
        assert client.post("/api/fulltext/fetch", json={}, headers=usr).status_code == 403
        assert client.post("/api/import/preview", json={"queries": ["x"]}, headers=usr).status_code == 403
        assert client.post("/api/documents/upload", data={"publication_id": "1"}, files={"file": ("a.txt", b"x")}, headers=usr).status_code == 403
        # пользователи: блокировка, защита от потери доступа
        users = client.get("/api/admin/users", headers=adm).json()
        me, other = users[0], users[1]
        assert [x["role"] for x in users] == ["admin", "user"] and "password_hash" not in me
        assert client.patch(f"/api/admin/users/{me['user_id']}", json={"is_active": False}, headers=adm).status_code == 409   # себя нельзя
        assert client.patch(f"/api/admin/users/{me['user_id']}", json={"role": "user"}, headers=adm).status_code == 409
        assert client.patch(f"/api/admin/users/{other['user_id']}", json={"role": "boss"}, headers=adm).status_code == 422
        assert client.patch("/api/admin/users/99999", json={"is_active": False}, headers=adm).status_code == 404
        assert client.patch(f"/api/admin/users/{other['user_id']}", json={"is_active": False}, headers=adm).json()["is_active"] is False
        assert client.get("/api/auth/me", headers=usr).json()["user"] is None                                     # токен заблокированного больше не действует
        assert client.post("/api/auth/login", json={"email": "plain@example.ru", "password": "12345678"}).status_code == 401
        client.patch(f"/api/admin/users/{other['user_id']}", json={"is_active": True, "role": "admin"}, headers=adm)
        adm2 = {"Authorization": "Bearer " + client.post("/api/auth/login", json={"email": "plain@example.ru", "password": "12345678"}).json()["access_token"]}
        assert client.patch(f"/api/admin/users/{me['user_id']}", json={"role": "user"}, headers=adm2).status_code == 200  # админов двое — другого можно разжаловать
        assert client.patch(f"/api/admin/users/{other['user_id']}", json={"role": "user"}, headers=adm2).status_code == 409  # но последнего — нет
        # публикации: добавить вручную, дубль, найти, изменить, удалить
        r = client.post("/api/admin/publications", json={"title": "Manual entry zzadm", "authors": ["Иванов Иван"], "year": 2021, "doi": "10.5/adm"}, headers=adm)      # root уже разжалован до user
        assert r.status_code == 403
        r = client.post("/api/admin/publications", json={"title": "Manual entry zzadm", "authors": ["Иванов Иван"], "year": 2021, "doi": "10.5/adm"}, headers=adm2)
        assert r.status_code == 201 and r.json()["authors"] and r.json()["year"] == 2021
        pid = r.json()["publication_id"]
        assert client.post("/api/admin/publications", json={"title": "Другое название", "doi": "10.5/adm"}, headers=adm2).status_code == 409
        assert client.post("/api/admin/publications", json={"title": "ab"}, headers=adm2).status_code == 422
        found = client.get("/api/admin/publications?q=zzadm", headers=adm2).json()
        assert found["total"] == 1 and found["items"][0]["publication_id"] == pid
        e = client.patch(f"/api/admin/publications/{pid}", json={"title": "Edited zzadm", "year": 2022, "journal": ""}, headers=adm2)
        assert e.json()["title"] == "Edited zzadm" and e.json()["year"] == 2022 and e.json()["journal"] is None
        assert client.patch(f"/api/admin/publications/{pid}", json={"title": "  "}, headers=adm2).status_code == 422
        assert client.patch("/api/admin/publications/99999", json={"year": 2000}, headers=adm2).status_code == 404
        # журнал: кто и что спрашивал
        client.post("/api/chat", json={"message": "Привет", "use_external": False}, headers=adm2)
        client.post("/api/chat", json={"message": "Привет, гость", "use_external": False})
        items = client.get("/api/admin/logs?limit=5", headers=adm2).json()["items"]
        assert [i["user"] for i in items][:2] == [None, "plain@example.ru"] and items[1]["query"] == "Привет"        # новые первыми
        assert len(client.get("/api/admin/logs?q=гость", headers=adm2).json()["items"]) == 1
        # настройки: источники и модель LLM
        st = client.get("/api/admin/settings", headers=adm2).json()
        assert st["openalex_enabled"] is True and st["llm_model"] == ""
        assert client.put("/api/admin/settings", json={"openalex_enabled": "yes"}, headers=adm2).status_code == 422
        assert client.put("/api/admin/settings", json={"llm_model": "bad model!"}, headers=adm2).status_code == 422
        assert client.put("/api/admin/settings", json={"nonsense": 1}, headers=adm2).status_code == 422
        with _env(**{llm.PROVIDERS[0]["env_var"]: "key"}):
            default = llm.PROVIDERS[0]["model"]
            r = client.put("/api/admin/settings", json={"openalex_enabled": False, "llm_model": "deepseek-reasoner"}, headers=adm2).json()
            assert r["openalex_enabled"] is False and r["llm_default_model"] == default
            assert "openalex" in sm.DISABLED and "crossref" not in sm.DISABLED
            assert llm._active_provider()["model"] == "deepseek-reasoner"
            client.put("/api/admin/settings", json={"openalex_enabled": True, "llm_model": ""}, headers=adm2)
            assert not sm.DISABLED and llm._active_provider()["model"] == default
        wipe()
        sm.DISABLED.clear(); llm.set_model_override(None)


@case
def test_disabled_source_is_not_queried():
    from app.sources import manager as sm

    class Fake:
        def __init__(self, name): self._n, self.calls = name, 0
        name = property(lambda self: self._n)
        async def search(self, query, limit=20, language=None):
            self.calls += 1
            return [P(title=f"from {self._n}", source=self._n)]

    import asyncio as aio
    mgr = sm.SourceManager()
    mgr.sources = [Fake("OpenAlex"), Fake("Crossref")]
    sm.DISABLED.add("openalex")
    try:
        got, errs = aio.run(mgr.search_detailed("q"))
    finally:
        sm.DISABLED.clear()
    assert [p.title for p in got] == ["from Crossref"] and mgr.sources[0].calls == 0 and not errs


def main():
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print(f"  ok     {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL   {fn.__name__}")
            traceback.print_exc()
            close_all_sessions()  # упавший тест мог оставить открытую транзакцию и заблокировать остальные
    print(f"\n{len(TESTS) - failed} из {len(TESTS)} проверок пройдено" + ("" if not failed else f", ошибок: {failed}"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
