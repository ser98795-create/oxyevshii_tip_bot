"""Обработчики сообщений Telegram."""

from __future__ import annotations

import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from .animations import AnimationPool
from .config import Config
from .dossier import Dossier, is_sensitive
from .llm import LLM
from .memory import HistoryMessage, Memory
from .persona import Persona, build_aliases_map, detect_subjects
from .reactions import Reactions

log = logging.getLogger(__name__)


def _full_user_name(message: Message) -> str:
    """Имя автора для истории чата.

    Если у пользователя задан Telegram-username, дописываем его в скобках
    («Антон (@wow_1_2_3)»). Это нужно, чтобы LLM мог сверить автора с таблицей
    прозвищ в persona.txt по @тегу, а не по «художественному» display name
    типа `--->---> - ^ - <---<---`, который ни с чем не сматчишь.
    """
    user = message.from_user
    if user is None:
        return "Аноним"
    parts: list[str] = []
    if user.first_name:
        parts.append(user.first_name)
    if user.last_name:
        parts.append(user.last_name)
    if user.username:
        tag = f"@{user.username}"
        parts.append(f"({tag})" if parts else tag)
    if not parts:
        parts.append(f"user{user.id}")
    return " ".join(parts)


def _is_admin(config: Config, message: Message) -> bool:
    user = message.from_user
    if user is None:
        return False
    # Если список админов не задан — никто не админ. Это сознательное решение:
    # лучше требовать явной настройки, чем случайно отдать управление кому угодно.
    return user.id in config.admin_user_ids


def setup(
    dp: Dispatcher,
    bot: Bot,
    config: Config,
    memory: Memory,
    persona: Persona,
    llm: LLM,
    dossier: Dossier,
) -> None:
    # Реакции и пул гифок инициализируем здесь, чтобы не раздувать
    # сигнатуру setup() в main.py. Пул гифок пока пустой — sendAnimation
    # отключён, архитектура только зарезервирована (см. bot/animations.py).
    reactions = Reactions(probability=config.reaction_probability)
    animations = AnimationPool()

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
        memory.add(
            chat_id,
            HistoryMessage(
                user_id=message.from_user.id if message.from_user else 0,
                user_name=user_name,
                text=text,
                is_bot=False,
            ),
        )

        if memory.is_muted(chat_id):
            log.debug("Чат %s заглушен админом — пропуск", chat_id)
            return

        # Реакции и гифки на пользовательское сообщение — независимо от
        # того, ответит ли бот текстом ниже. Реакцию и гифку НЕ комбинируем
        # (правило из ТЗ): пока пул гифок пуст, ветка animations.enabled
        # никогда не срабатывает, и мы всегда падаем в ветку реакции. Когда
        # потом включим sendAnimation, здесь будет выбор «или гифка, или
        # реакция, или ничего».
        try:
            if animations.enabled:
                # TODO: pool.pick(...) + bot.send_animation(...).
                # Сейчас сюда не попадаем — пул пуст.
                pass
            else:
                await reactions.maybe_react(bot, message, bot_id)
        except Exception as exc:  # noqa: BLE001 — украшение, бот ронять нельзя
            log.debug("Реакция/гифка не обработана: %s", exc)

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
            # Огромный cooldown (>= 1 года) трактуем как «вообще не вмешивайся
            # в чат сама — отвечай только когда обратились». Так блокируем и
            # первое сообщение после рестарта контейнера (когда last_response
            # ещё пустой → elapsed == inf и обычная проверка пропускает).
            no_auto = config.cooldown_seconds >= 31_536_000
            if no_auto or elapsed < config.cooldown_seconds:
                log.debug(
                    "Cooldown в чате %s: %.1fs из %ds (no_auto=%s) — пропуск",
                    chat_id,
                    elapsed,
                    config.cooldown_seconds,
                    no_auto,
                )
                return

        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception as exc:  # noqa: BLE001 — typing-индикатор не критичен
            log.debug("Не удалось отправить chat_action: %s", exc)

        history = memory.messages(chat_id)
        bot_replies = memory.last_bot_replies(chat_id, n=5)

        # Готовим выписку из досье: ищем упомянутых в этом сообщении и
        # в последних 3 строках истории. Так в контекст попадают и те,
        # о ком сейчас речь, и автор текущей реплики (его прозвища тоже
        # есть в карте псевдонимов). Карту парсим из persona.txt — она
        # авто-перечитывается на каждый persona.text().
        persona_text_now = persona.text()
        aliases_map = build_aliases_map(persona_text_now)
        scan_text_parts = [text]
        for h in history[-3:]:
            if h.user_name:
                scan_text_parts.append(h.user_name)
            if h.text:
                scan_text_parts.append(h.text)
        scan_text = "\n".join(scan_text_parts)
        subjects = detect_subjects(scan_text, aliases_map)
        dossier_block = dossier.relevant_block(
            chat_id=chat_id,
            subjects=subjects,
            aliases_map=aliases_map,
        )

        try:
            result = await llm.reply(
                persona_text=persona_text_now,
                history=history,
                forced=forced,
                current_message=text,
                bot_replies=bot_replies,
                dossier_block=dossier_block,
            )
        except Exception as exc:  # noqa: BLE001 — не хотим уронить хендлер на любом сбое
            log.exception("Ошибка генерации ответа: %s", exc)
            result = None

        if result is None:
            return
        reply_text = (result.text or "").strip()
        dossier_ops = list(result.dossier_ops)

        # Применяем действия с досье ДО отправки ответа, чтобы при ошибке
        # отправки запись осталась (модель уже «решила» — это её слова).
        author_name = user_name or "?"
        refusals: list[str] = []
        for op in dossier_ops:
            try:
                if op.op == "write":
                    # Локальная перепроверка: если модель забыла про
                    # запрет — всё равно отказываем и подкидываем в текст
                    # Юлину отбивку. Это страховка, основная проверка
                    # уже внутри dossier.add_note().
                    kind = is_sensitive(op.fact)
                    if kind is not None:
                        refusals.append(
                            f"(не пишу в досье {op.subject}: {kind} — не папочка позора, а статья)"
                        )
                        log.info(
                            "Отказ записи (handler) chat=%s subject=%s kind=%s",
                            chat_id,
                            op.subject,
                            kind,
                        )
                        continue
                    ok, reason = dossier.add_note(
                        chat_id=chat_id,
                        subject=op.subject,
                        fact=op.fact,
                        created_by=author_name,
                        aliases_map=aliases_map,
                    )
                    if not ok and reason.startswith("sensitive:"):
                        refusals.append(
                            f"(не пишу в досье {op.subject}: {reason.split(':', 1)[1]})"
                        )
                elif op.op == "delete":
                    dossier.delete_note(
                        chat_id=chat_id,
                        subject=op.subject,
                        fact_substring=op.fact,
                        aliases_map=aliases_map,
                    )
                elif op.op == "clear":
                    dossier.clear_participant(
                        chat_id=chat_id,
                        subject=op.subject,
                        aliases_map=aliases_map,
                    )
            except Exception as exc:  # noqa: BLE001 — досье не должно ронять бота
                log.warning("Ошибка применения операции досье %s: %s", op, exc)

        if refusals and reply_text:
            reply_text = reply_text + "\n" + " ".join(refusals)

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
        log.info(
            "Ответил в чате %s (msg_id=%s, dossier_ops=%d)",
            chat_id,
            sent.message_id,
            len(dossier_ops),
        )
