"""
Формирование ответа с использованием LLM (раздел 3.6.7 отчёта).

Использует DeepSeek через OpenAI-совместимый клиент (DeepSeek API
реализует тот же протокол, что и OpenAI Chat Completions).

Ключ читается из переменной окружения DEEPSEEK_API_KEY — никогда не
хранится в коде. Если ключ не задан, функции этого модуля возвращают
None, и вызывающий код (app/search.py, app/main.py) откатывается на
шаблонную сборку ответа — система остаётся рабочей без LLM, просто с
менее развёрнутыми ответами.
"""
import asyncio
import contextvars
import os
import re
import threading
import time
from typing import AsyncIterator, Callable, List, Optional, Tuple

from openai import OpenAI

from . import scope_embed, telemetry
from .services.text_utils import strip_false_friends

# Поддерживаемые провайдеры — все используют OpenAI-совместимый протокол,
# поэтому единый клиент openai.OpenAI подходит для всех. Задайте ОДНУ из
# этих переменных окружения в .env — какая найдётся первой, та и
# используется. Порядок — по убыванию приоритета.
PROVIDERS = [
    {
        "env_var": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "label": "DeepSeek",
    },
    {
        "env_var": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "label": "Groq (Llama 3.3 70B)",
    },
    {
        "env_var": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-2.5-flash",
        "label": "Google Gemini",
    },
]

SYSTEM_PROMPT = """\
Ты — ассистент академической базы знаний по исследованиям и разработке
нефтегазовых месторождений Западной Сибири. Тебе передаётся вопрос
пользователя и набор публикаций, найденных в локальной базе знаний и во
внешних научных базах (OpenAlex, Crossref). Для многих публикаций
доступна аннотация (summary), но не полный текст.

Обязательные правила (раздел 3.6.7 отчёта):
- Отвечай ТОЛЬКО на основании предоставленного контекста. Не используй
  сведения, отсутствующие в найденных источниках.
- Не выдумывай публикации, авторов, годы, DOI или факты, которых нет в
  контексте.
- Строй ответ по содержанию аннотаций: называй конкретные выводы, объекты,
  методы и результаты, которые в них указаны, и ссылайся на источник по
  номеру, например [3].
- Если у источника есть только название и метаданные, не делай из него
  выводов о содержании — просто можешь упомянуть, что такая публикация
  существует.
- В конце ответа перечисли использованные источники: название, авторы, год,
  DOI или ссылка (если есть в контексте).
- Чётко разделяй подтверждённые данные (из источников) и собственные
  предположения, если они необходимы для связности ответа.
- Если контекста недостаточно для ответа, прямо скажи об этом и объясни,
  чего не хватает; не додумывай.
- Если в сообщении есть «Справка из базы знаний», это достоверные структурированные
  данные локальной БД (иерархия объектов, параметры месторождений, число публикаций).
  Их можно использовать для ответа, указывая «по данным базы знаний» (номер [n] для
  них не ставится). Параметры, которых в справке нет, в базе не заполнены: не
  придумывай значения и не подставляй их из общих знаний.
- Если вопрос содержит условие (например, период «после 2010 года»),
  проверяй его по году публикации и не включай в ответ источники, которые
  условию не удовлетворяют.
- Отвечай на русском языке, лаконично и по существу. Названия публикаций
  на английском оставляй как в оригинале.
- Приоритет источников (НОВОЕ):
  1. Локальная база — первоочередной, цитируй в первую очередь.
  2. OpenAlex / Crossref — дополняют, если в локальной мало.
  Если источники противоречат друг другу — укажи оба.
- Ответ — не более 400 слов, если не запрошена подробная сводка.
  Структурируй списком. Не пиши вводных фраз вроде «Конечно, помогу».

Сравнение объектов (жёсткое правило). Если пользователь просит сравнить два объекта (или более), ты ОБЯЗАН
вывести ответ в виде Markdown-таблицы с колонками: «Параметр», затем по одной колонке на каждый объект
(в заголовке — название объекта). Если по какому-то объекту в найденном контексте нет данных, пиши в ячейке
«Нет данных (в базе знаний)». НЕ игнорируй второй объект, даже если он не найден в локальной базе. Обязательно
выведи таблицу, а после неё — список источников. Строки таблицы берутся из справки базы знаний и аннотаций
(например: год открытия, продуктивный пласт, залежь, число публикаций); значения не придумывай. Если одного из
объектов нет в контексте, добавь после таблицы примечание об этом.

Если вопрос не относится к нефтегазовой тематике, отвечай ОДНИМ коротким предложением (отказ) и предлагай 2-3 темы по профилю. НЕ перечисляй источники, которые не относятся к вопросу, и НЕ оправдывайся.
"""


def _active_provider() -> Optional[dict]:
    """Возвращает конфигурацию первого провайдера, для которого задан
    API-ключ в переменных окружения, или None, если ни один не задан."""
    for provider in PROVIDERS:
        if os.environ.get(provider["env_var"]):
            # модель можно сменить из админ-панели (set_model_override), не правя .env и не перезапуская сервер
            return {**provider, "model": _MODEL_OVERRIDE} if _MODEL_OVERRIDE else provider
    return None


_MODEL_OVERRIDE: Optional[str] = None


def set_model_override(model: Optional[str]) -> None:
    """Название модели активного провайдера вместо стандартного; пустое значение возвращает стандартное."""
    global _MODEL_OVERRIDE
    model = (model or "").strip() or None
    if model != _MODEL_OVERRIDE:
        _MODEL_OVERRIDE = model
        clear_caches()                  # ответы прежней модели не должны отдаваться как ответы новой


def default_model() -> Optional[str]:
    """Стандартная модель активного провайдера (без переопределения)."""
    for provider in PROVIDERS:
        if os.environ.get(provider["env_var"]):
            return provider["model"]
    return None


def _get_client() -> Optional[OpenAI]:
    provider = _active_provider()
    if provider is None:
        return None
    api_key = os.environ.get(provider["env_var"])
    return OpenAI(api_key=api_key, base_url=provider["base_url"])


def is_configured() -> bool:
    return _active_provider() is not None


# НОВОЕ: один автоматический повтор при сбое сети/таймауте/лимите. Ошибки клиента (неверный ключ,
# некорректный запрос) не повторяются — они не исправятся сами.
RETRY_DELAY_SEC = 0.6
_NO_RETRY_STATUS = {400, 401, 403, 404, 422}


def _chat_create(client, **kwargs):
    """``client.chat.completions.create`` с одним повтором при временной ошибке и учётом токенов.

    Raises:
        Exception: вторая ошибка подряд (или ошибка, которую повторять бессмысленно) пробрасывается
            вызывающему коду, который решает, что вернуть (обычно None).
    """
    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as e:  # noqa: BLE001
        if getattr(e, "status_code", None) in _NO_RETRY_STATUS:
            raise
        print(f"[llm] Временный сбой ({type(e).__name__}), повторяю запрос один раз")
        telemetry.bump("retries")
        time.sleep(RETRY_DELAY_SEC)
        response = client.chat.completions.create(**kwargs)
    telemetry.add_usage(response)
    return response


# НОВОЕ: кэши в памяти (ключ — хэш текста вопроса; размер — LLM_CACHE_SIZE, по умолчанию 1000)
_SCOPE_CACHE = telemetry.LRUCache()
_REWRITE_CACHE = telemetry.LRUCache()
_INTENT_CACHE = telemetry.LRUCache()
_COMPARE_CACHE = telemetry.LRUCache()


def _history_key(history: Optional[List[dict]], n: int) -> str:
    """Хэш последних ``n`` реплик: от истории зависят уточняющие вопросы («а после 2018?»)."""
    tail = (history or [])[-n:]
    return telemetry.cache_key(*[f"{m.get('role')}:{m.get('content')}" for m in tail]) if tail else ""


def clear_caches() -> None:
    """Очищает кэши классификации и переписывания (нужно в тестах и после смены провайдера)."""
    _SCOPE_CACHE.clear()
    _REWRITE_CACHE.clear()
    _INTENT_CACHE.clear()
    _COMPARE_CACHE.clear()


def active_provider_label() -> Optional[str]:
    provider = _active_provider()
    return provider["label"] if provider else None


# НОВОЕ: единые признаки «локального» источника — используются и при группировке контекста, и
# при расчёте уверенности, чтобы определения не разошлись.
def _is_local(pub: dict) -> bool:
    """Локальный ли источник.

    Args:
        pub: словарь публикации (поле ``origin``).

    Returns:
        True, если ``origin`` пустой или содержит «локальная» (например, «локальная база знаний»).
    """
    origin = str(pub.get("origin") or "").lower()
    return not origin or "локальная" in origin


def _order_publications(publications: list[dict]) -> list[dict]:
    """Расставляет источники в порядке нумерации: сначала локальные, затем внешние.

    Порядок внутри группы сохраняется. Если вызывающий код уже передаёт сначала локальные, а потом
    внешние источники (так делает /api/chat), номера [n] совпадают с индексами его списка.

    Args:
        publications: список словарей публикаций.

    Returns:
        Новый список: локальные источники, затем внешние.
    """
    return [p for p in publications if _is_local(p)] + [p for p in publications if not _is_local(p)]


def _completeness(pub: dict) -> str:
    """Насколько полно известно содержание источника.

    Args:
        pub: словарь публикации; ``fulltext`` — явный признак полного текста (если вызывающий код его
            передал), ``text_snippet`` — найденный фрагмент или аннотация.

    Returns:
        «полный текст», «только аннотация» или «только метаданные».
    """
    if pub.get("fulltext") and pub.get("text_snippet"):
        return "полный текст"
    if pub.get("text_snippet"):
        return "только аннотация"
    return "только метаданные"


def _format_context(publications: List[dict]) -> str:
    """publications: список словарей вида
    {title, authors, year, journal, text_snippet, doi, url, origin, object_name, research_type, fulltext}
    origin — "локальная база" или название внешнего источника.

    НОВОЕ: источники делятся на две группы («ЛОКАЛЬНАЯ БАЗА (проверено)» и «ВНЕШНИЕ ИСТОЧНИКИ») со
    сквозной нумерацией [Источник 1], [Источник 2]…; у каждого указаны полнота содержания, объект и тип
    исследования."""
    ordered = _order_publications(publications)
    n_local = sum(1 for p in ordered if _is_local(p))
    blocks = []
    for i, pub in enumerate(ordered, start=1):
        if i == 1 and n_local:
            blocks.append("=== ЛОКАЛЬНАЯ БАЗА (проверено) ===")
        if i == n_local + 1:
            blocks.append("=== ВНЕШНИЕ ИСТОЧНИКИ (OpenAlex / Crossref) ===")
        snippet = pub.get("text_snippet")
        content = snippet if snippet else "(аннотация/текст недоступны — только метаданные)"
        lines = [
            f"[Источник {i}] {pub.get('title')}",
            f"Авторы: {pub.get('authors') or 'не указаны'}; "
            f"Год: {pub.get('year') or 'не указан'}; "
            f"Журнал: {pub.get('journal') or 'не указан'}",
            f"Происхождение: {pub.get('origin') or 'локальная база'}",
            f"Полнота: {_completeness(pub)}",
            f"Объект: {pub.get('object_name') or 'не указан'}",
            f"Тип исследования: {pub.get('research_type') or 'не указан'}",
        ]
        if pub.get("doi"):
            lines.append(f"DOI: {pub['doi']}")
        elif pub.get("url"):
            lines.append(f"Ссылка: {pub['url']}")
        lines.append(f"Содержание: {content}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


QUERY_REWRITE_PROMPT = (
    "Ты помогаешь искать научные статьи в базах OpenAlex и Crossref по "
    "геологии и разработке нефтегазовых месторождений (в первую очередь "
    "Западной Сибири). Преврати последнее сообщение пользователя в короткий "
    "поисковый запрос НА АНГЛИЙСКОМ языке (3–6 ключевых слов). Названия "
    "объектов транслитерируй общепринятым способом (Шаимский район → "
    "Shaim, Шаимское месторождение → Shaim oil field, Западная Сибирь → "
    "West Siberia, тюменская свита → Tyumen formation). Не включай "
    "служебные слова и условия по годам. Верни ТОЛЬКО запрос, без кавычек "
    "и пояснений.\n\n"
    # НОВОЕ: запреты и примеры
    "НЕ делай так:\n"
    "- Не включай условия по годам («after 2010»)\n"
    "- Не включай служебные слова («расскажи про»)\n"
    "- Не переводи «что такое X» в «what is X»\n\n"
    "Примеры:\n"
    "- «Гидрогеология Шаима после 2010» → Shaim hydrogeology Jurassic\n"
    "- «Работы по Самотлору» → Samotlor oil field geology"
)


QUERY_REWRITE_PROMPT_RU = (
    "Ты помогаешь искать научные статьи в базах OpenAlex и Crossref по "
    "геологии и разработке нефтегазовых месторождений (в первую очередь "
    "Западной Сибири). Преврати последнее сообщение пользователя в короткий "
    "поисковый запрос НА РУССКОМ языке (2–6 ключевых слов) в терминологии "
    "российских научных публикаций, например: «Шаимский нефтегазоносный "
    "район», «Шаимское месторождение», «Западная Сибирь», «тюменская свита», "
    "«юрские отложения». Не используй английские слова, служебные слова и "
    "условия по годам. Верни ТОЛЬКО запрос, без кавычек и пояснений.\n\n"
    # НОВОЕ: запреты и примеры (ответы — на русском, как требует этот вариант промпта)
    "НЕ делай так:\n"
    "- Не включай условия по годам («после 2010»)\n"
    "- Не включай служебные слова («расскажи про»)\n"
    "- Не превращай «что такое X» в вопрос: оставь только термин X\n\n"
    "Примеры:\n"
    "- «Гидрогеология Шаима после 2010» → гидрогеология Шаимский район юрские отложения\n"
    "- «Работы по Самотлору» → Самотлорское месторождение геология"
)


# НОВОЕ: признаки того, что модель вместо запроса вернула служебное сообщение
MAX_QUERY_WORDS = 6
_JUNK_PHRASES = (
    "не относится", "не могу", "ошибка", "извините", "к сожалению", "вне темы", "не связан",
    "i cannot", "i can't", "i'm sorry", "sorry", "unable to", "not related", "as an ai",
)


def _is_junk_query(raw: str, first_line: str) -> bool:
    """Похож ли ответ модели на служебное сообщение, а не на поисковый запрос.

    Args:
        raw: весь ответ модели.
        first_line: первая строка ответа (то, что пошло бы в поиск).

    Returns:
        True, если в ответе есть фразы отказа/ошибки или запрос длиннее ``MAX_QUERY_WORDS`` слов.
    """
    low = raw.lower()
    if any(phrase in low for phrase in _JUNK_PHRASES):
        return True
    return len(first_line.split()) > MAX_QUERY_WORDS


def generate_search_query(
    message: str,
    conversation_history: Optional[List[dict]] = None,
    language: str = "en",
) -> Optional[str]:
    """Переписывает вопрос пользователя (иногда уточняющий, вроде «а после
    2018?») в короткий поисковый запрос для OpenAlex/Crossref. language="en"
    — английские ключевые слова (максимальный охват: эти базы индексируют в
    основном англоязычные метаданные), language="ru" — русские ключевые слова
    в терминологии российских публикаций (для поиска русскоязычных работ).
    Возвращает None, если LLM не настроен, вызов не удался или ответ модели
    непригоден как запрос (служебное сообщение, длиннее 6 слов); при настроенном
    LLM вызывающий код в этом случае не ходит во внешние базы."""
    client = _get_client()
    if client is None:
        return None
    provider = _active_provider()

    # Ключ кэша — вопрос и язык. История входит в ключ (и в запрос к LLM) только для коротких уточнений
    # вроде «а после 2018?»: их смысл зависит от беседы, и чужой кэш дал бы неверный запрос. Самостоятельный
    # вопрос в новой беседе берётся из кэша и не тратит токены на переписывание.
    uses_history = _depends_on_history(message, conversation_history)
    cache_key = telemetry.cache_key(
        "rewrite", provider["label"], language, message.strip().lower(),
        _history_key(conversation_history, 6) if uses_history else "",
    )
    cached = _REWRITE_CACHE.get(cache_key)
    if cached is not None:
        telemetry.bump("cache_hits")
        return cached

    prompt = QUERY_REWRITE_PROMPT_RU if language == "ru" else QUERY_REWRITE_PROMPT
    messages = [{"role": "system", "content": prompt}]
    if uses_history:
        messages.extend(conversation_history[-6:])
    messages.append({"role": "user", "content": message})

    try:
        response = _chat_create(
            client,
            model=provider["model"],
            messages=messages,
            temperature=0,
            max_tokens=40,
        )
        raw = (response.choices[0].message.content or "").strip()
        if not raw:
            return None
        text = raw.splitlines()[0].strip().strip("\"'«»`")
        # НОВОЕ: защита от «мусорного» переписывания — служебный ответ модели (отказ, извинение, ошибка)
        # или слишком длинный текст нельзя отправлять в OpenAlex/Crossref как поисковый запрос
        if _is_junk_query(raw, text):
            print(f"[llm] Отброшена непригодная поисковая формулировка: {raw[:80]!r}")
            return None
        if text:
            _REWRITE_CACHE.set(cache_key, text)
        return text or None
    except Exception as e:
        print(f"[llm] Ошибка переписывания запроса ({provider['label']}): {e}")
        return None


# НОВОЕ: сколько последних реплик истории передаётся модели
MAX_HISTORY_MESSAGES = 6
_CLARIFICATION_PREFIX = "(Это уточнение к предыдущему вопросу.)"
_CITATION_RE = re.compile(r"\[(\d+)\]")


def _compute_confidence(publications: list[dict], text: str) -> str:
    """Оценка уверенности ответа без обращения к LLM.

    Баллы: +1 за ≥5 источников, +1 за ≥2 источника с найденным содержанием (``text_snippet``), +1 за
    ≥1 локальный источник, +1 за ≥2 разных цитируемых номера в тексте.

    Args:
        publications: источники, переданные модели.
        text: текст ответа (после проверки цитат).

    Returns:
        «high» при ≥3 баллах, «medium» при 2, иначе «low».
    """
    n_sources = len(publications)
    n_fulltext = sum(1 for p in publications if p.get("text_snippet"))
    n_local = sum(1 for p in publications if _is_local(p))
    n_cited = len(set(_CITATION_RE.findall(text or "")))
    score = (n_sources >= 5) + (n_fulltext >= 2) + (n_local >= 1) + (n_cited >= 2)
    if score >= 3:
        return "high"
    return "medium" if score >= 2 else "low"


def _verify_citations(text: str, n_sources: int) -> tuple[str, list[int]]:
    """Заменяет ссылки [N] на несуществующие источники на [?].

    Args:
        text: текст ответа модели.
        n_sources: сколько источников реально передано модели.

    Returns:
        Пара (исправленный текст, список плохих номеров N: N < 1 или N > n_sources).
    """
    bad: list[int] = []

    def fix(m: re.Match) -> str:
        n = int(m.group(1))
        if n < 1 or n > n_sources:
            bad.append(n)
            return "[?]"
        return m.group(0)

    fixed = _CITATION_RE.sub(fix, text)
    if bad:
        print(f"[llm] Битые цитаты: {bad}")
    return fixed, bad


def _stream_completion(client, provider: dict, messages: list, on_token: Callable[[str], None],
                       cancel: Optional[threading.Event]) -> Tuple[str, Optional[str]]:
    """Запрашивает ответ потоком, отдаёт куски текста в ``on_token`` и возвращает (полный текст, finish_reason).

    Токены считаются по последнему чанку (``stream_options.include_usage``); если провайдер этот параметр
    не принимает (HTTP 400/422), запрос повторяется без него — стрим работает, но токены не учитываются.
    """
    kwargs = dict(model=provider["model"], messages=messages, temperature=0.2, max_tokens=3000, stream=True)
    try:
        stream = _chat_create(client, stream_options={"include_usage": True}, **kwargs)
    except Exception as e:  # noqa: BLE001
        if getattr(e, "status_code", None) in _NO_RETRY_STATUS:
            stream = _chat_create(client, **kwargs)
        else:
            raise
    parts: List[str] = []
    finish: Optional[str] = None
    try:
        for chunk in stream:
            if cancel is not None and cancel.is_set():
                break
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                telemetry.add_tokens(getattr(usage, "total_tokens", None))
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(getattr(choices[0], "delta", None), "content", None)
            if delta:
                parts.append(delta)
                on_token(delta)
            if getattr(choices[0], "finish_reason", None):
                finish = choices[0].finish_reason
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    return "".join(parts), finish


async def generate_answer_stream(
    query: str,
    publications: List[dict],
    conversation_history: Optional[List[dict]] = None,
    kb_context: Optional[str] = None,
    intent: Optional[str] = None,
) -> AsyncIterator[Tuple[str, object]]:
    """Асинхронный генератор потокового ответа.

    Отдаёт пары ``("token", кусок_текста)`` по мере поступления, а в конце — ровно одну пару
    ``("done", результат)``, где результат — то, что вернул бы ``generate_answer(..., return_dict=True)``
    по полному тексту (проверенный текст, источники, ``confidence``, ``bad_citations``), либо None, если
    LLM не настроен или вызов не удался (тогда вызывающий код использует шаблонный ответ).

    Проверка ссылок на неполном тексте невозможна, поэтому она выполняется один раз в конце. Если
    потребитель прекращает чтение (клиент нажал «Стоп» или закрыл вкладку), чтение потока у провайдера
    останавливается, чтобы не тратить токены впустую.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    cancel = threading.Event()

    def put(item) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, item)

    def work() -> None:
        try:
            result = generate_answer(
                query, publications, conversation_history, kb_context, return_dict=True,
                on_token=lambda t: put(("token", t)), cancel=cancel, intent=intent,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[llm] Ошибка потокового ответа: {type(e).__name__}: {str(e)[:120]}")
            result = None
        put(("done", result))

    # контекст копируется, чтобы счётчики запроса (токены, повторы) из рабочего потока попадали в журнал
    loop.run_in_executor(None, contextvars.copy_context().run, work)
    try:
        while True:
            kind, value = await queue.get()
            yield kind, value
            if kind == "done":
                return
    finally:
        cancel.set()


def generate_answer(
    query: str,
    publications: List[dict],
    conversation_history: Optional[List[dict]] = None,
    kb_context: Optional[str] = None,
    return_dict: bool = True,
    on_token: Optional[Callable[[str], None]] = None,
    cancel: Optional[threading.Event] = None,
    intent: Optional[str] = None,
) -> dict | str | None:
    """
    Формирует ответ на основе найденного контекста через DeepSeek LLM.
    Возвращает None, если DEEPSEEK_API_KEY не задан или произошла ошибка
    вызова API — в этом случае вызывающий код должен откатиться на
    шаблонный ответ (см. app/search.py:_build_answer).

    НОВОЕ: при ``return_dict=True`` (по умолчанию) возвращается словарь
    ``{"text", "sources", "provider", "confidence", "bad_citations"}``; при ``False`` — только строка,
    как раньше. Нумерация источников [n] — сквозная: сначала локальные, затем внешние (см.
    ``_order_publications``). Ссылки [N] вне диапазона заменяются на [?]. В модель уходят только
    последние ``MAX_HISTORY_MESSAGES`` реплик истории.

    Args:
        query: вопрос пользователя.
        publications: источники (словари, см. ``_format_context``).
        conversation_history: предыдущие реплики [{"role", "content"}].
        kb_context: справка из БД, если есть.
        return_dict: вернуть словарь (True) или только текст (False).
        on_token: если задан, ответ запрашивается потоком (``stream=True``), и каждый кусок текста
            передаётся этой функции по мере поступления. Итоговый результат (проверка ссылок, уверенность)
            считается ПОСЛЕ завершения потока по полному тексту, как и без потока.
        cancel: событие отмены; если оно установлено, чтение потока прекращается, возвращается None.
        intent: намерение пользователя (``detect_intent``): к системной инструкции добавляется
            указание, как строить ответ (обзор статей, справка об объекте, сравнение, объяснение).

    Returns:
        dict | str | None: None, если LLM не настроен, вызов не удался или генерация отменена.
    """
    client = _get_client()
    if client is None:
        return None
    provider = _active_provider()

    ordered = _order_publications(publications)
    context = _format_context(ordered)

    # НОВОЕ: ограничиваем историю; если её пришлось обрезать, а вопрос короткий — помечаем уточнение
    history = list(conversation_history or [])
    truncated = len(history) > MAX_HISTORY_MESSAGES
    history = history[-MAX_HISTORY_MESSAGES:]
    prefix = f"{_CLARIFICATION_PREFIX} " if truncated and len(query.split()) < 5 else ""

    messages = [{"role": "system", "content": SYSTEM_PROMPT + intent_instructions(intent)}]
    messages.extend(history)

    kb_block = f"{kb_context}\n\n" if kb_context else ""
    messages.append({
        "role": "user",
        "content": (
            f"{prefix}Вопрос пользователя: {query}\n\n"
            f"{kb_block}"
            f"Найденный контекст (локальная база знаний и внешние источники):\n{context}\n\n"
            "Сформируй ответ по правилам из системной инструкции."
        ),
    })

    try:
        if on_token is None:
            response = _chat_create(
                client,
                model=provider["model"],
                messages=messages,
                temperature=0.2,
                max_tokens=3000,
            )
            choice = response.choices[0]
            text, finish = choice.message.content, choice.finish_reason
        else:
            text, finish = _stream_completion(client, provider, messages, on_token, cancel)
            if cancel is not None and cancel.is_set():
                return None
        if text and finish == "length":
            # ответ упёрся в лимит длины — честно сообщаем, а не обрываем молча
            text += "\n\n*[Ответ обрезан по лимиту длины модели. Уточните вопрос или задайте его по частям.]*"
        if not text:
            return None
        # НОВОЕ: пост-проверка цитат
        text, bad = _verify_citations(text, len(ordered))
        if not return_dict:
            return text
        return {
            "text": text,
            "sources": [
                {
                    "n": i, "title": p.get("title"), "authors": p.get("authors"), "year": p.get("year"),
                    "doi": p.get("doi"), "url": p.get("url"), "origin": p.get("origin") or "локальная база знаний",
                    "has_fulltext": bool(p.get("fulltext") and p.get("text_snippet")),
                }
                for i, p in enumerate(ordered, start=1)
            ],
            "provider": provider["label"],
            "confidence": _compute_confidence(ordered, text),
            "bad_citations": bad,
        }
    except Exception as e:
        # Не роняем запрос пользователя из-за сбоя внешнего API —
        # логируем и откатываемся на шаблонный ответ.
        print(f"[llm] Ошибка вызова {provider['label']} API: {e}")
        return None


SCOPE_PROMPT = (
    "Ты фильтр запросов к научной базе знаний по геологии и разработке нефтегазовых месторождений "
    "Западной Сибири (публикации, месторождения, пласты, нефтегазоносность, гидрогеология, геофизика, "
    "геохимия, разработка, а также научная литература по этим темам). Определи, относится ли последний "
    "вопрос пользователя к этой области, ДАЖЕ ЕСЛИ точного ответа в базе может не быть. "
    "Ответь ОДНИМ словом: «тема» — если относится; «вне» — если не связан (погода, кулинария, арифметика, "
    "спорт, развлечения, творческие просьбы, общие знания не по геологии и нефтегазу)."
)


# НОВОЕ: быстрая классификация по ключевым словам (без вызова LLM). Совпадение — с началом слова,
# чтобы «газ» не срабатывало на «магазин».
DOMAIN_KEYWORDS = (
    "шаим", "самотлор", "приобск", "месторожд", "пласт", "свита", "юрск", "нефт", "газ", "скважин",
    "геофиз", "геолог", "сейсм", "бс10", "юк1", "тюменск", "гидрогеолог", "коллектор",
)
# «Ложные друзья» («пластик», «газета») вырезаются из текста до поиска ключевых слов, см. text_utils.strip_false_friends.
# «разработк» убрано из списка: «разработка сайта» — не нефтегаз (настоящие вопросы всё равно содержат
# «месторожд», «нефт», «скважин» и т. п.).
OFFTOPIC_KEYWORDS = (
    "погода", "рецепт", "анекдот", "привет", "приготов", "готовить", "борщ", "кулинар", "блюд",
    "курс валют", "биткоин", "футбол", "хоккей", "фильм", "сериал", "гороскоп", "песн", "стих",
)
_DOMAIN_RE = re.compile(r"\b(?:" + "|".join(map(re.escape, DOMAIN_KEYWORDS)) + ")", re.IGNORECASE)
_OFFTOPIC_RE = re.compile(r"\b(?:" + "|".join(map(re.escape, OFFTOPIC_KEYWORDS)) + ")", re.IGNORECASE)


# Английские признаки темы (вопросы по-английски раньше всегда уходили в LLM). Только однозначные слова:
# «field», «gas», «well» слишком общие (playing field, gasoline, well-being).
_DOMAIN_EN_RE = re.compile(
    r"\b(?:samotlor|shaim|priobsk|tyumen|siberia|bazhenov|vasyugan|petroleum|oil[ -]?fields?|oil and gas|hydrocarbons?)",
    re.IGNORECASE,
)

# Короткий вопрос, смысл которого зависит от предыдущих реплик («а после 2018?», «расскажи подробнее»).
_FOLLOWUP_RE = re.compile(
    r"^\s*(?:а|и|но|ещё|еще|тогда|тоже|также|and|but|also|what about|how about)\b"
    r"|\b(?:этот|эта|это|эти|этом|этой|там|тут|он|она|оно|они|его|её|ее|их|подробнее|дальше|продолж\w*|more|further|it|they)\b",
    re.IGNORECASE,
)
MAX_SCOPE_HISTORY_CHARS = 300


def _depends_on_history(message: str, history: Optional[List[dict]]) -> bool:
    """Нужна ли классификации тема предыдущих реплик: есть история, вопрос короткий и похож на уточнение.

    Самостоятельный вопрос («Как научиться плавать?») от истории не зависит: его не нужно ни отправлять
    в LLM вместе с длинными прошлыми ответами (лишние токены), ни ключевать кэш по истории (иначе
    повторный вопрос в новой беседе не попадёт в кэш).
    """
    if not history:
        return False
    return len((message or "").split()) <= 6 and bool(_FOLLOWUP_RE.search((message or "").lower()))


def classify_scope_fast(message: str) -> str | None:
    """Быстрая классификация темы по ключевым словам, без LLM.

    Args:
        message: текст вопроса.

    Returns:
        «domain», если есть слово предметной области; «offtopic», если есть явный признак постороннего
        вопроса (погода, рецепт, анекдот, приветствие); иначе None (нужна LLM). Слова ищутся с начала
        слова, регистр не важен.
    """
    text = strip_false_friends((message or "").lower())
    if _DOMAIN_RE.search(text) or _DOMAIN_EN_RE.search(text):
        return "domain"
    if _OFFTOPIC_RE.search(text):
        return "offtopic"
    return None  # < 3 слов или нет признаков — решает LLM


def classify_scope(message: str, conversation_history: Optional[List[dict]] = None) -> Optional[str]:
    """Определяет тему вопроса. Порядок: кэш → эмбеддинги → ключевые слова → LLM.

    Эмбеддинги (``app/scope_embed.py``) решают, когда близость к теме явно высокая или низкая; при
    «серой зоне» или недоступном сервисе используются прежние ключевые слова, затем LLM.

    Args:
        message: текст вопроса.
        conversation_history: предыдущие реплики (учитываются LLM-веткой и защитой коротких уточнений).

    Returns:
        «domain», «offtopic» или None (ничего не удалось определённо — вызывающий код использует свои правила).
    """
    uses_history = _depends_on_history(message, conversation_history)
    key = telemetry.cache_key("scope", (message or "").strip().lower(), _history_key(conversation_history, 4) if uses_history else "")
    cached = _SCOPE_CACHE.get(key)
    if cached is not None:
        telemetry.bump("cache_hits")
        telemetry.note("scope_method", "cache")
        return cached

    result, method = None, None
    emb = scope_embed.classify(message, has_history=uses_history)
    if emb:
        result, method = emb, "embeddings"
    if result is None:
        result = classify_scope_fast(message)
        method = "keywords" if result else None
    if result is None:
        result = classify_scope_llm(message, conversation_history if uses_history else None)
        method = "llm" if result else None
    telemetry.note("scope_method", method)
    if result:
        _SCOPE_CACHE.set(key, result)
    return result


def classify_scope_llm(message: str, conversation_history: Optional[List[dict]] = None) -> Optional[str]:
    """«domain» — вопрос по теме базы, «offtopic» — вне темы, None — LLM не настроен или
    не ответил определённо (тогда вызывающий код использует правила). Внутренняя функция."""
    client = _get_client()
    if client is None:
        return None
    provider = _active_provider()
    messages = [{"role": "system", "content": SCOPE_PROMPT}]
    if conversation_history:
        # уточняющие вопросы («а после 2018?») зависят от контекста; длинные прошлые ответы обрезаем
        for m in conversation_history[-4:]:
            messages.append({"role": m.get("role", "user"), "content": str(m.get("content", ""))[:MAX_SCOPE_HISTORY_CHARS]})
    messages.append({"role": "user", "content": message})
    try:
        response = _chat_create(
            client, model=provider["model"], messages=messages, temperature=0, max_tokens=12,
        )
        raw = (response.choices[0].message.content or "")
        text = re.sub(r"^[^\wа-яё]+", "", raw.strip().lower())  # убираем кавычки «», **, точки в начале
        print(f"[llm] Тема вопроса: {raw.strip()!r} -> {text[:12]!r}")
        if text.startswith(("вне", "off")):
            return "offtopic"
        if text.startswith(("тем", "dom", "по теме")):
            return "domain"
        if "вне" in text[:20] and "тем" not in text[:20].replace("вне темы", ""):
            return "offtopic"
        return None
    except Exception as e:
        print(f"[llm] Ошибка определения темы ({provider['label']}): {e}")
        return None


# ---------------------------------------------------------------------------
# Маршрутизация по намерению пользователя (Intent-Aware Routing)
# ---------------------------------------------------------------------------
INTENTS = ("FIND_PUBLICATION", "FIND_OBJECT", "COMPARE_OBJECTS", "EXPLAIN", "OFFTOPIC", "UNKNOWN")
DEFAULT_INTENT = "FIND_PUBLICATION"

INTENT_PROMPT = (
    "Определи намерение пользователя в вопросе к научной базе знаний по геологии и разработке нефтегазовых "
    "месторождений Западной Сибири. Ответь СТРОГО ОДНИМ словом из списка, без кавычек и пояснений:\n"
    "FIND_PUBLICATION — ищет статьи и публикации по теме (например: «исследования по Шаиму», «статьи про гидрогеологию после 2015»);\n"
    "FIND_OBJECT — спрашивает про конкретный геологический объект: район, месторождение, пласт, залежь "
    "(например: «что такое пласт БС10», «расскажи про Шаимское месторождение»);\n"
    "COMPARE_OBJECTS — хочет сравнить два объекта (например: «сравни геологию Шаима и Самотлора»);\n"
    "EXPLAIN — просит объяснить термин или концепцию (например: «что такое заводнение», «объясни, как работает сейсморазведка»);\n"
    "OFFTOPIC — вопрос не по теме базы (рецепты, погода, спорт, программирование и т. п.);\n"
    "UNKNOWN — если намерение непонятно."
)

_INTENT_INSTRUCTIONS = {
    "FIND_PUBLICATION": (
        "Сфокусируйся на обзоре найденных статей: сгруппируй их по годам или методам исследования и укажи авторов."
    ),
    "FIND_OBJECT": (
        "Дай геологическую справку об объекте, а затем перечисли публикации, где он упоминается. Справку составляй "
        "только из «Справки из базы знаний» и найденных источников; если сведений об объекте там нет, прямо скажи об этом."
    ),
    "COMPARE_OBJECTS": (
        "Выведи сравнение Markdown-таблицей | Параметр | Объект 1 | Объект 2 | (в заголовках — названия объектов из "
        "справки «Сравнение объектов»), затем список источников. Используй только данные из справки и источников; "
        "в ячейках без данных пиши «Нет данных (в базе знаний)». Оба объекта должны быть в таблице, даже если по одному из них "
        "ничего не найдено. После таблицы — одна-две строки: главные сходства и различия, и примечание, если по какому-то "
        "объекту данных нет. У источников для каждого объекта указано название объекта в поле «Объект»."
    ),
    "EXPLAIN": (
        "Объясни термин или концепцию простым языком, опираясь на источники: сначала короткое определение, затем "
        "пример или уточнение из публикаций. Если определения в источниках нет, прямо скажи об этом."
    ),
}


def intent_instructions(intent: Optional[str]) -> str:
    """Дополнение к системному промпту для данного намерения (пустая строка для UNKNOWN/None/OFFTOPIC)."""
    text = _INTENT_INSTRUCTIONS.get((intent or "").upper())
    return f"\n\nТип вопроса: {intent.upper()}. {text}" if text else ""


def parse_intent(raw: Optional[str]) -> Optional[str]:
    """Достаёт метку намерения из ответа модели: терпим регистр, кавычки, точку, «Intent: …», лишние слова.
    Возвращает метку из INTENTS либо None, если ни одной метки в ответе нет."""
    if not raw:
        return None
    text = re.sub(r"[^A-Za-z_\s]", " ", raw.upper())
    for token in text.split():
        if token in INTENTS:
            return token
    # модель могла написать метку через пробел или дефис («FIND PUBLICATION», «find-object»)
    squashed = re.sub(r"\s+", "_", " ".join(text.split()))
    for label in INTENTS:
        if label in squashed:
            return label
    return None


def detect_intent(query: str, history: Optional[List[dict]] = None) -> str:
    """Определяет намерение пользователя одним коротким обращением к LLM (контекст публикаций не передаётся).

    Возвращает одну из INTENTS. Если LLM не настроен — ``FIND_PUBLICATION`` (как было до маршрутизации). Если
    вызов не удался или ответ не распознан — ``UNKNOWN`` (не кэшируется, повторный вопрос попробует снова).
    Результат кэшируется по тексту вопроса; история учитывается только для коротких уточнений
    («а после 2018?»), как и в ``classify_scope``.
    """
    client = _get_client()
    if client is None:
        return DEFAULT_INTENT
    provider = _active_provider()
    uses_history = _depends_on_history(query, history)
    key = telemetry.cache_key("intent", provider["label"], (query or "").strip().lower(),
                              _history_key(history, 4) if uses_history else "")
    cached = _INTENT_CACHE.get(key)
    if cached is not None:
        telemetry.bump("cache_hits")
        return cached

    messages = [{"role": "system", "content": INTENT_PROMPT}]
    if uses_history and history:
        for m in history[-4:]:
            messages.append({"role": m.get("role", "user"), "content": str(m.get("content", ""))[:MAX_SCOPE_HISTORY_CHARS]})
    messages.append({"role": "user", "content": query})
    try:
        response = _chat_create(client, model=provider["model"], messages=messages, temperature=0, max_tokens=10)
        raw = response.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001
        print(f"[llm] Ошибка определения намерения ({provider['label']}): {e}")
        return "UNKNOWN"
    label = parse_intent(raw)
    print(f"[llm] Намерение: {raw.strip()!r} -> {label}")
    if label is None:
        return "UNKNOWN"
    _INTENT_CACHE.set(key, label)
    return label


COMPARE_PROMPT = (
    "Пользователь просит сравнить два объекта (районы, месторождения, пласты, методы). Выпиши названия ровно двух "
    "сравниваемых объектов в формате: «первый | второй». Названия — как в вопросе, без лишних слов. "
    "Если двух объектов нет, ответь: НЕТ."
)


def extract_compare_objects(query: str) -> Optional[Tuple[str, str]]:
    """Достаёт из просьбы о сравнении два объекта через LLM (запасной вариант, когда правила
    ``services/routing.split_compare`` не справились). None — LLM не настроен, сбой или двух объектов нет."""
    client = _get_client()
    if client is None:
        return None
    provider = _active_provider()
    key = telemetry.cache_key("compare", provider["label"], (query or "").strip().lower())
    cached = _COMPARE_CACHE.get(key)
    if cached is not None:
        telemetry.bump("cache_hits")
        return cached
    try:
        response = _chat_create(
            client, model=provider["model"], temperature=0, max_tokens=40,
            messages=[{"role": "system", "content": COMPARE_PROMPT}, {"role": "user", "content": query}],
        )
        raw = (response.choices[0].message.content or "").strip().strip("«»\"'`")
    except Exception as e:  # noqa: BLE001
        print(f"[llm] Ошибка выделения объектов сравнения ({provider['label']}): {e}")
        return None
    parts = [p.strip(" «»\"'`.") for p in raw.split("|")]
    if len(parts) != 2 or not all(parts) or parts[0].lower() == parts[1].lower() or raw.upper().startswith("НЕТ"):
        return None
    pair = (parts[0], parts[1])
    _COMPARE_CACHE.set(key, pair)
    return pair
