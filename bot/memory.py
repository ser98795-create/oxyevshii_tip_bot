"""История сообщений по чатам и cooldown ответов бота."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict


@dataclass
class HistoryMessage:
    user_id: int
    user_name: str
    text: str
    is_bot: bool


@dataclass
class ChatState:
    messages: Deque[HistoryMessage] = field(default_factory=deque)
    last_bot_response_ts: float = 0.0
    muted: bool = False


class Memory:
    """Хранит роллинг-историю и timestamp последнего ответа бота для каждого чата."""

    def __init__(self, history_size: int) -> None:
        self.history_size = history_size
        self._chats: Dict[int, ChatState] = {}

    def state(self, chat_id: int) -> ChatState:
        chat = self._chats.get(chat_id)
        if chat is None:
            chat = ChatState(messages=deque(maxlen=self.history_size))
            self._chats[chat_id] = chat
        return chat

    def add(self, chat_id: int, message: HistoryMessage) -> None:
        self.state(chat_id).messages.append(message)

    def messages(self, chat_id: int) -> list[HistoryMessage]:
        return list(self.state(chat_id).messages)

    def last_bot_replies(self, chat_id: int, n: int = 5) -> list[HistoryMessage]:
        """Последние n ответов бота в этом чате (новейшие в конце).

        Нужно, чтобы передавать в промпт отдельно от общей истории —
        модель видит свой недавний стиль и не штампует одно и то же
        начало/концовку подряд.
        """
        bot_msgs = [m for m in self.state(chat_id).messages if m.is_bot]
        if n <= 0:
            return []
        return bot_msgs[-n:]

    def mark_bot_responded(self, chat_id: int, ts: float) -> None:
        self.state(chat_id).last_bot_response_ts = ts

    def seconds_since_last_bot_response(self, chat_id: int, now: float) -> float:
        last = self.state(chat_id).last_bot_response_ts
        if last <= 0:
            return float("inf")
        return now - last

    def is_muted(self, chat_id: int) -> bool:
        return self.state(chat_id).muted

    def set_muted(self, chat_id: int, muted: bool) -> None:
        self.state(chat_id).muted = muted
