# CLI Chat

## Intent
Take-home interview project: CLI chat app with streaming LLM + tool calling against intentionally quirky APIs.

## Quick reference
- `make install` → `make run` to use
- `make check` runs lint + test (21 e2e tests)
- API keys in `.env` (OPENROUTER_API_KEY, ELYOS_API_KEY, optional LLM_MODEL)
- Design docs: README.md, ARCHITECTURE.md, DISCOVERIES.md

## Key decisions
- OpenRouter (OpenAI-compatible) for LLM, model configurable via LLM_MODEL in .env
- httpx async for external API calls
- Pydantic for all data models + settings
- Orchestrator pattern: separates turn lifecycle from chat/tool execution
- Async stdin via `loop.add_reader` (not `asyncio.to_thread(input)`) — avoids dangling threads on exit
- Ctrl+C during input: exits cleanly. Ctrl+C during processing: cancels operation. Double Ctrl+C during processing: exits.

## API quirks (see DISCOVERIES.md for details)
- Weather: non-deterministic response schemas (flat vs array, random per request)
- Weather + Research: both can return HTTP 200 for rate limits
- Research: stale cached results with age metadata
- Unicode inputs: HTML 400 from Cloud Run infra (not JSON)
- XSS payloads: sanitized/parsed (e.g., `<script>alert(1)</script>` → city "Alert")
