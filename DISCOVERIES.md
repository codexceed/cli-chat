# Discoveries

Documented behaviors, issues, and solutions encountered during development.

---

## API Quirks

### Weather API (`/weather`)

> [!IMPORTANT]
> **Constraints**
> - Response schema is non-deterministic (flat vs array, random per request); data values are stable
> - Rate-limits via HTTP 200 (not 429), shared pool with research (~3 req before throttle)
> - Temperature accurate within ~2-3°C; humidity can diverge up to 22pp
> - Location resolution unreliable for ambiguous names, fictional places, landmarks, ISO codes, and natural language
> - Best identifiers: exact city names, IATA codes, `lat,lon` coordinates, US ZIPs, UK postcodes
> - Fictional/mythological names may return data (Atlantis, Hogwarts) — no way to distinguish from real cities
> - Malformed input (XSS, SQLi) is silently parsed into location names, not rejected
> - Unicode works (accented + CJK); Indian PIN codes and 6-digit numerics return 404

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

The API strips or re-interprets non-city input rather than rejecting it:

- XSS `<script>alert(1)</script>` → resolved to city **"Alert"** (returns weather data)
- SQL injection `New York; DROP TABLE cities;` → resolved to **"Tiar Drop"** (returns weather data)

Neither is rejected — the API parses whatever text remains after stripping tags/punctuation, and if it matches a location, it returns 200.

#### 4. Unicode Input

Accented characters work: `São Paulo` → normalized to `"Sao Paulo"`, returns valid weather.

CJK characters work: `東京` → resolved to `"東京都"`, returns valid weather JSON.

**Note:** Earlier testing observed HTML 400 errors from Cloud Run infrastructure for unicode input. This appears to have been fixed server-side. The `content-type` guard in `_request()` still provides safety if it recurs.

#### 5. Fictitious / Fictional City Names

The API has a loose location database that includes some fictional and mythological names:

| Input | Result |
|-------|--------|
| `Xyzzyville`, `Qwplmzxn` | 404 — not found |
| `Mordor` | 404 — not found |
| `Atlantis` | **200** — returns weather (23.3°C, Partly Cloudy) |
| `Hogwarts` | **200** — returns weather (39.3°C, Sunny, 8% humidity) |
| `Gotham` | **200** — resolved to "Gotham Bridge" |
| `Springfield` | **200** — picks one real Springfield |

Purely nonsensical strings get 404. Names that happen to match entries (mythological, ambiguous, or fictional places that share a name with a real location) return 200 with weather data. The data for these is plausible-looking but not verifiable.

#### 6. Ambiguous Location Resolution

The API silently picks one location when a city name is shared across regions, with no disambiguation or indication of which one was chosen:

| Input | Resolved to | Evidence |
|-------|-------------|----------|
| `"New Delhi"` | New Delhi, India | 31.0°C — matches real-world (~33°C) |
| `"Delhi"` | **Unknown — not Delhi, India** | 1.1°C, 85% humidity — real Delhi is ~33°C, 20% |
| `"Springfield"` | One of many | 8.8°C — no way to tell which Springfield |

`"Delhi"` vs `"New Delhi"` is the clearest example: both should plausibly refer to the Indian capital, but the API returns drastically different data (31°C vs 1°C), indicating `"Delhi"` resolves to a different, likely Western location (e.g., Delhi, NY or Delhi, Ontario).

Comma-qualified names **partially work** for disambiguation:

| Input | Resolved to | Temperature | Notes |
|-------|-------------|------------|-------|
| `"London"` | London | 11.1°C | Defaults to UK |
| `"London, ON"` | London | 0.2°C | Ontario — different data, works |
| `"London, UK"` | London | 11.1°C | Same as bare "London" |
| `"London, Kentucky"` | London | 12.2°C | Different from UK — works |
| `"Delhi"` | Delhi | 1.1°C | Wrong — not India |
| `"Delhi, CA"` | Delhi | 1.1°C | Still wrong — returned same as bare "Delhi" |
| `"Delhi, California"` | Delhi | 31.0°C | Returns India data — qualifier ignored |
| `"Delhi, Louisiana"` | Delhi | 31.0°C | Returns India data — qualifier ignored |
| `"Paris"` | Paris | 15.4°C | Defaults to France |
| `"Paris, TX"` | Paris | 15.0°C | Different data — works |
| `"Birmingham"` | Birmingham | 10.3°C | Defaults to UK |
| `"Birmingham, AL"` | Birmingham | 10.6°C | Alabama — different data, works |
| `"Portland"` | Portland | 11.1°C | Defaults to OR |
| `"Portland, ME"` | Portland | 9.4°C | Maine — different data, works |

Disambiguation is **inconsistent**: state abbreviations sometimes work (ON, AL, ME, TX) but sometimes don't (CA returns wrong data). Full state names (California, Louisiana) can also fail. The `location` field always returns the bare city name with no region qualifier.

#### 7. Broad / Region-Level Location Names

The API attempts to resolve non-city inputs (countries, states, regions) to a specific city:

| Input | Resolved to | Notes |
|-------|-------------|-------|
| `"UK"` | London | Reasonable default |
| `"Georgia"` | Tbilisi | Resolved to the country, not the US state |
| `"Georgia, USA"` | New Georgia | Resolved to New Georgia (Solomon Islands?), not the US state |
| `"Georgia, country"` | Country Acres | Parsed "country" as a location name |
| `"Madhya Pradesh"` | Indore | Reasonable — largest city in MP |
| `"MP India"` | Saipan | Completely wrong — MP parsed as Mariana Pacific? |
| `"Texas"` | Texas City | Picked "Texas City" (a real town in TX) |
| `"California"` | California City | Picked "California City" (a real town in CA) |
| `"Bavaria"` | Bavaria | Returned data, unclear which point |

The API tries its best but results are unpredictable. Natural-language qualifiers like "country" or "USA" are parsed as location text, not understood semantically.

#### 8. Coordinate-Based Lookups

The API **supports lat/lon coordinates** in comma-separated format:

| Input | Resolved to | Notes |
|-------|-------------|-------|
| `"28.6139,77.2090"` | New Delhi | Correct (Delhi coords) |
| `"28.6139, 77.2090"` | New Delhi | Space-tolerant |
| `"51.5074,-0.1278"` | Strand | Correct area (central London) |
| `"-33.8688, 151.2093"` | Pyrmont | Correct area (Sydney harbour) |
| `"51.5074° N, 0.1278° W"` | Rancho W | Wrong — degree notation not supported |
| `"40.7128N 74.0060W"` | 404 | Compact notation not supported |

**`lat,lon` decimal format works** and resolves to a nearby named location. Degree symbols and cardinal-letter notation are not supported. This provides a reliable disambiguation path — coordinates are unambiguous where city names are not.

#### 9. Obscure Locations

| Input | Status | Resolved to |
|-------|--------|-------------|
| `"Chandigarh"` | 200 | Chandigarh |
| `"Dhanaulti"` | 200 | Dhanaulti |
| `"Ziro"` | 200 | Ziro |
| `"Pangong Tso"` | 200 | Pangong |
| `"Spiti Valley"` | 200 | Spiti (−10.4°C, snow — plausible) |
| `"Manimajra"` | 404 | Not found (suburb of Chandigarh) |
| `"Triund"` | 404 | Not found (trek/ridge near Dharamshala) |
| `"Turtuk"` | 404 | Not found (village in Ladakh) |

Coverage is decent for towns and named geographic features but drops off for very small localities (suburbs, treks, remote villages).

#### 10. ZIP / Postal Code Lookups

| Input | Resolved to | Notes |
|-------|-------------|-------|
| `"10001"` | New York | US ZIP — correct |
| `"90210"` | Beverly Hills | US ZIP — correct |
| `"SW1A 1AA"` | London | UK postcode — correct |
| `"W1A 0AX"` | London | UK postcode — correct |
| `"110001"` | 404 | Indian PIN code — not supported |

US ZIP codes and UK postcodes work. Indian PIN codes do not (6-digit numeric, 404'd — recall `"123456"` also 404'd).

#### 11. Airport / IATA Code Lookups

| Input | Resolved to | Notes |
|-------|-------------|-------|
| `"JFK"` | John F Kennedy International Airport | Correct |
| `"LHR"` | London Heathrow Airport | Correct |
| `"DEL"` | Indira Gandhi International Airport | Correct |
| `"CDG"` | Paris Charles de Gaulle Airport | Correct |
| `"SFO"` | San Francisco | City, not airport name, but correct area |

IATA codes work reliably and resolve to the airport or its city. This is another good disambiguation path — `"DEL"` resolves to Delhi India's airport while bare `"Delhi"` does not.

#### 12. Landmark / POI Lookups

| Input | Resolved to | Correct? |
|-------|-------------|----------|
| `"Taj Mahal"` | Taj Mahal | Yes (27.2°C — plausible for Agra) |
| `"Mount Everest"` | Everest | Wrong — 9.4°C/rain, real summit is −20 to −35°C; likely a town named "Everest" |
| `"Eiffel Tower"` | Eiffel | Wrong — 28°C (Eiffel, a town, not Paris) |
| `"Central Park"` | Winsar Park | Wrong |
| `"Statue of Liberty"` | Liberty | Wrong — resolved to a town called "Liberty" |
| `"Great Barrier Reef"` | Barrier | Wrong — resolved to a town called "Barrier" |

The API tokenizes compound names and may match on a single word. Only landmarks that are also unique location names work reliably.

#### 13. ISO Country Codes

| Input | Resolved to | Correct? |
|-------|-------------|----------|
| `"US"` | Ussuriysk | No — city in Russia |
| `"IN"` | Indore | No — a city in India, but not representative |
| `"GB"` | Gbongan | No — city in Nigeria |
| `"FR"` | Frankfurt | No — Germany, not France |
| `"JP"` | Asahi | No — arbitrary city in Japan |

ISO codes are **not treated as country codes** — the API matches them as partial city name strings.

#### 14. Descriptive / Natural Language Queries

| Input | Resolved to | Correct? |
|-------|-------------|----------|
| `"near London"` | Phumi Near Pisei | No — Cambodian village |
| `"north of Delhi"` | Delhi | Partially — got Delhi but ignored qualifier |
| `"capital of France"` | La France | No — a town, not Paris |
| `"largest city in Japan"` | Patna City | No — city in India |

The API has **no natural language understanding** — it tokenizes the input and matches fragments against its location database.

**Handling:** Not yet implemented. The LLM could be prompted to prefer qualified city names, IATA codes, or coordinates for ambiguous locations. Coordinate lookups (`lat,lon`) and IATA codes are the most reliable disambiguation methods the API supports.

#### 15. Weather Data Accuracy (tested 2026-04-11)

Compared Elyos API against wttr.in for 5 real cities:

| City | Elyos | wttr.in | Temp Δ | Humidity Δ | Condition |
|------|-------|---------|--------|------------|-----------|
| London | 11.1°C, 50% | 9°C, 71% | +2.1°C | −21% | Both "Partly cloudy" |
| New York | 10.6°C, 58% | 13°C, 66% | −2.4°C | −8% | Sunny vs Clear |
| Tokyo | 18.2°C, 83% | 18°C, 61% | +0.2°C | +22% | Partly cloudy vs Clear |
| Sydney | 16.2°C, 39% | 18°C, 37% | −1.8°C | +2% | Both "Clear" |
| Cairo | 24.3°C, 36% | 22°C, 43% | +2.3°C | −7% | Both "Sunny" |
| New Delhi | 31.0°C, 26% | 33°C, 20% | −2.0°C | +6% | Both "Partly cloudy" |

**Temperature** is in the right ballpark (within ~2-3°C) but not exact — likely sourced from a different provider or cached. **Humidity** can diverge significantly (up to 22 percentage points for Tokyo). **Conditions** mostly match but not always (e.g., "Sunny" vs "Clear").

Consistency check (3 sequential requests per city): values are stable across requests for the same city. The non-determinism is in response *shape* (flat vs array), not in the weather *data* itself.

#### 16. Standard Error Responses
- `404` for unknown cities: `{"error": "Location \"X\" not found"}`
- `422` for missing `location` param (pydantic validation error)
- `401` for invalid/missing API key
- `405` for non-GET methods

---

### Research API (`/research`)

> [!IMPORTANT]
> **Constraints**
> - **This is a mock API** — no real research is performed; all topics return the same fill-in-the-blank template
> - Sources are always the same three hardcoded values (`nature.com`, `sciencedirect.com`, `arxiv.org`)
> - Rate-limits via HTTP 200 (not 429), shared pool with weather (~3 req before throttle)
> - Response time is artificial `uniform(3, 8)` delay; ~8% of requests hit a 15s timeout returning empty data
> - Non-deterministic empty summary or empty `{}` on ~8% of requests — guarded with graceful fallback
> - Topics truncated at ~50 characters in the summary template
> - ~13% of responses are "cached" with stale 2024 dates

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

#### 3. Empty / Nonsense Topics Accepted

The API returns a valid 200 response for any topic string, no matter how nonsensical:

| Input | Result |
|-------|--------|
| `""` (empty) | 200 — cached generic summary |
| `"asdfghjkl qwerty zxcvbn"` | 200 — generic summary: *"Research summary for '...' This analysis covers key aspects..."* |
| `"zzz123 bloop flurb"` | 200 — same generic template |
| `"the color of invisible sound waves in dimension 7"` | 200 — same generic template |
| `"123456"` | 200 — same generic template |
| `"<script>alert(1)</script>"` | 200 — same generic template (not sanitized like weather) |
| `"東京の天気"` (Japanese) | 200 — same generic template |

The API never rejects a topic on content. In fact, **all topics — legitimate or nonsensical — return the same templated response**. There are only two templates:

**Fresh response:**
```json
{
  "topic": "solar energy",
  "summary": "Research summary for 'solar energy'. This analysis covers key aspects and recent developments in the field.",
  "sources": ["nature.com", "sciencedirect.com", "arxiv.org"],
  "generated_at": "2026-04-11T13:03:37.401717+00:00"
}
```

**Cached response:**
```json
{
  "topic": "quantum computing",
  "summary": "Research on 'quantum computing' from early 2024. This cached summary may not reflect recent developments.",
  "sources": ["nature.com", "sciencedirect.com", "arxiv.org"],
  "generated_at": "2024-03-15T09:00:00Z",
  "cached": true,
  "cache_age_seconds": 26784000
}
```

The summary is always a fill-in-the-blank template echoing the topic back (truncated at ~50 characters), the sources are always the same three hardcoded values, and no actual research content is ever returned. The only difference between a nonsense topic and a real one is whether it hits the cached or fresh template.

**The research API is a mock.** Evidence:
- Identical template for every topic (legitimate or nonsensical)
- Same topic repeated returns different timings — artificial delay, not real processing
- Same topic randomly returns empty summary or empty `{}` — non-deterministic, not content-dependent
- Sources are always the same three hardcoded values (`nature.com`, `sciencedirect.com`, `arxiv.org`) regardless of query — never topic-specific
- Topic string truncated at ~50 chars in the summary (simple string interpolation, not NLP)

#### 4. Response Time Distribution (n=200, tested 2026-04-11)

```
   3- 4s: █████████████████████████████████████ (37)
   4- 5s: ██████████████████████████████████████ (38)
   5- 6s: ██████████████████████████████ (30)
   6- 7s: █████████████████████████████████████ (37)
   7- 8s: █████████████████████████████████████████ (41)
   8- 9s: █ (1)
   9-15s:  (0)
  15-16s: ████████████████ (16)
```

| Stat | n=100 | n=200 |
|------|-------|-------|
| Min | 3.07s | 3.06s |
| Max | 15.14s | 15.32s |
| Mean | 6.23s | 6.32s |
| Median | 5.70s | 5.78s |
| P90 | 7.81s | 7.93s |
| P95 | 15.05s | 15.09s |
| Empty responses | 8/100 | 16/200 (8%) |
| Cached responses | 19/100 | 26/200 (13%) |

Two distinct behaviors:
- **~92% of requests**: uniform distribution across 3-8s — consistent with `sleep(uniform(3, 8))`
- **~8% of requests**: cluster at exactly ~15s with empty summary or `{}` — a timeout path, not a slow response
- **Zero responses between 9-15s** — clean gap confirms two separate code paths, not a continuous distribution
- Results stable across both sample sizes

#### 5. Non-Deterministic Empty Response

Some inputs (observed with whitespace-only `"   "` and XSS payloads) **occasionally return an empty `{}`** with HTTP 200. This is non-deterministic — the same input returns a normal response most of the time, but rarely returns `{}`.

**Impact:** Without a guard, `ResearchResponse(**{})` raises a Pydantic `ValidationError` (missing required `topic` and `summary`).

**Handling:** `_research_topic()` checks for empty or incomplete responses (missing `topic` or empty `summary`) before Pydantic parsing, returning a graceful "no results" message instead of crashing.

#### 6. Standard Error Responses
- `422` for missing `topic` param
- `401` for invalid/missing API key

---

### Shared Behavior (Both APIs)

#### Rate Limiter Characterization (tested 2026-04-11)

Both endpoints share a single rate limit pool. Behavior:

- **Threshold:** ~3 requests pass before throttling kicks in at high request rates (~10 req/s)
- **With 3s pauses:** throttle starts around request #5-6
- **With 5s pauses:** ~4-5 requests reliably pass; throttle intermittent after that
- **Throttle response:** HTTP 200 with `{"status":"throttled", "retry_after_seconds": N, "data": null}`
- **`retry_after_seconds`:** observed range 2-27s; counts down in real time (a shared server-side timer, not per-request)
- **Cross-endpoint:** triggering the limit on `/weather` also throttles `/research` and vice versa
- **Burst:** 20 concurrent weather requests → all 20 throttled. 10 concurrent research → all 10 throttled.

#### Invalid Payloads

| Scenario | Both APIs |
|----------|-----------|
| Missing required param | 422 (pydantic detail) |
| Wrong HTTP method (POST) | 405 `{"detail":"Method Not Allowed"}` |
| Wrong API key | 401 `{"error":"Invalid or missing API key"}` |
| No API key | 401 (same) |
| Extra unknown params | Silently ignored |

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
