# Architecture

## Overview

The application follows an **orchestrator pattern** where a central coordinator manages the turn lifecycle, delegating to stateless workers for LLM interaction and tool execution.

```mermaid
graph TD
    A[main.py] -->|creates & wires signals| B[Orchestrator]
    B -->|streams LLM requests| C[ChatClient]
    B -->|dispatches tool calls| D[ToolExecutor]
    B -->|renders output| E[Display]
    C -->|OpenAI SDK| F[OpenRouter API]
    D -->|httpx async| G[Elyos Weather API]
    D -->|httpx async| H[Elyos Research API]
```

## Module Responsibilities

| Module | Role | Stateful? |
|--------|------|-----------|
| `main.py` | Entry point, asyncio event loop, SIGINT wiring | No |
| `orchestrator.py` | Turn lifecycle, conversation history, cancellation coordination | Yes (history, cancel state) |
| `chat.py` | LLM streaming via OpenRouter | No |
| `tools.py` | API calls with retry, quirk handling, cancellation | No |
| `models.py` | Pydantic models for API responses, settings, tool results | No |
| `display.py` | Streaming output, spinners, styled messages | No |

## Turn Lifecycle

```mermaid
sequenceDiagram
    participant U as User
    participant O as Orchestrator
    participant C as ChatClient
    participant T as ToolExecutor
    participant D as Display

    U->>O: input text
    O->>C: stream(history)
    loop streaming chunks
        C-->>O: content delta / tool_call delta
        O->>D: print_streaming_token()
    end
    alt tool calls detected
        O->>D: tool_spinner()
        O->>T: execute(tool_call, cancel_event)
        T-->>O: ToolResult
        O->>C: stream(history + tool results)
        loop streaming final response
            C-->>O: content delta
            O->>D: print_streaming_token()
        end
    end
    O->>D: finish_streaming()
```

## Cancellation Flow

```mermaid
stateDiagram-v2
    [*] --> WaitingForInput
    WaitingForInput --> Exiting: Ctrl+C (no SIGINT handler)
    WaitingForInput --> Processing: user submits input
    Processing --> CancelRequested: 1st Ctrl+C
    CancelRequested --> WaitingForInput: operation cancelled
    CancelRequested --> Exiting: 2nd Ctrl+C
    Exiting --> [*]
```

The signal handler is context-dependent:
- **During input**: No custom SIGINT handler — `loop.add_reader(stdin)` races against `cancel_event.wait()`. Ctrl+C sets the cancel event, `_read_input()` returns `None`, and the app exits.
- **During processing**: Custom handler is installed. 1st Ctrl+C sets `cancel_event`; 2nd sets `should_exit`.
- **On cleanup**: Signal handler is removed before `asyncio.run()` shutdown to avoid stale handlers.

The cancel event is cleared at the start of each new turn.

### Why `add_reader` instead of `asyncio.to_thread(input)`?

Using `asyncio.to_thread(input)` spawns a thread that blocks on `input()`. When Ctrl+C fires, the thread can't be interrupted — it stays alive until the user presses Enter. This causes `asyncio.run()` to hang during executor shutdown (up to 10s timeout). `loop.add_reader(stdin)` avoids threads entirely, keeping shutdown instant.

## Data Flow

```mermaid
flowchart LR
    subgraph API Responses
        W1[Flat weather JSON] -->|from_api| WR[WeatherResponse]
        W2[Array weather JSON] -->|from_api| WR
        R1[Research JSON] --> RR[ResearchResponse]
        T1[Throttled JSON] --> TR[ThrottledResponse]
        HTML[HTML error] --> DE[DecodingError]
    end
    WR -->|display| S[String for LLM]
    RR -->|display| S
    TR -->|retry both endpoints| W1
    TR -->|retry both endpoints| R1
    DE -->|error result| S
```

Note: Both weather and research endpoints can return throttled responses (HTTP 200 with `status: "throttled"`). The retry logic is shared.

## Key Design Decisions

1. **Orchestrator owns all state** — ChatClient and ToolExecutor are stateless workers. This makes the system easy to reason about and test.
2. **Cancel via asyncio.Event** — shared between orchestrator and tool executor, checked cooperatively. No thread interruption or process killing.
3. **Pydantic normalization** — `WeatherResponse.from_api()` handles the non-deterministic API schemas at the boundary, so downstream code always sees a consistent model.
4. **Retry with respect** — throttle retries (both endpoints) use the server's `retry_after_seconds`, not arbitrary backoff. The sleep is cancellable.
5. **Content-type guard** — `_request()` checks for `application/json` before parsing, handling infrastructure-level HTML errors (e.g., unicode input → Cloud Run 400).
