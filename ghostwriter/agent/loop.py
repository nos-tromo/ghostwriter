"""One message's answer: model rounds, and workbench's tools between them, within the message's caps.

The loop discovers workbench's tools once, then streams rounds. Each round's messages are
fitted to the prompt budget (:func:`~ghostwriter.agent.context.fit`); a reasoning message has
the smaller budget and every round of it asks the model to think. Text goes out as it
arrives, a blank line between two rounds' texts, and the answer stored is exactly what went
out. A round that asks for calls runs them in order, at most ``MAX_CALLS_PER_ROUND``: a call
to a tool not offered, with arguments that are not an object or an id the record cannot hold
(:data:`CALL_ID`), or without its full timeout left of the message's wall is refused to the
model and never sent; the last also withdraws the tools for the rest of the message. A round
at its cap (its finish reason ``length``, or its output tokens at its ``max_tokens``: vLLM says
``tool_calls`` once a call's name streamed, at the cap too) may have cut its last call: that
call, its arguments not an object or empty, is refused as cut, and the model reads to make them
shorter. Without a usage count, only the finish reason tells the cap. A call workbench answers
busy is sent once more after ``BUSY_RETRY_S``, while its timeout still fits.
Every call is disclosed before it is sent, and every round recorded after it ends.

A round goes out without tools past ``MAX_TOOL_ROUNDS``, below ``TOOL_CUTOFF_S`` of the wall,
or once the tools were withdrawn, and it is the last: the model must answer. The turn also
ends on a round without calls, on a stop (after the call in flight; no round starts once the
stop is set) and on a model error. A round that fails once the stop is set, as a stalled
router's read timing out, ends the turn as stopped, not with the model error. No round is
assumed to end in any given time: the wall is read from the clock before each round, each
call and each retry. A call id an earlier call of the turn already has is made unique, so that
each call's images and records stay its own.

While the model sees (:class:`~ghostwriter.vision.Vision`), images go with the rounds
(:mod:`ghostwriter.agent.images`): ``view_file`` is offered, and the turn's calls ask workbench for
images. The question carries its attached pictures, and a follow-up those of earlier questions that
fit; their renditions are fetched before the round that first sends them, once per message, and one
not found is a note. A round's tool images follow its tool messages in one user message, the oldest
retired beyond ``GHOSTWRITER_MAX_IMAGES``; one the request had no room for stays named as never shown.
Ghostwriter sets the ``strips`` of ``view_file`` and ``read_document`` by the file a call reads. The
bytes live in the message's bank, in memory, until its answer ends, and a data URL exists only in the
list a round is streamed with. A round the router refuses with images before any text
(:data:`REFUSED_IMAGE_STATUSES`) is sent once more without them, after a :class:`NoticeEvent`, and
the rest of the turn goes without images. Each round is recorded with how many images it sent.

The log holds round numbers, counts and exception types; never a message, an argument, a result,
a label or an image.
"""

import dataclasses
import re
import threading
import time
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, Protocol, TypeAlias
from uuid import uuid4

import openai
from loguru import logger

from ghostwriter.agent.context import fit
from ghostwriter.agent.images import (
    UNSEEN,
    ImageBank,
    ImageBudget,
    attachment_parts,
    is_ref,
    materialise,
    newest_tool_images,
    note,
    parts_of,
    refs,
    retire_tool_images,
    strips_for_path,
    tool_image_message,
    without_images,
)
from ghostwriter.agent.limits import (
    BUSY_RETRY_S,
    MAX_ANSWER_TOKENS,
    MAX_CALLS_PER_ROUND,
    MAX_RESULTS_PER_MESSAGE,
    MAX_TOOL_ROUNDS,
    MESSAGE_WALL_S,
    REASONING_ANSWER_TOKENS,
    TOOL_CUTOFF_S,
    prompt_tokens,
)
from ghostwriter.agent.prompt import system_prompt
from ghostwriter.agent.stream import RoundDone, TextDelta, ToolCall, stream_round
from ghostwriter.agent.tools import (
    BUSY_TOKENS,
    CUT_ARGUMENTS,
    HIDDEN_ARGUMENTS,
    REFUSALS,
    STRIPPED_TOOLS,
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
from ghostwriter.store import ChatStore, UserTurn
from ghostwriter.utils.audit_event import AuditWriteError
from ghostwriter.vision import VIEW_TOOL, Vision
from ghostwriter.workbench import (
    CallFor,
    OutputFile,
    RenditionRequest,
    ToolFailure,
    ToolImage,
    ToolResult,
    WorkbenchClient,
    chat_folder,
)

#: A call id the store and the audit take; a call with another is refused, and answered under one the loop mints.
CALL_ID: Final = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
#: What the user is told when a model round failed.
MODEL_ERROR = "The model did not answer."
#: What the user is told when anything else broke the answer.
INTERNAL_ERROR = "The answer could not be finished."
#: The router's statuses for a request it may have refused for its images; such a round is sent once more without.
REFUSED_IMAGE_STATUSES: Final = frozenset({400, 413, 422})


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
class NoticeEvent:
    """Something the user is told while the answer goes on.

    Attributes:
        code: ``images_dropped``: the model refused the round's images, and the round went again without them.
    """

    code: Literal["images_dropped"]


@dataclass(frozen=True)
class ErrorEvent:
    """Why the answer ended early.

    Attributes:
        code: ``model``, ``audit`` or ``internal``.
        message: What the user is told.
    """

    code: Literal["model", "audit", "internal"]
    message: str


TurnEvent: TypeAlias = TokenEvent | StepEvent | ResultEvent | NoticeEvent | ErrorEvent


class TurnAudit(Protocol):
    """Where a turn records what it discloses and each model round."""

    def disclosed(self, *, tool: str, round: int, call_id: str, attempt: int) -> None:
        """Record a call about to be sent to workbench.

        Args:
            tool: The tool.
            round: The model round that asked for it.
            call_id: The call's id in the turn: the model's, unless an earlier call of the turn had it.
            attempt: 1, or 2 for the retry after a busy answer.

        Raises:
            AuditWriteError: When the record could not be written; the call is not sent.
        """
        ...

    def model_called(
        self, *, round: int, done: RoundDone | None, outcome: Literal["ok", "error"], images: int = 0
    ) -> None:
        """Record a model round once it ended; a round refused with images and sent again is recorded twice.

        Args:
            round: The round.
            done: How it ended; ``None`` when it failed.
            outcome: ``ok``, or ``error`` for a failed round.
            images: How many images it sent.

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


@dataclass
class _Sight:
    """What one message's answer does with images.

    Attributes:
        sees: Whether the model sees images: ``view_file`` is offered, the calls ask workbench for
            images, and Ghostwriter sets their ``strips``.
        budget: What an image costs a round and how many a request carries; ``None`` while the model
            does not see.
        known: The sizes of the pictures the chat attached that are ready to be seen, by path.
        bank: The bytes of the message's images, by ref key; it goes when the answer ends.
        tried: The keys of the attached pictures whose renditions were asked for, found or not.
        dropped: Whether the model refused images: the rest of the turn goes without them.
    """

    sees: bool
    budget: ImageBudget | None
    known: dict[str, tuple[int, int]]
    bank: ImageBank = field(default_factory=dict)
    tried: set[str] = field(default_factory=set)
    dropped: bool = False

    @property
    def showing(self) -> bool:
        """Whether images go to the model now: it sees, and has not refused them."""
        return self.sees and not self.dropped

    def arguments(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return a call's arguments with those Ghostwriter sets.

        Whatever the model sent for :data:`~ghostwriter.agent.tools.HIDDEN_ARGUMENTS` is dropped.
        While the model sees, a ``view_file`` or ``read_document`` call of a path gets the strips
        :func:`~ghostwriter.agent.images.strips_for_path` names for its file, if any.

        Args:
            tool: The tool.
            arguments: The arguments, as the model wrote them; never changed.

        Returns:
            The arguments to send.
        """
        kept = {key: value for key, value in arguments.items() if key not in HIDDEN_ARGUMENTS}
        path = arguments.get("path")
        if self.sees and tool in STRIPPED_TOOLS and isinstance(path, str):
            fixed = None if self.budget is None else self.budget.fixed
            strips = strips_for_path(path, fixed=fixed, known=self.known)
            if strips is not None:
                kept["strips"] = strips
        return kept


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


def _refuses_images(exc: Exception) -> bool:
    """Tell whether a round's failure may be the router refusing its images.

    Args:
        exc: What the round raised.

    Returns:
        Whether the router answered with a status of :data:`REFUSED_IMAGE_STATUSES`.
    """
    return isinstance(exc, openai.APIStatusError) and exc.status_code in REFUSED_IMAGE_STATUSES


def _unique(call: ToolCall, used: set[str], round_no: int) -> ToolCall:
    """Return a call under an id no earlier call of the turn has.

    A model may name calls of two rounds alike (``call_0`` each time), and a tool's images are kept
    by their call's id. A reused id gets the round after it, or a minted id when that one is taken
    too or too long. An id the record cannot hold is left as it is, for :meth:`AgentLoop._prepare`
    to refuse.

    Args:
        call: The call.
        used: The ids of the turn's calls so far.
        round_no: Its round.

    Returns:
        The call, its id unique in the turn.
    """
    if not CALL_ID.fullmatch(call.id) or call.id not in used:
        return call
    again = f"{call.id}-{round_no}"
    if again in used or not CALL_ID.fullmatch(again):
        again = f"call_{uuid4().hex}"
    return dataclasses.replace(call, id=again)


def _keep_unseen(current: list[dict[str, Any]], fitted: Sequence[Mapping[str, Any]], bank: ImageBank) -> None:
    """Keep as never shown, in place, the newest tool images a round had no room for.

    :func:`~ghostwriter.agent.context.fit` puts an :data:`~ghostwriter.agent.images.UNSEEN` note in the
    place of those it leaves out of the request, and of no other tool images. In ``current`` they are
    refs still, which a later round, for which they are older images, would retire as "not shown
    again". So each becomes its note in ``current`` and leaves the bank.

    Args:
        current: The current turn's messages.
        fitted: The messages the round sends, as ``fit`` returned them.
        bank: The bytes of the message's images.
    """
    newest = newest_tool_images(current)
    if newest is None:
        return
    went = {ref["key"] for ref in refs(fitted)}
    content = parts_of(newest)
    for index, part in enumerate(content):
        if is_ref(part) and part["key"] not in went:
            content[index] = note(part, UNSEEN)
            bank.pop(part["key"], None)


def _drop_images(current: list[dict[str, Any]], sight: _Sight) -> None:
    """Go without images for the rest of the turn, the model having refused them.

    Every image of the current turn becomes its :data:`~ghostwriter.agent.images.LEFT_OUT` note in place,
    and the bank is emptied.

    Args:
        current: The current turn's messages.
        sight: The turn's images.
    """
    sight.dropped = True
    current[:] = without_images(current)
    sight.bank.clear()


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
        vision: Vision | None = None,
    ) -> None:
        """Hold the loop's neighbours.

        Args:
            model: The chat model.
            workbench: workbench; ``None`` when it is not configured.
            store: The chats.
            settings: The tool call timeout, the file period and what images cost.
            clock: The monotonic clock the wall is read from.
            now: The time of day, for the system prompt's date.
            vision: Whether the model sees, read at the start of each message; ``None`` sends no image.
        """
        self._model = model
        self._workbench = workbench
        self._store = store
        self._settings = settings
        self._clock = clock
        self._now = now
        self._vision = vision

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
        settings = self._settings
        key = settings.workbench_api_key
        workbench = self._workbench
        sees = self._vision is not None and self._vision.model_sees
        discovery = workbench.discover(call=turn.call) if workbench is not None else None
        found = [tool for tool in discovery.tools if sees or tool.name != VIEW_TOOL] if discovery is not None else []
        offered: list[dict[str, Any]] = function_tools(found)
        names: list[str] = [tool.name for tool in found]
        folder = chat_folder(key, turn.chat_id) if discovery is not None and key else None
        question = self._store.user_turn(turn.chat_id, turn.user_message_id, seen=sees)
        past, expired = self._store.past_turns(turn.chat_id, before_position=question.position, seen=sees)
        system = system_prompt(
            today=self._now().date(),
            folder=folder,
            file_days=settings.file_days,
            expired=expired,
            presentations="write_presentation" in names,
            vision=sees and VIEW_TOOL in names,
        )
        attached = [*question.images, *(image for earlier in past for image in earlier.images)]
        sight = _Sight(
            sees=sees,
            budget=ImageBudget(settings.image_tokens, settings.max_images) if sees else None,
            known={image.path: (image.width, image.height) for image in attached},
        )
        budget = prompt_tokens(settings.context_tokens, reasoning=turn.reasoning)
        cap = REASONING_ANSWER_TOKENS if turn.reasoning else MAX_ANSWER_TOKENS
        headers = inference_headers(turn.owner, turn.request_id)
        current: list[dict[str, Any]] = [_asked(question, sight)]
        used: set[str] = set()
        round_no = 0
        while not turn.cancel.is_set():
            round_no += 1
            tools = (
                offered
                if round_no <= MAX_TOOL_ROUNDS and not message.withdrawn and self._left(message) >= TOOL_CUTOFF_S
                else []
            )
            fitted = fit(system, tools, past, current, budget=budget, images=sight.budget)
            done = yield from self._round(
                turn, audit, message, sight, current, fitted, tools=tools, round_no=round_no, headers=headers
            )
            if done is None or done.stopped or not done.calls or not tools or workbench is None:
                return
            # vLLM ends a round in which a call's name streamed with "tool_calls", at the cap too: the usage tells.
            at_cap = done.finish_reason == "length" or (done.output_tokens is not None and done.output_tokens >= cap)
            calls: list[dict[str, Any]] = []
            answers: list[dict[str, Any]] = []
            shown: list[tuple[str, str, tuple[ToolImage, ...]]] = []
            for index, asked in enumerate(done.calls):
                if turn.cancel.is_set():
                    return
                call = _unique(asked, used, round_no)
                cut = at_cap and index == len(done.calls) - 1
                outcome = yield from self._call(
                    turn, audit, message, workbench, call, index, round_no, names, cut, sight
                )
                call_id = call.id if CALL_ID.fullmatch(call.id) else f"call_{uuid4().hex}"
                used.add(call_id)
                calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": echoed_arguments(call.arguments)},
                    }
                )
                content = tool_message(outcome, images_seen=sight.showing)
                answers.append({"role": "tool", "tool_call_id": call_id, "content": content})
                if isinstance(outcome, ToolResult) and outcome.images:
                    shown.append((call.name, call_id, outcome.images))
            current.append({"role": "assistant", "content": done.text, "tool_calls": calls})
            current.extend(answers)
            self._tool_images(current, sight, shown)

    def _round(
        self,
        turn: Turn,
        audit: TurnAudit,
        message: _Progress,
        sight: _Sight,
        current: list[dict[str, Any]],
        fitted: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        round_no: int,
        headers: Mapping[str, str],
    ) -> Generator[TurnEvent, None, RoundDone | None]:
        """Stream one round with its images and record it; a round refused with images goes once more without.

        The renditions of the attached pictures the round is the first to send are fetched first, and
        the newest tool images it has no room for are kept as never shown. A failure before any text,
        with a status of :data:`REFUSED_IMAGE_STATUSES`, of a round that carried images is recorded as a
        failed round and told with a :class:`NoticeEvent`; the round goes again without images, as the
        rest of the turn does, and a second failure is the model error.

        Args:
            turn: The message.
            audit: The turn's audit.
            message: The answer so far.
            sight: The turn's images.
            current: The current turn's messages.
            fitted: The round's messages, as :func:`~ghostwriter.agent.context.fit` returned them.
            tools: The tool schemas the round offers.
            round_no: The round.
            headers: The inference headers.

        Yields:
            TurnEvent: The round's text, and a notice when it goes again without images.

        Returns:
            How the round ended; ``None`` when the turn ends with it, as it failed.
        """
        if sight.showing:
            self._fetch(turn, sight, fitted)
            _keep_unseen(current, fitted, sight.bank)
        while True:
            wire, count = materialise(without_images(fitted) if sight.dropped else fitted, sight.bank)
            said = False
            done: RoundDone | None = None
            try:
                for event in stream_round(
                    self._model,
                    wire,
                    tools=tools,
                    headers=headers,
                    cancel=turn.cancel,
                    reasoning=turn.reasoning,
                ):
                    if isinstance(event, TextDelta):
                        if not said and message.shown:
                            message.shown.append("\n\n")
                            yield TokenEvent("\n\n")
                        said = True
                        message.shown.append(event.text)
                        yield TokenEvent(event.text)
                    else:
                        done = event
            except Exception as exc:
                stopped = turn.cancel.is_set()
                logger.warning(
                    "Model round {} failed{}: {}", round_no, " after a stop" if stopped else "", type(exc).__name__
                )
                audit.model_called(round=round_no, done=None, outcome="error", images=count)
                if count and not said and not stopped and _refuses_images(exc):
                    logger.info("Model round {} goes again without its {} images", round_no, count)
                    _drop_images(current, sight)
                    yield NoticeEvent("images_dropped")
                    continue
                if not stopped:
                    yield ErrorEvent("model", MODEL_ERROR)
                return None
            if done is None:
                raise RuntimeError("a model round ended without its last event")
            audit.model_called(round=round_no, done=done, outcome="ok", images=count)
            return done

    def _fetch(self, turn: Turn, sight: _Sight, fitted: Sequence[Mapping[str, Any]]) -> None:
        """Fetch, in one exchange, the renditions of the attached pictures a round is the first to send.

        Each is asked for once per message: one not found stays out of the bank, and its ref goes as
        the :data:`~ghostwriter.agent.images.NOT_LOADED` note.

        Args:
            turn: The message.
            sight: The turn's images; the renditions found go into its bank.
            fitted: The round's messages.
        """
        wanted = {
            ref["key"]: ref
            for ref in refs(fitted)
            if ref.get("origin") == "attachment" and ref["key"] not in sight.bank and ref["key"] not in sight.tried
        }
        if not wanted:
            return
        sight.tried.update(wanted)
        if self._workbench is None:
            return
        asked = list(wanted.values())
        requests = [
            RenditionRequest(ref["path"], n=ref["n"], strips=ref["strips"], strip=ref["strip"]) for ref in asked
        ]
        for ref, picture in zip(asked, self._workbench.fetch_renditions(requests, call=turn.call), strict=True):
            if picture is not None:
                sight.bank[ref["key"]] = picture

    def _tool_images(
        self, current: list[dict[str, Any]], sight: _Sight, shown: Sequence[tuple[str, str, Sequence[ToolImage]]]
    ) -> None:
        """Put a round's tool images after its tool messages, and retire the turn's oldest past the cap.

        While images go to the model, the images the round's calls showed go into one user message
        (:func:`~ghostwriter.agent.images.tool_image_message`), their bytes into the bank, and then the
        turn's tool images beyond the newest ``GHOSTWRITER_MAX_IMAGES`` are retired. Otherwise nothing
        changes: a tool's images were named in its tool message as not shown.

        Args:
            current: The current turn's messages, its tool messages last.
            sight: The turn's images.
            shown: Per call that showed images, in the round's order: the tool, the call's id and the images.
        """
        if not sight.showing:
            return
        if shown:
            images, bank = tool_image_message(shown)
            current.append(images)
            sight.bank.update(bank)
        retire_tool_images(current, sight.bank, keep=self._settings.max_images)

    def _prepare(
        self, message: _Progress, call: ToolCall, index: int, names: list[str], cut: bool, sight: _Sight
    ) -> dict[str, Any] | Refusal:
        """Prepare a call, or refuse it before it is sent.

        Args:
            message: The answer so far; a call without its timeout left withdraws the tools.
            call: The call.
            index: Its place in the round.
            names: The tools offered.
            cut: Whether the round ended at its cap with this call the last, so its arguments may be half
                written, or not begun when it was cut right after its name.
            sight: The turn's images, which set the arguments Ghostwriter sets.

        Returns:
            The arguments to send, those Ghostwriter sets included, or why the call is not sent.
        """
        if index >= MAX_CALLS_PER_ROUND:
            return Refusal("call_limit", REFUSALS["call_limit"])
        if call.name not in names:
            text = REFUSALS["unknown_tool"].format(name=call.name, names=", ".join(names) or "none")
            return Refusal("unknown_tool", text)
        arguments = parse_arguments(call.arguments)
        if cut and (arguments is None or not call.arguments.strip()):
            return Refusal("cut_arguments", CUT_ARGUMENTS.format(tool=call.name))
        if arguments is None or not CALL_ID.fullmatch(call.id):
            return Refusal("bad_arguments", REFUSALS["bad_arguments"].format(name=call.name))
        if message.withdrawn or self._left(message) < self._settings.workbench_timeout_s:
            message.withdrawn = True
            return Refusal("no_time", REFUSALS["no_time"])
        return sight.arguments(call.name, arguments)

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
        cut: bool,
        sight: _Sight,
    ) -> Generator[TurnEvent, None, ToolResult | ToolFailure | Refusal]:
        """Run one call of a round, or refuse it.

        The call asks workbench for images while the model sees, and its step keeps the text the model reads.

        Args:
            turn: The message.
            audit: The turn's audit.
            message: The answer so far.
            workbench: workbench.
            call: The call, under its id in the turn.
            index: Its place in the round.
            round_no: The round.
            names: The tools offered.
            cut: Whether the length limit cut this call off, as for :meth:`_prepare`.
            sight: The turn's images.

        Yields:
            TurnEvent: The step's ``running`` and final frames, then its new cards.

        Returns:
            What the model reads back about it.
        """
        arguments = self._prepare(message, call, index, names, cut, sight)
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
            outcome = workbench.call_tool(call.name, arguments, call=turn.call, timeout_s=timeout, images=sight.sees)
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
            result=tool_message(outcome, images_seen=sight.showing),
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


def _asked(question: UserTurn, sight: _Sight) -> dict[str, Any]:
    """Return the user's message that opens the turn.

    Args:
        question: The question, as the store reads it.
        sight: The turn's images.

    Returns:
        While the model sees and the question attached pictures, its text part and their refs
        (:func:`~ghostwriter.agent.images.attachment_parts`); otherwise its text.
    """
    if not sight.sees or not question.images:
        return {"role": "user", "content": question.text}
    fixed = None if sight.budget is None else sight.budget.fixed
    parts = attachment_parts(question.images, fixed=fixed)
    return {"role": "user", "content": [{"type": "text", "text": question.text}, *parts]}
