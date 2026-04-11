# CLI Chat

## Intent

Take-home interview project: CLI chat app with streaming LLM + tool calling against intentionally quirky APIs.

## Quick reference

- `make install` → `make run` to use
- `make check` runs lint + test (20 e2e tests)
- API keys in `.env` (OPENROUTER_API_KEY, ELYOS_API_KEY, optional LLM_MODEL)
- Logs: `cli_chat_<timestamp>_<uuid>.log` per session
- Design docs: README.md, ARCHITECTURE.md, DISCOVERIES.md

## Key decisions

- OpenRouter (OpenAI-compatible) for LLM, model configurable via LLM_MODEL in .env
- httpx async for external API calls, with `asyncio.wait` racing requests against cancel events
- Plain dicts for API responses, formatted by `_format_weather` / `_format_research` helpers
- Simple retry loop for throttle handling (reads `retry_after_seconds`, max 3 attempts)
- Flat async functions: `_process_turn` → `_stream_response` → `_execute_tools`
- Async stdin via `loop.add_reader` (not `asyncio.to_thread(input)`) — avoids dangling threads
- Cancellation adds stub tool results to keep conversation history valid for the LLM
- Rich spinner for tool call pending state
- File-only logging (DEBUG level) with timestamped + UUID session files

## Ctrl+C behavior

- During input: exits cleanly
- During processing: first Ctrl+C cancels current operation, second exits
- During HTTP requests: instant cancellation via `_race_with_cancel` (asyncio.wait race)

## API quirks (see DISCOVERIES.md for details)

- Weather: non-deterministic response schemas (flat vs array, random per request)
- Weather + Research: both can return HTTP 200 for rate limits
- Research: stale cached results with age metadata
- Unicode inputs: HTML 400 from Cloud Run infra (not JSON)
- XSS payloads: sanitized/parsed (e.g., `<script>alert(1)</script>` → city "Alert")
