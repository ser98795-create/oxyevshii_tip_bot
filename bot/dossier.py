"""Мемные «карточки-досье» на участников чата.

Идея — игровая. Юля делает вид, что ведёт «папочку для ФСБ», в реальности
тут просто JSON на диск, сегментированный по `chat_id`. Никаких настоящих
персданных тут не хранится: модуль явно отказывается принимать телефоны,
адреса, паспорта, банковские карты, диагнозы и т.п. Это и здравый смысл,
и часть характера Юли в `persona.txt`.

Структура файла (см. README):

    {
      "<chat_id>": {
        "participants": {
          "<canonical_key>": {
            "display": "Лёха",          # как изначально написали
            "aliases": ["лёха", "алексей", "темщик"],
            "notes": [
              {
                "id": "uuid4",
                "text": "собирается в Испанию",
                "created_by": "Антон",
                "created_at": "2026-05-11T00:00:00+00:00",
                "enabled": true
              }
            ]
          }
        }
      }
    }

`canonical_key` — это первое прозвище в строке таблицы из `persona.txt`,
lowercased (см. `bot/persona.py` → `aliases_map`). Если человек не из
таблицы — `canonical_key` = lowercased subject как написали. Это значит,
что записи на «неизвестных» по сути разойдутся (Лёха и Алёша попадут в
одну карточку благодаря таблице, а вот «Дима» и «Дим» — пока в две).
Это сознательный компромисс v1: лучше дублирующиеся карточки, чем
случайно слитые («Никитос» и «Никита» — это РАЗНЫЕ люди в этом чате).

Все мутации идут под `threading.Lock` и пишут файл атомарно
(`.tmp` + `os.replace`). I/O синхронный — JSON маленький, дёргается
изредка, блокировка event loop незаметна на практике.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


# Что считаем «чувствительными данными» и отказываемся писать в карточку.
# Регэкспы должны срабатывать ДО подтверждения записи — это последняя
# линия защиты, даже если модель почему-то решит, что «телефон не страшно».
_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Российский / международный телефон в самых разных форматах.
    (
        "телефон",
        re.compile(
            r"(?:\+?\d[\s\-().]?){10,15}",
            re.UNICODE,
        ),
    ),
    # Почта.
    (
        "почта",
        re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.UNICODE),
    ),
    # «г. Москва, ул. ..., д. ..., кв. ...» / «проспект ...»
    (
        "адрес",
        re.compile(
            r"\b(?:ул(?:\.|ица)|просп(?:\.|ект)|пер(?:\.|еулок)|"
            r"бульвар|шоссе|проезд|наб(?:\.|ережная)|"
            r"д(?:ом)?\s*\.?\s*\d+|кв(?:артира)?\s*\.?\s*\d+)\b",
            re.IGNORECASE | re.UNICODE,
        ),
    ),
    # Серия+номер паспорта РФ типа «4509 123456» и просто 10-значные
    # последовательности подряд (с пробелом или без).
    (
        "паспорт/документ",
        re.compile(r"\b\d{4}\s?\d{6}\b", re.UNICODE),
    ),
    # Банковская карта 13-19 цифр с пробелами или дефисами.
    (
        "банковская карта",
        re.compile(r"\b(?:\d[\s\-]?){13,19}\b", re.UNICODE),
    ),
    # Явные слова про диагнозы/медкарту — даже без цифр.
    (
        "медданные",
        re.compile(
            r"\b(диагноз|вич|спид|гепатит|онколог|психиатр|нарколог|депресси)",
            re.IGNORECASE | re.UNICODE,
        ),
    ),
    # Дети (фио ребёнка, день рождения и т.п. — отдельный класс).
    (
        "данные ребёнка",
        re.compile(
            r"\b(дочь|сын|ребёнок|ребенок|школьник|детс?ад)\b.*"
            r"\b(имени|зовут|год(?:а|у)?\s+рожд)",
            re.IGNORECASE | re.UNICODE,
        ),
    ),
    # Пароли / токены — самое жирное.
    (
        "пароль/токен",
        re.compile(
            r"(?i)(?:пароль|password|passwd|api[_\s-]?key|token|bearer)\s*[:=]\s*\S+",
        ),
    ),
)


def is_sensitive(fact: str) -> str | None:
    """Возвращает категорию запрета, если факт — чувствительный. Иначе None."""
    for kind, rx in _SENSITIVE_PATTERNS:
        if rx.search(fact):
            return kind
    return None


@dataclass
class DossierNote:
    id: str
    text: str
    created_by: str
    created_at: str
    enabled: bool = True

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "enabled": self.enabled,
        }

    @classmethod
    def from_json(cls, data: dict) -> "DossierNote":
        return cls(
            id=str(data.get("id") or uuid.uuid4()),
            text=str(data.get("text") or "").strip(),
            created_by=str(data.get("created_by") or ""),
            created_at=str(data.get("created_at") or _now_iso()),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class ParticipantCard:
    display: str
    aliases: list[str] = field(default_factory=list)
    notes: list[DossierNote] = field(default_factory=list)

    def add_alias(self, alias: str) -> None:
        a = alias.strip().lower()
        if a and a not in self.aliases:
            self.aliases.append(a)

    def active_notes(self) -> list[DossierNote]:
        return [n for n in self.notes if n.enabled and n.text]

    def to_json(self) -> dict:
        return {
            "display": self.display,
            "aliases": list(self.aliases),
            "notes": [n.to_json() for n in self.notes],
        }

    @classmethod
    def from_json(cls, data: dict) -> "ParticipantCard":
        return cls(
            display=str(data.get("display") or "").strip() or "?",
            aliases=[str(a).lower() for a in (data.get("aliases") or [])],
            notes=[DossierNote.from_json(n) for n in (data.get("notes") or [])],
        )


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _canonical(subject: str, aliases_map: dict[str, str] | None) -> str:
    """Приводит subject к канонической форме.

    Если `aliases_map` («alias_lower → canonical_key») знает про
    `subject` — возвращаем его канонический ключ. Иначе — lowercased
    subject как есть.
    """
    key = subject.strip().lower()
    if not key:
        return ""
    if aliases_map and key in aliases_map:
        return aliases_map[key]
    return key


class Dossier:
    """Менеджер карточек-досье с JSON-персистом."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        # `_data[str(chat_id)]["participants"][canonical_key] = ParticipantCard`
        self._data: dict[str, dict[str, ParticipantCard]] = {}
        self._load()

    # ---- persistence ----------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            log.info("Файл досье %s не найден, начинаем с пустого.", self.path)
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "Не смогла прочитать %s (%s) — оставляю досье пустым в памяти. "
                "Файл на диске НЕ перезапишется, пока не появится корректная "
                "запись.",
                self.path,
                exc,
            )
            return
        if not isinstance(data, dict):
            log.warning("В %s не объект на верхнем уровне — игнорирую.", self.path)
            return
        loaded: dict[str, dict[str, ParticipantCard]] = {}
        for chat_key, chat_payload in data.items():
            if not isinstance(chat_payload, dict):
                continue
            parts_raw = chat_payload.get("participants") or {}
            parts: dict[str, ParticipantCard] = {}
            for pkey, pdata in parts_raw.items():
                if not isinstance(pdata, dict):
                    continue
                card = ParticipantCard.from_json(pdata)
                parts[str(pkey).lower()] = card
            loaded[str(chat_key)] = parts
        self._data = loaded
        log.info(
            "Досье прочитано из %s: чатов=%d, всего карточек=%d",
            self.path,
            len(self._data),
            sum(len(p) for p in self._data.values()),
        )

    def _save_locked(self) -> None:
        """Атомарная запись. Должна вызываться под self._lock."""
        out: dict = {}
        for chat_key, parts in self._data.items():
            out[chat_key] = {
                "participants": {k: card.to_json() for k, card in parts.items()},
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(out, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Не смогла сохранить досье в %s: %s", self.path, exc)
            # tmp может остаться рядом — это норм, в следующий раз перезапишется.

    # ---- chat-scoped access --------------------------------------------

    def _chat(self, chat_id: int) -> dict[str, ParticipantCard]:
        return self._data.setdefault(str(chat_id), {})

    def add_note(
        self,
        chat_id: int,
        subject: str,
        fact: str,
        created_by: str,
        aliases_map: dict[str, str] | None = None,
    ) -> tuple[bool, str]:
        """Пишет факт в карточку.

        Возвращает `(ok, reason)`:
          • `(True, "")` — записали;
          • `(False, "empty")` — нечего писать;
          • `(False, "sensitive:<kind>")` — отказались из-за чувствительных данных;
          • `(False, "io")` — не смогли записать на диск.
        """
        fact = (fact or "").strip()
        subject_clean = (subject or "").strip()
        if not subject_clean or not fact:
            return False, "empty"
        kind = is_sensitive(fact)
        if kind is not None:
            log.info(
                "Отказ записи в досье chat=%s subject=%s: чувствительные данные (%s)",
                chat_id,
                subject_clean,
                kind,
            )
            return False, f"sensitive:{kind}"
        ckey = _canonical(subject_clean, aliases_map)
        if not ckey:
            return False, "empty"
        with self._lock:
            parts = self._chat(chat_id)
            card = parts.get(ckey)
            if card is None:
                card = ParticipantCard(display=subject_clean)
                parts[ckey] = card
            card.add_alias(subject_clean)
            card.notes.append(
                DossierNote(
                    id=uuid.uuid4().hex,
                    text=fact,
                    created_by=created_by or "",
                    created_at=_now_iso(),
                    enabled=True,
                )
            )
            self._save_locked()
        return True, ""

    def delete_note(
        self,
        chat_id: int,
        subject: str,
        fact_substring: str,
        aliases_map: dict[str, str] | None = None,
    ) -> bool:
        """Помечает enabled=False у первой записи, текст которой содержит подстроку.

        Возвращает True, если что-то реально стёрли. Полное удаление файла
        не делаем — оставляем «надгробие», на случай если потом понадобится
        отмена. Для отображения active_notes() это уже невидимо.
        """
        subject_clean = (subject or "").strip()
        needle = (fact_substring or "").strip().lower()
        if not subject_clean:
            return False
        ckey = _canonical(subject_clean, aliases_map)
        if not ckey:
            return False
        with self._lock:
            parts = self._chat(chat_id)
            card = parts.get(ckey)
            if card is None:
                return False
            for note in card.notes:
                if not note.enabled or not note.text:
                    continue
                if needle and needle not in note.text.lower():
                    continue
                note.enabled = False
                self._save_locked()
                return True
            return False

    def clear_participant(
        self,
        chat_id: int,
        subject: str,
        aliases_map: dict[str, str] | None = None,
    ) -> bool:
        """Гасит всю карточку участника (enabled=False у всех заметок)."""
        subject_clean = (subject or "").strip()
        if not subject_clean:
            return False
        ckey = _canonical(subject_clean, aliases_map)
        if not ckey:
            return False
        with self._lock:
            parts = self._chat(chat_id)
            card = parts.get(ckey)
            if card is None:
                return False
            changed = False
            for note in card.notes:
                if note.enabled:
                    note.enabled = False
                    changed = True
            if changed:
                self._save_locked()
            return changed

    # ---- read access ----------------------------------------------------

    def notes_for(
        self,
        chat_id: int,
        subject: str,
        aliases_map: dict[str, str] | None = None,
    ) -> tuple[str, list[DossierNote]]:
        """Возвращает (display_name, active_notes) для участника.

        Если карточки нет — `("", [])`.
        """
        subject_clean = (subject or "").strip()
        ckey = _canonical(subject_clean, aliases_map)
        if not ckey:
            return "", []
        parts = self._data.get(str(chat_id), {})
        card = parts.get(ckey)
        if card is None:
            return "", []
        return card.display, card.active_notes()

    def all_subjects(self, chat_id: int) -> list[tuple[str, list[DossierNote]]]:
        """Сводка по всем участникам с непустыми карточками."""
        out: list[tuple[str, list[DossierNote]]] = []
        for card in self._data.get(str(chat_id), {}).values():
            active = card.active_notes()
            if active:
                out.append((card.display, active))
        return out

    # ---- prompt-rendering helpers --------------------------------------

    def relevant_block(
        self,
        chat_id: int,
        subjects: Iterable[str],
        aliases_map: dict[str, str] | None = None,
        max_per_subject: int = 4,
    ) -> str:
        """Готовит текстовый блок для подкладывания в user-промпт LLM.

        Берёт ТОЛЬКО упомянутых участников, каждому — последние
        `max_per_subject` активных заметок. Возвращает пустую строку,
        если ни по кому из subjects ничего нет.
        """
        seen_keys: set[str] = set()
        lines: list[str] = []
        for s in subjects:
            ckey = _canonical(s, aliases_map)
            if not ckey or ckey in seen_keys:
                continue
            seen_keys.add(ckey)
            display, notes = self.notes_for(chat_id, s, aliases_map)
            if not notes:
                continue
            facts = "; ".join(n.text for n in notes[-max_per_subject:])
            lines.append(f"— {display}: {facts}.")
        return "\n".join(lines)
