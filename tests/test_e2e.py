"""End-to-end stress tests against real APIs and orchestrator logic."""

from __future__ import annotations

import asyncio
import os

import pytest

from cli_chat import tools as tools_module
from cli_chat.tools import _format_research, _format_weather

ELYOS_BASE_URL = os.getenv("ELYOS_BASE_URL", "https://elyos-interview-907656039105.europe-west2.run.app")

@pytest.fixture
def executor() -> tools_module.ToolExecutor:
    return tools_module.ToolExecutor(ELYOS_BASE_URL, os.environ["ELYOS_API_KEY"])


def _weather_call(location: str) -> dict:
    return {
        "id": "test-id",
        "type": "function",
        "function": {"name": "get_weather", "arguments": f'{{"location": "{location}"}}'},
    }


def _research_call(topic: str) -> dict:
    return {
        "id": "test-id",
        "type": "function",
        "function": {"name": "research_topic", "arguments": f'{{"topic": "{topic}"}}'},
    }


def _no_cancel() -> asyncio.Event:
    return asyncio.Event()

class TestWeatherAPI:
    @pytest.mark.asyncio
    async def test_valid_city_returns_data(self, executor: tools_module.ToolExecutor) -> None:
        result = await executor.execute(_weather_call("London"), _no_cancel())
        assert not result["error"]
        assert "London" in result["content"]

    @pytest.mark.asyncio
    async def test_handles_array_format(self, executor: tools_module.ToolExecutor) -> None:
        """Weather API non-deterministically returns flat or array format."""
        result = await executor.execute(_weather_call("Tokyo"), _no_cancel())
        assert not result["error"]
        assert "Tokyo" in result["content"]

    @pytest.mark.asyncio
    async def test_multi_word_city(self, executor: tools_module.ToolExecutor) -> None:
        result = await executor.execute(_weather_call("San Francisco"), _no_cancel())
        assert not result["error"]
        assert "San Francisco" in result["content"]

    @pytest.mark.asyncio
    async def test_invalid_city_returns_error(self, executor: tools_module.ToolExecutor) -> None:
        result = await executor.execute(_weather_call("FakeCity999"), _no_cancel())
        assert result["error"]

    @pytest.mark.asyncio
    async def test_empty_location_returns_error(self, executor: tools_module.ToolExecutor) -> None:
        """Empty location returns 404 or may hit rate limit — both are errors."""
        result = await executor.execute(_weather_call(""), _no_cancel())
        assert result["error"]
        assert result["content"]  # should have an error message

    @pytest.mark.asyncio
    async def test_format_consistency_across_calls(self, executor: tools_module.ToolExecutor) -> None:
        """Regardless of flat vs array format, our display is consistent."""
        successful = []
        for _ in range(3):
            r = await executor.execute(_weather_call("Berlin"), _no_cancel())
            if not r["error"]:
                successful.append(r)
            await asyncio.sleep(1)  # avoid triggering rate limit
        assert len(successful) >= 1, "All 3 calls were rate-limited"
        for r in successful:
            assert "Berlin" in r["content"]
            assert "°C" in r["content"]

class TestResearchAPI:
    @pytest.mark.asyncio
    async def test_returns_summary(self, executor: tools_module.ToolExecutor) -> None:
        result = await executor.execute(_research_call("solar energy"), _no_cancel())
        assert result["content"]
        assert not result["error"] or "rate-limited" in result["content"]

    @pytest.mark.asyncio
    async def test_cached_result_shows_age(self, executor: tools_module.ToolExecutor) -> None:
        """Some topics return stale cached results with age metadata."""
        result = await executor.execute(_research_call("climate change"), _no_cancel())
        if not result["error"]:
            assert "climate change" in result["content"].lower() or "cached" in result["content"].lower()

    @pytest.mark.asyncio
    async def test_empty_topic_handled(self, executor: tools_module.ToolExecutor) -> None:
        """API accepts empty topic without error — we should handle it."""
        result = await executor.execute(_research_call(""), _no_cancel())
        assert result["content"]

class TestCancellation:
    @pytest.mark.asyncio
    async def test_pre_cancelled_weather(self, executor: tools_module.ToolExecutor) -> None:
        cancel = asyncio.Event()
        cancel.set()
        result = await executor.execute(_weather_call("London"), cancel)
        assert result["error"]
        assert "cancelled" in result["content"].lower()

    @pytest.mark.asyncio
    async def test_pre_cancelled_research(self, executor: tools_module.ToolExecutor) -> None:
        cancel = asyncio.Event()
        cancel.set()
        result = await executor.execute(_research_call("test"), cancel)
        assert result["error"]
        assert "cancelled" in result["content"].lower()

    @pytest.mark.asyncio
    async def test_cancel_during_research_sleep(self, executor: tools_module.ToolExecutor) -> None:
        """Cancel event fires during throttle retry sleep."""
        cancel = asyncio.Event()

        async def cancel_after_delay() -> None:
            await asyncio.sleep(0.5)
            cancel.set()

        async def execute() -> dict:
            return await executor.execute(_research_call("test cancel"), cancel)

        result, _ = await asyncio.gather(execute(), cancel_after_delay())
        assert result["content"]

class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_invalid_json_arguments(self, executor: tools_module.ToolExecutor) -> None:
        tc = {"id": "test-id", "type": "function", "function": {"name": "get_weather", "arguments": "not valid json"}}
        result = await executor.execute(tc, _no_cancel())
        assert result["error"]
        assert "invalid" in result["content"].lower()

    @pytest.mark.asyncio
    async def test_unknown_tool_name(self, executor: tools_module.ToolExecutor) -> None:
        tc = {"id": "test-id", "type": "function", "function": {"name": "nonexistent_tool", "arguments": '{"arg": "val"}'}}
        result = await executor.execute(tc, _no_cancel())
        assert "Unknown tool" in result["content"]

    @pytest.mark.asyncio
    async def test_unicode_location_handled_gracefully(self, executor: tools_module.ToolExecutor) -> None:
        """Unicode input causes HTML 400 from Cloud Run infra — shouldn't crash."""
        result = await executor.execute(_weather_call("東京"), _no_cancel())
        assert result["error"] or "Tokyo" not in result["content"]

    @pytest.mark.asyncio
    async def test_special_chars_in_location(self, executor: tools_module.ToolExecutor) -> None:
        """Special characters shouldn't cause crashes."""
        result = await executor.execute(_weather_call("London'; DROP TABLE --"), _no_cancel())
        assert result["content"]

class TestFormatting:
    def test_flat_weather_formatted(self) -> None:
        data = {
            "location": "London",
            "temperature_c": 13.0,
            "condition": "Overcast",
            "humidity": 38,
        }
        output = _format_weather(data)
        assert "London" in output
        assert "13.0°C" in output
        assert "Overcast" in output

    def test_array_weather_formatted(self) -> None:
        data = {
            "location": "Tokyo",
            "conditions": [
                {"temperature_c": 19.2, "condition": "Partly Cloudy", "humidity": 88},
                {"temperature_c": 18.2, "condition": "light rain", "humidity": 100},
            ],
            "note": "Multiple conditions reported",
        }
        output = _format_weather(data)
        assert "Tokyo" in output
        assert "Partly Cloudy" in output
        assert "Note:" in output

    def test_research_cached_display(self) -> None:
        data = {
            "summary": "Test summary",
            "sources": ["a.com"],
            "cached": True,
            "cache_age_seconds": 86400 * 30,
        }
        output = _format_research(data)
        assert "cached" in output.lower()
        assert "30 days" in output

    def test_research_basic_display(self) -> None:
        data = {"summary": "Solar energy is growing.", "sources": ["source1.com", "source2.com"]}
        output = _format_research(data)
        assert "Solar energy" in output
        assert "source1.com" in output
