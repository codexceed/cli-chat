# CLI Chat

## Intent

Take-home interview project: CLI chat app with streaming LLM + tool calling against intentionally quirky APIs.

## Quick reference

- `make install` → `make run` to use
- `make check` runs lint + test (21 e2e tests)
- API keys in `.env` (LLM_API_KEY, ELYOS_API_KEY; optional LLM_BASE_URL, LLM_MODEL)
- Logs: `cli_chat_<timestamp>_<uuid>.log` per session
- Design docs: README.md, ARCHITECTURE.md, DISCOVERIES.md

## Key decisions

- Any OpenAI-compatible endpoint for LLM, configurable via LLM_BASE_URL and LLM_MODEL in .env
- httpx async for external API calls, with `asyncio.wait` racing requests against cancel events
- Pydantic for all data models + settings
- Tenacity `@retry` decorator for throttle retry (custom wait from `retry_after_seconds`)
- Orchestrator pattern: separates turn lifecycle from chat/tool execution
- Async stdin via `loop.add_reader` (not `asyncio.to_thread(input)`) — avoids dangling threads
- Cancellation adds stub tool results to keep conversation history valid for the LLM
- Styled display via rich: cyan user, green assistant, yellow tools, red errors
- File-only logging (DEBUG level) with timestamped + UUID session files

## Ctrl+C behavior

- During input: exits cleanly
- During processing: first Ctrl+C cancels current operation, second exits
- During HTTP requests: instant cancellation via `_cancellable_request` (asyncio.wait race)

## API quirks (see DISCOVERIES.md for details)

- Weather: non-deterministic response schemas (flat vs array, random per request)
- Weather + Research: both can return HTTP 200 for rate limits
- Research: stale cached results with age metadata
- Unicode inputs: HTML 400 from Cloud Run infra (not JSON)
- XSS payloads: sanitized/parsed (e.g., `<script>alert(1)</script>` → city "Alert")
