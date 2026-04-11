from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import httpx

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

REQUEST_TIMEOUT = 15.0
MAX_RETRIES = 3
MAX_THROTTLE_WAIT = 15

def _format_weather(data: dict) -> str:
    conditions = data.get("conditions")
    if not conditions:
        conditions = [
            {
                "temperature_c": data["temperature_c"],
                "condition": data["condition"],
                "humidity": data["humidity"],
            }
        ]

    parts = [f"Weather in {data['location']}:"]
    for condition in conditions:
        parts.append(
            f"  {condition['condition']}, {condition['temperature_c']}°C, {condition['humidity']}% humidity"
        )
    if data.get("note"):
        parts.append(f"  Note: {data['note']}")
    return "\n".join(parts)

def _format_research(data: dict) -> str:
    parts = [data.get("summary", "No research summary returned.")]
    if data.get("sources"):
        parts.append(f"Sources: {', '.join(data['sources'])}")
    if data.get("cached") and data.get("cache_age_seconds") is not None:
        days_old = data["cache_age_seconds"] // 86400
        parts.append(f"Note: cached result ({days_old} days old)")
    return "\n".join(parts)

class ToolExecutor:
    def __init__(self, base_url: str, api_key: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-API-Key": api_key},
            timeout=REQUEST_TIMEOUT,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def execute(self, tool_call: dict, cancel_event: asyncio.Event) -> dict:
        name = tool_call["function"]["name"]
        try:
            args = json.loads(tool_call["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            logger.error("Invalid JSON arguments for tool %s: %s", name, tool_call["function"]["arguments"])
            return {"tool_call_id": tool_call["id"], "content": "Error: invalid tool arguments", "error": True}

        logger.info("Tool call: %s(%s)", name, args)
        try:
            if name == "get_weather":
                content = await self._get_weather(args.get("location", ""), cancel_event)
            elif name == "research_topic":
                content = await self._research_topic(args.get("topic", ""), cancel_event)
            else:
                logger.warning("Unknown tool requested: %s", name)
                content = f"Unknown tool: {name}"
            logger.info("Tool %s completed", name)
            logger.debug("Tool %s result: %s", name, content[:200])
            return {"tool_call_id": tool_call["id"], "content": content, "error": False}
        except asyncio.CancelledError:
            logger.warning("Tool %s cancelled by user", name)
            return {"tool_call_id": tool_call["id"], "content": "Tool call was cancelled by the user.", "error": True}
        except httpx.HTTPStatusError as exc:
            logger.error("Tool %s HTTP error %d: %s", name, exc.response.status_code, exc.response.text[:200])
            msg = f"API error ({exc.response.status_code}): {exc.response.text}"
            return {"tool_call_id": tool_call["id"], "content": msg, "error": True}
        except (httpx.RequestError, httpx.TimeoutException, httpx.DecodingError, RuntimeError) as exc:
            logger.error("Tool %s request failed: %s", name, exc)
            return {"tool_call_id": tool_call["id"], "content": f"Request failed: {exc}", "error": True}

    async def _get_weather(self, location: str, cancel_event: asyncio.Event) -> str:
        data = await self._request_with_retry("Weather", "/weather", {"location": location}, cancel_event)
        return _format_weather(data)

    async def _research_topic(self, topic: str, cancel_event: asyncio.Event) -> str:
        data = await self._request_with_retry("Research", "/research", {"topic": topic}, cancel_event)
        return _format_research(data)

    async def _request_with_retry(
        self,
        label: str,
        path: str,
        params: dict,
        cancel_event: asyncio.Event,
    ) -> dict:
        retry_after = 1
        for attempt in range(MAX_RETRIES):
            data = await self._request(path, params, cancel_event)
            if data.get("status") != "throttled":
                return data

            retry_after = min(int(data.get("retry_after_seconds", 1)), MAX_THROTTLE_WAIT)
            logger.warning("%s throttled (attempt %d/%d), retry in %ds", label, attempt + 1, MAX_RETRIES, retry_after)
            if attempt < MAX_RETRIES - 1:
                await _wait_or_cancel(retry_after, cancel_event)

        raise RuntimeError(f"{label} API is rate-limited. Please try again in {retry_after}s.")
    async def _request(self, path: str, params: dict, cancel_event: asyncio.Event) -> dict:
        if cancel_event.is_set():
            raise asyncio.CancelledError

        logger.debug("API request: GET %s params=%s", path, params)
        response = await _race_with_cancel(self._client.get(path, params=params), cancel_event)
        logger.debug("API response: %d %s", response.status_code, response.headers.get("content-type", ""))
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            raise httpx.DecodingError(f"Unexpected response format (got {content_type})")

        return response.json()

async def _wait_or_cancel(delay: float, cancel_event: asyncio.Event) -> None:
    await _race_with_cancel(asyncio.sleep(delay), cancel_event)

async def _race_with_cancel(awaitable, cancel_event: asyncio.Event):
    work_task = asyncio.create_task(awaitable)
    cancel_task = asyncio.create_task(cancel_event.wait())
    done, pending = await asyncio.wait({work_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if cancel_task in done:
        work_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await work_task
        raise asyncio.CancelledError
    return work_task.result()
