"""Orchestrator — manages the turn lifecycle, history, and cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from typing import TYPE_CHECKING

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionToolMessageParam, ChatCompletionUserMessageParam
from openai.types.chat.chat_completion_message_tool_call import ChatCompletionMessageToolCall, Function

from cli_chat.display import (
    finish_streaming,
    print_assistant_header,
    print_dim,
    print_error,
    print_input_prompt,
    print_streaming_token,
    print_tool_call,
    print_tool_result_error,
    print_tool_result_ok,
    tool_spinner,
)
from cli_chat.tools import TOOL_DEFINITIONS, ToolExecutor

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

    from cli_chat.models import Settings, ToolResult

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = "You are a helpful assistant. Be concise and helpful."


class Orchestrator:
    def __init__(self, settings: Settings) -> None:
        self._client = AsyncOpenAI(api_key=settings.openrouter_api_key, base_url="https://openrouter.ai/api/v1")
        self._model = settings.llm_model
        self._tools = ToolExecutor(settings)
        self._history: list[ChatCompletionMessageParam] = []
        self._cancel_event = asyncio.Event()
        self._should_exit = False
        self._turn_count = 0

    async def close(self) -> None:
        logger.info("Orchestrator closing (turns=%d, history_len=%d)", self._turn_count, len(self._history))
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
        print_input_prompt()

        line_future: asyncio.Future[str] = loop.create_future()

        def _on_stdin_ready() -> None:
            if not line_future.done():
                line_future.set_result(sys.stdin.readline())

        fd = sys.stdin.fileno()
        loop.add_reader(fd, _on_stdin_ready)
        cancel_task = asyncio.create_task(self._cancel_event.wait())

        try:
            await asyncio.wait({asyncio.ensure_future(line_future), cancel_task}, return_when=asyncio.FIRST_COMPLETED)
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
        self._history.append(ChatCompletionUserMessageParam(role="user", content=user_input))

        while not self._should_exit:
            result = await self._stream_response()
            if result is None:
                break

            content, tool_calls = result
            self._append_assistant_message(content, tool_calls)

            if not tool_calls:
                finish_streaming()
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
                        ChatCompletionToolMessageParam(
                            role="tool", tool_call_id=tc.id, content="[cancelled by user]"
                        )
                    )
                break

            for tr in tool_results:
                self._history.append(
                    ChatCompletionToolMessageParam(role="tool", tool_call_id=tr.tool_call_id, content=tr.content)
                )

    async def _stream_response(self) -> tuple[str, list[ChatCompletionMessageToolCall]] | None:  # pylint: disable=too-many-branches
        try:
            logger.info("LLM stream request (model=%s, messages=%d)", self._model, len(self._history))
            stream = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, *self._history],
                tools=TOOL_DEFINITIONS,  # type: ignore[arg-type]
                stream=True,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error("LLM stream creation failed: %s", exc, exc_info=True)
            print_error(f"LLM error: {exc}")
            return None

        content = ""
        tool_calls_by_index: dict[int, dict] = {}
        header_printed = False

        try:
            async for chunk in stream:
                if self._cancel_event.is_set():
                    logger.info("Stream cancelled by user (content so far: %d chars)", len(content))
                    await stream.close()
                    print_dim("\n[cancelled]")
                    return None

                delta = chunk.choices[0].delta if chunk.choices else None
                if delta is None:
                    continue

                if delta.content:
                    if not header_printed:
                        print_assistant_header()
                        header_printed = True
                    content += delta.content
                    print_streaming_token(delta.content)

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
            print_error(f"\nStream error: {exc}")
            return None

        logger.debug("Stream completed: %d chars content, %d tool calls", len(content), len(tool_calls_by_index))

        tool_calls = [
            ChatCompletionMessageToolCall(
                id=tc["id"], type="function", function=Function(name=tc["name"], arguments=tc["arguments"])
            )
            for tc in tool_calls_by_index.values()
        ]
        return content, tool_calls

    async def _execute_tools(self, tool_calls: list[ChatCompletionMessageToolCall]) -> list[ToolResult] | None:
        results: list[ToolResult] = []
        for tc in tool_calls:
            if self._cancel_event.is_set():
                logger.info("Tool execution cancelled before %s", tc.function.name)
                print_dim("[cancelled]")
                return None

            args = {}
            with contextlib.suppress(json.JSONDecodeError):
                args = json.loads(tc.function.arguments)

            print_tool_call(tc.function.name, args)
            with tool_spinner(tc.function.name, args):
                result = await self._tools.execute(tc, self._cancel_event)

            if result.error:
                print_tool_result_error(tc.function.name, result.content)
            else:
                print_tool_result_ok(tc.function.name)
            results.append(result)

        return results

    def _append_assistant_message(self, content: str, tool_calls: list[ChatCompletionMessageToolCall]) -> None:
        msg: dict = {"role": "assistant"}
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
        self._history.append(msg)  # type: ignore[arg-type]
