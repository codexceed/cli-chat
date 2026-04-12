"""Entry point — CLI setup, logging configuration, and signal wiring."""

from __future__ import annotations

import asyncio
import datetime
import logging
import signal
import uuid

from cli_chat import display, models, orchestrator

logger = logging.getLogger(__name__)


def _configure_logging() -> str:
    """Set up file-only DEBUG logging with a unique session filename.

    Noisy third-party loggers (httpx, openai, httpcore) are suppressed to
    WARNING level.

    Returns:
        The path to the created log file.
    """
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    session_id = uuid.uuid4().hex[:8]
    log_file = f"cli_chat_{timestamp}_{session_id}.log"
    logging.basicConfig(
        filename=log_file,
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for noisy in ("httpx", "openai", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file


async def _run() -> None:
    """Initialize settings, logging, and signal handling, then start the orchestrator."""
    settings = models.Settings()  # type: ignore[call-arg]
    log_file = _configure_logging()
    logger.info("Session started (model=%s, log_file=%s)", settings.llm_model, log_file)

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
        logger.info("Session ended")


def main() -> None:
    """CLI entry point that runs the async application loop."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
