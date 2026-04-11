# CLI Chat

A command-line chat application with streaming LLM responses and tool calling. Built as a take-home implementation demonstrating real-world API integration, including handling of intentionally quirky external services.

## Requirements

- Python 3.12+
- API keys in `.env`:
  ```
  OPENROUTER_API_KEY=sk-or-...
  ELYOS_API_KEY=...
  ```

## Setup

### With uv (recommended)

```bash
make install   # runs uv sync
make run       # runs uv run cli-chat
```

### Without uv (bare Python)

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install .
```

## Usage

```bash
# With uv
make run

# Without uv
source .venv/bin/activate
cli-chat
```

**Example session:**
```
CLI Chat — type 'exit' to quit, Ctrl+C to cancel

You: What's the weather in Tokyo?
Weather in Tokyo:
  Partly Cloudy, 19.2°C, 88% humidity
  light rain, 18.2°C, 100% humidity
  Note: Multiple conditions reported

You: Research quantum computing
⠋ Researching quantum computing... (Ctrl+C to cancel)
[3-8 seconds]
Quantum computing is a revolutionary approach...

You: exit
Goodbye!
```

- **Ctrl+C once** — cancels the current operation (tool call or LLM stream)
- **Ctrl+C twice** — exits the application
- Type `exit` or `quit` to leave

## Testing

```bash
make test       # run e2e tests
make lint       # ruff + pyright + pylint
make check      # lint + test (CI gate)
```

## Design

The application uses flat async functions to manage the conversation turn lifecycle, with a `ToolExecutor` class handling API calls. See [ARCHITECTURE.md](ARCHITECTURE.md) for diagrams and detailed design rationale.

**API quirks** discovered during development are documented in [DISCOVERIES.md](DISCOVERIES.md).

### Project Structure

```
src/cli_chat/
├── orchestrator.py  # entry point, turn lifecycle, LLM streaming, cancellation
└── tools.py         # API calls with retry, response formatting
```
