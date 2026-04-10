"""Display helpers — streaming text output and spinners."""

from __future__ import annotations

import sys

from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner

console = Console()


def print_streaming_token(token: str) -> None:
    sys.stdout.write(token)
    sys.stdout.flush()


def finish_streaming() -> None:
    sys.stdout.write("\n")
    sys.stdout.flush()


def tool_spinner(tool_name: str, args: dict) -> Live:
    if tool_name == "get_weather":
        label = f"Getting weather for {args.get('location', '?')}..."
    elif tool_name == "research_topic":
        label = f"Researching {args.get('topic', '?')}... (Ctrl+C to cancel)"
    else:
        label = f"Running {tool_name}..."
    return Live(Spinner("dots", text=label), console=console, transient=True)


def print_error(msg: str) -> None:
    console.print(f"[red]{msg}[/red]")


def print_dim(msg: str) -> None:
    console.print(f"[dim]{msg}[/dim]")
