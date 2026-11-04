"""Экспорт публикаций в CSV (открывается в Excel), JSON, BibTeX и RIS."""
import csv
import io
import json
import re
from typing import Callable, Dict, Iterable, List, Tuple

FORMATS: Dict[str, Tuple[str, str]] = {   # формат → (тип содержимого, расширение)
    "csv": ("text/csv; charset=utf-8", "csv"),
    "json": ("application/json; charset=utf-8", "json"),
    "bibtex": ("application/x-bibtex; charset=utf-8", "bib"),
    "ris": ("application/x-research-info-systems; charset=utf-8", "ris"),
}


def record(pub) -> dict:
    """Плоское описание публикации для любого формата. Строки с переводами строк/кавычками обрабатывают сами форматы."""
    places = [("месторождение", f.name) for f in pub.fields] + [("район", r.name) for r in pub.rayons] + \
             [("область", o.name) for o in pub.oblasts] + [("провинция", p.name) for p in pub.provinces]
    return {
        "id": pub.publication_id,
        "title": pub.title,
        "authors": [l.author.full_name for l in pub.author_links],
        "year": pub.year,
        "journal": pub.journal,
        "doi": pub.doi,
        "url": pub.link,
        "type": pub.type,
        "objects": [f"{kind}: {name}" for kind, name in places],
        "research_types": [t.name for t in pub.research_types],
    }


def _csv(rows: List[dict]) -> str:
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(["id", "Название", "Авторы", "Год", "Журнал", "DOI", "Ссылка", "Тип", "Объекты", "Типы исследований"])
    for r in rows:
        # защита от формул в Excel: ячейка, начинающаяся с = + - @, станет текстом
        safe = lambda v: ("'" + v) if isinstance(v, str) and v[:1] in "=+-@" else v  # noqa: E731
        w.writerow([r["id"], safe(r["title"]), safe("; ".join(r["authors"])), r["year"] or "", safe(r["journal"] or ""),
                    r["doi"] or "", r["url"] or "", r["type"] or "", safe("; ".join(r["objects"])), safe("; ".join(r["research_types"]))])
    return "﻿" + out.getvalue()   # BOM — чтобы Excel распознал UTF-8 и кириллицу


def _bib_escape(text: str) -> str:
    return re.sub(r"([&%$#_{}])", r"\\\1", text or "").replace("\n", " ")


def _bibtex(rows: List[dict]) -> str:
    entries, used = [], set()
    for r in rows:
        first = (r["authors"][0].split()[0] if r["authors"] else "anon")
        key = re.sub(r"\W+", "", first, flags=re.UNICODE) or "anon"
        key = f"{key}{r['year'] or 'nd'}"
        base, n = key, 1
        while key in used:
            n += 1
            key = f"{base}{chr(96 + n)}" if n < 27 else f"{base}{n}"
        used.add(key)
        kind = "article" if r["journal"] else "misc"
        fields = [("title", r["title"]), ("author", " and ".join(r["authors"])), ("year", r["year"]),
                  ("journal", r["journal"]), ("doi", r["doi"]), ("url", r["url"])]
        body = ",\n".join(f"  {k} = {{{_bib_escape(str(v))}}}" for k, v in fields if v)
        entries.append(f"@{kind}{{{key},\n{body}\n}}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def _ris(rows: List[dict]) -> str:
    out = []
    for r in rows:
        out.append("TY  - JOUR" if r["journal"] else "TY  - GEN")
        out.append(f"TI  - {r['title']}")
        out += [f"AU  - {a}" for a in r["authors"]]
        for tag, key in (("PY", "year"), ("JO", "journal"), ("DO", "doi"), ("UR", "url")):
            if r[key]:
                out.append(f"{tag}  - {r[key]}")
        out.append("ER  - ")
    return "\r\n".join(out) + ("\r\n" if out else "")


_RENDER: Dict[str, Callable[[List[dict]], str]] = {
    "csv": _csv, "bibtex": _bibtex, "ris": _ris,
    "json": lambda rows: json.dumps(rows, ensure_ascii=False, indent=2),
}


def export(pubs: Iterable, fmt: str) -> Tuple[str, str, str]:
    """Возвращает (текст, content-type, расширение файла). Порядок публикаций сохраняется."""
    if fmt not in FORMATS:
        raise ValueError(f"Неизвестный формат: {fmt}")
    ctype, ext = FORMATS[fmt]
    return _RENDER[fmt]([record(p) for p in pubs]), ctype, ext
