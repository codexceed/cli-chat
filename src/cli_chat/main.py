"""Entry point — CLI setup and signal wiring."""

from __future__ import annotations

import asyncio
import signal

from cli_chat import display, models, orchestrator


async def _run() -> None:
    settings = models.Settings()  # type: ignore[call-arg]
    orch = orchestrator.Orchestrator(settings)

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, orch.handle_interrupt)

    display.console.print("[bold]CLI Chat[/bold] — type 'exit' to quit, Ctrl+C to cancel\n")

    try:
        await orch.run()
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        await orch.close()
        display.print_dim("\nGoodbye!")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
