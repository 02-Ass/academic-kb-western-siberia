"""
Автоматическая оценка найденной во внешних базах публикации:
1) относится ли она к предметной области (регион + нефтегазовая тематика);
2) к каким месторождениям из локальной базы её привязать;
3) какие типы исследований ей соответствуют.

Это правила по ключевым словам, а не «понимание» текста: они дают разумную
первичную разметку, которую пользователь проверяет в предпросмотре перед
сохранением.
"""
import re
from dataclasses import dataclass, field as dc_field
from typing import Iterable, List, Optional, Sequence, Tuple

# Публикация должна упоминать регион исследования ...
_ANCHOR = re.compile(
    r"shaim|шаим|shushm|шушм|lovinsk|ловин|"
    r"west(?:ern)?[\s-]+siberia|западн\w*[\s-]+сибир|"
    r"tyumen|тюмен|priural|приураль|khanty|ханты|ural[\s-]+basin|"
    # Западная Сибирь часто называется не так, а через области и округа
    # или крупные месторождения («Мёговский район (Новосибирская и Томская области)»)
    r"yamal|ямал|\bugra\b|югр|"
    r"(?:tomsk|novosibirsk|omsk|kurgan)\s+(?:region|oblast)|(?:томск|новосибирск|омск|курган)\w*\s+област|"
    r"surgut|сургут|nizhnevartovsk|нижневартовск|samotlor|самотлор|priobsk|приобск|urengoy|уренгой",
    re.IGNORECASE,
)
# ... И нефтегазово-геологическую тематику. Без этого условия проходили бы
# статьи вроде «Левая оппозиция на Урале и в Западной Сибири».
_CONTEXT = re.compile(
    r"\boil\b|\bgas\b|petrol|hydrocarbon|reservoir|\bwells?\b|drill|formation|"
    r"aquifer|geolog|geophys|geochem|tecton|stratigr|lithol|basin|"
    r"нефт|\bгаз|углеводород|месторожд|коллектор|геолог|геофиз|геохим|"
    r"тектон|стратигр|литолог|скважин|бурен|водоносн|гидрогеолог|свит[аыу]\b|пласт",
    re.IGNORECASE,
)

# Привязка к месторождениям: (шаблон, фрагмент названия в БД). Название района «Шаим» само по себе месторождением
# не считается: «Shaim oil and gas region» — работа о районе, а «Shaim oil field» — о месторождении.
_FIELD_RULES: Sequence[Tuple["re.Pattern[str]", str]] = (
    (re.compile(r"shushm|шушм", re.I), "шушм"),
    (re.compile(r"lovinsk|ловин", re.I), "ловин"),
    (re.compile(
        r"shaim\w*[\s-]+(?:oil[\s-]+|gas[\s-]+|oil[\s-]and[\s-]gas[\s-]+)?(?:field|deposit)|"
        r"шаимск\w*\s+(?:нефтян\w+\s+|газов\w+\s+|нефтегазов\w+\s+)?месторожд", re.I), "шаим"),
)
# Верхние уровни: (шаблон в тексте, фрагмент названия объекта в БД).
_RAYON_RULES: Sequence[Tuple["re.Pattern[str]", str]] = (
    (re.compile(r"shaim|шаим", re.I), "шаим"),
)
_OBLAST_RULES: Sequence[Tuple["re.Pattern[str]", str]] = (
    (re.compile(r"priural|приураль|pre-?ural", re.I), "приураль"),
)
# Любая публикация, прошедшая is_relevant, относится как минимум к провинции (Западная Сибирь).
_PROVINCE_STEMS: Sequence[str] = ("сибир", "siberia")

# Типы исследований: (точное название в БД, шаблон)
_TYPE_RULES: Sequence[Tuple[str, "re.Pattern[str]"]] = (
    ("Гидрогеологическое", re.compile(
        r"groundwater|hydrogeolog|aquifer|formation water|mineraliz|"
        r"подземн\w*\s+вод|гидрогеолог|водоносн|минерализ|пластов\w*\s+вод", re.I)),
    ("Геофизическое", re.compile(
        r"seismic|geophys|well[\s-]log|logging|сейсм|геофиз|каротаж", re.I)),
    ("Геодинамическое", re.compile(
        r"tecton|geodynam|\brift|\bfault|subsidence|тектон|геодинам|разлом", re.I)),
    # (?<!hydro) / (?<!гидро): «гидрогеология» не должна считаться просто геологией
    ("Геологическое", re.compile(
        r"(?<!hydro)geolog|stratigr|lithol|sedimentolog|petroleum system|source rock|"
        r"reservoir|geochem|(?<!гидро)геолог|стратигр|литолог|коллектор|нефтегазонос|геохим", re.I)),
)


def publication_text(pub) -> str:
    # Название журнала сознательно НЕ учитывается: «Геология и геофизика» или
    # «Геология нефти и газа» иначе давали бы любой статье из них и тематику
    # нефти/геологии, и сразу несколько типов исследований независимо от
    # содержания самой статьи.
    parts = [pub.title or "", pub.abstract or ""]
    parts += list(getattr(pub, "keywords", None) or [])
    parts += list(getattr(pub, "alt_titles", None) or [])
    return " ".join(parts)


def is_relevant(pub) -> bool:
    text = publication_text(pub)
    return bool(_ANCHOR.search(text)) and bool(_CONTEXT.search(text))


def match_fields(text: str, fields: Iterable[Tuple[int, str]]) -> List[int]:
    """fields: (field_id, название). Возвращает id месторождений, упомянутых в тексте именно как месторождения."""
    fields = list(fields)
    found: List[int] = []
    for pattern, name_stem in _FIELD_RULES:
        if pattern.search(text):
            for field_id, name in fields:
                if name_stem in name.lower() and field_id not in found:
                    found.append(field_id)
    return found


def _match_named(text: str, rules, objects: Iterable[Tuple[int, str]]) -> List[int]:
    objects = list(objects)
    found: List[int] = []
    for pattern, stem in rules:
        if pattern.search(text):
            for obj_id, name in objects:
                if stem in name.lower() and obj_id not in found:
                    found.append(obj_id)
    return found


LEVEL_NAMES = {"field": "месторождение", "rayon": "район", "oblast": "область", "province": "провинция"}


@dataclass
class Placement:
    """Самый конкретный уровень иерархии, к которому относится публикация, и объекты этого уровня.
    Вышестоящие уровни не хранятся: они выводятся по иерархии (месторождение → район → область → провинция)."""
    level: Optional[str] = None          # field | rayon | oblast | province | None (регион не определён)
    field_ids: List[int] = dc_field(default_factory=list)
    rayon_ids: List[int] = dc_field(default_factory=list)
    oblast_ids: List[int] = dc_field(default_factory=list)
    province_ids: List[int] = dc_field(default_factory=list)

    def ids(self) -> List[int]:
        return getattr(self, f"{self.level}_ids") if self.level else []


def locate(text: str, fields=(), rayons=(), oblasts=(), provinces=()) -> Placement:
    """Определяет уровень привязки по тексту: месторождение > район > область > провинция.

    Правила по ключевым словам, как и остальная разметка: пользователь проверяет результат в предпросмотре.
    Каждый аргумент — список пар (id, название) из БД."""
    found = match_fields(text, fields)
    if found:
        return Placement("field", field_ids=found)
    found = _match_named(text, _RAYON_RULES, rayons)
    if found:
        return Placement("rayon", rayon_ids=found)
    found = _match_named(text, _OBLAST_RULES, oblasts)
    if found:
        return Placement("oblast", oblast_ids=found)
    if _ANCHOR.search(text) and _CONTEXT.search(text):
        provinces = list(provinces)
        ids = [pid for pid, name in provinces if any(st in name.lower() for st in _PROVINCE_STEMS)]
        if not ids and len(provinces) == 1:
            ids = [provinces[0][0]]
        if ids:
            return Placement("province", province_ids=ids)
    return Placement()


def match_research_types(text: str, types: Iterable[Tuple[int, str]]) -> List[int]:
    """types: (type_id, точное название типа исследования)."""
    by_name = {name: type_id for type_id, name in types}
    found: List[int] = []
    for type_name, pattern in _TYPE_RULES:
        if type_name in by_name and pattern.search(text):
            found.append(by_name[type_name])
    return found
