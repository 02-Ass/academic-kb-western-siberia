"""
Двуязычный словарь предметных терминов (геология и разработка нефтяных
месторождений Западной Сибири) для сквозного поиска.

Проблема, которую он решает: публикации из OpenAlex/Crossref в основном
англоязычные, а вопросы задаются по-русски. Поиск по общим словам не находит
английскую аннотацию по русскому вопросу (у них нет ни одного общего слова),
и наоборот. Словарь добавляет к запросу переводы известных терминов, поэтому
поиск работает даже без LLM и без внешних сервисов эмбеддингов.

Это сознательно небольшой словарь по предметной области, а не машинный
перевод: он не «понимает» фразы, а лишь добавляет соответствия для слов,
которые в этой теме встречаются постоянно. Дополнять его можно просто
добавив строку в ENTRIES.
"""
import re
from typing import Dict, List, Tuple

# (русский префикс слова, английские слова, русские формы для обратного перевода)
ENTRIES: List[Tuple[str, List[str], List[str]]] = [
    # регион и объекты
    ("шаим", ["shaim", "shaimsky"], ["шаимский", "шаимское"]),
    ("шушм", ["shushma", "shushminskoye"], ["шушминское"]),
    ("ловин", ["lovinskoye", "lovinsky"], ["ловинское"]),
    ("приурал", ["priural", "priuralsky"], ["приуральская"]),
    ("западн", ["west", "western"], ["западная"]),
    ("сибир", ["siberia", "siberian"], ["сибирь", "сибирская"]),
    ("ханты", ["khanty"], ["ханты"]),
    ("мансийск", ["mansi"], ["мансийский"]),
    ("югр", ["yugra", "ugra"], ["югра"]),
    ("тюмен", ["tyumen"], ["тюменская"]),
    ("сургут", ["surgut"], ["сургутский"]),
    ("самотлор", ["samotlor", "samotlorskoye"], ["самотлорское"]),
    ("приобск", ["priobskoye", "priobskoe"], ["приобское"]),
    ("уренгой", ["urengoy", "urengoi"], ["уренгойское"]),
    ("нижневартовск", ["nizhnevartovsk"], ["нижневартовский"]),
    ("томск", ["tomsk"], ["томская"]),
    ("васюган", ["vasyugan"], ["васюганская"]),
    ("баженов", ["bazhenov"], ["баженовская"]),
    ("нефтегазонос", ["petroleum", "oil", "gas", "bearing"], ["нефтегазоносный"]),
    ("нефтеносн", ["oil-bearing", "petroleum"], ["нефтеносный"]),
    ("нефтегаз", ["oil", "gas"], ["нефтегазовый"]),
    ("нефтеотдач", ["oil", "recovery"], ["нефтеотдача"]),
    ("нефт", ["oil", "petroleum", "crude"], ["нефть", "нефтяной"]),
    ("углеводород", ["hydrocarbon"], ["углеводороды"]),
    ("месторожд", ["field", "deposit"], ["месторождение"]),
    ("залеж", ["pool", "accumulation", "deposit"], ["залежь"]),
    ("пласт", ["reservoir", "bed", "formation", "horizon"], ["пласт"]),
    ("свит", ["formation", "suite"], ["свита"]),
    ("горизонт", ["horizon"], ["горизонт"]),
    ("коллектор", ["reservoir"], ["коллектор"]),
    ("бассейн", ["basin"], ["бассейн"]),
    ("провинц", ["province"], ["провинция"]),
    ("район", ["district", "region", "area"], ["район"]),
    # возраст и породы
    ("юрск", ["jurassic"], ["юрский"]),
    ("мелов", ["cretaceous"], ["меловой"]),
    ("триас", ["triassic"], ["триасовый"]),
    ("палеозо", ["paleozoic"], ["палеозойский"]),
    ("мезозо", ["mesozoic"], ["мезозойский"]),
    ("фундамент", ["basement"], ["фундамент"]),
    ("осадочн", ["sedimentary"], ["осадочный"]),
    ("песчан", ["sandstone", "sand"], ["песчаник"]),
    # свойства пластов и разработка
    ("пористост", ["porosity"], ["пористость"]),
    ("проницаем", ["permeability"], ["проницаемость"]),
    ("обводнен", ["water", "cut"], ["обводнённость"]),
    ("заводнен", ["waterflooding", "flooding"], ["заводнение"]),
    ("запас", ["reserves"], ["запасы"]),
    ("разработк", ["development", "exploitation"], ["разработка"]),
    ("разведк", ["exploration"], ["разведка"]),
    ("добыч", ["production", "extraction"], ["добыча"]),
    ("скважин", ["well", "borehole"], ["скважина"]),
    ("бурен", ["drilling"], ["бурение"]),
    ("керн", ["core"], ["керн"]),
    ("трудноизвлекаем", ["hard-to-recover", "unconventional"], ["трудноизвлекаемые"]),
    # науки и методы
    ("гидрогеолог", ["hydrogeology", "hydrogeological", "groundwater"], ["гидрогеология", "гидрогеологический"]),
    ("подземн", ["underground", "groundwater"], ["подземный"]),
    ("водоносн", ["aquifer"], ["водоносный"]),
    ("минерализ", ["mineralization", "salinity"], ["минерализация"]),
    ("геофиз", ["geophysical", "geophysics"], ["геофизический", "геофизика"]),
    ("сейсм", ["seismic"], ["сейсмический", "сейсмика"]),
    ("каротаж", ["logging", "log"], ["каротаж"]),
    ("геохим", ["geochemistry", "geochemical"], ["геохимия"]),
    ("геодинам", ["geodynamics", "geodynamic"], ["геодинамика"]),
    ("тектон", ["tectonic", "tectonics"], ["тектоника", "тектонический"]),
    ("разлом", ["fault"], ["разлом"]),
    ("стратиграф", ["stratigraphy", "stratigraphic"], ["стратиграфия"]),
    ("литолог", ["lithology", "lithological"], ["литология"]),
    ("геолог", ["geology", "geological"], ["геология", "геологический"]),
    ("строени", ["structure", "structural"], ["строение"]),
    ("металл", ["metal", "metals"], ["металл"]),
    ("редк", ["rare"], ["редкий"]),
    ("скандий", ["scandium"], ["скандий"]),
]
# одно слово без префиксного совпадения (иначе «вод» совпало бы с «водород»)
_EXACT_RU: Dict[str, List[str]] = {
    "вода": ["water"], "воды": ["water"], "воду": ["water"], "водой": ["water"], "вод": ["water"],
}

_ENTRIES_BY_LEN = sorted(ENTRIES, key=lambda e: len(e[0]), reverse=True)
_EN_INDEX: Dict[str, List[int]] = {}
for _i, (_pref, _en, _ru) in enumerate(ENTRIES):
    for _w in _en:
        _EN_INDEX.setdefault(_w, []).append(_i)

_WORD_RE = re.compile(r"[а-яёa-z0-9]+")
MAX_EXTRA = 14  # чтобы расширение не вытесняло исходный смысл запроса


def translations_split(text: str) -> Tuple[str, str]:
    """Переводы известных терминов запроса отдельно по направлениям:
    (русские слова -> английские, английские слова -> русские). Исходные слова
    запроса в результат не входят."""
    low = (text or "").lower()
    present = set(_WORD_RE.findall(low))
    fwd: List[str] = []
    rev: List[str] = []

    def add(target: List[str], words: List[str]) -> None:
        for w in words:
            if w not in present and w not in target:
                target.append(w)

    for token in _WORD_RE.findall(low):
        if re.search(r"[а-яё]", token):
            if token in _EXACT_RU:
                add(fwd, _EXACT_RU[token])
                continue
            for prefix, en_words, _ru in _ENTRIES_BY_LEN:
                if token.startswith(prefix):
                    add(fwd, en_words)
                    break
        else:
            base = token[:-1] if token.endswith("s") and len(token) > 3 else token
            for key in (token, base):
                for idx in _EN_INDEX.get(key, []):
                    add(rev, ENTRIES[idx][2])
    return " ".join(fwd[:MAX_EXTRA]), " ".join(rev[:MAX_EXTRA])


def translations(text: str) -> str:
    """Все переводы одной строкой (оба направления)."""
    fwd, rev = translations_split(text)
    return " ".join(x for x in (fwd, rev) if x)


def expand_query(text: str) -> str:
    """Исходный запрос плюс переводы известных терминов одной строкой."""
    extra = translations(text)
    return f"{text} {extra}" if extra else text
