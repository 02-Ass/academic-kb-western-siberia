"""Авторизация: регистрация, вход, роли (user / admin). Только стандартная библиотека — новых пакетов не нужно.

Пароль хранится как PBKDF2-HMAC-SHA256 (соль + 240 000 итераций); вход выдаёт подписанный токен (JWT, HS256).
Первый зарегистрированный пользователь становится администратором (или тот, чей email указан в ADMIN_EMAIL).

Настройки (.env):
  AUTH_ENFORCE=1      0 — защита выключена (все действия доступны без входа; удобно при разработке)
  AUTH_SECRET=...     ключ подписи токенов; если не задан — создаётся файл .auth_secret рядом с проектом
  AUTH_TOKEN_HOURS=12 срок жизни токена
  ADMIN_EMAIL=...     этот email всегда получает роль admin при регистрации
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from . import models
from .database import get_db

_ITERATIONS = 240_000
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SECRET_FILE = Path(__file__).resolve().parent.parent / ".auth_secret"
_secret_cache: Optional[bytes] = None


def enforced() -> bool:
    return os.getenv("AUTH_ENFORCE", "1") != "0"


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL.match(email) or len(email) > 254:
        raise HTTPException(status_code=422, detail="Некорректный email")
    return email


def check_password_strength(password: str) -> None:
    if len(password or "") < 8:
        raise HTTPException(status_code=422, detail="Пароль должен быть не короче 8 символов")


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def _secret() -> bytes:
    global _secret_cache
    env = os.getenv("AUTH_SECRET")
    if env:
        return env.encode("utf-8")
    if _secret_cache is None:
        try:
            _secret_cache = _SECRET_FILE.read_text().strip().encode("utf-8") if _SECRET_FILE.exists() else b""
            if not _secret_cache:
                _secret_cache = secrets.token_hex(32).encode("utf-8")
                _SECRET_FILE.write_text(_secret_cache.decode("utf-8"))
        except OSError:
            _secret_cache = secrets.token_hex(32).encode("utf-8")  # только на время работы процесса
    return _secret_cache


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def create_token(user: "models.User", now: Optional[float] = None) -> str:
    hours = float(os.getenv("AUTH_TOKEN_HOURS", "12"))
    issued = int(now if now is not None else time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps({"sub": user.user_id, "role": user.role, "iat": issued, "exp": issued + int(hours * 3600)}).encode())
    sig = _b64(hmac.new(_secret(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"


def decode_token(token: str, now: Optional[float] = None) -> Optional[dict]:
    try:
        header, body, sig = token.split(".")
        expected = _b64(hmac.new(_secret(), f"{header}.{body}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        if json.loads(_unb64(header)).get("alg") != "HS256":
            return None
        payload = json.loads(_unb64(body))
        if payload["exp"] < (now if now is not None else time.time()):
            return None
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def register_user(db: Session, email: str, password: str, full_name: Optional[str] = None) -> "models.User":
    email = normalize_email(email)
    check_password_strength(password)
    if db.query(models.User).filter(models.User.email == email).first():
        raise HTTPException(status_code=409, detail="Пользователь с таким email уже зарегистрирован")
    first = db.query(models.User).count() == 0
    admin_email = (os.getenv("ADMIN_EMAIL") or "").strip().lower()
    user = models.User(
        email=email, password_hash=hash_password(password), full_name=(full_name or "").strip() or None,
        role="admin" if first or (admin_email and email == admin_email) else "user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, email: str, password: str) -> "models.User":
    user = db.query(models.User).filter(models.User.email == (email or "").strip().lower()).first()
    # одинаковая ошибка для «нет пользователя» и «неверный пароль» — по ответу нельзя перебирать адреса
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")
    return user


def _user_from_header(authorization: Optional[str], db: Session) -> Optional["models.User"]:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    payload = decode_token(authorization[7:].strip())
    user = db.get(models.User, payload["sub"]) if payload else None
    return user if user is not None and user.is_active else None


def current_user(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> Optional["models.User"]:
    """Пользователь по токену или None (читать и искать можно без входа)."""
    return _user_from_header(authorization, db)


def require_user(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> Optional["models.User"]:
    if not enforced():
        return _user_from_header(authorization, db)
    user = _user_from_header(authorization, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Нужно войти в систему", headers={"WWW-Authenticate": "Bearer"})
    return user


def require_admin(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> Optional["models.User"]:
    user = require_user(authorization, db)
    if enforced() and user.role != "admin":
        raise HTTPException(status_code=403, detail="Действие доступно только администратору")
    return user
