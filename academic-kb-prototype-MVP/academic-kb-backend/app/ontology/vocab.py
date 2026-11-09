"""
Словарь онтологии: адреса классов и свойств ВАШЕЙ онтологии (data/oil-gas-ontology.owl, Protégé) и
вспомогательные функции.

Принцип «онтология первична»: структуру (классы, свойства, подклассы, транзитивность, обратные свойства,
области определения и значений) задаёт файл онтологии. БД поставляет экземпляры и связывается с онтологией
по явной таблице соответствия (mapping.py). Всё, что приложение добавляет сверху, считается РАСШИРЕНИЕМ,
помечается как расширение и перечисляется отдельно (tbox.py, EXTENSIONS).
"""
import re

from rdflib import Namespace

NS = "http://west-siberia-ontology.org/ontology#"
O = Namespace(NS)

# --- классы из вашей онтологии
C_OBJECT = O["НефтегазовыйОбъект"]
C_PROVINCE = O["Провинция"]
C_OBLAST = O["Область"]
C_RAYON = O["Район"]
C_ZONE = O["ЗонаНефтегазонакопления"]
C_FIELD = O["Месторождение"]
C_HORIZON = O["Пласт"]
C_DEPOSIT = O["Залежь"]
C_PUB = O["Публикация"]
C_RESEARCH = O["Исследование"]

LEVEL_CLASS = {
    "province": C_PROVINCE, "oblast": C_OBLAST, "rayon": C_RAYON,
    "field": C_FIELD, "horizon": C_HORIZON, "deposit": C_DEPOSIT,
}
LEVELS = ["province", "oblast", "rayon", "field", "horizon", "deposit"]
LEVEL_NOM = {"province": "провинция", "oblast": "область", "rayon": "район",
             "field": "месторождение", "horizon": "пласт", "deposit": "залежь"}
LEVEL_ACC = {"province": "провинцию", "oblast": "область", "rayon": "район",
             "field": "месторождение", "horizon": "пласт", "deposit": "залежь"}
LEVEL_DAT = {"province": "провинции", "oblast": "области", "rayon": "району",
             "field": "месторождению", "horizon": "пласту", "deposit": "залежи"}
LEVEL_GEN = {"province": "провинции", "oblast": "области", "rayon": "района",
             "field": "месторождения", "horizon": "пласта", "deposit": "залежи"}

# --- объектные свойства из вашей онтологии
P_IN = O["входитВ"]
P_CONTAINS = O["содержит"]
P_HAS_FIELD = O["имеетМесторождение"]
P_HAS_HORIZON = O["имеетПласт"]
P_HAS_DEPOSIT = O["имеетЗалежь"]
P_ABOUT = O["относитсяКОбъекту"]
P_RESEARCH = O["относитсяКТипуИсследования"]

# --- свойства-данные из вашей онтологии
D_AUTHOR = O["автор"]
D_YEAR = O["год"]
D_JOURNAL = O["журнал"]
D_TITLE = O["название"]
D_LINK = O["ссылка"]

# --- РАСШИРЕНИЯ приложения (в вашем файле их нет)
P_HAS_PUB = O["имеетПубликацию"]          # обратное к относитсяКОбъекту
D_DOI = O["DOI"]
D_DESC = O["описание"]
D_DISCOVERY = O["годОткрытия"]
D_COORD = O["координаты"]
D_DBREF = O["записьБД"]                    # ключ записи в БД: «таблица:первичный ключ»

MEANINGFUL_PREDICATES = {P_IN, P_CONTAINS, P_ABOUT, P_HAS_PUB}

DEFINED_PREFIX = {"province": "Публикация_провинции_", "oblast": "Публикация_области_", "rayon": "Публикация_района_"}
DEFINED_LABEL = {"province": "Публикации по провинции «{}»", "oblast": "Публикации по области «{}»",
                 "rayon": "Публикации по району «{}»"}


def slug(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^\w]+", "_", text or "", flags=re.UNICODE).strip("_")
    return s[:limit]


def short(term) -> str:
    """Короткое имя IRI для показа: Шаимское вместо полного адреса."""
    t = str(term)
    return t[len(NS):] if t.startswith(NS) else t


def build_tbox(g, lit, base, reg):
    """Применяет к основе (вашей онтологии) расширения приложения и создаёт определяемые классы.
    Основа уже загружена в g (tbox.load_base). Возвращает список описаний расширений."""
    from . import tbox  # локальный импорт: tbox использует константы этого модуля
    return tbox.apply_extensions(g, lit, base, reg)
