"""
Распознавание намерения в сообщении чата и ответы на «неподатные для поиска»
сообщения: приветствие, «как дела», благодарность, прощание, «что ты умеешь».

Зачем: на «Привет» бот раньше искал публикации (и при включённом внешнем поиске
мог притянуть случайные статьи), а в лучшем случае отвечал «релевантных публикаций
не найдено». Такие сообщения не требуют ни поиска, ни LLM: ответ мгновенный,
детерминированный и честный. Описание возможностей собирается из реального
состояния базы, поэтому бот не может пообещать то, чего нет.

Правила намеренно простые (регулярные выражения), а не вызов LLM:
- ответ не зависит от ключа и платных вызовов;
- не бывает «галлюцинаций» о собственных возможностях.

Главная опасность — принять за болтовню настоящий вопрос. Поэтому сообщение считается
разговорным, только если после вычёркивания приветствий и стандартных фраз в нём не
осталось содержательных слов. «Привет, расскажи про Шаимское месторождение» — это вопрос,
из него убирается только приветствие.
"""
import re
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy.orm import Session

from .. import models

_GREETINGS = [
    "добрый день", "добрый вечер", "доброе утро", "доброй ночи", "добрейшего дня", "добрейшего вечера",
    "здравствуйте", "здравствуй", "приветствую", "приветик", "привет", "здорово", "салют", "хай",
    "good morning", "good afternoon", "good evening", "hello", "hey", "hi",
]
_GREETINGS.sort(key=len, reverse=True)

_HOW_RE = re.compile(
    r"как (?:у (?:тебя|вас) )?(?:дела|жизнь|поживаешь|поживаете|настроение)|как (?:ты|вы)(?: там)?$|как сам|"
    r"how are you|how are things|how do you do|what'?s up|how'?s it going"
)
_THANKS_RE = re.compile(r"спасибо|благодарю|благодарствую|thanks|thank you|thx")
_BYE_RE = re.compile(r"^(?:ну )?(?:пока|до свидания|до встречи|всего доброго|всего хорошего|увидимся|bye|goodbye|see you)\b")
_CAPS_RE = re.compile(
    r"что (?:ты|вы) (?:умеешь|умеете|можешь|можете)(?: делать)?|"
    r"что (?:ты|вы) за (?:бот|программа|система)|"
    r"чем (?:ты|вы) (?:можешь|можете) (?:мне )?(?:помочь|быть полезн\w*)|"
    r"кто (?:ты|вы)(?: такой| такая| такое)?|что ты такое|"
    r"расскажи (?:мне )?(?:о|про) себе|расскажи что ты умеешь|"
    r"для чего (?:ты|тебя|нужен)|зачем (?:ты|тебя)|"
    r"как (?:тобой|вами|тебя|с тобой|с вами) (?:пользоваться|работать)|"
    r"какие (?:у тебя|у вас) (?:функции|возможности)|твои (?:функции|возможности)|"
    r"^(?:справка|помощь|помоги(?:те)?|help)$|"
    r"what can you do|what do you do|who are you|how (?:do i|to) use you|what are your (?:features|capabilities)"
)
# слова, которые сами по себе не делают сообщение вопросом по существу
_FILLER = set(
    "мне меня тебе тебя ты вы ваш твой это вот так ну же ли бы пожалуйста пожалуйста еще ещё "
    "делать делаешь делаете уметь можешь можете мочь сегодня сейчас там тут друг бот чат "
    "please you your the are how can do what is a me i to today there bot chatbot thanks thank "
    "всем всех пока кстати теперь большое огромное помощь помощи помощью ответ ответы отлично хорошо супер ок окей понятно ясно".split()
)


@dataclass
class Intent:
    kind: str  # smalltalk | capabilities | domain
    sub: Optional[str] = None  # greeting | howareyou | thanks | farewell
    message: str = ""  # для domain: сообщение без приветствия
    lang: str = "ru"


def _normalize(text: str) -> str:
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"[^\w\s?]", " ", t)   # знаки препинания и эмодзи в пробелы (вопросительный знак сохраняем)
    return re.sub(r"\s+", " ", t).strip()


def _lang(text: str) -> str:
    has_cyr = bool(re.search(r"[а-яё]", text, re.I))
    has_lat = bool(re.search(r"[a-z]", text, re.I))
    return "en" if has_lat and not has_cyr else "ru"


def _content_tokens(text: str) -> List[str]:
    return [w for w in re.findall(r"[a-zа-я0-9]+", text) if len(w) >= 3 and w not in _FILLER]


def classify(message: str) -> Intent:
    original = (message or "").strip()
    lang = _lang(original)
    rest = _normalize(original).replace("?", " ").strip()
    if not rest:
        return Intent("smalltalk", "greeting", original, lang)

    # 1. приветствие в начале сообщения (возможно, несколько подряд)
    had_greeting = False
    changed = True
    while changed and rest:
        changed = False
        for g in _GREETINGS:
            if rest == g or rest.startswith(g + " "):
                rest = rest[len(g):].strip()
                had_greeting = changed = True
                break

    # 2. разговорные шаблоны в остатке
    patterns = [("caps", _CAPS_RE), ("thanks", _THANKS_RE), ("howareyou", _HOW_RE), ("farewell", _BYE_RE)]
    found = {name: rx.search(rest) for name, rx in patterns}
    if any(found.values()):
        residual = rest
        for m in found.values():
            if m:
                residual = residual.replace(m.group(0), " ")
        if not _content_tokens(residual):
            if found["caps"]:
                return Intent("capabilities", None, original, lang)
            for name in ("thanks", "farewell", "howareyou"):
                if found[name]:
                    return Intent("smalltalk", name, original, lang)

    # 3. одно лишь приветствие
    if had_greeting and not _content_tokens(rest):
        return Intent("smalltalk", "greeting", original, lang)

    # 4. настоящий вопрос; приветствие из текста убираем, чтобы оно не мешало поиску
    if had_greeting and rest:
        stripped = re.sub(r"^[\s,.!;:-]*(?:" + "|".join(re.escape(g) for g in _GREETINGS) + r")[\s,.!;:-]*", "",
                          original, flags=re.I).strip() or original
        return Intent("domain", None, stripped, lang)
    return Intent("domain", None, original, lang)


# ---------------------------------------------------------------------------
# Ответы
# ---------------------------------------------------------------------------

def _pick(options: List[str], message: str) -> str:
    return options[sum(ord(c) for c in (message or "")) % len(options)]


def _stats(db: Session) -> dict:
    total = db.query(models.Publication).count()
    chunks = db.query(models.DocumentChunk.publication_id, models.DocumentChunk.source_filename).all()
    with_abstract = {pid for pid, src in chunks if (src or "").startswith("аннотация")}
    with_fulltext = {pid for pid, src in chunks if (src or "").startswith("полный текст")}
    rayon = db.query(models.Rayon).order_by(models.Rayon.rayon_id).first()
    field = db.query(models.Field).order_by(models.Field.field_id).first()
    return {"total": total, "abstract": len(with_abstract), "fulltext": len(with_fulltext),
            "rayon": rayon.name if rayon else None, "field": field.name if field else None}


def example_questions(db: Session, lang: str = "ru") -> List[str]:
    st = _stats(db)
    if lang == "en":
        return ["What research was carried out in the Shaim district?", "What is known about groundwater mineralization near oil fields?"]
    ex = []
    if st["rayon"]:
        ex += [f"Какие исследования проводились в районе «{st['rayon']}»?", f"Какие месторождения входят в «{st['rayon']}»?"]
    ex.append("Что известно о минерализации подземных вод вблизи нефтяных месторождений?")
    return ex[:3]


def smalltalk_reply(intent: Intent, db: Session) -> str:
    ex = example_questions(db, intent.lang)
    sub = intent.sub or "greeting"
    if intent.lang == "en":
        offer = f" For example: “{ex[0]}”"
        return {
            "greeting": _pick(["Hello!", "Hi!"], intent.message) + " I help find information in an academic knowledge base on the geology and development of oil and gas fields in West Siberia." + offer,
            "howareyou": "Thanks, I'm fine — I'm a program, but I'm ready to work. I can search the knowledge base and answer with references." + offer,
            "thanks": "You're welcome! Ask me anything else about publications or fields.",
            "farewell": "Goodbye! Come back whenever you need to find something on the topic.",
        }[sub]
    offer = f" Например: «{ex[0]}»"
    return {
        "greeting": _pick(["Здравствуйте!", "Привет!"], intent.message)
        + " Я помогаю искать информацию в академической базе знаний по геологии и разработке нефтегазовых месторождений Западной Сибири."
        + offer + " Напишите «что ты умеешь», чтобы узнать подробнее.",
        "howareyou": "Спасибо, всё в порядке: я программа, но к работе готова. Могу искать по базе публикаций и отвечать со ссылками на источники."
        + offer,
        "thanks": _pick(["Пожалуйста!", "Рад помочь!"], intent.message) + " Если появятся ещё вопросы по публикациям или месторождениям, спрашивайте.",
        "farewell": "До свидания! Возвращайтесь, когда понадобится найти что-нибудь по теме.",
    }[sub]


def capabilities_reply(intent: Intent, db: Session, llm_configured: bool, external_possible: bool = True) -> str:
    """Описание возможностей по реальному состоянию системы: числа берутся из базы,
    а ограничения перечисляются явно."""
    st = _stats(db)
    ex = example_questions(db, intent.lang)
    if intent.lang == "en":
        lines = [
            "I'm a chatbot for an academic knowledge base on the geology and development of oil and gas fields in West Siberia.",
            "",
            "What I can do:",
            f"- answer questions from the local base ({st['total']} publications; {st['abstract']} have an abstract"
            + (f", {st['fulltext']} have full text" if st["fulltext"] else "") + "), with numbered references you can open;",
            "- add recent publications found in OpenAlex and Crossref (the checkbox under the chat);",
            "- describe objects in the base (district, fields, formations, deposits) using the fields that are filled in;",
            "- understand questions in Russian and English.",
            "",
            "Limits: I answer only from what I find and say so when there is no information; I mostly read abstracts, not full texts; "
            "I don't answer questions outside geology and the oil and gas industry."
            + ("" if llm_configured else " The language model is not connected now, so I only list what I found."),
            "",
            "Try: " + " / ".join(f"“{e}”" for e in ex),
        ]
        return "\n".join(lines)
    texts = [
        "Я чат-бот академической базы знаний по геологии и разработке нефтегазовых месторождений Западной Сибири.",
        "",
        "Что я умею:",
        f"- отвечать на вопросы по публикациям локальной базы ({st['total']} публикаций, у {st['abstract']} есть аннотация"
        + (f", у {st['fulltext']} загружен полный текст" if st["fulltext"] else "")
        + ") со ссылками на источники, которые можно открыть;",
    ]
    if external_possible:
        texts.append("- дополнять ответ свежими публикациями из OpenAlex и Crossref (галочка под чатом; язык можно выбрать);")
    texts += [
        "- рассказывать об объектах базы (район, месторождения, пласты, залежи) по тем данным, которые заполнены;",
        "- понимать вопросы на русском и английском: поиск идёт на обоих языках.",
        "",
        "Ограничения: отвечаю только по найденному и прямо говорю, если информации нет, а не придумываю; "
        "чаще всего читаю аннотации, а не полные тексты; на вопросы вне геологии и нефтегазовой отрасли не отвечаю."
        + ("" if llm_configured else " Сейчас языковая модель не подключена, поэтому я только перечисляю найденное без пересказа."),
        "",
        "Попробуйте: " + "; ".join(f"«{e}»" for e in ex),
    ]
    return "\n".join(texts)


# НОВОЕ: фиксированный ответ при жёстком блоке вопроса вне темы (когда тема определена до всякого поиска)
OFFTOPIC_HARD_REPLY = (
    "Я — академический ассистент по геологии и разработке нефтегазовых месторождений Западной Сибири. "
    "К сожалению, я не могу помочь с вопросами из других областей.\n\nПопробуйте спросить что-то по теме:"
)
OFFTOPIC_HARD_REPLY_EN = (
    "I am an academic assistant on the geology and development of oil and gas fields in West Siberia. "
    "Unfortunately, I cannot help with questions from other areas.\n\nTry asking something on topic:"
)
OFFTOPIC_SUGGESTIONS = [
    "Какие исследования проводились в Шаимском районе?",
    "Геологическое строение Самотлора",
    "Публикации по пласту БС10",
]
OFFTOPIC_SUGGESTIONS_EN = [
    "What research has been done in the Shaim district?",
    "Geological structure of the Samotlor field",
    "Publications on the BS10 formation",
]


def offtopic_suggestions(intent: Intent) -> list:
    """Темы-подсказки для кнопок под отказом (язык по вопросу)."""
    return list(OFFTOPIC_SUGGESTIONS_EN if intent.lang == "en" else OFFTOPIC_SUGGESTIONS)


def offtopic_hard_reply(intent: Intent) -> str:
    """Фиксированный отказ без поиска и без обращения к LLM (русский или английский по языку вопроса)."""
    return OFFTOPIC_HARD_REPLY_EN if intent.lang == "en" else OFFTOPIC_HARD_REPLY


def offtopic_reply(intent: Intent, db: Session, confident: bool = True) -> str:
    ex = example_questions(db, intent.lang)
    if intent.lang == "en":
        base = ("This question is outside the topic of the knowledge base (geology and development of oil and gas fields in West Siberia), "
                "so I won't answer it or search publications for it." if confident else
                "I couldn't connect this question with the topic of the knowledge base (geology and oil and gas fields of West Siberia). "
                "If it is on topic, please rephrase it with specific terms.")
        return base + f" I can help with questions such as “{ex[0]}”. Write “what can you do” to learn more."
    base = ("Этот вопрос не относится к теме базы знаний (геология и разработка нефтегазовых месторождений Западной Сибири), "
            "поэтому я не буду на него отвечать и не стану искать для него публикации." if confident else
            "Не удалось связать этот вопрос с темой базы знаний (геология и нефтегазовые месторождения Западной Сибири). "
            "Если он по теме, переформулируйте его, указав конкретные термины или объекты.")
    return base + f" Я могу помочь, например, с такими вопросами: «{ex[0]}». Напишите «что ты умеешь», чтобы узнать подробнее."


def looks_domain(message: str) -> bool:
    """Запасная проверка «по теме ли вопрос», если LLM не подключён: есть ли в тексте
    термины предметной области (двуязычный словарь, признаки региона, нефтегазовая лексика)."""
    from . import classifier, glossary
    from .text_utils import strip_false_friends

    message = strip_false_friends(message)  # «пластик», «газета» не должны срабатывать как «пласт», «газ»
    fwd, rev = glossary.translations_split(message)
    if fwd or rev:
        return True
    return bool(classifier._ANCHOR.search(message) or classifier._CONTEXT.search(message))
