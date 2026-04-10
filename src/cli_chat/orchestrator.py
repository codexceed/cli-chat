"""Orchestrator — manages the turn lifecycle, history, and cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from typing import TYPE_CHECKING

from openai.types import chat as oai_chat
from openai.types.chat import chat_completion_message_tool_call as tc_module

from cli_chat import chat as chat_client
from cli_chat import display
from cli_chat import tools as tools_module

if TYPE_CHECKING:
    from cli_chat import models

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, settings: models.Settings) -> None:
        self._chat = chat_client.ChatClient(settings)
        self._tools = tools_module.ToolExecutor(settings)
        self._history: list[oai_chat.ChatCompletionMessageParam] = []
        self._cancel_event = asyncio.Event()
        self._should_exit = False
        self._turn_count = 0

    async def close(self) -> None:
        logger.info(
            "Orchestrator closing (turns=%d, history_len=%d)", self._turn_count, len(self._history)
        )
        await self._tools.close()

    def handle_interrupt(self) -> None:
        """Called by signal handler. First call cancels; second exits."""
        if self._cancel_event.is_set():
            logger.info("SIGINT: second interrupt, requesting exit")
            self._should_exit = True
        else:
            logger.info("SIGINT: cancelling current operation")
            self._cancel_event.set()

    async def run(self) -> None:
        """Main input loop."""
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
        """Read from stdin without threads, cancellable via Ctrl+C."""
        loop = asyncio.get_running_loop()
        display.print_input_prompt()

        line_future: asyncio.Future[str] = loop.create_future()

        def _on_stdin_ready() -> None:
            if not line_future.done():
                line_future.set_result(sys.stdin.readline())

        fd = sys.stdin.fileno()
        loop.add_reader(fd, _on_stdin_ready)

        cancel_task = asyncio.create_task(self._cancel_event.wait())

        try:
            await asyncio.wait(
                {asyncio.ensure_future(line_future), cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            loop.remove_reader(fd)
            if not cancel_task.done():
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task

        if not line_future.done():
            return None  # cancelled

        line = line_future.result()
        return line.rstrip("\n") if line else None

    async def _process_turn(self, user_input: str) -> None:
        self._history.append(
            oai_chat.ChatCompletionUserMessageParam(role="user", content=user_input)
        )

        while not self._should_exit:
            result = await self._stream_response()
            if result is None:
                break  # cancelled or error during streaming

            content, tool_calls = result
            self._append_assistant_message(content, tool_calls)

            if not tool_calls:
                display.finish_streaming()
                logger.info(
                    "Turn %d: assistant responded (%d chars)", self._turn_count, len(content)
                )
                break

            logger.info(
                "Turn %d: LLM requested %d tool call(s): %s",
                self._turn_count,
                len(tool_calls),
                [tc.function.name for tc in tool_calls],
            )

            # Execute tool calls
            tool_results = await self._execute_tools(tool_calls)
            if tool_results is None:
                break  # cancelled

            for tr in tool_results:
                self._history.append(
                    oai_chat.ChatCompletionToolMessageParam(
                        role="tool", tool_call_id=tr.tool_call_id, content=tr.content
                    )
                )
            # Loop back: LLM will process tool results

    async def _stream_response(  # pylint: disable=too-many-branches
        self,
    ) -> tuple[str, list[tc_module.ChatCompletionMessageToolCall]] | None:
        try:
            stream = await self._chat.stream(self._history)
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
                    display.print_dim("\n[cancelled]")
                    return None

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
                            tool_calls_by_index[idx] = {
                                "id": tc_delta.id or "",
                                "name": "",
                                "arguments": "",
                            }
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

        logger.debug(
            "Stream completed: %d chars content, %d tool calls",
            len(content), len(tool_calls_by_index),
        )

        # Build tool call objects
        tool_calls = [
            tc_module.ChatCompletionMessageToolCall(
                id=tc["id"],
                type="function",
                function=tc_module.Function(name=tc["name"], arguments=tc["arguments"]),
            )
            for tc in tool_calls_by_index.values()
        ]

        return content, tool_calls

    async def _execute_tools(
        self, tool_calls: list[tc_module.ChatCompletionMessageToolCall]
    ) -> list[models.ToolResult] | None:
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
        self,
        content: str,
        tool_calls: list[tc_module.ChatCompletionMessageToolCall],
    ) -> None:
        msg: dict = {"role": "assistant"}
        if content:
            msg["content"] = content
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
        self._history.append(msg)  # type: ignore[arg-type]
