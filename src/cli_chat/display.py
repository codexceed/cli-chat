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
    """Print a separator rule and the colored 'You:' input prompt.

    The cursor stays on the same line so the user can type inline.
    """
    console.print()
    console.rule(style="meta")
    sys.stdout.write(f"{_CYAN_BOLD}You:{_RESET} ")
    sys.stdout.flush()


def print_assistant_header() -> None:
    """Print the styled 'Assistant:' header before a streamed response."""
    console.print()
    console.print("Assistant:", style="assistant")


def print_streaming_token(token: str) -> None:
    """Write a single streaming token to stdout and flush immediately.

    Args:
        token: The text fragment to display.
    """
    sys.stdout.write(token)
    sys.stdout.flush()


def finish_streaming() -> None:
    """Write a trailing newline after a streamed response completes."""
    sys.stdout.write("\n")
    sys.stdout.flush()


def print_tool_call(tool_name: str, args: dict) -> None:
    """Print a styled line indicating which tool is being invoked.

    Args:
        tool_name: Name of the tool (e.g. ``get_weather``).
        args: Parsed arguments passed to the tool.
    """
    if tool_name == "get_weather":
        detail = args.get("location", "?")
    elif tool_name == "research_topic":
        detail = args.get("topic", "?")
    else:
        detail = str(args)
    console.print(f"  [tool]⚡ [tool.name]{tool_name}[/tool.name]({detail})[/tool]")


def tool_spinner(tool_name: str, args: dict) -> Live:
    """Create a transient Rich Live spinner for the duration of a tool call.

    Intended to be used as a context manager (``with tool_spinner(...)``).

    Args:
        tool_name: Name of the tool being executed.
        args: Parsed arguments passed to the tool, used to build the label.

    Returns:
        A ``rich.live.Live`` instance wrapping a dots spinner.
    """
    if tool_name == "get_weather":
        label = f"Getting weather for {args.get('location', '?')}..."
    elif tool_name == "research_topic":
        label = f"Researching {args.get('topic', '?')}... (Ctrl+C to cancel)"
    else:
        label = f"Running {tool_name}..."
    spinner = Spinner("dots", text=f"[tool]{label}[/tool]")
    return Live(spinner, console=console, transient=True)


def print_tool_result_ok(tool_name: str) -> None:
    """Print a green check mark indicating a tool call succeeded.

    Args:
        tool_name: Name of the tool that completed.
    """
    console.print(f"  [green]✓[/green] [meta]{tool_name} completed[/meta]")


def print_tool_result_error(tool_name: str, message: str) -> None:
    """Print a red error indicator for a failed tool call.

    Args:
        tool_name: Name of the tool that failed.
        message: Error description to display.
    """
    console.print(f"  [error]✗ {tool_name}:[/error] {message}")


def print_error(msg: str) -> None:
    """Print a general error message in bold red.

    Args:
        msg: The error text to display.
    """
    console.print(f"[error]{msg}[/error]")


def print_dim(msg: str) -> None:
    """Print dimmed/muted text for secondary information.

    Args:
        msg: The text to display in dim style.
    """
    console.print(f"[meta]{msg}[/meta]")
