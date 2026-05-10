"""Логика обращения к LLM: решение «вмешаться?» и генерация ответа."""

from __future__ import annotations

import json
import logging
from typing import Iterable

from openai import APIError, AsyncOpenAI

from .memory import HistoryMessage

log = logging.getLogger(__name__)

_DECISION_INSTRUCTION = (
    "Ты находишься в групповом чате друзей и читаешь последние сообщения. "
    "Реши, стоит ли тебе ответить прямо сейчас. "
    "Вмешивайся ТОЛЬКО если есть что сказать по существу: "
    "ответить на прямой вопрос (даже если не к тебе обращены, но повисло без ответа), "
    "поправить фактическую ошибку, поддержать обсуждение удачной ремаркой или шуткой. "
    "В остальных случаях молчи. "
    "Не комментируй каждое сообщение — это раздражает. "
    "Не повторяйся. Не спрашивай, чем помочь. Не извиняйся."
)

_FORCED_INSTRUCTION = (
    "К тебе обращаются напрямую (упомянули или ответили на твоё сообщение). "
    "Ответь по существу, в роли своей персоны. "
    "Если вопрос непонятен — уточни кратко."
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
        dossier_block: str = "",
    ) -> str | None:
        """Возвращает текст ответа или None, если бот решил промолчать.

        forced=True означает, что бот ОБЯЗАН ответить (на упоминание или reply).
        Тогда модель не выбирает — отвечать или нет, а сразу формулирует ответ.

        dossier_block — необязательный кусок системного промпта со сведениями
        про чат и его участников (см. `Dossier.render_block`). Подставляется
        между текстом персоны и инструкцией.
        """
        instruction = _FORCED_INSTRUCTION if forced else _DECISION_INSTRUCTION
        sections = [persona_text.strip()]
        dossier_block = (dossier_block or "").strip()
        if dossier_block:
            sections.append(dossier_block)
        sections.append(instruction)
        sections.append(_OUTPUT_FORMAT)
        system_prompt = "\n\n".join(sections)
        history_str = format_history(history) or "(история пуста)"
        user_prompt = f"Последние сообщения чата:\n{history_str}\n\nТвой ответ (JSON):"

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        except APIError as exc:
            log.warning("Ошибка OpenAI API: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 — не хотим уронить бота на любой сбой сети
            log.warning("Не удалось получить ответ от LLM: %s", exc)
            return None

        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("LLM вернул не-JSON: %r", raw[:200])
            # Если forced — попытаемся отдать как есть, чтобы не молчать в лицо собеседнику.
            return raw if forced else None

        if not isinstance(data, dict):
            return raw if forced else None
        if not data.get("speak"):
            return None
        text = data.get("text")
        if not isinstance(text, str):
            return None
        text = text.strip()
        return text or None
