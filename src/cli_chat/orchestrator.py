"""Orchestrator — manages the turn lifecycle, history, and cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from typing import TYPE_CHECKING

import openai
from openai.types import chat as openai_chat
from openai.types.chat import chat_completion_message_tool_call as tc_mod

from cli_chat import display, tools

if TYPE_CHECKING:
    from cli_chat import models

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = "You are a helpful assistant. Be concise and helpful."


class Orchestrator:
    """Manages the chat turn lifecycle, conversation history, and cancellation."""

    def __init__(self, settings: models.Settings) -> None:
        """Initialize the orchestrator with an LLM client and tool executor.

        Args:
            settings: Application settings containing LLM and API
                configuration.
        """
        self._client = openai.AsyncOpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
        self._model = settings.llm_model
        self._tools = tools.ToolExecutor(settings)
        self._history: list[openai_chat.ChatCompletionMessageParam] = []
        self._cancel_event = asyncio.Event()
        self._should_exit = False
        self._turn_count = 0

    async def close(self) -> None:
        """Shut down the tool executor and release resources."""
        logger.info("Orchestrator closing (turns=%d, history_len=%d)", self._turn_count, len(self._history))
        await self._tools.close()

    def handle_interrupt(self) -> None:
        """Handle a SIGINT signal.

        First invocation cancels the current operation. Second invocation
        requests a full exit from the run loop.
        """
        if self._cancel_event.is_set():
            logger.info("SIGINT: second interrupt, requesting exit")
            self._should_exit = True
        else:
            logger.info("SIGINT: cancelling current operation")
            self._cancel_event.set()

    async def run(self) -> None:
        """Run the main read-eval-print loop until the user exits.

        Reads user input, processes each turn through the LLM, and
        handles exit commands (``exit``, ``quit``, EOF, Ctrl+C).
        """
        while not self._should_exit:
            self._cancel_event.clear()
            user_input = await self._read_input()
            if user_input is None:
                logger.info("Input cancelled or EOF, exiting loop")
                break

            user_input = user_input.strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                logger.info("User requested exit via '%s'", user_input)
                break

            self._turn_count += 1
            logger.info("Turn %d: user input: %s", self._turn_count, user_input)
            await self._process_turn(user_input)

    async def _read_input(self) -> str | None:
        """Read a line from stdin using ``loop.add_reader``, without threads.

        The read is raced against the cancel event so Ctrl+C returns
        immediately instead of blocking.

        Returns:
            The stripped input line, or ``None`` if cancelled or EOF.
        """
        loop = asyncio.get_running_loop()
        display.print_input_prompt()

        line_future: asyncio.Future[str] = loop.create_future()

        def _on_stdin_ready() -> None:
            """Callback fired when stdin has data ready to read."""
            if not line_future.done():
                line_future.set_result(sys.stdin.readline())

        fd = sys.stdin.fileno()
        loop.add_reader(fd, _on_stdin_ready)
        cancel_task = asyncio.create_task(self._cancel_event.wait())

        try:
            await asyncio.wait({line_future, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            loop.remove_reader(fd)
            if not cancel_task.done():
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task

        if not line_future.done():
            return None
        line = line_future.result()
        return line.rstrip("\n") if line else None

    async def _process_turn(self, user_input: str) -> None:
        """Process a single conversation turn.

        Streams the LLM response, executes any requested tool calls, and
        loops until the assistant produces a final text reply or the
        operation is cancelled.

        Args:
            user_input: The user's message text for this turn.
        """
        rollback_point = len(self._history)
        self._history.append(openai_chat.ChatCompletionUserMessageParam(role="user", content=user_input))

        while not self._should_exit:
            result = await self._stream_response()
            if result is None:
                # Infra failure: roll back so the next turn starts from a clean history,
                # instead of leaving a dangling user message or partial tool_call/result pairs.
                logger.info(
                    "Turn %d: rolling back %d history entries after stream failure",
                    self._turn_count,
                    len(self._history) - rollback_point,
                )
                del self._history[rollback_point:]
                break

            content, tool_calls = result

            # Cancelled mid-stream: save partial content to history and stop
            if self._cancel_event.is_set():
                if content:
                    self._append_assistant_message(content + "\n[cancelled]", [])
                    logger.info("Turn %d: saved partial response (%d chars)", self._turn_count, len(content))
                break

            self._append_assistant_message(content, tool_calls)

            if not tool_calls:
                display.finish_streaming()
                logger.info("Turn %d: assistant responded (%d chars)", self._turn_count, len(content))
                break

            logger.info(
                "Turn %d: LLM requested %d tool call(s): %s",
                self._turn_count,
                len(tool_calls),
                [tc.function.name for tc in tool_calls],
            )

            tool_results = await self._execute_tools(tool_calls)
            if tool_results is None:
                # Cancelled: add stub tool results so history stays valid for the LLM
                for tc in tool_calls:
                    self._history.append(
                        openai_chat.ChatCompletionToolMessageParam(
                            role="tool", tool_call_id=tc.id, content="[cancelled by user]"
                        )
                    )
                break

            for tr in tool_results:
                self._history.append(
                    openai_chat.ChatCompletionToolMessageParam(
                        role="tool", tool_call_id=tr.tool_call_id, content=tr.content
                    )
                )

    async def _stream_response(self) -> tuple[str, list[tc_mod.ChatCompletionMessageToolCall]] | None:  # pylint: disable=too-many-branches
        """Stream a chat completion from the LLM and collect the result.

        Prints tokens to stdout as they arrive. Handles mid-stream
        cancellation by closing the stream and returning partial content.

        Returns:
            A tuple of ``(content, tool_calls)`` on success, or ``None``
            if a fatal error occurs during streaming.
        """
        try:
            logger.info("LLM stream request (model=%s, messages=%d)", self._model, len(self._history))
            stream = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, *self._history],
                tools=tools.TOOL_DEFINITIONS,  # type: ignore[arg-type]
                stream=True,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("LLM stream creation failed: %s", exc, exc_info=True)
            display.print_error(f"LLM error: {exc}")
            return None

        content = ""
        tool_calls_by_index: dict[int, dict] = {}
        header_printed = False

        try:
            async for chunk in stream:
                if self._cancel_event.is_set():
                    logger.info("Stream cancelled by user (content so far: %d chars)", len(content))
                    await stream.close()
                    if content:
                        display.finish_streaming()
                        display.print_dim("[cancelled]")
                    else:
                        display.print_dim("\n[cancelled]")
                    # Return partial content so it can be saved to history
                    return content, []

                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue

                if delta.content:
                    if not header_printed:
                        display.print_assistant_header()
                        header_printed = True
                    content += delta.content
                    display.print_streaming_token(delta.content)

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_by_index:
                            tool_calls_by_index[idx] = {"id": tc_delta.id or "", "name": "", "arguments": ""}
                        entry = tool_calls_by_index[idx]
                        if tc_delta.id:
                            entry["id"] = tc_delta.id
                        if tc_delta.function:
                            if tc_delta.function.name:
                                entry["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                entry["arguments"] += tc_delta.function.arguments

        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("Stream error: %s", exc, exc_info=True)
            display.print_error(f"\nStream error: {exc}")
            return None

        logger.debug("Stream completed: %d chars content, %d tool calls", len(content), len(tool_calls_by_index))

        tool_calls = [
            tc_mod.ChatCompletionMessageToolCall(
                id=tc["id"],
                type="function",
                function=tc_mod.Function(name=tc["name"], arguments=tc["arguments"]),
            )
            for tc in tool_calls_by_index.values()
        ]
        return content, tool_calls

    async def _execute_tools(
        self, tool_calls: list[tc_mod.ChatCompletionMessageToolCall]
    ) -> list[models.ToolResult] | None:
        """Execute a list of tool calls sequentially with cancellation checks.

        Args:
            tool_calls: Tool calls requested by the LLM.

        Returns:
            List of ``ToolResult`` objects, or ``None`` if cancelled
            before all tools complete.
        """
        results: list[models.ToolResult] = []
        for tc in tool_calls:
            if self._cancel_event.is_set():
                logger.info("Tool execution cancelled before %s", tc.function.name)
                display.print_dim("[cancelled]")
                return None

            args = {}
            with contextlib.suppress(json.JSONDecodeError):
                args = json.loads(tc.function.arguments)

            display.print_tool_call(tc.function.name, args)
            with display.tool_spinner(tc.function.name, args):
                result = await self._tools.execute(tc, self._cancel_event)

            if result.error:
                display.print_tool_result_error(tc.function.name, result.content)
            else:
                display.print_tool_result_ok(tc.function.name)
            results.append(result)

        return results

    def _append_assistant_message(
        self, content: str, tool_calls: list[tc_mod.ChatCompletionMessageToolCall]
    ) -> None:
        """Append an assistant message to the conversation history.

        Args:
            content: The assistant's text response (may be empty).
            tool_calls: Tool calls the assistant requested (may be empty).
        """
        msg = openai_chat.ChatCompletionAssistantMessageParam(role="assistant")
        if content:
            msg["content"] = content
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ]
        self._history.append(msg)
