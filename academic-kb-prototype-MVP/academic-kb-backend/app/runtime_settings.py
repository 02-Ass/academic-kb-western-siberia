"""Настройки, которые меняются в админ-панели без перезапуска: внешние источники и модель LLM.
Хранятся в таблице ``app_setting`` и применяются при запуске и при каждом изменении."""
import json
import re
from typing import Any, Dict

from sqlalchemy.orm import Session

from . import llm, models
from .sources import manager as sources_manager

DEFAULTS: Dict[str, Any] = {"openalex_enabled": True, "crossref_enabled": True, "llm_model": ""}
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,99}$")


def load(db: Session) -> Dict[str, Any]:
    values = dict(DEFAULTS)
    for row in db.query(models.AppSetting).all():
        if row.key in DEFAULTS:
            try:
                values[row.key] = json.loads(row.value)
            except ValueError:
                pass
    return values


def validate(updates: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in updates.items():
        if key not in DEFAULTS:
            raise ValueError(f"Неизвестная настройка: {key}")
        if isinstance(DEFAULTS[key], bool):
            if not isinstance(value, bool):
                raise ValueError(f"{key}: ожидается true/false")
        else:
            value = (value or "").strip() if isinstance(value, str) or value is None else None
            if value is None or (value and not _MODEL_RE.match(value)):
                raise ValueError("Название модели: латиница, цифры и . _ : / - (до 100 символов)")
        clean[key] = value
    return clean


def save(db: Session, updates: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in validate(updates).items():
        row = db.get(models.AppSetting, key)
        if row is None:
            db.add(models.AppSetting(key=key, value=json.dumps(value)))
        else:
            row.value = json.dumps(value)
    db.commit()
    values = load(db)
    apply(values)
    return values


def apply(values: Dict[str, Any]) -> None:
    sources_manager.DISABLED.clear()
    for name in ("openalex", "crossref"):
        if not values.get(f"{name}_enabled", True):
            sources_manager.DISABLED.add(name)
    llm.set_model_override(values.get("llm_model") or None)
