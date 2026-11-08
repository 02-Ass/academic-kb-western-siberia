"""
Вспомогательные средства для чата: кэш, учёт токенов и логирование запросов.

Что здесь:
- ``LRUCache`` — простой потокобезопасный кэш в памяти с ограничением размера (старые записи вытесняются).
  Размер задаётся переменной окружения ``LLM_CACHE_SIZE`` (по умолчанию 1000; 0 — кэш выключен).
- ``start_request`` / ``note`` / ``add_usage`` — счётчики одного запроса к чату (токены LLM, повторы,
  попадания в кэш, решение классификатора, переписанный запрос). Хранятся в ``ContextVar`` как ссылка на
  один словарь, поэтому значения, записанные из рабочих потоков (``run_in_threadpool``), видны в обработчике.
- ``log_chat`` — запись одной строки JSON на запрос в ``logs/chat_logs.jsonl`` (путь — ``CHAT_LOG_PATH``,
  отключение — ``CHAT_LOG=0``). Любая ошибка записи молча игнорируется: журнал не должен ломать чат.
"""
import hashlib
import json
import os
import threading
from collections import OrderedDict
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_SIZE = 1000


def cache_size() -> int:
    """Максимальный размер кэша из окружения (0 — выключен). Читается при каждом обращении."""
    try:
        return max(0, int(os.environ.get("LLM_CACHE_SIZE", DEFAULT_CACHE_SIZE)))
    except ValueError:
        return DEFAULT_CACHE_SIZE


def cache_key(*parts: Any) -> str:
    """Ключ кэша: SHA-256 от текстов, из которых он составлен."""
    raw = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class LRUCache:
    """Потокобезопасный кэш «ключ → значение» с вытеснением давно не использованных записей."""

    def __init__(self) -> None:
        self._data: "OrderedDict[str, Any]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        if cache_size() == 0:
            return None
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return self._data[key]

    def set(self, key: str, value: Any) -> None:
        limit = cache_size()
        if limit == 0 or value is None:  # None (сбой/«неясно») не кэшируем: повторный запрос может удаться
            return
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > limit:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


# -- счётчики одного запроса -------------------------------------------------------
_request: ContextVar[Optional[Dict[str, Any]]] = ContextVar("chat_request_stats", default=None)


def start_request() -> Dict[str, Any]:
    """Начинает учёт запроса и возвращает словарь, в который пишут ``note`` и ``add_usage``."""
    stats: Dict[str, Any] = {"tokens_used": 0, "llm_calls": 0, "retries": 0, "cache_hits": 0}
    _request.set(stats)
    return stats


def note(key: str, value: Any) -> None:
    """Записывает значение в статистику текущего запроса (если учёт запущен)."""
    stats = _request.get()
    if stats is not None:
        stats[key] = value


def bump(key: str, n: int = 1) -> None:
    """Увеличивает числовой счётчик текущего запроса."""
    stats = _request.get()
    if stats is not None:
        stats[key] = stats.get(key, 0) + n


def add_usage(response: Any) -> None:
    """Прибавляет токены из ответа LLM (поле ``usage.total_tokens``) и считает вызов."""
    stats = _request.get()
    if stats is None:
        return
    stats["llm_calls"] = stats.get("llm_calls", 0) + 1
    try:
        total = getattr(getattr(response, "usage", None), "total_tokens", None)
        if isinstance(total, int):
            stats["tokens_used"] = stats.get("tokens_used", 0) + total
    except Exception:
        pass


def add_tokens(n) -> None:
    """Прибавляет токены, посчитанные отдельно (например, из последнего чанка потокового ответа)."""
    stats = _request.get()
    if stats is not None and isinstance(n, int):
        stats["tokens_used"] = stats.get("tokens_used", 0) + n


# -- журнал запросов ---------------------------------------------------------------
_log_lock = threading.Lock()


def log_path() -> Path:
    custom = os.environ.get("CHAT_LOG_PATH")
    return Path(custom) if custom else ROOT / "logs" / "chat_logs.jsonl"


def log_enabled() -> bool:
    return os.environ.get("CHAT_LOG", "1").strip().lower() not in ("0", "false", "no", "off")


def log_chat(record: Dict[str, Any]) -> None:
    """Сохраняет запись в базу данных. Ошибки записи не пробрасываются."""
    if not log_enabled():
        return
    try:
        from .database import SessionLocal
        from .models import ChatLog
        
        db = SessionLocal()
        try:
            log_entry = ChatLog(
                user_email=record.get("user"),
                query=record.get("query", ""),
                intent=record.get("intent") or record.get("scope"),
                route=record.get("route"),
                sources_count=record.get("sources_found", 0),
                tokens_used=record.get("tokens_used", 0),
                latency_ms=record.get("latency_ms", 0),
            )
            db.add(log_entry)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        print(f"[telemetry] не удалось записать журнал в БД: {type(e).__name__}: {str(e)[:100]}")