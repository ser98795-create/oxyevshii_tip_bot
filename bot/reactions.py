"""Лёгкие реакции бота на сообщения пользователей через setMessageReaction.

Архитектура такая, чтобы потом можно было прикрутить семантический выбор
реакции (LLM-подсказка или эвристика по тексту), не трогая handlers: вся
логика «ставить ли / что ставить» сидит в этом модуле. handlers просто
зовёт `Reactions.maybe_react(...)` после прихода пользовательского
сообщения.

Текущее поведение:
— на каждое входящее сообщение независимо роняется монетка с вероятностью
  `probability` (по умолчанию 0.2). Если упала орлом — выбираем эмодзи
  и ставим. Если решкой — молча пропускаем.
— выбор эмодзи — случайный из разрешённого списка (ALLOWED_REACTIONS).
  Семантика «по смыслу» пока упрощённая: с probability 0.2 × 1/7 = ~3%
  каждое из 7 эмодзи случается достаточно редко, чтобы случайный
  «кринж» не задушнил чат.
— ошибки Bot API глотаем (чат может ограничивать набор реакций; у бота
  может не быть прав). Реакция — украшение, не функциональность.
"""

from __future__ import annotations

import logging
import random
from typing import Iterable

from aiogram import Bot
from aiogram.types import Message, ReactionTypeEmoji

log = logging.getLogger(__name__)


# Белый список из ТЗ. Только обычные emoji-реакции — никаких CustomEmoji,
# Star-реакций (платных) и прочей экзотики. Если будете расширять —
# держите список консервативным: чат может разрешать только подмножество,
# и тогда лишние эмодзи будут проваливаться с REACTION_INVALID.
ALLOWED_REACTIONS: tuple[str, ...] = (
    "\U0001f44d",   # 👍 — нормально / принято
    "\U0001f602",   # 😂 — смешно
    "\U0001f921",   # 🤡 — дичь / кринж
    "\U0001f480",   # 💀 — очень смешно или безнадёжно
    "\U0001f525",   # 🔥 — годно
    "\U0001f440",   # 👀 — интрига
    "\U0001fae0",   # 🫠 — неловко / кринжово
)


class Reactions:
    """Управляет реакциями бота на сообщения пользователей."""

    def __init__(
        self,
        probability: float,
        allowlist: Iterable[str] = ALLOWED_REACTIONS,
    ) -> None:
        self.probability = max(0.0, min(1.0, probability))
        cleaned = tuple(e for e in allowlist if e in ALLOWED_REACTIONS)
        self.allowlist: tuple[str, ...] = cleaned or ALLOWED_REACTIONS

    def _pick_emoji(self, message: Message) -> str | None:
        """Выбирает emoji для реакции или возвращает None («не ставим»).

        Точка расширения. Сейчас — равномерный рандом из allowlist. Сюда
        потом можно завести семантическую логику: LLM-подсказку, эвристику
        по ключевым словам («ору» → 😂/💀, «?» → 👀, «бля» → 🤡 и т.д.).
        """
        if not self.allowlist:
            return None
        return random.choice(self.allowlist)

    async def maybe_react(self, bot: Bot, message: Message, bot_id: int) -> bool:
        """Иногда ставит реакцию на пользовательское сообщение.

        Возвращает True, если реакция действительно ушла в Telegram.
        """
        if self.probability <= 0:
            return False
        if message.from_user is None:
            return False
        # На свои сообщения реакции не ставим (Bot API всё равно не даст,
        # но защищаемся явно — на случай, если кто-то позовёт этот метод
        # на эхо собственного ответа).
        if message.from_user.id == bot_id:
            return False
        if random.random() > self.probability:
            return False
        emoji = self._pick_emoji(message)
        if not emoji or emoji not in ALLOWED_REACTIONS:
            return False
        try:
            await bot.set_message_reaction(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
                is_big=False,
            )
        except Exception as exc:  # noqa: BLE001 — реакция украшение, не функциональность
            # Самые частые причины: чат запрещает этот emoji,
            # у бота нет прав, моргнуло соединение. Падать не имеем права.
            log.debug(
                "setMessageReaction(%s) на msg=%s в chat=%s не прошёл: %s",
                emoji,
                message.message_id,
                message.chat.id,
                exc,
            )
            return False
        log.info(
            "Поставила реакцию %s на msg=%s в chat=%s",
            emoji,
            message.message_id,
            message.chat.id,
        )
        return True
