"""Display helpers — styled output, spinners, and streaming."""

from __future__ import annotations

import sys

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.theme import Theme

_THEME = Theme(
    {
        "user": "bold cyan",
        "assistant": "bold green",
        "tool": "yellow",
        "tool.name": "bold yellow",
        "error": "bold red",
        "meta": "dim",
    }
)

console = Console(theme=_THEME)

# ANSI codes for raw stdout (used in input prompt where rich can't help)
_CYAN_BOLD = "\033[1;36m"
_RESET = "\033[0m"


def print_input_prompt() -> None:
    """Print separator + colored 'You:' prompt. Cursor stays on the same line."""
    console.print()
    console.rule(style="meta")
    sys.stdout.write(f"{_CYAN_BOLD}You:{_RESET} ")
    sys.stdout.flush()


def print_assistant_header() -> None:
    console.print()
    console.print("Assistant:", style="assistant")


def print_streaming_token(token: str) -> None:
    sys.stdout.write(token)
    sys.stdout.flush()


def finish_streaming() -> None:
    sys.stdout.write("\n")
    sys.stdout.flush()


def print_tool_call(tool_name: str, args: dict) -> None:
    if tool_name == "get_weather":
        detail = args.get("location", "?")
    elif tool_name == "research_topic":
        detail = args.get("topic", "?")
    else:
        detail = str(args)
    console.print(f"  [tool]⚡ [tool.name]{tool_name}[/tool.name]({detail})[/tool]")


def tool_spinner(tool_name: str, args: dict) -> Live:
    if tool_name == "get_weather":
        label = f"Getting weather for {args.get('location', '?')}..."
    elif tool_name == "research_topic":
        label = f"Researching {args.get('topic', '?')}... (Ctrl+C to cancel)"
    else:
        label = f"Running {tool_name}..."
    spinner = Spinner("dots", text=f"[tool]{label}[/tool]")
    return Live(spinner, console=console, transient=True)


def print_tool_result_ok(tool_name: str) -> None:
    console.print(f"  [green]✓[/green] [meta]{tool_name} completed[/meta]")


def print_tool_result_error(tool_name: str, message: str) -> None:
    console.print(f"  [error]✗ {tool_name}:[/error] {message}")


def print_error(msg: str) -> None:
    console.print(f"[error]{msg}[/error]")


def print_dim(msg: str) -> None:
    console.print(f"[meta]{msg}[/meta]")
