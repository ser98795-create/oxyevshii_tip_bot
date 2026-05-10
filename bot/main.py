"""Точка входа: запуск Telegram-бота на long polling."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from .config import Config
from .handlers import setup as setup_handlers
from .llm import LLM
from .memory import Memory
from .persona import Persona


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # aiogram сам по себе шумный — приглушаем DEBUG.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def _amain() -> None:
    config = Config.from_env()
    _configure_logging(config.log_level)
    log = logging.getLogger("bot")
    log.info(
        "Старт бота: model=%s base_url=%s allowed_chats=%s admins=%s cooldown=%ds history=%d",
        config.model,
        config.openai_base_url or "<openai-default>",
        config.allowed_chat_ids or "<все чаты — НЕ РЕКОМЕНДУЕТСЯ>",
        config.admin_user_ids or "<нет — управляющие команды НИКОМУ недоступны>",
        config.cooldown_seconds,
        config.history_size,
    )
    if not config.admin_user_ids:
        log.warning(
            "ADMIN_USER_IDS не задан. Команды управления (/persona, /reload, /mute, "
            "/unmute, /status) будут отклоняться. Узнайте свой user_id командой /whoami "
            "и пропишите его в .env."
        )
    if not config.allowed_chat_ids:
        log.warning(
            "ALLOWED_CHAT_IDS не задан. Бот будет отвечать в ЛЮБОМ чате, куда его добавят. "
            "Узнайте chat_id командой /chatid и пропишите его в .env."
        )

    # parse_mode по умолчанию НЕ ставим: ответы LLM — свободный текст,
    # и любой случайный «<», «>» или «&» от модели ронял отправку
    # (Telegram отбивал «can't parse entities»). Команды, которым нужен
    # HTML (/chatid, /whoami, /status), указывают parse_mode явно.
    bot = Bot(token=config.bot_token)
    dp = Dispatcher()
    memory = Memory(history_size=config.history_size)
    persona = Persona(path=config.persona_path)
    llm = LLM(
        api_key=config.openai_api_key,
        base_url=config.openai_base_url,
        model=config.model,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
    )
    setup_handlers(dp, bot, config, memory, persona, llm)
    log.info("Персона (превью): %s", persona.text()[:120].replace("\n", " "))

    me = await bot.me()
    log.info("Бот @%s (id=%s) готов", me.username, me.id)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(_amain())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
