# Implementation Plan

## Phase 1: Scaffold
- [x] pyproject.toml with deps + tool configs (ruff, pyright, pylint, isort)
- [x] Makefile with run/test/lint/format/check/clean
- [x] .gitignore
- [x] CLAUDE.md
- [x] IMPLEMENTATION.md
- [x] Package skeleton (src/cli_chat/, tests/)
- [x] `uv sync` passes

## Phase 2: Models + Tools
- [x] Pydantic models for weather (flat + array), research (normal, throttled, cached)
- [x] Tool executor: weather + research via httpx async
- [x] Retry logic for throttled research (respect retry_after_seconds)
- [x] Normalize inconsistent weather schemas via WeatherResponse.from_api()

## Phase 3: Chat Client
- [x] OpenAI SDK streaming via OpenRouter
- [x] Tool call detection and extraction from stream
- [x] Tool definitions (get_weather, research_topic)

## Phase 4: Orchestrator + Entry Point
- [x] Turn lifecycle: input → LLM stream → tool calls → results → LLM stream
- [x] Conversation history management
- [x] Display: streaming text output + spinner for tool calls
- [x] Signal handling: double Ctrl+C (cancel op, then exit)
- [x] Main entry point wiring

## Phase 5: Polish
- [x] e2e tests (5 tests, all passing)
- [x] README.md
- [x] ARCHITECTURE.md with mermaid diagrams
- [x] DISCOVERIES.md with API quirks
- [x] All linters pass clean (ruff, pyright 0 errors, pylint 10/10)
