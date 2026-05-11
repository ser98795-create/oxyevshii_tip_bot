"""Заглушка под отправку гифок (sendAnimation) с локальным набором file_id.

Сейчас пул пуст и `pick()` всегда возвращает `None` — гифки не шлются.
Архитектура зарезервирована, чтобы потом можно было дописать поддержку,
не перетряхивая handlers.

Как включить (в будущем):
1. Один раз руками отправить нужные гифки в служебный чат боту, забрать
   из ответа Telegram `file_id` каждой анимации (или из логов
   `bot.send_animation(...)` — у возвращённого объекта `.animation.file_id`).
2. Заполнить `FILE_IDS` в этом модуле по категориям, например:
       FILE_IDS = {
           "approve": ["BAACAg..."],
           "cringe":  ["BAACAg..."],
           "wtf":     ["BAACAg..."],
       }
   и передать их в `AnimationPool(FILE_IDS)` при инициализации.
3. handlers решит, какую категорию выбрать (LLM-подсказка / эвристика),
   и вызовет `bot.send_animation(chat_id, pool.pick(category))`.

Важное правило из ТЗ: реакция и гифка одновременно НЕ комбинируются.
Сейчас оно держится тривиально — пул пуст, `pool.enabled` всегда False,
handlers видит, что гифка не идёт, и спокойно зовёт реакцию. Когда
гифки подключим, handlers будет уже явно выбирать «или гифка, или
реакция» — соответствующая ветка уже размечена в `handlers.on_message`.
"""

from __future__ import annotations

import random
from typing import Mapping


class AnimationPool:
    """Пул локальных Telegram `file_id` для отправки гифок."""

    def __init__(self, file_ids: Mapping[str, list[str]] | None = None) -> None:
        # По умолчанию — пусто. Это и есть «гифки выключены».
        self.file_ids: dict[str, list[str]] = {
            k: list(v) for k, v in (file_ids or {}).items() if v
        }

    @property
    def enabled(self) -> bool:
        """True, если в пуле есть хотя бы один file_id."""
        return any(self.file_ids.values())

    def pick(self, category: str | None = None) -> str | None:
        """Возвращает случайный file_id или None.

        Если задана непустая категория и она есть в пуле — берём оттуда.
        Иначе тянем из всего пула. Если пул пуст — None (handlers это
        и трактует как «гифку не шлём»).
        """
        if not self.enabled:
            return None
        if category and category in self.file_ids and self.file_ids[category]:
            return random.choice(self.file_ids[category])
        pool: list[str] = []
        for ids in self.file_ids.values():
            pool.extend(ids)
        return random.choice(pool) if pool else None
