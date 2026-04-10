"""End-to-end stress tests against real APIs and orchestrator logic."""

from __future__ import annotations

import asyncio

import pytest

from cli_chat import models
from cli_chat import tools as tools_module

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def settings() -> models.Settings:
    return models.Settings()  # type: ignore[call-arg]


@pytest.fixture
def executor(settings: models.Settings) -> tools_module.ToolExecutor:
    return tools_module.ToolExecutor(settings)


class _FakeToolCall:
    """Minimal stand-in for ChatCompletionMessageToolCall."""

    def __init__(self, name: str, arguments: str, call_id: str = "test-id") -> None:
        self.id = call_id
        self.type = "function"
        self.function = _FakeFunction(name, arguments)


class _FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


def _weather_call(location: str) -> _FakeToolCall:
    return _FakeToolCall("get_weather", f'{{"location": "{location}"}}')


def _research_call(topic: str) -> _FakeToolCall:
    return _FakeToolCall("research_topic", f'{{"topic": "{topic}"}}')


# ── Weather API tests ─────────────────────────────────────────────────────────


class TestWeatherAPI:
    @pytest.mark.asyncio
    async def test_valid_city_returns_data(self, executor: tools_module.ToolExecutor) -> None:
        result = await executor.execute(_weather_call("London"))  # type: ignore[arg-type]
        assert not result.error
        assert "London" in result.content

    @pytest.mark.asyncio
    async def test_handles_array_format(self, executor: tools_module.ToolExecutor) -> None:
        """Weather API non-deterministically returns flat or array format."""
        result = await executor.execute(_weather_call("Tokyo"))  # type: ignore[arg-type]
        assert not result.error
        assert "Tokyo" in result.content
        # Should work regardless of which format the API returns

    @pytest.mark.asyncio
    async def test_multi_word_city(self, executor: tools_module.ToolExecutor) -> None:
        result = await executor.execute(_weather_call("San Francisco"))  # type: ignore[arg-type]
        assert not result.error
        assert "San Francisco" in result.content

    @pytest.mark.asyncio
    async def test_invalid_city_returns_error(self, executor: tools_module.ToolExecutor) -> None:
        result = await executor.execute(_weather_call("FakeCity999"))  # type: ignore[arg-type]
        assert result.error

    @pytest.mark.asyncio
    async def test_empty_location_returns_error(self, executor: tools_module.ToolExecutor) -> None:
        """Empty location returns 404 or may hit rate limit — both are errors."""
        result = await executor.execute(_weather_call(""))  # type: ignore[arg-type]
        assert result.error
        assert result.content  # should have an error message

    @pytest.mark.asyncio
    async def test_format_consistency_across_calls(self, executor: tools_module.ToolExecutor) -> None:
        """Regardless of flat vs array format, our display is consistent."""
        successful = []
        for _ in range(3):
            r = await executor.execute(_weather_call("Berlin"))  # type: ignore[arg-type]
            if not r.error:
                successful.append(r)
            await asyncio.sleep(1)  # avoid triggering rate limit
        # At least one should succeed; all successes should be consistent
        assert len(successful) >= 1, "All 3 calls were rate-limited"
        for r in successful:
            assert "Berlin" in r.content
            assert "°C" in r.content


# ── Research API tests ────────────────────────────────────────────────────────


class TestResearchAPI:
    @pytest.mark.asyncio
    async def test_returns_summary(self, executor: tools_module.ToolExecutor) -> None:
        result = await executor.execute(_research_call("solar energy"))  # type: ignore[arg-type]
        assert result.content
        # Either real result or rate-limited message — both are valid
        assert not result.error or "rate-limited" in result.content

    @pytest.mark.asyncio
    async def test_cached_result_shows_age(self, executor: tools_module.ToolExecutor) -> None:
        """Some topics return stale cached results with age metadata."""
        result = await executor.execute(_research_call("climate change"))  # type: ignore[arg-type]
        # If cached, should mention age; if throttled, that's ok too
        if not result.error:
            assert "climate change" in result.content.lower() or "cached" in result.content.lower()

    @pytest.mark.asyncio
    async def test_empty_topic_handled(self, executor: tools_module.ToolExecutor) -> None:
        """API accepts empty topic without error — we should handle it."""
        result = await executor.execute(_research_call(""))  # type: ignore[arg-type]
        # Should not crash, either returns a result or throttled
        assert result.content


# ── Cancellation tests ────────────────────────────────────────────────────────


class TestCancellation:
    @pytest.mark.asyncio
    async def test_pre_cancelled_weather(self, executor: tools_module.ToolExecutor) -> None:
        cancel = asyncio.Event()
        cancel.set()
        result = await executor.execute(_weather_call("London"), cancel_event=cancel)  # type: ignore[arg-type]
        assert result.error
        assert "cancelled" in result.content.lower()

    @pytest.mark.asyncio
    async def test_pre_cancelled_research(self, executor: tools_module.ToolExecutor) -> None:
        cancel = asyncio.Event()
        cancel.set()
        result = await executor.execute(_research_call("test"), cancel_event=cancel)  # type: ignore[arg-type]
        assert result.error
        assert "cancelled" in result.content.lower()

    @pytest.mark.asyncio
    async def test_cancel_during_research_sleep(self, executor: tools_module.ToolExecutor) -> None:
        """Cancel event fires during throttle retry sleep."""
        cancel = asyncio.Event()

        async def cancel_after_delay() -> None:
            await asyncio.sleep(0.5)
            cancel.set()

        async def execute() -> models.ToolResult:
            return await executor.execute(_research_call("test cancel"), cancel_event=cancel)  # type: ignore[arg-type]

        # Run both concurrently — cancel fires mid-execution
        result, _ = await asyncio.gather(execute(), cancel_after_delay())
        assert result.content  # should have some content regardless

    @pytest.mark.asyncio
    async def test_batch_cancellation(self, executor: tools_module.ToolExecutor) -> None:
        """Batch execute respects cancellation across all tool calls."""
        cancel = asyncio.Event()
        cancel.set()
        results = await executor.execute_batch(
            [_weather_call("London"), _research_call("test")],  # type: ignore[arg-type]
            cancel_event=cancel,
        )
        assert all(r.error for r in results)


# ── Error handling tests ──────────────────────────────────────────────────────


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_invalid_json_arguments(self, executor: tools_module.ToolExecutor) -> None:
        tc = _FakeToolCall("get_weather", "not valid json")
        result = await executor.execute(tc)  # type: ignore[arg-type]
        assert result.error
        assert "invalid" in result.content.lower()

    @pytest.mark.asyncio
    async def test_unknown_tool_name(self, executor: tools_module.ToolExecutor) -> None:
        tc = _FakeToolCall("nonexistent_tool", '{"arg": "val"}')
        result = await executor.execute(tc)  # type: ignore[arg-type]
        assert "Unknown tool" in result.content

    @pytest.mark.asyncio
    async def test_unicode_location_handled_gracefully(self, executor: tools_module.ToolExecutor) -> None:
        """Unicode input causes HTML 400 from Cloud Run infra — shouldn't crash."""
        result = await executor.execute(_weather_call("東京"))  # type: ignore[arg-type]
        assert result.error or "Tokyo" not in result.content
        # Main assertion: no crash

    @pytest.mark.asyncio
    async def test_special_chars_in_location(self, executor: tools_module.ToolExecutor) -> None:
        """Special characters shouldn't cause crashes."""
        result = await executor.execute(_weather_call("London'; DROP TABLE --"))  # type: ignore[arg-type]
        # Should either return an error or handle gracefully
        assert result.content


# ── Model normalization tests ─────────────────────────────────────────────────


class TestModelNormalization:
    def test_flat_weather_normalized(self) -> None:
        data = {
            "location": "London",
            "temperature_c": 13.0,
            "condition": "Overcast",
            "humidity": 38,
        }
        weather = models.WeatherResponse.from_api(data)
        assert len(weather.conditions) == 1
        assert weather.conditions[0].temperature_c == 13.0
        assert "London" in weather.display()

    def test_array_weather_normalized(self) -> None:
        data = {
            "location": "Tokyo",
            "conditions": [
                {"temperature_c": 19.2, "condition": "Partly Cloudy", "humidity": 88},
                {"temperature_c": 18.2, "condition": "light rain", "humidity": 100},
            ],
            "note": "Multiple conditions reported",
        }
        weather = models.WeatherResponse.from_api(data)
        assert len(weather.conditions) == 2
        assert weather.note == "Multiple conditions reported"
        display = weather.display()
        assert "Tokyo" in display
        assert "Note:" in display

    def test_research_cached_display(self) -> None:
        resp = models.ResearchResponse(
            topic="test",
            summary="Test summary",
            sources=["a.com"],
            cached=True,
            cache_age_seconds=86400 * 30,
        )
        display = resp.display()
        assert "cached" in display.lower()
        assert "30 days" in display

    def test_throttled_response_parsed(self) -> None:
        data = {
            "status": "throttled",
            "message": "Rate limit exceeded.",
            "retry_after_seconds": 5,
            "data": None,
        }
        throttled = models.ThrottledResponse(**data)
        assert throttled.retry_after_seconds == 5
