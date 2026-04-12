from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import os
import signal
import sys
import uuid

from openai import AsyncOpenAI
from rich.live import Live
from rich.spinner import Spinner

from cli_chat.tools import TOOL_DEFINITIONS, ToolExecutor

SYSTEM_PROMPT = "You are a helpful assistant. Be concise and helpful."
DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_ELYOS_BASE_URL = "https://elyos-interview-907656039105.europe-west2.run.app"
DEFAULT_MODEL = "openai/gpt-4o-mini"
logger = logging.getLogger(__name__)


def _configure_logging() -> str:
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
def _load_config() -> tuple[str, str, str, str, str]:
    return (
        os.environ["LLM_API_KEY"],
        os.getenv("LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
        os.getenv("LLM_MODEL", DEFAULT_MODEL),
        os.environ["ELYOS_API_KEY"],
        os.getenv("ELYOS_BASE_URL", DEFAULT_ELYOS_BASE_URL),
    )

async def _read_input(cancel_event: asyncio.Event) -> str | None:
    loop = asyncio.get_running_loop()
    print(flush=True)
    sys.stdout.write("You: ")
    sys.stdout.flush()
    line_future: asyncio.Future[str] = loop.create_future()

    def _on_stdin_ready() -> None:
        if not line_future.done():
            line_future.set_result(sys.stdin.readline())
    loop.add_reader(sys.stdin.fileno(), _on_stdin_ready)
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        await asyncio.wait({line_future, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        loop.remove_reader(sys.stdin.fileno())
        cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancel_task
    if not line_future.done():
        return None
    line = line_future.result()
    return line.rstrip("\n") if line else None


def _merge_tool_call_delta(tool_calls_by_index: dict[int, dict], tc_delta) -> None:
    entry = tool_calls_by_index.setdefault(
        tc_delta.index,
        {"id": tc_delta.id or "", "type": "function", "function": {"name": "", "arguments": ""}},
    )
    if tc_delta.id:
        entry["id"] = tc_delta.id
    if not tc_delta.function:
        return
    if tc_delta.function.name:
        entry["function"]["name"] = tc_delta.function.name
    if tc_delta.function.arguments:
        entry["function"]["arguments"] += tc_delta.function.arguments

async def _stream_response(  # pylint: disable=too-many-branches
    client: AsyncOpenAI,
    model: str,
    history: list[dict],
    cancel_event: asyncio.Event,
) -> tuple[str, list[dict]] | None:
    logger.info("LLM stream request (model=%s, messages=%d)", model, len(history))
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
        stream = await client.chat.completions.create(  # pyright: ignore[reportCallIssue, reportArgumentType]
            model=model, messages=messages, tools=TOOL_DEFINITIONS, stream=True,  # pyright: ignore[reportArgumentType]
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("LLM stream creation failed: %s", exc, exc_info=True)
        print(f"Error: LLM error: {exc}", file=sys.stderr)
        return None
    content = ""
    tool_calls_by_index: dict[int, dict] = {}
    header_printed = False
    try:
        async for chunk in stream:
            if cancel_event.is_set():
                logger.info("Stream cancelled by user (content so far: %d chars)", len(content))
                await stream.close()
                if content:
                    sys.stdout.write("\n[cancelled]\n")
                else:
                    print("[cancelled]")
                return content, []
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is None:
                continue
            if delta.content:
                if not header_printed:
                    print("\nAssistant:")
                    header_printed = True
                content += delta.content
                sys.stdout.write(delta.content)
                sys.stdout.flush()
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    _merge_tool_call_delta(tool_calls_by_index, tc_delta)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("Stream error: %s", exc, exc_info=True)
        print(f"\nError: Stream error: {exc}", file=sys.stderr)
        return None
    logger.debug("Stream completed: %d chars content, %d tool calls", len(content), len(tool_calls_by_index))
    return content, list(tool_calls_by_index.values())


async def _execute_tools(
    tools: ToolExecutor,
    tool_calls: list[dict],
    cancel_event: asyncio.Event,
) -> list[dict] | None:
    results = []
    for tool_call in tool_calls:
        if cancel_event.is_set():
            print("[cancelled]")
            return None
        name = tool_call["function"]["name"]
        args = {}
        with contextlib.suppress(json.JSONDecodeError):
            args = json.loads(tool_call["function"]["arguments"] or "{}")
        if name == "research_topic":
            topic = args.get("topic", "").strip() or "topic"
            print(f"Researching {topic}... (Ctrl+C to cancel)")
        else:
            print(f"Calling {name}({args})")
        with Live(Spinner("dots", text=f"{name}..."), transient=True):
            result = await tools.execute(tool_call, cancel_event)
        if result["error"]:
            print(f"{name} failed: {result['content']}")
        results.append(result)
    return results

async def _process_turn(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    client: AsyncOpenAI,
    model: str,
    tools: ToolExecutor,
    history: list[dict],
    user_input: str,
    cancel_event: asyncio.Event,
) -> None:
    logger.info("User input: %s", user_input)
    rollback_point = len(history)
    history.append({"role": "user", "content": user_input})
    while True:
        result = await _stream_response(client, model, history, cancel_event)
        if result is None:
            logger.info("Rolling back turn (%d entries) after stream failure", len(history) - rollback_point)
            del history[rollback_point:]
            return
        content, tool_calls = result
        if cancel_event.is_set():
            if content:
                logger.info("Saved partial response (%d chars)", len(content))
                history.append({"role": "assistant", "content": content + "\n[cancelled]"})
            return
        assistant_message: dict = {"role": "assistant"}
        if content:
            assistant_message["content"] = content
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls  # pyright: ignore[reportArgumentType]
        history.append(assistant_message)
        if not tool_calls:
            sys.stdout.write("\n")
            sys.stdout.flush()
            return
        tool_results = await _execute_tools(tools, tool_calls, cancel_event)
        if tool_results is None:
            for tc in tool_calls:
                history.append({"role": "tool", "tool_call_id": tc["id"], "content": "[cancelled by user]"})
            return
        for r in tool_results:
            history.append({"role": "tool", "tool_call_id": r["tool_call_id"], "content": r["content"]})

async def run_chat() -> None:
    llm_api_key, llm_base_url, model, elyos_api_key, elyos_base_url = _load_config()
    log_file = _configure_logging()
    client = AsyncOpenAI(api_key=llm_api_key, base_url=llm_base_url)
    tools = ToolExecutor(elyos_base_url, elyos_api_key)
    history: list[dict] = []
    cancel_event = asyncio.Event()
    state = {"should_exit": False}

    def _handle_interrupt() -> None:
        if cancel_event.is_set():
            state["should_exit"] = True
        else:
            cancel_event.set()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, _handle_interrupt)
    logger.info("Session started (model=%s, log_file=%s)", model, log_file)
    print("CLI Chat — type 'exit' to quit, Ctrl+C to cancel\n")
    try:
        while not state["should_exit"]:
            cancel_event.clear()
            user_input = await _read_input(cancel_event)
            if user_input is None:
                break
            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                break
            await _process_turn(client, model, tools, history, user_input, cancel_event)
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        await tools.close()
        print("\nGoodbye!")
        logger.info("Session ended")


def main() -> None:
    asyncio.run(run_chat())


if __name__ == "__main__":
    main()
