"""Конфигурация бота из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_str(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        if required:
            raise RuntimeError(
                f"Не задана переменная окружения {name}. "
                f"Проверьте файл .env (см. .env.example)."
            )
        return default or ""
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Переменная {name} должна быть целым числом, получено: {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"Переменная {name} должна быть числом, получено: {raw!r}") from exc


def _env_chat_ids(name: str) -> list[int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError as exc:
            raise RuntimeError(
                f"Переменная {name} должна быть списком целых chat_id через запятую, "
                f"получено: {raw!r}"
            ) from exc
    return out


@dataclass
class Config:
    bot_token: str
    openai_api_key: str
    openai_base_url: str | None
    model: str
    allowed_chat_ids: list[int]
    admin_user_ids: list[int]
    cooldown_seconds: int
    history_size: int
    persona_path: Path
    temperature: float
    max_tokens: int
    log_level: str
    telegram_proxy_url: str | None
    telegram_api_url: str | None

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            bot_token=_env_str("BOT_TOKEN", required=True),
            openai_api_key=_env_str("OPENAI_API_KEY", required=True),
            openai_base_url=_env_str("OPENAI_BASE_URL") or None,
            model=_env_str("MODEL", "gpt-4o-mini"),
            allowed_chat_ids=_env_chat_ids("ALLOWED_CHAT_IDS"),
            admin_user_ids=_env_chat_ids("ADMIN_USER_IDS"),
            cooldown_seconds=_env_int("COOLDOWN_SECONDS", 60),
            history_size=_env_int("HISTORY_SIZE", 20),
            persona_path=Path(_env_str("PERSONA_FILE", "persona.txt")),
            temperature=_env_float("TEMPERATURE", 0.8),
            max_tokens=_env_int("MAX_TOKENS", 600),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
            telegram_proxy_url=_env_str("TELEGRAM_PROXY_URL") or None,
            telegram_api_url=_env_str("TELEGRAM_API_URL") or None,
        )
