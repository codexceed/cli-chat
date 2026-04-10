# Discoveries

Documented behaviors, issues, and solutions encountered during development.

---

## API Quirks

### Weather API (`/weather`)

#### 1. Non-deterministic Response Schema

The API returns two different response shapes **randomly for the same city**. Concurrent requests for London can return either format:

**Flat format:**
```json
{"location": "London", "temperature_c": 13.0, "condition": "Overcast", "humidity": 38}
```

**Array format:**
```json
{
  "location": "London",
  "conditions": [
    {"temperature_c": 12.2, "condition": "Cloudy", "humidity": 44},
    {"temperature_c": 11.2, "condition": "partly cloudy", "humidity": 57}
  ],
  "note": "Multiple conditions reported"
}
```

**Handling:** `WeatherResponse.from_api()` detects the shape and normalizes both into a consistent `conditions` list.

#### 2. Weather Also Rate-Limits (HTTP 200)

Same throttling behavior as research — returns HTTP 200 with `status: "throttled"`. Triggered by rapid requests or long input strings.

**Handling:** Same retry-with-backoff logic as research, using `retry_after_seconds`.

#### 3. Input Sanitization / Parsing

XSS payload `<script>alert(1)</script>` is interpreted as city **"Alert"** (returns valid weather data). The API appears to strip HTML tags and use the remaining text.

#### 4. Unicode Causes Infrastructure Error

Non-ASCII characters (e.g., `東京`) trigger an **HTML 400 error** from Google Cloud Run infrastructure, not a JSON response. The `content-type` header is `text/html`, not `application/json`.

**Handling:** We check `content-type` before parsing JSON and raise `DecodingError` for non-JSON responses, which is caught and reported gracefully.

#### 5. Standard Error Responses
- `404` for unknown cities: `{"error": "Location \"X\" not found"}`
- `422` for missing `location` param (pydantic validation error)
- `401` for invalid/missing API key
- `405` for non-GET methods

---

### Research API (`/research`)

#### 1. Rate Limiting Returns HTTP 200

Instead of `429`, returns **HTTP 200** with:

```json
{
  "status": "throttled",
  "message": "Rate limit exceeded. Please wait.",
  "retry_after_seconds": 11,
  "data": null
}
```

**Handling:** Check response body for `status: "throttled"`, retry up to 3 times respecting `retry_after_seconds` (capped at 15s). Sleep is cancellable.

#### 2. Stale Cached Results

Some topics return cached results months/years old:

```json
{
  "topic": "climate change",
  "summary": "Research on 'climate change' from early 2024...",
  "cached": true,
  "cache_age_seconds": 26784000
}
```

**Handling:** Surface cache age to user: "Note: cached result (X days old)".

#### 3. Empty Topic Accepted

The API returns a valid response for an empty `topic=""` string — no validation error. Returns generic summary text.

#### 4. Standard Error Responses
- `422` for missing `topic` param
- `401` for invalid/missing API key

---

## Application-Level Issues

### 1. Ctrl+C Not Working During Input (Fixed)

**Problem:** Using `asyncio.to_thread(input)` for user input, combined with `loop.add_signal_handler(signal.SIGINT, ...)`, meant Ctrl+C at the prompt was consumed by the signal handler without interrupting `input()`. The app appeared frozen — pressing Ctrl+C showed `^C` but nothing happened.

**Root cause:** `loop.add_signal_handler` fully consumes SIGINT. `input()` in a thread never receives `KeyboardInterrupt`. Attempting to toggle the handler with `loop.remove_signal_handler` restored `SIG_DFL`, which raised `KeyboardInterrupt` inside the event loop's `select()` — crashing on shutdown with a traceback.

**Fix:** Replaced `asyncio.to_thread(input)` with `loop.add_reader(sys.stdin.fileno())`. The stdin reader races against `cancel_event.wait()` via `asyncio.wait(FIRST_COMPLETED)`. No threads, no dangling thread on exit, instant and clean shutdown.

### 2. Ctrl+C Delayed During Tool Calls (Fixed)

**Problem:** Pressing Ctrl+C during a slow research API call (3-8 seconds) did not cancel immediately. The cancel event was set, but the `await self._client.get(...)` blocked the coroutine until the full HTTP response arrived. Users had to double Ctrl+C to exit instead of cancelling.

**Root cause:** The cancel event was only checked before and after the HTTP request, not during it. The httpx `await` held the coroutine for the full request duration.

**Fix:** Added `_cancellable_request()` which races the httpx coroutine against `cancel_event.wait()` via `asyncio.wait(FIRST_COMPLETED)`. When Ctrl+C fires, the HTTP request task is immediately cancelled and the connection closed. Same pattern used by `_read_input` and `_cancellable_sleep`.

### 3. Rate-Limited Results Not Marked as Errors (Fixed)

**Problem:** When the weather or research API returned a throttled response and retries were exhausted, the rate-limit message was returned as a successful `ToolResult(error=False)`. The LLM treated it as a valid tool response, and the user saw no error indication.

**Fix:** Introduced `_RateLimitError` exception. Exhausted throttle retries now raise this exception, which is caught by `execute()` and returned as `ToolResult(error=True)`.

### 4. Unresponsive Exit After Goodbye (Fixed)

**Problem:** After the orchestrator exited and "Goodbye!" was printed, `asyncio.run()` hung during executor shutdown because the `input()` thread (from `asyncio.to_thread`) was still blocking. Python's default executor `shutdown(wait=True)` waited for the thread, causing a 10-second hang before the process exited.

**Fix:** Resolved by the same `loop.add_reader` approach from issue #1 — no threads means no dangling thread during shutdown. The signal handler is also removed in the `finally` block before `asyncio.run()` cleanup, preventing stale handlers.
