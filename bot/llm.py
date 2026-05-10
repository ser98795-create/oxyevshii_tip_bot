"""Логика обращения к LLM: решение «вмешаться?» и генерация ответа."""

from __future__ import annotations

import json
import logging
from typing import Iterable

from openai import APIError, AsyncOpenAI

from .memory import HistoryMessage

log = logging.getLogger(__name__)

_ADDRESS_RULE = (
    "ВАЖНО ПРО ОБРАЩЕНИЕ: отвечаешь ЛИЧНО автору последнего сообщения — обращайся "
    "к нему по имени или прозвищу (если в персоне есть таблица прозвищ — бери "
    "оттуда). НЕ обращайся ко всему чату собирательно («пацаны», «мужики», "
    "«ребят», «братаны») — только к конкретному человеку, который только что написал."
)

_DECISION_INSTRUCTION = (
    "Ты находишься в групповом чате друзей и читаешь последние сообщения. "
    "Реши, стоит ли тебе ответить прямо сейчас. "
    "Вмешивайся ТОЛЬКО если есть что сказать по существу: "
    "ответить на прямой вопрос (даже если не к тебе обращены, но повисло без ответа), "
    "поправить фактическую ошибку, поддержать обсуждение удачной ремаркой или шуткой. "
    "В остальных случаях молчи. "
    "Не комментируй каждое сообщение — это раздражает. "
    "Не повторяйся. Не спрашивай, чем помочь. Не извиняйся. "
    + _ADDRESS_RULE
)

_FORCED_INSTRUCTION = (
    "К тебе обращаются напрямую (упомянули или ответили на твоё сообщение). "
    "Ответь по существу, в роли своей персоны. "
    "Если вопрос непонятен — уточни кратко. "
    + _ADDRESS_RULE
)

_OUTPUT_FORMAT = (
    'Верни СТРОГО JSON одного из двух видов: '
    '{"speak": false} — если ничего не отвечать, '
    'или {"speak": true, "text": "<твой ответ>"} — если отвечаешь. '
    'Никакого markdown, никаких пояснений вокруг JSON.'
)


def format_history(messages: Iterable[HistoryMessage]) -> str:
    lines: list[str] = []
    for m in messages:
        speaker = "[bot]" if m.is_bot else f"[{m.user_name}]"
        text = m.text.replace("\n", " ").strip()
        if not text:
            continue
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


class LLM:
    def __init__(
        self,
        api_key: str,
        base_url: str | None,
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> None:
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def reply(
        self,
        persona_text: str,
        history: list[HistoryMessage],
        forced: bool,
    ) -> str | None:
        """Возвращает текст ответа или None, если бот решил промолчать.

        forced=True означает, что бот ОБЯЗАН ответить (упоминание / reply / личка).
        В этом случае модель просят сразу выдать обычный текст без JSON-обвязки —
        иначе модель иногда возвращает {"speak": false} даже когда её прямо звали,
        и пользователь видел тишину в ответ на свой вопрос.

        forced=False — режим «решить, влезать ли». Тут JSON нужен, чтобы отличить
        «промолчать» от «ответить вот так».
        """
        history_str = format_history(history) or "(история пуста)"
        # Кому именно отвечаем — берём последнего «не-бота» из истории.
        # Подсовываем модели его имя в открытую, чтобы она не сваливалась
        # на собирательные «пацаны»/«мужики».
        last_speaker = ""
        for h in reversed(history):
            if not h.is_bot and h.user_name:
                last_speaker = h.user_name
                break
        addressee_line = (
            f"Последнее сообщение написал: {last_speaker}. "
            "Если отвечаешь — обращайся именно к нему по имени или прозвищу, "
            "а не ко всему чату.\n\n"
            if last_speaker
            else ""
        )

        if forced:
            system_prompt = (
                f"{persona_text.strip()}\n\n"
                f"{_FORCED_INSTRUCTION}"
            )
            user_prompt = (
                f"Последние сообщения чата:\n{history_str}\n\n"
                f"{addressee_line}"
                "Ответь одним коротким сообщением (1–3 предложения) в роли "
                "своей персоны. Просто текст, без префиксов и без JSON."
            )
            response_format = None
        else:
            system_prompt = (
                f"{persona_text.strip()}\n\n"
                f"{_DECISION_INSTRUCTION}\n\n"
                f"{_OUTPUT_FORMAT}"
            )
            user_prompt = (
                f"Последние сообщения чата:\n{history_str}\n\n"
                f"{addressee_line}"
                "Твой ответ (JSON):"
            )
            response_format = {"type": "json_object"}

        kwargs: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format

        try:
            resp = await self.client.chat.completions.create(**kwargs)
        except APIError as exc:
            log.warning("Ошибка OpenAI API: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 — не хотим уронить бота на любой сбой сети
            log.warning("Не удалось получить ответ от LLM: %s", exc)
            return None

        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            return None

        if forced:
            # Прямой ответ — без JSON. Возвращаем как есть.
            return raw

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("LLM вернул не-JSON в режиме решения: %r", raw[:200])
            return None

        if not isinstance(data, dict):
            return None
        if not data.get("speak"):
            return None
        text = data.get("text")
        if not isinstance(text, str):
            return None
        text = text.strip()
        return text or None
