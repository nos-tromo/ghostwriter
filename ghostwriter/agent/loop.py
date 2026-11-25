"""One message's answer: model rounds, and workbench's tools between them, within the message's caps.

The loop discovers workbench's tools, then streams rounds. Each round's messages are
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

The user is told, as a notice, what limited the answer (:class:`~ghostwriter.agent.notices.NoticeEvent`),
and the answer's progress keeps each notice told. The message's first round fills a
:class:`~ghostwriter.agent.context.FitReport`, filled again when the round goes once more without images:
when the request the router took left earlier turns out, ``context_trimmed`` is told with that request's
first event, before any text, once per message; a request the router refused tells nothing. The last round,
one without calls and not stopped, that ends at its cap (:func:`_at_cap`, the reasoning cap for a reasoning
message) is followed by ``answer_cut``. Each call goes into the answer's :class:`~ghostwriter.agent.notices.Reading`
with what the model reads back of it; once the rounds have ended, a stop or a model error included, the loop tells
``document_partial`` for each attachment of the chat a ``read_document`` read only in part, then ``document_unread``
for each attachment of the question no call opened, none of either after a ``run_python`` or ``search_files``
call. An error that ended the rounds comes after them, last.

A round goes out without tools past ``MAX_TOOL_ROUNDS``, below ``TOOL_CUTOFF_S`` of the wall,
or once the tools were withdrawn, and it is the last: the model must answer. The turn also
ends on a round without calls, on a stop (after the call in flight; no round starts once the
stop is set) and on a model error. A round that fails once the stop is set, as a stalled
router's read timing out, ends the turn as stopped, not with the model error. No round is
assumed to end in any given time: the wall is read from the clock before each round, each
call and each retry. A call id an earlier call of the turn already has is made unique, so that
each call's images and records stay its own.

A turn sees when the model sees (:class:`~ghostwriter.vision.Vision`) and workbench's listing for a
call that takes images holds ``view_file``. Then images go with the rounds
(:mod:`ghostwriter.agent.images`): the rounds are offered that listing, ``view_file`` included, the system
prompt says how to look (and, while Nextext is up and workbench is 0.9.0 or later, how to take a clip's
transcript and frames), and the turn's calls ask workbench for images. A turn that does not see is
offered the listing of a call without images, as workbench 0.6 gave it, which promises no image; so is
every round after a refusal. The question carries its attached pictures, and a
follow-up those of earlier questions that fit; their renditions are fetched before the round that
first sends them, once per message, and one not found is a note. A round's tool images follow its
tool messages in one user message, the oldest retired beyond ``GHOSTWRITER_MAX_IMAGES``; one the
request had no room for stays named as never shown, and the tool message says how many were
returned, never that they were shown. Ghostwriter sets the ``strips`` of ``view_file`` and
``read_document`` by the file a call reads. The bytes live in the message's bank, in memory, until
its answer ends, and a data URL exists only in the list a round is streamed with. A round the
router refuses with images before any text
(:data:`REFUSED_IMAGE_STATUSES`) is sent once more without them, after a :class:`NoticeEvent`, and
the rest of the turn goes as for a model that does not see: no ``view_file``, no word on how to look,
no images asked for, no ``strips``, and a tool's images named as not shown; no note, those the turn
wrote before included, sends the model to ``view_file`` any more. Each round is recorded with how many
images it sent.

A round the router refuses before any text for its length (:func:`_refuses_context`: a status of
:data:`REFUSED_CONTEXT_STATUSES` and one of :data:`CONTEXT_REFUSALS` in its message or body) goes the same
way while it carried images. A round without images, or sent again without them and refused again, ends the
turn with the ``context`` error (:data:`CONTEXT_ERROR`), not the model error, and the log names
``GHOSTWRITER_CONTEXT_TOKENS``, which may be above the model's context.

When the answer ends, however it ends, the loop estimates the tokens the next message's prompt starts with
(:func:`~ghostwriter.agent.context.next_prompt_tokens`: the offer's system prompt and tools, and every turn of the
chat, this one as it will be replayed) and stores it with the answer; the caller's :class:`TurnOutcome` carries it
too. A turn stopped before it began has none, and nor has an answer whose estimate failed.

The log holds round numbers, counts and exception types; never a message, an argument, a result,
a label, an image or the router's refusal text.
"""

import dataclasses
import re
import threading
import time
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final, Literal, Protocol, TypeAlias
from uuid import uuid4

import openai
from loguru import logger

from ghostwriter.agent.context import FitReport, PastTurn, finished_turn, fit, next_prompt_tokens
from ghostwriter.agent.images import (
    UNSEEN,
    ImageBank,
    ImageBudget,
    attachment_parts,
    forget_view_file,
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
from ghostwriter.agent.notices import NoticeEvent, Reading
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
from ghostwriter.nextext import NextextHealth, serves_media
from ghostwriter.schemas import ResultOut, StepOut
from ghostwriter.store import ChatStore, UserTurn
from ghostwriter.utils.audit_event import AuditWriteError
from ghostwriter.vision import VIEW_TOOL, Vision
from ghostwriter.workbench import (
    CallFor,
    Discovery,
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
#: What the user is told when the router refused a round for its length.
CONTEXT_ERROR: Final = "This chat is too long for the model."
#: What the user is told when anything else broke the answer.
INTERNAL_ERROR = "The answer could not be finished."
#: The router's statuses for a request it may have refused for its images; such a round is sent once more without.
REFUSED_IMAGE_STATUSES: Final = frozenset({400, 413, 422})
#: The router's statuses for a request it refused for its length.
REFUSED_CONTEXT_STATUSES: Final = frozenset({400, 413})
#: What the router says of a prompt longer than the model's context, lower case: one of these in a refusal's
#: message or body makes it a refusal for length.
CONTEXT_REFUSALS: Final = (
    "context length",
    "context window",
    "maximum context",
    "contextwindowexceeded",
    "too many tokens",
    "prompt is too long",
    "input is too long",
)


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
        code: ``model``, ``audit``, ``internal``, or ``context``: the router refused a round for its length.
        message: What the user is told.
    """

    code: Literal["model", "audit", "internal", "context"]
    message: str


TurnEvent: TypeAlias = TokenEvent | StepEvent | ResultEvent | NoticeEvent | ErrorEvent


@dataclass
class TurnOutcome:
    """What a turn leaves for its caller once it has ended, filled by :meth:`AgentLoop.run`.

    Attributes:
        context_estimate: The estimated tokens of the prompt after this answer, as stored with it; ``None`` when
            the turn ended before it began or the estimate failed.
    """

    context_estimate: int | None = None


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
        notices: Every notice the user was told, in order.
        next_prompt: Estimates the tokens of the next message's prompt from the answer's text, as the turn stands
            when it is called (the offer in force, the turn's messages); set once the turn's messages exist, and
            ``None`` until then.
        reading: What the message's calls opened and read, which the notices about files are told from.
    """

    started: float
    shown: list[str] = field(default_factory=list)
    steps: int = 0
    cards: set[str] = field(default_factory=set)
    withdrawn: bool = False
    notices: list[NoticeEvent] = field(default_factory=list)
    next_prompt: Callable[[str], int] | None = None
    reading: Reading = field(default_factory=Reading)

    def told(self, event: NoticeEvent) -> NoticeEvent:
        """Keep a notice the user is told, for the answer.

        Args:
            event: The notice.

        Returns:
            The same notice, to be yielded.
        """
        self.notices.append(event)
        return event


@dataclass
class _Sight:
    """What one message's answer does with images.

    Attributes:
        sees: Whether the turn sees, as the message began: the model sees, and workbench's listing for
            images holds ``view_file``.
        budget: What an image costs a round and how many a request carries; ``None`` while the turn
            does not see.
        known: The sizes of the pictures the chat attached that are ready to be seen, by path.
        bank: The bytes of the message's images, by ref key; it goes when the answer ends.
        tried: The keys of the attached pictures whose renditions were asked for, found or not.
        dropped: Whether the model refused images: the rest of the turn goes as without sight.
    """

    sees: bool
    budget: ImageBudget | None
    known: dict[str, tuple[int, int]]
    bank: ImageBank = field(default_factory=dict)
    tried: set[str] = field(default_factory=set)
    dropped: bool = False

    @property
    def showing(self) -> bool:
        """Whether images go to the model now: it sees, and has not refused them.

        Only then is ``view_file`` offered, do the calls ask workbench for images and carry ``strips``,
        and do a tool's images go to the model.
        """
        return self.sees and not self.dropped

    @property
    def fixed(self) -> int | None:
        """The tokens one image costs, or ``None`` to count it by its pixels."""
        return None if self.budget is None else self.budget.fixed

    def arguments(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return a call's arguments with those Ghostwriter sets.

        Whatever the model sent for :data:`~ghostwriter.agent.tools.HIDDEN_ARGUMENTS` is dropped.
        While images go to the model, a ``view_file`` or ``read_document`` call of a path gets the
        strips :func:`~ghostwriter.agent.images.strips_for_path` names for its file, if any.

        Args:
            tool: The tool.
            arguments: The arguments, as the model wrote them; never changed.

        Returns:
            The arguments to send.
        """
        kept = {key: value for key, value in arguments.items() if key not in HIDDEN_ARGUMENTS}
        path = arguments.get("path")
        if self.showing and tool in STRIPPED_TOOLS and isinstance(path, str):
            strips = strips_for_path(path, fixed=self.fixed, known=self.known)
            if strips is not None:
                kept["strips"] = strips
        return kept


@dataclass(frozen=True)
class _Offer:
    """A round's system prompt and the tools it may offer.

    Attributes:
        system: The system prompt.
        tools: The tools, as OpenAI functions.
        names: Their names.
    """

    system: str
    tools: list[dict[str, Any]]
    names: list[str]


@dataclass
class _Request:
    """What each round's request is fitted from, besides the current turn.

    Attributes:
        seeing: The offer while images go to the model: workbench's listing for images, ``view_file``
            among the tools, and the system prompt saying how to look; ``None`` for a turn that does not see.
        blind: Makes the offer without either, from workbench's listing for a call without images, for a
            turn that does not see, or once the model refused images; asked at most once, when a round
            first needs it.
        past: The earlier turns, oldest first.
        budget: The tokens a round's prompt may take.
    """

    seeing: _Offer | None
    blind: Callable[[], _Offer]
    past: Sequence[PastTurn]
    budget: int
    _blind: _Offer | None = field(default=None, init=False, repr=False)

    def offer(self, sight: _Sight) -> _Offer:
        """Return the offer of a round.

        Args:
            sight: The turn's images.

        Returns:
            :attr:`seeing` while images go to the model, else the blind offer.
        """
        if sight.showing and self.seeing is not None:
            return self.seeing
        if self._blind is None:
            self._blind = self.blind()
        return self._blind

    def fitted(
        self, current: list[dict[str, Any]], sight: _Sight, *, tooled: bool, report: FitReport | None = None
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return a round's messages, fitted to the budget, and the tools it offers.

        Args:
            current: The current turn's messages.
            sight: The turn's images: the offer, and what an image costs.
            tooled: Whether the round may offer tools.
            report: Filled with the earlier turns there were and those replayed; ``None`` for none.

        Returns:
            The messages (:func:`~ghostwriter.agent.context.fit`), their refs still refs; and the tools.
        """
        offer = self.offer(sight)
        tools = offer.tools if tooled else []
        fitted = fit(
            offer.system,
            tools,
            self.past,
            current,
            budget=self.budget,
            images=sight.budget,
            view=sight.showing,
            report=report,
        )
        return fitted, tools


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


def _refuses_context(exc: Exception) -> bool:
    """Tell whether a round's failure is the router refusing its prompt as longer than the model's context.

    Args:
        exc: What the round raised.

    Returns:
        Whether the router answered with a status of :data:`REFUSED_CONTEXT_STATUSES` and one of
        :data:`CONTEXT_REFUSALS` is in its message or body, in any case.
    """
    if not isinstance(exc, openai.APIStatusError) or exc.status_code not in REFUSED_CONTEXT_STATUSES:
        return False
    told = f"{exc.message} {exc.body}".lower()
    return any(pattern in told for pattern in CONTEXT_REFUSALS)


def _at_cap(done: RoundDone, cap: int) -> bool:
    """Tell whether a round ended at its token cap.

    vLLM ends a round in which a call's name streamed with ``tool_calls``, at the cap too: the usage tells.
    Without a usage count, only the finish reason does.

    Args:
        done: How the round ended.
        cap: Its ``max_tokens``.

    Returns:
        Whether its finish reason is ``length``, or its output tokens reached ``cap``.
    """
    return done.finish_reason == "length" or (done.output_tokens is not None and done.output_tokens >= cap)


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

    A picture of the question that ``fit`` leaves out (:data:`~ghostwriter.agent.images.NO_ROOM`) stays a
    ref: only tool images ever retire, so no later round calls it shown before, and one with room sends it.

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
    the notes earlier rounds wrote (a retired tool image) no longer name ``view_file``, which the rest of
    the turn does not offer, and the bank is emptied. The question's own text is left as the user wrote it.

    Args:
        current: The current turn's messages.
        sight: The turn's images.
    """
    sight.dropped = True
    unseen = without_images(current)
    current[:] = [*unseen[:1], *forget_view_file(unseen[1:])]
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
        media: NextextHealth | None = None,
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
            media: Whether Nextext is up (its ``healthy``), which with the version of the discovery the turn holds
                says whether clips are offered, read at the start of each message; ``None`` while Nextext is off.
        """
        self._model = model
        self._workbench = workbench
        self._store = store
        self._settings = settings
        self._clock = clock
        self._now = now
        self._vision = vision
        self._media = media

    def run(self, turn: Turn, audit: TurnAudit, *, outcome: TurnOutcome | None = None) -> Iterator[TurnEvent]:
        """Answer a message.

        The answer's text, its notices and the estimate of the next prompt are stored when the run ends, however
        it ends.

        Args:
            turn: The message.
            audit: Where disclosures and model rounds are recorded.
            outcome: Filled with the estimate when the run ends; ``None`` keeps it for the store alone.

        Yields:
            TurnEvent: Text, steps, cards and notices as they come, each notice also kept in the answer's
            progress; ``answer_cut`` after the last round's text when it ended at its cap; then
            ``document_partial`` and ``document_unread``; an :class:`ErrorEvent` last when the answer ended early.

        Raises:
            AuditWriteError: When a record could not be written; raised after the answer is stored.
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
            estimate = _estimated(message)
            if outcome is not None:
                outcome.context_estimate = estimate
            self._store_answer(turn, message, estimate)

    def _store_answer(self, turn: Turn, message: _Progress, context_estimate: int | None) -> None:
        """Store the answer's text, its notices and its estimate; a failure is logged, never raised over the turn.

        Args:
            turn: The message.
            message: The answer: everything shown, and every notice told, stored without its name.
            context_estimate: The estimated tokens of the next prompt, or ``None``.
        """
        try:
            self._store.finish_assistant_message(
                turn.assistant_message_id,
                "".join(message.shown),
                notices=[notice.stored() for notice in message.notices],
                context_estimate=context_estimate,
            )
        except Exception as exc:
            logger.error("An answer could not be stored: {}", type(exc).__name__)

    def _left(self, message: _Progress) -> float:
        """Return the seconds left of the message's wall.

        Args:
            message: The answer so far.

        Returns:
            The seconds; negative once the wall is past.
        """
        return MESSAGE_WALL_S - (self._clock() - message.started)

    def _answer(self, turn: Turn, audit: TurnAudit, message: _Progress) -> Iterator[TurnEvent]:
        """Set the turn up, run its rounds, then tell what the message read of the files.

        Once the rounds have ended without raising (also on a stop or a model error), each ``document_partial`` of
        the chat's attachments, then each ``document_unread`` of the question's, is told; an error that ended the
        rounds comes after them. A turn stopped before it began tells nothing.

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
        workbench = self._workbench
        model_sees = self._vision is not None and self._vision.model_sees
        full = workbench.discover(call=turn.call, images=True) if workbench is not None and model_sees else None
        sees = full is not None and any(tool.name == VIEW_TOOL for tool in full.tools)
        question = self._store.user_turn(turn.chat_id, turn.user_message_id, seen=sees)
        past, expired = self._store.past_turns(turn.chat_id, before_position=question.position, seen=sees)
        today = self._now().date()

        def blind() -> _Offer:
            # A workbench that failed the discovery for images offers no tool this turn, as any failed one.
            failed = model_sees and full is None
            plain = workbench.discover(call=turn.call, images=False) if workbench is not None and not failed else None
            return self._offer(plain, seeing=False, chat_id=turn.chat_id, expired=expired, today=today)

        # Clips are offered as NextextHealth.offered says (Nextext healthy, workbench 0.9.0 or later), but from the
        # discovery this turn already holds: no second listing is read on the turn thread.
        clips = (
            sees
            and full is not None
            and self._media is not None
            and self._media.healthy
            and serves_media(full.server_version)
        )
        seeing = (
            self._offer(full, seeing=True, chat_id=turn.chat_id, expired=expired, today=today, media=clips)
            if sees
            else None
        )
        attached = [*question.images, *(image for earlier in past for image in earlier.images)]
        sight = _Sight(
            sees=sees,
            budget=ImageBudget(settings.image_tokens, settings.max_images) if sees else None,
            known={image.path: (image.width, image.height) for image in attached},
        )
        request = _Request(
            seeing=seeing,
            blind=blind,
            past=past,
            budget=prompt_tokens(settings.context_tokens, reasoning=turn.reasoning),
        )
        cap = REASONING_ANSWER_TOKENS if turn.reasoning else MAX_ANSWER_TOKENS
        headers = inference_headers(turn.owner, turn.request_id)
        current: list[dict[str, Any]] = [_asked(question, sight)]

        def next_prompt(text: str) -> int:
            # As the turn stands when the answer ends: the offer of the last round (the blind one after a refusal),
            # and the turn's messages as they were left; read then, never now.
            offer = request.offer(sight)
            answered = finished_turn(question.text, question.images, current, text)
            return next_prompt_tokens(offer.system, offer.tools, [*past, answered], images=sight.budget)

        message.next_prompt = next_prompt
        # The rounds' error, if one ended them, is held back: the file notices come first, and the error is last.
        error: ErrorEvent | None = None
        with closing(self._rounds(turn, audit, message, sight, request, current, cap=cap, headers=headers)) as rounds:
            for event in rounds:
                if isinstance(event, ErrorEvent):
                    error = event
                else:
                    yield event
        reading = message.reading
        told = [
            *reading.partial(self._store.chat_files(turn.chat_id)),
            *reading.unread(question.files, sees=sight.sees),
        ]
        for notice in told:
            yield message.told(notice)
        if error is not None:
            yield error

    def _rounds(
        self,
        turn: Turn,
        audit: TurnAudit,
        message: _Progress,
        sight: _Sight,
        request: _Request,
        current: list[dict[str, Any]],
        *,
        cap: int,
        headers: Mapping[str, str],
    ) -> Generator[TurnEvent]:
        """Run the rounds, and the calls between them, until the turn ends.

        Each call is recorded in the answer's reading (:class:`~ghostwriter.agent.notices.Reading`) with what the
        model reads back of it.

        Args:
            turn: The message.
            audit: The turn's audit.
            message: The answer so far.
            sight: The turn's images.
            request: What each round's request is fitted from.
            current: The current turn's messages, which the rounds extend.
            cap: The ``max_tokens`` of a round, which tells an answer cut.
            headers: The inference headers.

        Yields:
            TurnEvent: As :meth:`run`, the notices about files left out; an :class:`ErrorEvent` ends them.
        """
        workbench = self._workbench
        used: set[str] = set()
        round_no = 0
        while not turn.cancel.is_set():
            round_no += 1
            tooled = round_no <= MAX_TOOL_ROUNDS and not message.withdrawn and self._left(message) >= TOOL_CUTOFF_S
            done, tools = yield from self._round(
                turn, audit, message, sight, request, current, tooled=tooled, round_no=round_no, headers=headers
            )
            # The last round, one without calls, ending at the cap left the answer cut short.
            if done is not None and not done.stopped and not done.calls and _at_cap(done, cap):
                yield message.told(NoticeEvent("answer_cut"))
            if done is None or done.stopped or not done.calls or not tools or workbench is None:
                return
            names = request.offer(sight).names
            at_cap = _at_cap(done, cap)
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
                message.reading.call(call.name, parse_arguments(call.arguments), outcome, content)
                answers.append({"role": "tool", "tool_call_id": call_id, "content": content})
                if isinstance(outcome, ToolResult) and outcome.images:
                    shown.append((call.name, call_id, outcome.images))
            current.append({"role": "assistant", "content": done.text, "tool_calls": calls})
            current.extend(answers)
            self._tool_images(current, sight, shown)

    def _offer(
        self,
        discovery: Discovery | None,
        *,
        seeing: bool,
        chat_id: str,
        expired: bool,
        today: date,
        media: bool = False,
    ) -> _Offer:
        """Return what a round offers, with sight or without it.

        Args:
            discovery: The listing discovery found: for images while ``seeing``, else for a call without
                images; ``None`` offers no tool.
            seeing: Whether images go to the model: only then is ``view_file`` offered and the system
                prompt says how to look.
            chat_id: The chat, whose folder the prompt names while a tool is offered.
            expired: Whether files or tool results of earlier messages have expired.
            today: The date the message is answered on.
            media: Whether clips are offered at this turn; the system prompt says how to read and look at one
                only while ``seeing`` and ``view_file`` is offered.

        Returns:
            The system prompt, and the tools with their names.
        """
        key = self._settings.workbench_api_key
        folder = chat_folder(key, chat_id) if discovery is not None and key else None
        discovered = discovery.tools if discovery is not None else ()
        found = [tool for tool in discovered if seeing or tool.name != VIEW_TOOL]
        names = [tool.name for tool in found]
        system = system_prompt(
            today=today,
            folder=folder,
            file_days=self._settings.file_days,
            expired=expired,
            presentations="write_presentation" in names,
            vision=seeing and VIEW_TOOL in names,
            media=media,
        )
        return _Offer(system, function_tools(found), names)

    def _round(
        self,
        turn: Turn,
        audit: TurnAudit,
        message: _Progress,
        sight: _Sight,
        request: _Request,
        current: list[dict[str, Any]],
        *,
        tooled: bool,
        round_no: int,
        headers: Mapping[str, str],
    ) -> Generator[TurnEvent, None, tuple[RoundDone | None, list[dict[str, Any]]]]:
        """Fit one round, stream it with its images and record it; a round refused with images goes once more.

        The message's first round is fitted with a :class:`~ghostwriter.agent.context.FitReport`, and so is
        its try without images, which fills the report again. ``context_trimmed`` is told with the first
        event of the try the router took, before any text, when that try's fit left earlier turns out: the
        trim told is the one of the request sent and taken, never of one refused. A later round's trim is
        not told: the chat was trimmed in the first, and results the model pages past are its own.
        The renditions of the attached pictures the round is the first to send are fetched first, and
        the newest tool images it has no room for are kept as never shown. A failure before any text,
        with a status of :data:`REFUSED_IMAGE_STATUSES` (a refusal for length among them,
        :func:`_refuses_context`), of a round that carried images is recorded as a failed round and told
        with a :class:`NoticeEvent`. The round is then fitted again without sight, as the rest of the turn
        is, and sent without images. A failure of a round without images is the ``context`` error when it
        is a refusal for length, which a second failure of a round sent again is too, and the model error
        otherwise; a failure after text, or after a stop, is never the ``context`` error.

        Args:
            turn: The message.
            audit: The turn's audit.
            message: The answer so far.
            sight: The turn's images.
            request: What the round's request is fitted from.
            current: The current turn's messages.
            tooled: Whether the round may offer tools.
            round_no: The round.
            headers: The inference headers.

        Yields:
            TurnEvent: ``images_dropped`` when it goes again without images; ``context_trimmed`` before the
            text of the try the router took, when the first round's fit left earlier turns out; the round's text.

        Returns:
            How the round ended, ``None`` when the turn ends with it as it failed; and the tools it offered.
        """
        report = FitReport() if round_no == 1 else None
        fitted, tools = request.fitted(current, sight, tooled=tooled, report=report)
        if sight.showing:
            self._fetch(turn, sight, fitted)
            _keep_unseen(current, fitted, sight.bank)
        while True:
            wire, count = materialise(
                without_images(fitted) if sight.dropped else fitted, sight.bank, view=sight.showing
            )
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
                    if report is not None:
                        # The router took this attempt: its fit is the one sent, and its trim is told, once.
                        if report.trimmed:
                            yield message.told(NoticeEvent("context_trimmed"))
                        report = None
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
                for_length = not said and not stopped and _refuses_context(exc)
                if count and not said and not stopped and (for_length or _refuses_images(exc)):
                    logger.info("Model round {} goes again without its {} images", round_no, count)
                    _drop_images(current, sight)
                    yield message.told(NoticeEvent("images_dropped"))
                    fitted, tools = request.fitted(current, sight, tooled=tooled, report=report)
                    continue
                if for_length:
                    logger.error(
                        "The router refused round {} for its length; GHOSTWRITER_CONTEXT_TOKENS may be above the "
                        "model's context.",
                        round_no,
                    )
                    yield ErrorEvent("context", CONTEXT_ERROR)
                elif not stopped:
                    yield ErrorEvent("model", MODEL_ERROR)
                return None, tools
            if done is None:
                raise RuntimeError("a model round ended without its last event")
            audit.model_called(round=round_no, done=done, outcome="ok", images=count)
            return done, tools

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

        The call asks workbench for images while they go to the model, and its step keeps the text the model reads.

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
            outcome = workbench.call_tool(call.name, arguments, call=turn.call, timeout_s=timeout, images=sight.showing)
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


def _estimated(message: _Progress) -> int | None:
    """Estimate the tokens of the next prompt from the answer as it ended.

    Args:
        message: The answer.

    Returns:
        The estimate; ``None`` when the turn never began, or when the estimate failed, which is logged by its type.
    """
    if message.next_prompt is None:
        return None
    try:
        return message.next_prompt("".join(message.shown))
    except Exception as exc:
        logger.warning("The next prompt could not be estimated: {}", type(exc).__name__)
        return None


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
    parts = attachment_parts(question.images, fixed=sight.fixed)
    return {"role": "user", "content": [{"type": "text", "text": question.text}, *parts]}
