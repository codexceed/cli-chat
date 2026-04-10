"""Pydantic models for API responses and application state."""

from __future__ import annotations

import pydantic
import pydantic_settings


class Settings(pydantic_settings.BaseSettings):
    model_config = {"env_file": ".env", "extra": "ignore"}

    openrouter_api_key: str = pydantic.Field(alias="OPENROUTER_API_KEY")
    elyos_api_key: str = pydantic.Field(alias="ELYOS_API_KEY")
    elyos_base_url: str = "https://elyos-interview-907656039105.europe-west2.run.app"
    llm_model: str = pydantic.Field(default="openai/gpt-4o-mini", alias="LLM_MODEL")


# ── Weather models ────────────────────────────────────────────────────────────


class WeatherCondition(pydantic.BaseModel):
    temperature_c: float
    condition: str
    humidity: int | float


class WeatherResponse(pydantic.BaseModel):
    """Normalized weather response — always uses a list of conditions."""

    location: str
    conditions: list[WeatherCondition]
    note: str | None = None

    @classmethod
    def from_api(cls, data: dict) -> WeatherResponse:
        """Handle both flat and array response shapes from the API."""
        if "conditions" in data:
            return cls(**data)
        # Flat response: promote to single-element conditions list
        return cls(
            location=data["location"],
            conditions=[
                WeatherCondition(
                    temperature_c=data["temperature_c"],
                    condition=data["condition"],
                    humidity=data["humidity"],
                )
            ],
        )

    def display(self) -> str:
        parts = [f"Weather in {self.location}:"]
        for c in self.conditions:
            parts.append(f"  {c.condition}, {c.temperature_c}°C, {c.humidity}% humidity")
        if self.note:
            parts.append(f"  Note: {self.note}")
        return "\n".join(parts)


# ── Research models ───────────────────────────────────────────────────────────


class ResearchResponse(pydantic.BaseModel):
    topic: str
    summary: str
    sources: list[str] = []
    generated_at: str | None = None
    cached: bool = False
    cache_age_seconds: int | None = None

    def display(self) -> str:
        parts = [self.summary]
        if self.sources:
            parts.append(f"Sources: {', '.join(self.sources)}")
        if self.cached and self.cache_age_seconds is not None:
            days = self.cache_age_seconds // 86400
            parts.append(f"Note: cached result ({days} days old)")
        return "\n".join(parts)


class ThrottledResponse(pydantic.BaseModel):
    status: str  # "throttled"
    message: str
    retry_after_seconds: int
    data: None = None


# ── Tool result wrapper ───────────────────────────────────────────────────────


class ToolResult(pydantic.BaseModel):
    tool_call_id: str
    name: str
    content: str
    error: bool = False
