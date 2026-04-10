"""Tool executor — weather and research API calls with retry and quirk handling."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import typing
from typing import TYPE_CHECKING

import httpx
from tenacity import RetryCallState, retry, retry_if_exception_type, stop_after_attempt

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
                "properties": {"location": {"type": "string", "description": "City name, e.g. London, Tokyo"}},
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research_topic",
            "description": "Research a topic in depth. Takes 3-8 seconds. Use for detailed research.",
            "parameters": {
                "type": "object",
                "properties": {"topic": {"type": "string", "description": "Topic to research, e.g. 'solar energy'"}},
                "required": ["topic"],
            },
        },
    },
]

MAX_RETRIES = 3
REQUEST_TIMEOUT = 15.0
MAX_THROTTLE_WAIT = 15


# ── Retry infrastructure ─────────────────────────────────────────────────────────────────────────


class _ThrottledError(Exception):
    """Raised when the API returns a throttled response (HTTP 200)."""

    def __init__(self, retry_after: int, endpoint: str) -> None:
        super().__init__(f"{endpoint} throttled, retry after {retry_after}s")
        self.retry_after = retry_after
        self.endpoint = endpoint


class _RateLimitError(Exception):
    """Raised when retries are exhausted due to API rate limiting."""


def _throttle_wait(retry_state: RetryCallState) -> float:
    exc = retry_state.outcome.exception()  # type: ignore[union-attr]
    return min(exc.retry_after, MAX_THROTTLE_WAIT) if isinstance(exc, _ThrottledError) else 1


def _log_before_retry(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception()  # type: ignore[union-attr]
    if isinstance(exc, _ThrottledError):
        logger.warning(
            "%s throttled (attempt %d/%d), retrying in %ds",
            exc.endpoint,
            retry_state.attempt_number,
            MAX_RETRIES,
            min(exc.retry_after, MAX_THROTTLE_WAIT),
        )


def _on_retries_exhausted(retry_state: RetryCallState) -> typing.NoReturn:
    exc = retry_state.outcome.exception()  # type: ignore[union-attr]
    if isinstance(exc, _ThrottledError):
        raise _RateLimitError(f"{exc.endpoint} API is rate-limited. Please try again in {exc.retry_after}s.") from exc
    raise _RateLimitError("Request failed after retries.")


_throttle_retry = retry(
    retry=retry_if_exception_type(_ThrottledError),
    wait=_throttle_wait,  # type: ignore[arg-type]
    stop=stop_after_attempt(MAX_RETRIES),
    before_sleep=_log_before_retry,  # type: ignore[arg-type]
    retry_error_callback=_on_retries_exhausted,  # type: ignore[arg-type]
)


# ── Tool executor ────────────────────────────────────────────────────────────────────────────────


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
            logger.error("Invalid JSON arguments for tool %s: %s", name, tool_call.function.arguments)
            return models.ToolResult(
                tool_call_id=tool_call.id, name=name, content="Error: invalid tool arguments", error=True
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
                tool_call_id=tool_call.id, name=name, content="Tool call was cancelled by the user.", error=True
            )
        except httpx.HTTPStatusError as exc:
            logger.error("Tool %s HTTP error %d: %s", name, exc.response.status_code, exc.response.text[:200])
            return models.ToolResult(
                tool_call_id=tool_call.id,
                name=name,
                content=f"API error ({exc.response.status_code}): {exc.response.text}",
                error=True,
            )
        except (httpx.RequestError, httpx.TimeoutException, httpx.DecodingError) as exc:
            logger.error("Tool %s request failed: %s", name, exc)
            return models.ToolResult(tool_call_id=tool_call.id, name=name, content=f"Request failed: {exc}", error=True)
        except _RateLimitError as exc:
            logger.warning("Tool %s rate-limited after %d retries: %s", name, MAX_RETRIES, exc)
            return models.ToolResult(tool_call_id=tool_call.id, name=name, content=str(exc), error=True)

    @_throttle_retry
    async def _get_weather(self, location: str, cancel_event: asyncio.Event | None) -> str:
        resp = await self._request("/weather", {"location": location}, cancel_event)
        if resp.get("status") == "throttled":
            raise _ThrottledError(models.ThrottledResponse(**resp).retry_after_seconds, "Weather")
        weather = models.WeatherResponse.from_api(resp)
        logger.debug("Weather response format: %s", "array" if "conditions" in resp else "flat")
        return weather.display()

    @_throttle_retry
    async def _research_topic(self, topic: str, cancel_event: asyncio.Event | None) -> str:
        resp = await self._request("/research", {"topic": topic}, cancel_event)
        if resp.get("status") == "throttled":
            raise _ThrottledError(models.ThrottledResponse(**resp).retry_after_seconds, "Research")
        research = models.ResearchResponse(**resp)
        if research.cached:
            logger.info("Research returned cached result (age=%ds)", research.cache_age_seconds or 0)
        return research.display()

    async def _request(self, path: str, params: dict, cancel_event: asyncio.Event | None) -> dict:
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError

        logger.debug("API request: GET %s params=%s", path, params)
        request_coro = self._client.get(path, params=params)
        resp = await (self._cancellable_request(request_coro, cancel_event) if cancel_event else request_coro)

        logger.debug("API response: %d %s", resp.status_code, resp.headers.get("content-type", ""))
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if "json" not in content_type:
            raise httpx.DecodingError(f"Unexpected response format (got {content_type})")

        return resp.json()

    @staticmethod
    async def _cancellable_request(
        request_coro: typing.Coroutine[typing.Any, typing.Any, httpx.Response],
        cancel_event: asyncio.Event,
    ) -> httpx.Response:
        """Race an HTTP request against a cancel event."""
        request_task = asyncio.create_task(request_coro)
        cancel_task = asyncio.create_task(cancel_event.wait())
        done, pending = await asyncio.wait({request_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if cancel_task in done:
            raise asyncio.CancelledError
        return request_task.result()

    async def execute_batch(
        self,
        tool_calls: list[tc_module.ChatCompletionMessageToolCall],
        cancel_event: asyncio.Event | None = None,
    ) -> list[models.ToolResult]:
        logger.info("Executing %d tool calls in parallel", len(tool_calls))
        tasks = [self.execute(tc, cancel_event) for tc in tool_calls]
        return list(await asyncio.gather(*tasks))
