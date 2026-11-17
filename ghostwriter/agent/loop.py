"""One message's answer: model rounds, and workbench's tools between them, within the message's caps.

The loop discovers workbench's tools once, then streams rounds. Each round's messages are
fitted to the prompt budget (:func:`~ghostwriter.agent.context.fit`); a reasoning message has
the smaller budget and every round of it asks the model to think. Text goes out as it
arrives, a blank line between two rounds' texts, and the answer stored is exactly what went
out. A round that asks for calls runs them in order, at most ``MAX_CALLS_PER_ROUND``: a call
to a tool not offered, with arguments that are not an object or an id the record cannot hold
(:data:`CALL_ID`), or without its full timeout left of the message's wall is refused to the
model and never sent; the last also withdraws the tools for the rest of the message. A call
workbench answers busy is sent once more after ``BUSY_RETRY_S``, while its timeout still fits.
Every call is disclosed before it is sent, and every round recorded after it ends.

A round goes out without tools past ``MAX_TOOL_ROUNDS``, below ``TOOL_CUTOFF_S`` of the wall,
or once the tools were withdrawn, and it is the last: the model must answer. The turn also
ends on a round without calls, on a stop (after the call in flight; no round starts once the
stop is set) and on a model error. A round that fails once the stop is set, as a stalled
router's read timing out, ends the turn as stopped, not with the model error. No round is
assumed to end in any given time: the wall is read from the clock before each round, each
call and each retry.

The log holds round numbers, counts and exception types; never a message, an argument or a result.
"""

import re
import threading
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, Protocol, TypeAlias
from uuid import uuid4

from loguru import logger

from ghostwriter.agent.context import fit
from ghostwriter.agent.limits import (
    BUSY_RETRY_S,
    MAX_CALLS_PER_ROUND,
    MAX_RESULTS_PER_MESSAGE,
    MAX_TOOL_ROUNDS,
    MESSAGE_WALL_S,
    PROMPT_TOKENS,
    REASONING_PROMPT_TOKENS,
    TOOL_CUTOFF_S,
)
from ghostwriter.agent.prompt import system_prompt
from ghostwriter.agent.stream import RoundDone, TextDelta, ToolCall, stream_round
from ghostwriter.agent.tools import (
    BUSY_TOKENS,
    REFUSALS,
    Refusal,
    echoed_arguments,
    function_tools,
    parse_arguments,
    result_kind,
    step_detail,
    tool_message,
)
from ghostwriter.config import Settings
from ghostwriter.db import utcnow
from ghostwriter.inference import ModelClient, inference_headers
from ghostwriter.schemas import ResultOut, StepOut
from ghostwriter.store import ChatStore
from ghostwriter.utils.audit_event import AuditWriteError
from ghostwriter.workbench import CallFor, OutputFile, ToolFailure, ToolResult, WorkbenchClient, chat_folder

#: A call id the store and the audit take; a call with another is refused, and answered under one the loop mints.
CALL_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: What the user is told when a model round failed.
MODEL_ERROR = "The model did not answer."
#: What the user is told when anything else broke the answer.
INTERNAL_ERROR = "The answer could not be finished."


@dataclass(frozen=True)
class Turn:
    """One message to answer.

    Attributes:
        chat_id: The chat.
        owner: Its owner.
        user_message_id: The message answered.
        assistant_message_id: The answer being written.
        call: The chat as workbench is called for it.
        request_id: The request's correlation id.
        cancel: Set to stop the answer.
        reasoning: The message's reasoning switch: every round may think first.
    """

    chat_id: str
    owner: str
    user_message_id: str
    assistant_message_id: str
    call: CallFor
    request_id: str
    cancel: threading.Event
    reasoning: bool = False


@dataclass(frozen=True)
class TokenEvent:
    """Answer text, as it arrived.

    Attributes:
        text: Text not sent before.
    """

    text: str


@dataclass(frozen=True)
class StepEvent:
    """A tool step: ``running``, then ``ok`` or ``error`` under the same id.

    Attributes:
        step: The step.
    """

    step: StepOut


@dataclass(frozen=True)
class ResultEvent:
    """A file a tool wrote, as a card.

    Attributes:
        result: The card.
    """

    result: ResultOut


@dataclass(frozen=True)
class ErrorEvent:
    """Why the answer ended early.

    Attributes:
        code: ``model``, ``audit`` or ``internal``.
        message: What the user is told.
    """

    code: Literal["model", "audit", "internal"]
    message: str


TurnEvent: TypeAlias = TokenEvent | StepEvent | ResultEvent | ErrorEvent


class TurnAudit(Protocol):
    """Where a turn records what it discloses and each model round."""

    def disclosed(self, *, tool: str, round: int, call_id: str, attempt: int) -> None:
        """Record a call about to be sent to workbench.

        Args:
            tool: The tool.
            round: The model round that asked for it.
            call_id: The model's id for the call.
            attempt: 1, or 2 for the retry after a busy answer.

        Raises:
            AuditWriteError: When the record could not be written; the call is not sent.
        """
        ...

    def model_called(self, *, round: int, done: RoundDone | None, outcome: Literal["ok", "error"]) -> None:
        """Record a model round once it ended.

        Args:
            round: The round.
            done: How it ended; ``None`` when it failed.
            outcome: ``ok``, or ``error`` for a failed round.

        Raises:
            AuditWriteError: When the record could not be written.
        """
        ...


@dataclass
class _Progress:
    """What one message's answer has done so far.

    Attributes:
        started: The clock's reading when the answer began.
        shown: Every text sent, in order.
        steps: Steps stored so far.
        cards: The ids of the files shown as cards.
        withdrawn: Whether the tools were withdrawn for the rest of the message.
    """

    started: float
    shown: list[str] = field(default_factory=list)
    steps: int = 0
    cards: set[str] = field(default_factory=set)
    withdrawn: bool = False


def _preview(output: OutputFile) -> dict[str, Any] | None:
    """Return a table preview as the store keeps it.

    Args:
        output: The file.

    Returns:
        The preview's columns, rows, row count and truncation; ``None`` without one.
    """
    preview = output.preview
    if preview is None:
        return None
    return {
        "columns": list(preview.columns),
        "rows": [list(row) for row in preview.rows],
        "row_count": preview.row_count,
        "truncated": preview.truncated,
    }


class AgentLoop:
    """Answer messages with the model and workbench's tools."""

    def __init__(
        self,
        *,
        model: ModelClient,
        workbench: WorkbenchClient | None,
        store: ChatStore,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        """Hold the loop's neighbours.

        Args:
            model: The chat model.
            workbench: workbench; ``None`` when it is not configured.
            store: The chats.
            settings: The tool call timeout and the file period.
            clock: The monotonic clock the wall is read from.
            now: The time of day, for the system prompt's date.
        """
        self._model = model
        self._workbench = workbench
        self._store = store
        self._settings = settings
        self._clock = clock
        self._now = now

    def run(self, turn: Turn, audit: TurnAudit) -> Iterator[TurnEvent]:
        """Answer a message.

        The answer's text is stored when the run ends, however it ends.

        Args:
            turn: The message.
            audit: Where disclosures and model rounds are recorded.

        Yields:
            TurnEvent: Text, steps and cards as they come; an :class:`ErrorEvent` last when
            the answer ended early.

        Raises:
            AuditWriteError: When a record could not be written; raised after the text is stored.
        """
        message = _Progress(started=self._clock())
        try:
            try:
                yield from self._answer(turn, audit, message)
            except AuditWriteError:
                raise
            except Exception as exc:
                logger.error("An answer broke off: {}", type(exc).__name__)
                yield ErrorEvent("internal", INTERNAL_ERROR)
        finally:
            self._store_text(turn, "".join(message.shown))

    def _store_text(self, turn: Turn, text: str) -> None:
        """Store the answer's text; a failure is logged, never raised over the turn's own outcome.

        Args:
            turn: The message.
            text: Everything shown.
        """
        try:
            self._store.finish_assistant_message(turn.assistant_message_id, text)
        except Exception as exc:
            logger.error("An answer's text could not be stored: {}", type(exc).__name__)

    def _left(self, message: _Progress) -> float:
        """Return the seconds left of the message's wall.

        Args:
            message: The answer so far.

        Returns:
            The seconds; negative once the wall is past.
        """
        return MESSAGE_WALL_S - (self._clock() - message.started)

    def _answer(self, turn: Turn, audit: TurnAudit, message: _Progress) -> Iterator[TurnEvent]:
        """Run the rounds.

        Args:
            turn: The message.
            audit: The turn's audit.
            message: The answer so far.

        Yields:
            TurnEvent: As :meth:`run`.
        """
        if turn.cancel.is_set():
            return
        key = self._settings.workbench_api_key
        workbench = self._workbench
        discovery = workbench.discover(call=turn.call) if workbench is not None else None
        offered: list[dict[str, Any]] = function_tools(discovery.tools) if discovery is not None else []
        names: list[str] = [tool.name for tool in discovery.tools] if discovery is not None else []
        folder = chat_folder(key, turn.chat_id) if discovery is not None and key else None
        text, position = self._store.user_turn(turn.chat_id, turn.user_message_id)
        past, expired = self._store.past_turns(turn.chat_id, before_position=position)
        system = system_prompt(
            today=self._now().date(), folder=folder, file_days=self._settings.file_days, expired=expired
        )
        budget = REASONING_PROMPT_TOKENS if turn.reasoning else PROMPT_TOKENS
        headers = inference_headers(turn.owner, turn.request_id)
        current: list[dict[str, Any]] = [{"role": "user", "content": text}]
        round_no = 0
        while not turn.cancel.is_set():
            round_no += 1
            tools = (
                offered
                if round_no <= MAX_TOOL_ROUNDS and not message.withdrawn and self._left(message) >= TOOL_CUTOFF_S
                else []
            )
            done: RoundDone | None = None
            said: list[str] = []
            messages = fit(system, tools, past, current, budget=budget)
            try:
                for event in stream_round(
                    self._model,
                    messages,
                    tools=tools,
                    headers=headers,
                    cancel=turn.cancel,
                    reasoning=turn.reasoning,
                ):
                    if isinstance(event, TextDelta):
                        if not said and message.shown:
                            message.shown.append("\n\n")
                            yield TokenEvent("\n\n")
                        said.append(event.text)
                        message.shown.append(event.text)
                        yield TokenEvent(event.text)
                    else:
                        done = event
            except Exception as exc:
                stopped = turn.cancel.is_set()
                logger.warning(
                    "Model round {} failed{}: {}", round_no, " after a stop" if stopped else "", type(exc).__name__
                )
                audit.model_called(round=round_no, done=None, outcome="error")
                if not stopped:
                    yield ErrorEvent("model", MODEL_ERROR)
                return
            if done is None:
                raise RuntimeError("a model round ended without its last event")
            audit.model_called(round=round_no, done=done, outcome="ok")
            if done.stopped or not done.calls or not tools or workbench is None:
                return
            calls: list[dict[str, Any]] = []
            answers: list[dict[str, Any]] = []
            for index, call in enumerate(done.calls):
                if turn.cancel.is_set():
                    return
                outcome = yield from self._call(turn, audit, message, workbench, call, index, round_no, names)
                call_id = call.id if CALL_ID.fullmatch(call.id) else f"call_{uuid4().hex}"
                calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": echoed_arguments(call.arguments)},
                    }
                )
                answers.append({"role": "tool", "tool_call_id": call_id, "content": tool_message(outcome)})
            current.append({"role": "assistant", "content": done.text, "tool_calls": calls})
            current.extend(answers)

    def _prepare(self, message: _Progress, call: ToolCall, index: int, names: list[str]) -> dict[str, Any] | Refusal:
        """Prepare a call, or refuse it before it is sent.

        Args:
            message: The answer so far; a call without its timeout left withdraws the tools.
            call: The call.
            index: Its place in the round.
            names: The tools offered.

        Returns:
            The arguments to send, or why the call is not sent.
        """
        if index >= MAX_CALLS_PER_ROUND:
            return Refusal("call_limit", REFUSALS["call_limit"])
        if call.name not in names:
            text = REFUSALS["unknown_tool"].format(name=call.name, names=", ".join(names) or "none")
            return Refusal("unknown_tool", text)
        arguments = parse_arguments(call.arguments)
        if arguments is None or not CALL_ID.fullmatch(call.id):
            return Refusal("bad_arguments", REFUSALS["bad_arguments"].format(name=call.name))
        if message.withdrawn or self._left(message) < self._settings.workbench_timeout_s:
            message.withdrawn = True
            return Refusal("no_time", REFUSALS["no_time"])
        return arguments

    def _call(
        self,
        turn: Turn,
        audit: TurnAudit,
        message: _Progress,
        workbench: WorkbenchClient,
        call: ToolCall,
        index: int,
        round_no: int,
        names: list[str],
    ) -> Generator[TurnEvent, None, ToolResult | ToolFailure | Refusal]:
        """Run one call of a round, or refuse it.

        Args:
            turn: The message.
            audit: The turn's audit.
            message: The answer so far.
            workbench: workbench.
            call: The call.
            index: Its place in the round.
            round_no: The round.
            names: The tools offered.

        Yields:
            TurnEvent: The step's ``running`` and final frames, then its new cards.

        Returns:
            What the model reads back about it.
        """
        arguments = self._prepare(message, call, index, names)
        if isinstance(arguments, Refusal):
            return arguments
        step_id = str(uuid4())
        detail = step_detail(call.name, arguments)
        running = StepOut(
            id=step_id,
            tool=call.name,
            status="running",
            detail=detail,
            arguments=call.arguments,
            result=None,
            expired=False,
        )
        yield StepEvent(running)
        # Before anything goes out: the chat may hold files from now on, and its delete asks workbench.
        self._store.mark_files(turn.chat_id)
        timeout = self._settings.workbench_timeout_s
        attempt = 1
        while True:
            audit.disclosed(tool=call.name, round=round_no, call_id=call.id, attempt=attempt)
            outcome = workbench.call_tool(call.name, arguments, call=turn.call, timeout_s=timeout)
            busy = isinstance(outcome, ToolFailure) and outcome.token in BUSY_TOKENS
            if not busy or attempt == 2 or self._left(message) - BUSY_RETRY_S < timeout:
                break
            if turn.cancel.wait(BUSY_RETRY_S) or self._left(message) < timeout:
                break
            attempt = 2
        step = self._store.add_step(
            turn.assistant_message_id,
            id=step_id,
            position=message.steps,
            round=round_no,
            call_id=call.id,
            tool=call.name,
            arguments=call.arguments,
            result=tool_message(outcome),
            status="ok" if isinstance(outcome, ToolResult) else "error",
        )
        message.steps += 1
        cards: list[ResultOut] = []
        if isinstance(outcome, ToolResult):
            for output in outcome.outputs:
                if output.id in message.cards or len(message.cards) >= MAX_RESULTS_PER_MESSAGE:
                    continue
                message.cards.add(output.id)
                cards.append(
                    self._store.add_result(
                        turn.assistant_message_id,
                        kind=result_kind(output),
                        file_id=output.id,
                        path=output.path,
                        name=output.name,
                        mime=output.mime,
                        size=output.size,
                        preview=_preview(output),
                    )
                )
        yield StepEvent(step)
        for card in cards:
            yield ResultEvent(card)
        return outcome
