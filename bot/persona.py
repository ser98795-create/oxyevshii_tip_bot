"""Загрузка системного промпта (персоны) из файла с авто-перечитыванием."""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)


# Строки таблицы прозвищ в persona.txt — формат:
#   @tag → Имя, Прозвище1, Прозвище2 (опциональный коммент в скобках)
# Мы парсим их в `build_aliases_map`, чтобы досье по-разному написанные
# обращения (Лёха, Алёша, Темщик) клало в одну карточку.
_ALIASES_LINE_RE = re.compile(
    r"^\s*(?P<tag>@\w+)\s*→\s*(?P<names>[^()]+?)(?:\s*\(.*\))?\s*$",
    re.UNICODE,
)


def build_aliases_map(persona_text: str) -> dict[str, str]:
    """Парсит строки «@tag → Имя, Прозвище1, Прозвище2» в alias→canonical.

    `canonical_key` для строки = первое имя (lowercased). В карту добавляем:
      • сам @tag (с @ и без, lowercased)
      • каждое имя/прозвище (lowercased, с обрезанными хвостами после
        первого пробела — «Никитос» из «Никитос» оставляем, но
        многословные хвосты типа «Антон Есим» сохраняем целиком тоже).

    Если несколько строк дадут одинаковый canonical_key — побеждает первая
    встретившаяся (так у нас два «Никиты» из разных тегов получат разные
    canonical, что и нужно — см. @Evreik vs @Nazgyyyl).
    """
    out: dict[str, str] = {}
    for raw in persona_text.splitlines():
        m = _ALIASES_LINE_RE.match(raw)
        if not m:
            continue
        tag = m.group("tag").lower()
        names_raw = m.group("names") or ""
        names = [n.strip() for n in names_raw.split(",") if n.strip()]
        if not names:
            continue
        canonical = names[0].lower()
        if not canonical:
            continue
        # @tag, tag, и сам канонический ключ.
        out.setdefault(tag, canonical)
        out.setdefault(tag.lstrip("@"), canonical)
        out.setdefault(canonical, canonical)
        for n in names[1:]:
            key = n.lower()
            if key:
                out.setdefault(key, canonical)
                # Иногда в чате обращаются полным именем без сокращения
                # («Алексей» при первом упоминании). Если в прозвище есть
                # пробел («Антон Есим») — добавим и каждое слово.
                for part in key.split():
                    if len(part) >= 3:
                        out.setdefault(part, canonical)
    return out


def detect_subjects(
    text: str,
    aliases_map: dict[str, str],
    *,
    max_subjects: int = 5,
) -> list[str]:
    """Находит в `text` упомянутых участников через карту псевдонимов.

    Возвращает список канонических ключей (в порядке первого появления,
    без повторов), ограниченный `max_subjects`. Используется для отбора
    выписки из досье — что подложить модели в контекст.
    """
    if not text or not aliases_map:
        return []
    lowered = text.lower()
    found: list[str] = []
    seen: set[str] = set()
    # Сортируем алиасы по убыванию длины, чтобы «темщик» матчился раньше
    # «тем» (если бы такой был). Слово-границы — \b с UNICODE-флагом.
    for alias in sorted(aliases_map.keys(), key=len, reverse=True):
        if not alias:
            continue
        # @tag матчим как подстроку (там и так редкий префикс).
        if alias.startswith("@"):
            if alias in lowered:
                ckey = aliases_map[alias]
                if ckey not in seen:
                    seen.add(ckey)
                    found.append(ckey)
        else:
            # Для обычных слов — границы; иначе «ден» съест половину чата.
            if len(alias) < 3:
                continue
            pattern = re.compile(rf"(?<![\w@]){re.escape(alias)}(?!\w)", re.UNICODE)
            if pattern.search(lowered):
                ckey = aliases_map[alias]
                if ckey not in seen:
                    seen.add(ckey)
                    found.append(ckey)
        if len(found) >= max_subjects:
            break
    return found

DEFAULT_PERSONA = (
    "Ты — собеседник в небольшом закрытом чате друзей. "
    "Отвечаешь по-русски, кратко (1–3 предложения), с мягкой иронией и лёгким сарказмом. "
    "Уместно шутишь, поддерживаешь разговор, но не лезешь без надобности. "
    "Если не уверен в ответе — честно говоришь, что не знаешь."
)


class Persona:
    """Читает текст персоны из файла и перечитывает, если файл изменился."""

    def __init__(self, path: Path, default: str = DEFAULT_PERSONA) -> None:
        self.path = path
        self.default = default
        self._mtime: float = -1.0
        self._text: str = default
        self._reload()

    def _reload(self) -> None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            if self._text != self.default:
                log.warning("Файл персоны %s не найден, использую дефолтный текст", self.path)
            self._text = self.default
            self._mtime = -1.0
            return
        if stat.st_mtime == self._mtime:
            return
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("Не удалось прочитать %s: %s — использую дефолт", self.path, exc)
            self._text = self.default
            self._mtime = -1.0
            return
        self._text = text or self.default
        self._mtime = stat.st_mtime
        log.info("Загружена персона из %s (%d символов)", self.path, len(self._text))

    def text(self) -> str:
        self._reload()
        return self._text
