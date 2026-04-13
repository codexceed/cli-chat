# CLI Chat

A command-line chat application with streaming LLM responses and tool calling. Built as a take-home implementation demonstrating real-world API integration, including handling of intentionally quirky external services.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Environment variables:
  - `LLM_API_KEY` (required)
  - `ELYOS_API_KEY` (required)
  - `LLM_BASE_URL` (optional, default: OpenRouter)
  - `LLM_MODEL` (optional, default: `openai/gpt-4o-mini`)

  Set them however you prefer — exported in your shell, via CI secrets, or dropped into a local `.env` file, which `pydantic-settings` picks up automatically:
  ```
  LLM_API_KEY=sk-...
  ELYOS_API_KEY=...
  ```

## Setup

```bash
make install
```

### Without `uv`

If you'd rather not install `uv`, use `pip` with a standard virtualenv:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Then run the app directly via the installed entry point:

```bash
cli-chat
```

## Usage

```bash
make run
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

The application uses an **orchestrator pattern** — a central coordinator manages the conversation turn lifecycle, delegating to stateless workers for LLM streaming and tool execution. See [ARCHITECTURE.md](ARCHITECTURE.md) for diagrams and detailed design rationale.

**API quirks** discovered during development are documented in [DISCOVERIES.md](DISCOVERIES.md).

### Project Structure

```
src/cli_chat/
├── main.py          # entry point, signal wiring
├── orchestrator.py  # turn lifecycle, history, LLM streaming, cancellation
├── tools.py         # API calls with retry + quirk handling
├── models.py        # pydantic models (settings, responses)
└── display.py       # streaming output, spinners
```
