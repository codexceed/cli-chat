# API Discoveries

Documented behaviors discovered through testing the Elyos interview APIs.

## Weather API (`/weather`)

### 1. Non-deterministic Response Schema

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

### 2. Weather Also Rate-Limits (HTTP 200)

Same throttling behavior as research — returns HTTP 200 with `status: "throttled"`. Triggered by rapid requests or long input strings.

**Handling:** Same retry-with-backoff logic as research, using `retry_after_seconds`.

### 3. Input Sanitization / Parsing

XSS payload `<script>alert(1)</script>` is interpreted as city **"Alert"** (returns valid weather data). The API appears to strip HTML tags and use the remaining text.

### 4. Unicode Causes Infrastructure Error

Non-ASCII characters (e.g., `東京`) trigger an **HTML 400 error** from Google Cloud Run infrastructure, not a JSON response. The `content-type` header is `text/html`, not `application/json`.

**Handling:** We check `content-type` before parsing JSON and raise `DecodingError` for non-JSON responses, which is caught and reported gracefully.

### 5. Standard Error Responses
- `404` for unknown cities: `{"error": "Location \"X\" not found"}`
- `422` for missing `location` param (pydantic validation error)
- `401` for invalid/missing API key
- `405` for non-GET methods

---

## Research API (`/research`)

### 1. Rate Limiting Returns HTTP 200

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

### 2. Stale Cached Results

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

### 3. Empty Topic Accepted

The API returns a valid response for an empty `topic=""` string — no validation error. Returns generic summary text.

### 4. Standard Error Responses
- `422` for missing `topic` param
- `401` for invalid/missing API key
