"""Tool executor — weather and research API calls with retry and quirk handling."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import typing
from typing import TYPE_CHECKING

import httpx

from cli_chat import models

if TYPE_CHECKING:
    from openai.types.chat import chat_completion_message_tool_call as tc_module

logger = logging.getLogger(__name__)

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
            logger.error(
                "Invalid JSON arguments for tool %s: %s", name, tool_call.function.arguments
            )
            return models.ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                content="Error: invalid tool arguments",
                error=True,
            )

        logger.info("Tool call: %s(%s)", name, args)

        try:
            if name == "get_weather":
                content = await self._get_weather(args.get("location", ""), cancel_event)
            elif name == "research_topic":
                content = await self._research_topic(args.get("topic", ""), cancel_event)
            else:
                logger.warning("Unknown tool requested: %s", name)
                content = f"Unknown tool: {name}"
            logger.info("Tool %s completed successfully", name)
            logger.debug("Tool %s result: %s", name, content[:200])
            return models.ToolResult(tool_call_id=tool_call.id, name=name, content=content)
        except asyncio.CancelledError:
            logger.warning("Tool %s cancelled by user", name)
            return models.ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                content="Tool call was cancelled by the user.",
                error=True,
            )
        except httpx.HTTPStatusError as exc:
            body = exc.response.text
            logger.error("Tool %s HTTP error %d: %s", name, exc.response.status_code, body[:200])
            return models.ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                content=f"API error ({exc.response.status_code}): {body}",
                error=True,
            )
        except (httpx.RequestError, httpx.TimeoutException, httpx.DecodingError) as exc:
            logger.error("Tool %s request failed: %s", name, exc)
            return models.ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                content=f"Request failed: {exc}",
                error=True,
            )
        except _RateLimitError as exc:
            logger.warning("Tool %s rate-limited after %d retries: %s", name, MAX_RETRIES, exc)
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
                    logger.warning(
                        "Weather throttled (attempt %d/%d), retrying in %ds",
                        attempt + 1, MAX_RETRIES, wait,
                    )
                    await self._cancellable_sleep(wait, cancel_event)
                    continue
                raise _RateLimitError(
                    "Weather API is rate-limited. "
                    f"Please try again in {throttled.retry_after_seconds}s."
                )

            weather = models.WeatherResponse.from_api(resp)
            response_format = "array" if "conditions" in resp else "flat"
            logger.debug("Weather response format: %s", response_format)
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
                    logger.warning(
                        "Research throttled (attempt %d/%d), retrying in %ds",
                        attempt + 1, MAX_RETRIES, wait,
                    )
                    await self._cancellable_sleep(wait, cancel_event)
                    continue
                raise _RateLimitError(
                    "Research API is rate-limited. "
                    f"Please try again in {throttled.retry_after_seconds}s."
                )

            research = models.ResearchResponse(**resp)
            if research.cached:
                logger.info(
                    "Research returned cached result (age=%ds)",
                    research.cache_age_seconds or 0,
                )
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

        logger.debug("API request: GET %s params=%s", path, params)

        # Race the HTTP request against cancellation so Ctrl+C is instant
        request_coro = self._client.get(path, params=params)
        if cancel_event is None:
            resp = await request_coro
        else:
            resp = await self._cancellable_request(request_coro, cancel_event)

        logger.debug("API response: %d %s", resp.status_code, resp.headers.get("content-type", ""))
        resp.raise_for_status()

        # Quirk: infrastructure errors (e.g. unicode input) return HTML, not JSON
        content_type = resp.headers.get("content-type", "")
        if "json" not in content_type:
            msg = f"Unexpected response format (got {content_type})"
            raise httpx.DecodingError(msg)

        return resp.json()

    @staticmethod
    async def _cancellable_request(
        request_coro: typing.Coroutine[typing.Any, typing.Any, httpx.Response],
        cancel_event: asyncio.Event,
    ) -> httpx.Response:
        """Race an HTTP request against a cancel event."""
        request_task = asyncio.create_task(request_coro)
        cancel_task = asyncio.create_task(cancel_event.wait())

        done, pending = await asyncio.wait(
            {request_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if cancel_task in done:
            raise asyncio.CancelledError

        return request_task.result()

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
        logger.info("Executing %d tool calls in parallel", len(tool_calls))
        tasks = [self.execute(tc, cancel_event) for tc in tool_calls]
        return list(await asyncio.gather(*tasks))
