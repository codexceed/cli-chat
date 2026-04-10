"""LLM chat client — streaming responses via OpenRouter (OpenAI-compatible)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from openai import AsyncOpenAI, AsyncStream

from cli_chat.tools import TOOL_DEFINITIONS

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionChunk, ChatCompletionMessageParam

    from cli_chat.models import Settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a helpful assistant with access to weather and research tools. "
    "Use get_weather for weather queries and research_topic for in-depth research. "
    "Be concise and helpful."
)


class ChatClient:
    def __init__(self, settings: Settings) -> None:
        self._client = AsyncOpenAI(api_key=settings.openrouter_api_key, base_url="https://openrouter.ai/api/v1")
        self._model = settings.llm_model

    async def stream(self, messages: list[ChatCompletionMessageParam]) -> AsyncStream[ChatCompletionChunk]:
        logger.info("LLM stream request (model=%s, messages=%d)", self._model, len(messages))
        return await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, *messages],
            tools=TOOL_DEFINITIONS,  # type: ignore[arg-type]
            stream=True,
        )
