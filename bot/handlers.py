"""Обработчики сообщений Telegram."""

from __future__ import annotations

import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, User

from .config import Config
from .dossier import Dossier
from .llm import LLM
from .memory import HistoryMessage, Memory
from .persona import Persona

log = logging.getLogger(__name__)


def _user_display_name(user: User | None) -> str:
    if user is None:
        return "Аноним"
    parts: list[str] = []
    if user.first_name:
        parts.append(user.first_name)
    if user.last_name:
        parts.append(user.last_name)
    if not parts and user.username:
        parts.append(user.username)
    if not parts:
        parts.append(f"user{user.id}")
    return " ".join(parts)


def _full_user_name(message: Message) -> str:
    return _user_display_name(message.from_user)


def _is_admin(config: Config, message: Message) -> bool:
    user = message.from_user
    if user is None:
        return False
    # Если список админов не задан — никто не админ. Это сознательное решение:
    # лучше требовать явной настройки, чем случайно отдать управление кому угодно.
    return user.id in config.admin_user_ids


def _resolve_target_from_reply(message: Message) -> tuple[int, str] | None:
    """Если команда — reply на чьё-то сообщение, вернёт (user_id, имя) этого человека."""
    reply = message.reply_to_message
    if reply is None or reply.from_user is None:
        return None
    u = reply.from_user
    return (u.id, _user_display_name(u))


def setup(
    dp: Dispatcher,
    bot: Bot,
    config: Config,
    memory: Memory,
    persona: Persona,
    llm: LLM,
    dossier: Dossier,
) -> None:
    @dp.message(Command("chatid"))
    async def cmd_chatid(message: Message) -> None:
        # Публичная команда (нужна для самой первой настройки —
        # узнать chat_id, чтобы добавить его в ALLOWED_CHAT_IDS).
        await message.reply(
            f"chat_id: <code>{message.chat.id}</code>\n"
            f"тип: {message.chat.type}",
            parse_mode="HTML",
        )

    @dp.message(Command("whoami"))
    async def cmd_whoami(message: Message) -> None:
        # Публичная команда: показывает user_id отправителя — нужно для
        # того, чтобы вписать себя в ADMIN_USER_IDS при первоначальной настройке.
        if message.from_user is None:
            return
        await message.reply(
            f"user_id: <code>{message.from_user.id}</code>",
            parse_mode="HTML",
        )

    @dp.message(Command("persona"))
    async def cmd_persona(message: Message) -> None:
        if not _is_admin(config, message):
            return
        text = persona.text()
        snippet = text if len(text) <= 3500 else text[:3500] + "…"
        await message.reply(f"Текущая персона:\n\n{snippet}")

    @dp.message(Command("reload"))
    async def cmd_reload(message: Message) -> None:
        if not _is_admin(config, message):
            return
        # Persona перечитывает файл по mtime автоматически на каждом обращении,
        # эта команда — просто явное подтверждение и удобство для админа.
        text = persona.text()
        await message.reply(
            f"Персона перечитана из файла. Длина: {len(text)} символов.\n"
            f"Превью: {text[:200]!s}…"
        )

    @dp.message(Command("mute"))
    async def cmd_mute(message: Message) -> None:
        if not _is_admin(config, message):
            return
        memory.set_muted(message.chat.id, True)
        await message.reply("Заглушил себя в этом чате. Снять: /unmute")

    @dp.message(Command("unmute"))
    async def cmd_unmute(message: Message) -> None:
        if not _is_admin(config, message):
            return
        memory.set_muted(message.chat.id, False)
        await message.reply("Снова на связи.")

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        if not _is_admin(config, message):
            return
        chat_id = message.chat.id
        muted = memory.is_muted(chat_id)
        history_len = len(memory.messages(chat_id))
        elapsed = memory.seconds_since_last_bot_response(chat_id, time.time())
        elapsed_str = "никогда" if elapsed == float("inf") else f"{int(elapsed)} сек назад"
        await message.reply(
            "Статус в этом чате:\n"
            f"— заглушен: {'да' if muted else 'нет'}\n"
            f"— модель: <code>{config.model}</code>\n"
            f"— кулдаун: {config.cooldown_seconds} сек\n"
            f"— история: {history_len}/{config.history_size} сообщений\n"
            f"— последний ответ: {elapsed_str}\n"
            f"— админов в .env: {len(config.admin_user_ids)}",
            parse_mode="HTML",
        )

    # ---------- досье ----------

    async def _need_reply_target(message: Message) -> tuple[int, str] | None:
        """Достать цель команды из reply'а; если reply нет — подсказать админу."""
        target = _resolve_target_from_reply(message)
        if target is None:
            await message.reply(
                "Эту команду используй REPLY'ем на сообщение нужного человека "
                "(зажми сообщение → «Ответить»)."
            )
            return None
        return target

    @dp.message(Command("note"))
    async def cmd_note(message: Message, command: CommandObject) -> None:
        if not _is_admin(config, message):
            return
        target = await _need_reply_target(message)
        if target is None:
            return
        target_id, target_name = target
        args = (command.args or "").strip()
        if not args:
            profile = dossier.get_user(message.chat.id, target_id)
            if profile is None or profile.is_empty_meta():
                await message.reply(f"Про {target_name} пока ничего не записано.")
                return
            await message.reply(
                f"Досье на {target_name}:\n"
                + (f"— клички: {', '.join(profile.nicknames)}\n" if profile.nicknames else "")
                + (f"— теги: {', '.join(profile.tags)}\n" if profile.tags else "")
                + (f"— заметка: {profile.note}" if profile.note else "")
            )
            return
        if args.lower() in {"clear", "стер", "стереть", "забудь", "сбросить"}:
            dossier.clear_note(message.chat.id, target_id)
            await message.reply(f"Заметку про {target_name} стёр.")
            return
        dossier.set_note(message.chat.id, target_id, args)
        await message.reply(f"Записал про {target_name}: {args}")

    @dp.message(Command("nick"))
    async def cmd_nick(message: Message, command: CommandObject) -> None:
        if not _is_admin(config, message):
            return
        target = await _need_reply_target(message)
        if target is None:
            return
        target_id, target_name = target
        nickname = (command.args or "").strip()
        if not nickname:
            await message.reply("Использование: /nick <кличка> (reply'ем на человека).")
            return
        if dossier.add_nickname(message.chat.id, target_id, nickname):
            await message.reply(f"Теперь {target_name} — это «{nickname}». Запомнила.")
        else:
            await message.reply(f"«{nickname}» уже была у {target_name}.")

    @dp.message(Command("unnick"))
    async def cmd_unnick(message: Message, command: CommandObject) -> None:
        if not _is_admin(config, message):
            return
        target = await _need_reply_target(message)
        if target is None:
            return
        target_id, target_name = target
        nickname = (command.args or "").strip()
        if not nickname:
            await message.reply("Использование: /unnick <кличка> (reply'ем на человека).")
            return
        if dossier.remove_nickname(message.chat.id, target_id, nickname):
            await message.reply(f"Убрала «{nickname}» у {target_name}.")
        else:
            await message.reply(f"«{nickname}» у {target_name} и не было.")

    @dp.message(Command("tag"))
    async def cmd_tag(message: Message, command: CommandObject) -> None:
        if not _is_admin(config, message):
            return
        target = await _need_reply_target(message)
        if target is None:
            return
        target_id, target_name = target
        raw = (command.args or "").strip()
        if not raw:
            await message.reply("Использование: /tag <тег[, тег2, ...]> (reply'ем на человека).")
            return
        added: list[str] = []
        skipped: list[str] = []
        for chunk in raw.replace(";", ",").split(","):
            tag = chunk.strip().lstrip("#")
            if not tag:
                continue
            if dossier.add_tag(message.chat.id, target_id, tag):
                added.append(tag)
            else:
                skipped.append(tag)
        parts: list[str] = []
        if added:
            parts.append(f"добавила теги {target_name}: {', '.join(added)}")
        if skipped:
            parts.append(f"уже были: {', '.join(skipped)}")
        await message.reply("; ".join(parts) if parts else "пусто")

    @dp.message(Command("untag"))
    async def cmd_untag(message: Message, command: CommandObject) -> None:
        if not _is_admin(config, message):
            return
        target = await _need_reply_target(message)
        if target is None:
            return
        target_id, target_name = target
        raw = (command.args or "").strip()
        if not raw:
            await message.reply("Использование: /untag <тег[, тег2, ...]> (reply'ем на человека).")
            return
        removed: list[str] = []
        missing: list[str] = []
        for chunk in raw.replace(";", ",").split(","):
            tag = chunk.strip().lstrip("#")
            if not tag:
                continue
            if dossier.remove_tag(message.chat.id, target_id, tag):
                removed.append(tag)
            else:
                missing.append(tag)
        parts: list[str] = []
        if removed:
            parts.append(f"сняла теги у {target_name}: {', '.join(removed)}")
        if missing:
            parts.append(f"не нашлись: {', '.join(missing)}")
        await message.reply("; ".join(parts) if parts else "пусто")

    @dp.message(Command("forget"))
    async def cmd_forget(message: Message) -> None:
        if not _is_admin(config, message):
            return
        target = await _need_reply_target(message)
        if target is None:
            return
        target_id, target_name = target
        if dossier.forget(message.chat.id, target_id):
            await message.reply(f"Стёрла всё, что знала про {target_name}.")
        else:
            await message.reply(f"Я про {target_name} ничего и не знала.")

    @dp.message(Command("whois"))
    async def cmd_whois(message: Message) -> None:
        if not _is_admin(config, message):
            return
        target = _resolve_target_from_reply(message)
        if target is None:
            await message.reply(
                "Эту команду используй REPLY'ем на сообщение нужного человека."
            )
            return
        target_id, target_name = target
        profile = dossier.get_user(message.chat.id, target_id)
        if profile is None:
            await message.reply(
                f"{target_name} (id <code>{target_id}</code>): про этого человека ничего не записано.",
                parse_mode="HTML",
            )
            return
        lines = [
            f"<b>{target_name}</b> (id <code>{target_id}</code>)",
            f"— имя в Telegram: {profile.name or '—'}",
            f"— клички: {', '.join(profile.nicknames) if profile.nicknames else '—'}",
            f"— теги: {', '.join(profile.tags) if profile.tags else '—'}",
            f"— заметка: {profile.note or '—'}",
        ]
        await message.reply("\n".join(lines), parse_mode="HTML")

    @dp.message(Command("lore"))
    async def cmd_lore(message: Message, command: CommandObject) -> None:
        if not _is_admin(config, message):
            return
        chat_id = message.chat.id
        args = (command.args or "").strip()
        if not args:
            current = dossier.get_lore(chat_id)
            if not current:
                await message.reply(
                    "Лора чата нет. Задать: /lore <текст про чат: кто эти люди, "
                    "какие темы, какой тон>."
                )
                return
            snippet = current if len(current) <= 3500 else current[:3500] + "…"
            await message.reply(f"Лор чата:\n\n{snippet}")
            return
        if args.lower() in {"clear", "стер", "стереть", "забудь", "сбросить"}:
            dossier.clear_lore(chat_id)
            await message.reply("Лор чата стёрла.")
            return
        dossier.set_lore(chat_id, args)
        await message.reply(f"Лор чата обновила ({len(args)} символов).")

    # ---------- основной обработчик сообщений ----------

    @dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP, ChatType.PRIVATE}))
    async def on_message(message: Message) -> None:
        text = message.text or message.caption
        if not text:
            return

        chat_id = message.chat.id
        if config.allowed_chat_ids and chat_id not in config.allowed_chat_ids:
            log.debug("Игнорирую сообщение из чата %s (нет в ALLOWED_CHAT_IDS)", chat_id)
            return

        bot_user = await bot.me()
        bot_username = (bot_user.username or "").lower()
        bot_id = bot_user.id

        user_name = _full_user_name(message)
        sender_id = message.from_user.id if message.from_user else 0
        memory.add(
            chat_id,
            HistoryMessage(
                user_id=sender_id,
                user_name=user_name,
                text=text,
                is_bot=False,
            ),
        )
        # Авто-апдейт «как зовут в Telegram» в досье — без срабатывания на ботов и системные.
        if sender_id > 0 and message.from_user and not message.from_user.is_bot:
            dossier.seen_user(chat_id, sender_id, user_name)

        if memory.is_muted(chat_id):
            log.debug("Чат %s заглушен админом — пропуск", chat_id)
            return

        is_private = message.chat.type == ChatType.PRIVATE
        is_mention = bool(bot_username) and (f"@{bot_username}" in text.lower())
        is_reply_to_bot = (
            message.reply_to_message is not None
            and message.reply_to_message.from_user is not None
            and message.reply_to_message.from_user.id == bot_id
        )
        forced = is_private or is_mention or is_reply_to_bot

        now = time.time()
        if not forced:
            elapsed = memory.seconds_since_last_bot_response(chat_id, now)
            if elapsed < config.cooldown_seconds:
                log.debug(
                    "Cooldown в чате %s: %.1fs из %ds — пропуск",
                    chat_id,
                    elapsed,
                    config.cooldown_seconds,
                )
                return

        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception as exc:  # noqa: BLE001 — typing-индикатор не критичен
            log.debug("Не удалось отправить chat_action: %s", exc)

        history = memory.messages(chat_id)
        # Соберём всех, кого надо «помнить» сейчас: автор текущего сообщения
        # плюс все, кто засветился в недавней истории.
        active_user_ids: list[int] = [sender_id] if sender_id > 0 else []
        for h in history:
            if h.user_id > 0 and h.user_id != bot_id and h.user_id not in active_user_ids:
                active_user_ids.append(h.user_id)
        dossier_block = dossier.render_block(chat_id, active_user_ids)
        try:
            reply_text = await llm.reply(
                persona_text=persona.text(),
                history=history,
                forced=forced,
                dossier_block=dossier_block,
            )
        except Exception as exc:  # noqa: BLE001 — не хотим уронить хендлер на любом сбое
            log.exception("Ошибка генерации ответа: %s", exc)
            reply_text = None

        if not reply_text:
            return

        try:
            sent = await message.reply(reply_text)
        except Exception as exc:  # noqa: BLE001 — Telegram может временно отказать
            log.warning("Не удалось отправить ответ: %s", exc)
            return

        memory.add(
            chat_id,
            HistoryMessage(
                user_id=bot_id,
                user_name=bot_user.first_name or "bot",
                text=reply_text,
                is_bot=True,
            ),
        )
        memory.mark_bot_responded(chat_id, time.time())
        log.info("Ответил в чате %s (msg_id=%s)", chat_id, sent.message_id)
