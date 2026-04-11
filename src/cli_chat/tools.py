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
        """Initialize with retry timing and the throttled endpoint name.

        Args:
            retry_after: Seconds the API asks us to wait before retrying.
            endpoint: Human-readable name of the throttled endpoint
                (e.g. ``"Weather"``).
        """
        super().__init__(f"{endpoint} throttled, retry after {retry_after}s")
        self.retry_after = retry_after
        self.endpoint = endpoint


class _RateLimitError(Exception):
    """Raised when retries are exhausted due to API rate limiting."""


def _throttle_wait(retry_state: RetryCallState) -> float:
    """Compute the wait time before the next retry attempt.

    Uses the ``retry_after`` value from a ``_ThrottledError``, capped at
    ``MAX_THROTTLE_WAIT``. Falls back to 1 second for other exceptions.

    Args:
        retry_state: Tenacity retry state containing the failed outcome.

    Returns:
        Number of seconds to wait before retrying.
    """
    exc = retry_state.outcome.exception()  # type: ignore[union-attr]
    return min(exc.retry_after, MAX_THROTTLE_WAIT) if isinstance(exc, _ThrottledError) else 1


def _log_before_retry(retry_state: RetryCallState) -> None:
    """Log a warning before each throttle retry attempt.

    Args:
        retry_state: Tenacity retry state containing the failed outcome.
    """
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
    """Raise a ``_RateLimitError`` after all retry attempts are exhausted.

    Args:
        retry_state: Tenacity retry state containing the final failed outcome.

    Raises:
        _RateLimitError: Always raised with details from the last failure.
    """
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


class ToolExecutor:
    """Dispatches tool calls to the appropriate API endpoint with retry logic."""

    def __init__(self, settings: models.Settings) -> None:
        """Initialize the executor with an httpx client configured from settings.

        Args:
            settings: Application settings containing API URLs and keys.
        """
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.elyos_base_url,
            headers={"X-API-Key": settings.elyos_api_key},
            timeout=REQUEST_TIMEOUT,
        )

    async def close(self) -> None:
        """Close the underlying httpx client and release connections."""
        await self._client.aclose()

    async def execute(
        self,
        tool_call: tc_module.ChatCompletionMessageToolCall,
        cancel_event: asyncio.Event | None = None,
    ) -> models.ToolResult:
        """Execute a single tool call and return the result.

        Dispatches to the appropriate API method based on the tool name.
        Handles JSON parse errors, HTTP errors, cancellation, and rate
        limiting, wrapping all outcomes in a ``ToolResult``.

        Args:
            tool_call: The LLM-generated tool call to execute.
            cancel_event: Optional event that, when set, cancels the
                in-flight request.

        Returns:
            A ``ToolResult`` containing the tool output or an error message.
        """
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
        """Fetch weather data for a location from the Elyos API.

        Retries automatically on throttled responses.

        Args:
            location: City name to look up.
            cancel_event: Optional cancellation event.

        Returns:
            Formatted weather display string.

        Raises:
            _ThrottledError: When the API returns a throttled response
                (caught by the retry decorator).
        """
        resp = await self._request("/weather", {"location": location}, cancel_event)
        if resp.get("status") == "throttled":
            raise _ThrottledError(models.ThrottledResponse(**resp).retry_after_seconds, "Weather")
        weather = models.WeatherResponse.from_api(resp)
        logger.debug("Weather response format: %s", "array" if "conditions" in resp else "flat")
        return weather.display()

    @_throttle_retry
    async def _research_topic(self, topic: str, cancel_event: asyncio.Event | None) -> str:
        """Fetch research data for a topic from the Elyos API.

        Retries automatically on throttled responses.

        Args:
            topic: Subject to research.
            cancel_event: Optional cancellation event.

        Returns:
            Formatted research display string.

        Raises:
            _ThrottledError: When the API returns a throttled response
                (caught by the retry decorator).
        """
        resp = await self._request("/research", {"topic": topic}, cancel_event)
        if resp.get("status") == "throttled":
            raise _ThrottledError(models.ThrottledResponse(**resp).retry_after_seconds, "Research")
        if not resp or "topic" not in resp or not resp.get("summary"):
            logger.warning("Research returned empty/incomplete response: %s", resp)
            return f"Research for '{topic}' returned no results."
        research = models.ResearchResponse(**resp)
        if research.cached:
            logger.info("Research returned cached result (age=%ds)", research.cache_age_seconds or 0)
        return research.display()

    async def _request(self, path: str, params: dict, cancel_event: asyncio.Event | None) -> dict:
        """Make a GET request to the Elyos API and return parsed JSON.

        If a ``cancel_event`` is provided, the request is raced against
        it for instant cancellation.

        Args:
            path: API path (e.g. ``"/weather"``).
            params: Query parameters to send.
            cancel_event: Optional event that cancels the request when set.

        Returns:
            Parsed JSON response as a dict.

        Raises:
            asyncio.CancelledError: If the cancel event fires before
                or during the request.
            httpx.HTTPStatusError: On non-2xx status codes.
            httpx.DecodingError: If the response content-type is not JSON.
        """
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
        """Race an HTTP request against a cancel event using ``asyncio.wait``.

        Whichever task completes first wins; the loser is cancelled and
        awaited to avoid dangling coroutines.

        Args:
            request_coro: The HTTP request coroutine to execute.
            cancel_event: Event that, when set, aborts the request.

        Returns:
            The HTTP response if the request finishes first.

        Raises:
            asyncio.CancelledError: If the cancel event fires first.
        """
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
        """Execute multiple tool calls concurrently via ``asyncio.gather``.

        Args:
            tool_calls: List of LLM-generated tool calls to execute.
            cancel_event: Optional event that cancels all in-flight
                requests when set.

        Returns:
            List of ``ToolResult`` objects in the same order as the input.
        """
        logger.info("Executing %d tool calls in parallel", len(tool_calls))
        tasks = [self.execute(tc, cancel_event) for tc in tool_calls]
        return list(await asyncio.gather(*tasks))
