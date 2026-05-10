"""Досье на участников чата.

Бот сам копит в этом досье то, что админ ему сообщает командами:
— клички (`/nick`),
— теги (`/tag`),
— произвольная заметка про человека (`/note`),
— общий «лор» чата (`/lore`).

Плюс автоматически запоминает «как зовут в Telegram» для каждого user_id —
чтобы потом можно было ссылаться на человека по имени, даже если тот
сейчас молчит.

Всё хранится в одном JSON-файле (по умолчанию `data/dossier.json`),
сохраняется на каждое изменение атомарной записью через `.tmp` + rename.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


@dataclass
class UserProfile:
    """Метаданные про конкретного человека в конкретном чате."""

    user_id: int
    name: str = ""  # последнее имя из Telegram (first/last/username)
    nicknames: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "nicknames": list(self.nicknames),
            "tags": list(self.tags),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> UserProfile:
        return cls(
            user_id=int(data.get("user_id", 0)),
            name=str(data.get("name") or ""),
            nicknames=[str(x) for x in data.get("nicknames") or []],
            tags=[str(x) for x in data.get("tags") or []],
            note=str(data.get("note") or ""),
        )

    def is_empty_meta(self) -> bool:
        """True, если про человека НИЧЕГО не известно, кроме telegram-имени."""
        return not (self.nicknames or self.tags or self.note)

    def render(self) -> str:
        """Однострочное (или двустрочное) описание для системного промпта."""
        parts: list[str] = []
        head = self.name or f"user{self.user_id}"
        if self.nicknames:
            head += f" (зовём: {', '.join(self.nicknames)})"
        parts.append(head)
        if self.tags:
            parts.append("теги: " + ", ".join(self.tags))
        if self.note:
            parts.append("заметка: " + self.note)
        return " — ".join(parts)


@dataclass
class ChatDossier:
    """Досье по одному чату: общий лор + словарь профилей."""

    chat_id: int
    lore: str = ""
    users: dict[int, UserProfile] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "lore": self.lore,
            "users": {str(uid): p.to_dict() for uid, p in self.users.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> ChatDossier:
        users: dict[int, UserProfile] = {}
        for uid_str, raw in (data.get("users") or {}).items():
            try:
                uid = int(uid_str)
            except (TypeError, ValueError):
                continue
            profile = UserProfile.from_dict(raw or {})
            if profile.user_id == 0:
                profile.user_id = uid
            users[uid] = profile
        return cls(
            chat_id=int(data.get("chat_id", 0)),
            lore=str(data.get("lore") or ""),
            users=users,
        )


class Dossier:
    """Хранит досье по всем чатам с персистом в JSON.

    Все мутации сериализуются под одним lock'ом — потоков у бота немного
    (aiogram однопоточный по сути), но lock добавляет копеечную защиту
    от гонок при сохранении.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._chats: dict[int, ChatDossier] = {}
        self._load()

    # ---------- персист ----------

    def _load(self) -> None:
        if not self.path.exists():
            log.info("Досье: файл %s не найден, начинаю с пустого", self.path)
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("Досье: не удалось прочитать %s: %s — стартую с пустого", self.path, exc)
            return
        if not raw.strip():
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            log.error(
                "Досье: %s повреждён (%s). Стартую с пустого, файл НЕ перезаписываю "
                "автоматически — почините руками или удалите.",
                self.path,
                exc,
            )
            return
        if not isinstance(data, dict):
            log.error("Досье: ожидался JSON-объект в %s, получено %s", self.path, type(data).__name__)
            return
        chats_raw = data.get("chats") or {}
        for cid_str, chat_raw in chats_raw.items():
            try:
                cid = int(cid_str)
            except (TypeError, ValueError):
                continue
            chat = ChatDossier.from_dict(chat_raw or {})
            if chat.chat_id == 0:
                chat.chat_id = cid
            self._chats[cid] = chat
        log.info("Досье: загружено %d чатов из %s", len(self._chats), self.path)

    def _save_locked(self) -> None:
        """Должно вызываться под self._lock."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("Досье: не удалось создать каталог %s: %s", self.path.parent, exc)
            return
        payload = {
            "chats": {str(cid): chat.to_dict() for cid, chat in self._chats.items()},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Досье: не удалось сохранить %s: %s", self.path, exc)

    # ---------- доступ ----------

    def _chat(self, chat_id: int) -> ChatDossier:
        chat = self._chats.get(chat_id)
        if chat is None:
            chat = ChatDossier(chat_id=chat_id)
            self._chats[chat_id] = chat
        return chat

    def _user(self, chat_id: int, user_id: int) -> UserProfile:
        chat = self._chat(chat_id)
        profile = chat.users.get(user_id)
        if profile is None:
            profile = UserProfile(user_id=user_id)
            chat.users[user_id] = profile
        return profile

    def get_user(self, chat_id: int, user_id: int) -> UserProfile | None:
        chat = self._chats.get(chat_id)
        if chat is None:
            return None
        return chat.users.get(user_id)

    def get_lore(self, chat_id: int) -> str:
        chat = self._chats.get(chat_id)
        if chat is None:
            return ""
        return chat.lore

    # ---------- мутации: имя из Telegram ----------

    def seen_user(self, chat_id: int, user_id: int, name: str) -> None:
        """Обновить «как зовут в Telegram» для user_id (без сохранения, если имя то же)."""
        if user_id <= 0:
            return
        with self._lock:
            profile = self._user(chat_id, user_id)
            if profile.name == name:
                return
            profile.name = name
            self._save_locked()

    # ---------- мутации: команды админа ----------

    def set_note(self, chat_id: int, user_id: int, note: str) -> None:
        with self._lock:
            profile = self._user(chat_id, user_id)
            profile.note = note.strip()
            self._save_locked()

    def clear_note(self, chat_id: int, user_id: int) -> None:
        with self._lock:
            profile = self._user(chat_id, user_id)
            profile.note = ""
            self._save_locked()

    def add_nickname(self, chat_id: int, user_id: int, nickname: str) -> bool:
        nickname = nickname.strip()
        if not nickname:
            return False
        with self._lock:
            profile = self._user(chat_id, user_id)
            if any(n.lower() == nickname.lower() for n in profile.nicknames):
                return False
            profile.nicknames.append(nickname)
            self._save_locked()
            return True

    def remove_nickname(self, chat_id: int, user_id: int, nickname: str) -> bool:
        nickname = nickname.strip()
        if not nickname:
            return False
        with self._lock:
            profile = self._user(chat_id, user_id)
            before = len(profile.nicknames)
            profile.nicknames = [n for n in profile.nicknames if n.lower() != nickname.lower()]
            if len(profile.nicknames) == before:
                return False
            self._save_locked()
            return True

    def add_tag(self, chat_id: int, user_id: int, tag: str) -> bool:
        tag = tag.strip().lstrip("#")
        if not tag:
            return False
        with self._lock:
            profile = self._user(chat_id, user_id)
            if any(t.lower() == tag.lower() for t in profile.tags):
                return False
            profile.tags.append(tag)
            self._save_locked()
            return True

    def remove_tag(self, chat_id: int, user_id: int, tag: str) -> bool:
        tag = tag.strip().lstrip("#")
        if not tag:
            return False
        with self._lock:
            profile = self._user(chat_id, user_id)
            before = len(profile.tags)
            profile.tags = [t for t in profile.tags if t.lower() != tag.lower()]
            if len(profile.tags) == before:
                return False
            self._save_locked()
            return True

    def forget(self, chat_id: int, user_id: int) -> bool:
        with self._lock:
            chat = self._chats.get(chat_id)
            if chat is None or user_id not in chat.users:
                return False
            del chat.users[user_id]
            self._save_locked()
            return True

    def set_lore(self, chat_id: int, lore: str) -> None:
        with self._lock:
            chat = self._chat(chat_id)
            chat.lore = lore.strip()
            self._save_locked()

    def clear_lore(self, chat_id: int) -> None:
        with self._lock:
            chat = self._chat(chat_id)
            chat.lore = ""
            self._save_locked()

    # ---------- рендер для системного промпта ----------

    def render_block(self, chat_id: int, user_ids: Iterable[int]) -> str:
        """Сформировать кусок системного промпта про этот чат и его участников.

        Включаем:
        — общий лор чата (если есть);
        — досье на пользователей, которые ЕСТЬ в `user_ids` (обычно — те,
          что встречались в недавней истории), и про которых есть хоть
          какая-то метаинформация (клички/теги/заметка) или хотя бы имя.

        Если про чат ничего не известно — возвращаем пустую строку.
        """
        chat = self._chats.get(chat_id)
        if chat is None:
            return ""
        seen: set[int] = set()
        lines_users: list[str] = []
        for uid in user_ids:
            if uid in seen or uid <= 0:
                continue
            seen.add(uid)
            profile = chat.users.get(uid)
            if profile is None:
                continue
            # Скип, если про человека нет ни заметок, ни кличек, ни тегов и нет даже имени.
            if profile.is_empty_meta() and not profile.name:
                continue
            lines_users.append("— " + profile.render())

        sections: list[str] = []
        if chat.lore:
            sections.append("Контекст чата:\n" + chat.lore.strip())
        if lines_users:
            sections.append("Что ты знаешь про участников:\n" + "\n".join(lines_users))
        return "\n\n".join(sections)
