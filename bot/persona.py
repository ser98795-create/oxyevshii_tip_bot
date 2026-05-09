"""Загрузка системного промпта (персоны) из файла с авто-перечитыванием."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

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
