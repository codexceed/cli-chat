"""Pydantic models for API responses and application state."""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "extra": "ignore"}

    openrouter_api_key: str = Field(alias="OPENROUTER_API_KEY")
    elyos_api_key: str = Field(alias="ELYOS_API_KEY")
    elyos_base_url: str = "https://elyos-interview-907656039105.europe-west2.run.app"
    llm_model: str = Field(default="openai/gpt-4o-mini", alias="LLM_MODEL")


class WeatherCondition(BaseModel):
    temperature_c: float
    condition: str
    humidity: int | float


class WeatherResponse(BaseModel):
    """Normalized weather response — always uses a list of conditions."""

    location: str
    conditions: list[WeatherCondition]
    note: str | None = None

    @classmethod
    def from_api(cls, data: dict) -> WeatherResponse:
        """Handle both flat and array response shapes from the API."""
        if "conditions" in data:
            return cls(**data)
        return cls(
            location=data["location"],
            conditions=[
                WeatherCondition(
                    temperature_c=data["temperature_c"], condition=data["condition"], humidity=data["humidity"]
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


class ResearchResponse(BaseModel):
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
            parts.append(f"Note: cached result ({self.cache_age_seconds // 86400} days old)")
        return "\n".join(parts)


class ThrottledResponse(BaseModel):
    status: str  # "throttled"
    message: str
    retry_after_seconds: int
    data: None = None


class ToolResult(BaseModel):
    tool_call_id: str
    name: str
    content: str
    error: bool = False
