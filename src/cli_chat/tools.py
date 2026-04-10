"""Tool executor — weather and research API calls with retry and quirk handling."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx

from cli_chat import models

if TYPE_CHECKING:
    from openai.types.chat import chat_completion_message_tool_call as tc_module

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather for a city. Fast response (~200ms).",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name, e.g. London, Tokyo",
                    }
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research_topic",
            "description": (
                "Research a topic in depth. Takes 3-8 seconds."
                " Use for questions requiring detailed research."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic to research, e.g. 'solar energy'",
                    }
                },
                "required": ["topic"],
            },
        },
    },
]

MAX_RETRIES = 3
REQUEST_TIMEOUT = 15.0


class _RateLimitError(Exception):
    """Raised when retries are exhausted due to API rate limiting."""


class ToolExecutor:
    def __init__(self, settings: models.Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.elyos_base_url,
            headers={"X-API-Key": settings.elyos_api_key},
            timeout=REQUEST_TIMEOUT,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def execute(
        self,
        tool_call: tc_module.ChatCompletionMessageToolCall,
        cancel_event: asyncio.Event | None = None,
    ) -> models.ToolResult:
        name = tool_call.function.name
        try:
            args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            return models.ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                content="Error: invalid tool arguments",
                error=True,
            )

        try:
            if name == "get_weather":
                content = await self._get_weather(args.get("location", ""), cancel_event)
            elif name == "research_topic":
                content = await self._research_topic(args.get("topic", ""), cancel_event)
            else:
                content = f"Unknown tool: {name}"
            return models.ToolResult(tool_call_id=tool_call.id, name=name, content=content)
        except asyncio.CancelledError:
            return models.ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                content="Tool call was cancelled by the user.",
                error=True,
            )
        except httpx.HTTPStatusError as exc:
            body = exc.response.text
            return models.ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                content=f"API error ({exc.response.status_code}): {body}",
                error=True,
            )
        except (httpx.RequestError, httpx.TimeoutException, httpx.DecodingError) as exc:
            return models.ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                content=f"Request failed: {exc}",
                error=True,
            )
        except _RateLimitError as exc:
            return models.ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                content=str(exc),
                error=True,
            )

    async def _get_weather(
        self, location: str, cancel_event: asyncio.Event | None
    ) -> str:
        for attempt in range(MAX_RETRIES):
            resp = await self._request("/weather", {"location": location}, cancel_event)

            # Quirk: weather can also be throttled (same format as research)
            if resp.get("status") == "throttled":
                throttled = models.ThrottledResponse(**resp)
                if attempt < MAX_RETRIES - 1:
                    wait = min(throttled.retry_after_seconds, 15)
                    await self._cancellable_sleep(wait, cancel_event)
                    continue
                raise _RateLimitError(
                    "Weather API is rate-limited. "
                    f"Please try again in {throttled.retry_after_seconds}s."
                )

            weather = models.WeatherResponse.from_api(resp)
            return weather.display()

        raise _RateLimitError("Weather request failed after retries.")

    async def _research_topic(
        self, topic: str, cancel_event: asyncio.Event | None
    ) -> str:
        for attempt in range(MAX_RETRIES):
            resp = await self._request("/research", {"topic": topic}, cancel_event)

            # Quirk: API returns HTTP 200 for throttling
            if resp.get("status") == "throttled":
                throttled = models.ThrottledResponse(**resp)
                if attempt < MAX_RETRIES - 1:
                    wait = min(throttled.retry_after_seconds, 15)
                    await self._cancellable_sleep(wait, cancel_event)
                    continue
                raise _RateLimitError(
                    "Research API is rate-limited. "
                    f"Please try again in {throttled.retry_after_seconds}s."
                )

            research = models.ResearchResponse(**resp)
            return research.display()

        raise _RateLimitError("Research failed after retries.")

    async def _request(
        self,
        path: str,
        params: dict,
        cancel_event: asyncio.Event | None,
    ) -> dict:
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError

        resp = await self._client.get(path, params=params)
        resp.raise_for_status()

        # Quirk: infrastructure errors (e.g. unicode input) return HTML, not JSON
        content_type = resp.headers.get("content-type", "")
        if "json" not in content_type:
            msg = f"Unexpected response format (got {content_type})"
            raise httpx.DecodingError(msg)

        return resp.json()

    async def _cancellable_sleep(
        self, seconds: float, cancel_event: asyncio.Event | None
    ) -> None:
        if cancel_event is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=seconds)
            raise asyncio.CancelledError
        except TimeoutError:
            pass  # sleep completed without cancellation

    async def execute_batch(
        self,
        tool_calls: list[tc_module.ChatCompletionMessageToolCall],
        cancel_event: asyncio.Event | None = None,
    ) -> list[models.ToolResult]:
        tasks = [self.execute(tc, cancel_event) for tc in tool_calls]
        return list(await asyncio.gather(*tasks))
