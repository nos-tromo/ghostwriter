"""Messages: send one and stream its answer as Server-Sent Events, stop the answer, regenerate the last one.

A message runs one turn of :class:`~ghostwriter.agent.loop.AgentLoop`, at most one per chat
(:class:`~ghostwriter.turns.TurnRegistry`), on a thread of the app's turn limiter
(``TURN_THREADS``) rather than the shared pool; a turn beyond them waits for one. The frames
are ``start`` (the stored question's and answer's ids), always first, then ``token``,
``step``, ``result``, ``notice`` and ``error``, and ``final``, which is always last; a quiet
stream carries a keep-alive comment every 15 s, which is no frame. ``notice`` names by its
``code`` what the user is told while the answer goes on: ``images_dropped``, the model refused
a round's images and it went again without them. ``error`` names by its ``code`` why the answer
ended early: ``model``, ``audit``, ``internal``, or ``context``, the router refused a round as longer
than the model's context, with no image left to drop. A stop, or
the client going away, sets the turn's stop: the call in flight finishes and is stored, no
further round runs, the text so far is stored, and the chat takes a new message as soon as the
turn has ended. A turn that never got its thread ends at once on a stop, with ``final`` as its
only frame, and when its response ends.

A turn is recorded as one primary event, written before anything is stored (``collect`` /
``chat.message``, or ``modify`` / ``chat.regenerate``), with a child per model round
(``ai_call`` / ``chat.answer``, naming how many images it sent; a round sent again without its
images has two) and one per call sent to workbench (``disclose`` / ``chat.tool``): references
only. A record that cannot be written stops the answer with the ``audit`` error frame; a step
it left running is then ended by its own ``error`` frame.
"""

import threading
from collections.abc import Callable, Iterator
from typing import Annotated, Final, Literal, Self
from uuid import uuid4

from anyio import CapacityLimiter
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator
from starlette.types import Receive, Scope, Send
from typing_extensions import override

from ghostwriter.agent.loop import (
    INTERNAL_ERROR,
    AgentLoop,
    ErrorEvent,
    NoticeEvent,
    ResultEvent,
    StepEvent,
    TokenEvent,
    Turn,
    TurnAudit,
    TurnEvent,
)
from ghostwriter.agent.stream import RoundDone
from ghostwriter.api.chats import owned_chat
from ghostwriter.audit import Operation, audited, forward_headers
from ghostwriter.config import Settings
from ghostwriter.identity import Principal, resolve_principal
from ghostwriter.schemas import ChatOut, StepOut
from ghostwriter.sse import aiter_sync_gen, sse_frame
from ghostwriter.store import AttachmentNotPending, ChatStore
from ghostwriter.turns import TurnRegistry
from ghostwriter.utils.audit_event import AiCall, AuditWriteError, Target, sha256_text
from ghostwriter.workbench import CallFor

router = APIRouter(prefix="/api/chats", tags=["Messages"])

#: The longest message, after stripping.
MAX_MESSAGE_CHARS: Final = 20_000
#: The most distinct attachments one message may send.
MAX_MESSAGE_ATTACHMENTS: Final = 10
ANSWER_RUNNING: Final = "An answer is still being written in this chat."
BUSY_CODE: Final = "busy"
UNKNOWN_ATTACHMENT: Final = "Unknown attachment."
PROCESSING: Final = "Wait until the attached files are ready."
PROCESSING_CODE: Final = "attachment_processing"
NO_ANSWER: Final = "There is no answer to regenerate."
NO_ANSWER_CODE: Final = "no_answer"
#: What the user is told when a record of the turn could not be written.
AUDIT_ERROR: Final = "The answer stopped: it could not be recorded."
STREAM_HEADERS: Final = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

User = Annotated[Principal, Depends(resolve_principal)]


class MessageIn(BaseModel):
    """A message to send.

    Attributes:
        content: At most :data:`MAX_MESSAGE_CHARS` characters after stripping; empty only
            beside an attachment.
        attachment_ids: Pending attachments of the chat; a repeated id counts once, and at
            most :data:`MAX_MESSAGE_ATTACHMENTS` distinct ones.
        reasoning: Whether the model may think before each round's answer.
    """

    content: Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_MESSAGE_CHARS)] = ""
    attachment_ids: list[str] = Field(default_factory=list)
    reasoning: bool = False

    @field_validator("attachment_ids")
    @classmethod
    def _distinct(cls, ids: list[str]) -> list[str]:
        """Count a repeated id once, and refuse more than the limit.

        Args:
            ids: The ids as sent.

        Returns:
            The distinct ids, in the order sent.

        Raises:
            ValueError: For more than :data:`MAX_MESSAGE_ATTACHMENTS` distinct ids.
        """
        distinct = list(dict.fromkeys(ids))
        if len(distinct) > MAX_MESSAGE_ATTACHMENTS:
            raise ValueError(f"at most {MAX_MESSAGE_ATTACHMENTS} attachments per message")
        return distinct

    @model_validator(mode="after")
    def _says_something(self) -> Self:
        """Refuse a message with neither text nor an attachment.

        Returns:
            The message.

        Raises:
            ValueError: When it is empty.
        """
        if not self.content and not self.attachment_ids:
            raise ValueError("a message needs text or an attachment")
        return self


class RegenerateIn(BaseModel):
    """How to answer the last question again.

    Attributes:
        reasoning: Whether the model may think before each round's answer.
    """

    reasoning: bool = False


class OperationTurnAudit(TurnAudit):
    """A turn's records, as children of the request's primary event."""

    def __init__(
        self, op: Operation, *, answer_id: str, text_model: str, reasoning: bool, cancel: threading.Event
    ) -> None:
        """Hold the operation and what every record names.

        Args:
            op: The request's operation, its primary event written.
            answer_id: The answer being written.
            text_model: The model every round asks for.
            reasoning: The message's reasoning switch.
            cancel: The turn's stop.
        """
        self._op = op
        self._answer_id = answer_id
        self._text_model = text_model
        self._reasoning = reasoning
        self._cancel = cancel

    @override
    def disclosed(self, *, tool: str, round: int, call_id: str, attempt: int) -> None:
        """Record a call about to be sent to workbench: ``disclose`` / ``chat.tool``.

        Args:
            tool: The tool.
            round: The model round that asked for it.
            call_id: The call's id in the turn.
            attempt: 1, or 2 for the retry after a busy answer.
        """
        self._op.child(
            "disclose",
            "chat.tool",
            target=Target(kind="message", ids=(self._answer_id,)),
            criteria={"tool": tool, "round": round, "call_id": call_id, "attempt": attempt},
            recipient=f"workbench:{tool}",
        )

    @override
    def model_called(
        self, *, round: int, done: RoundDone | None, outcome: Literal["ok", "error"], images: int = 0
    ) -> None:
        """Record a model round once it ended: ``ai_call`` / ``chat.answer``, the output by its hash only.

        Args:
            round: The round.
            done: How it ended; ``None`` when it failed.
            outcome: ``ok``, or ``error`` for a failed round.
            images: How many images it sent.
        """
        if done is None:
            ai = AiCall(model_alias=self._text_model)
            criteria = {"round": round, "tool_calls": 0, "finish_reason": None, "stopped": self._cancel.is_set()}
        else:
            ai = AiCall(
                model_alias=self._text_model,
                model_id=done.model_id,
                output_sha256=sha256_text(done.text),
                input_tokens=done.input_tokens,
                output_tokens=done.output_tokens,
            )
            criteria = {
                "round": round,
                "tool_calls": len(done.calls),
                "finish_reason": done.finish_reason,
                "stopped": done.stopped,
            }
        self._op.child(
            "ai_call",
            "chat.answer",
            criteria={**criteria, "images": images, "reasoning": self._reasoning},
            recipient="inference:chat",
            ai=ai,
            outcome=outcome,
        )


def _frame(event: TurnEvent) -> str:
    """Write a turn's event as its frame.

    Args:
        event: The event.

    Returns:
        The frame.
    """
    if isinstance(event, TokenEvent):
        return sse_frame("token", {"text": event.text})
    if isinstance(event, StepEvent):
        return sse_frame("step", event.step.model_dump(mode="json"))
    if isinstance(event, ResultEvent):
        return sse_frame("result", event.result.model_dump(mode="json"))
    if isinstance(event, NoticeEvent):
        return sse_frame("notice", {"code": event.code})
    if isinstance(event, ErrorEvent):
        return sse_frame("error", {"code": event.code, "message": event.message})
    raise TypeError(f"not a turn event: {type(event).__name__}")


class TurnStream:
    """One turn's frames, written on a worker thread, and who ends the turn.

    The worker ends the turn once it ran. A turn stopped, or whose response ended, before the
    worker started is ended at once by :meth:`attach`'s hook or by :meth:`close`, and never runs.
    """

    def __init__(
        self,
        *,
        agent: AgentLoop,
        turn: Turn,
        audit: TurnAudit,
        turns: TurnRegistry,
        chat: ChatOut,
        user_message_id: str | None,
    ) -> None:
        """Hold the turn.

        Args:
            agent: The loop.
            turn: The turn, holding the chat's claim as its stop.
            audit: Its records.
            turns: The registry the claim is in.
            chat: The chat, for the final frame.
            user_message_id: The message sent, or ``None`` for a regenerated answer.
        """
        self._agent = agent
        self._turn = turn
        self._audit = audit
        self._turns = turns
        self._final = {
            "message_id": turn.assistant_message_id,
            "user_message_id": user_message_id,
            "chat": chat.model_dump(mode="json"),
        }
        self._lock = threading.Lock()
        self._state: Literal["waiting", "running", "ended"] = "waiting"

    def _start(self) -> bool:
        with self._lock:
            if self._state != "waiting":
                return False
            self._state = "running"
            return True

    def _end_waiting(self) -> bool:
        with self._lock:
            if self._state != "waiting":
                return False
            self._state = "ended"
        self._turns.end(self._turn.chat_id, self._turn.cancel)
        return True

    def attach(self, put: Callable[[str | None], None]) -> None:
        """Send ``start`` first, and let a stop end the turn while it still waits for a thread.

        ``start`` names the rows the turn stored, so a view that reads the chat meanwhile
        shows them once. A stop before the worker starts ends the stream with ``final``.

        Args:
            put: Queues a frame for the response from any thread; ``None`` ends the response.
        """
        put(sse_frame("start", {key: self._final[key] for key in ("message_id", "user_message_id")}))

        def stopped() -> None:
            if self._end_waiting():
                put(sse_frame("final", {**self._final, "stopped": True}))
                put(None)

        self._turns.on_stop(self._turn.chat_id, self._turn.cancel, stopped)

    def frames(self) -> Iterator[str]:
        """Run the turn and write its frames; the turn ends just before ``final``, which comes last.

        Yields:
            str: The frames.
        """
        if not self._start():
            return
        turn = self._turn
        running: dict[str, StepOut] = {}
        try:
            try:
                for event in self._agent.run(turn, self._audit):
                    if isinstance(event, StepEvent):
                        if event.step.status == "running":
                            running[event.step.id] = event.step
                        else:
                            running.pop(event.step.id, None)
                    yield _frame(event)
            except AuditWriteError:
                logger.error("An audit event could not be written; the answer in chat {} stopped.", turn.chat_id)
                yield sse_frame("error", {"code": "audit", "message": AUDIT_ERROR})
            except Exception as exc:
                logger.error("An answer's stream broke off: {}", type(exc).__name__)
                yield sse_frame("error", {"code": "internal", "message": INTERNAL_ERROR})
            for step in running.values():
                yield sse_frame("step", step.model_copy(update={"status": "error"}).model_dump(mode="json"))
            final = sse_frame("final", {**self._final, "stopped": turn.cancel.is_set()})
            self._turns.end(turn.chat_id, turn.cancel)
            yield final
        finally:
            self._turns.end(turn.chat_id, turn.cancel)

    def close(self) -> None:
        """Stop the turn, as its response has ended; end it here if it never ran."""
        self._turn.cancel.set()
        self._end_waiting()


class TurnResponse(StreamingResponse):
    """A turn's SSE stream; however the response ends, closed tab included, the turn is stopped."""

    def __init__(self, stream: TurnStream, request: Request, limiter: CapacityLimiter) -> None:
        """Stream the turn's frames from its worker thread.

        Args:
            stream: The turn.
            request: The request, polled for a client gone.
            limiter: The turn threads.
        """
        super().__init__(
            aiter_sync_gen(stream.frames, request, limiter, feed=stream.attach, keepalive=True),
            media_type="text/event-stream",
            headers=STREAM_HEADERS,
        )
        self._stream = stream

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the stream, then stop the turn, however the sending ended.

        Args:
            scope: The request's scope.
            receive: The request's channel.
            send: The response's channel.
        """
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._stream.close()


def _store(request: Request) -> ChatStore:
    store: ChatStore = request.app.state.store
    return store


def _turns(request: Request) -> TurnRegistry:
    turns: TurnRegistry = request.app.state.turns
    return turns


def _conflict(detail: str, code: str) -> JSONResponse:
    """Refuse a turn with 409 and the reason's code.

    Args:
        detail: What the user is told.
        code: The reason, for the SPA.

    Returns:
        The refusal.
    """
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": detail, "code": code})


def _claim(request: Request, owner: str, chat_id: str) -> threading.Event | None:
    """Claim the owner's chat for a turn.

    Args:
        request: The request.
        owner: Who asks.
        chat_id: The chat.

    Returns:
        The claim, the turn's stop; ``None`` while a turn runs in the chat.

    Raises:
        HTTPException: 404 for another owner's chat or an unknown one.
    """
    owned_chat(_store(request), owner, chat_id)
    return _turns(request).begin(chat_id)


def _answer(
    request: Request,
    op: Operation,
    owner: str,
    chat_id: str,
    claim: threading.Event,
    *,
    question_id: str,
    answer_id: str,
    user_message_id: str | None,
    reasoning: bool,
) -> TurnResponse:
    """Stream the answer to a question; the turn's claim passes to the stream.

    Args:
        request: The request.
        op: Its operation, the primary event written.
        owner: The chat's owner.
        chat_id: The chat.
        claim: The chat's claim.
        question_id: The user's message answered.
        answer_id: The answer to write.
        user_message_id: The message sent, for the final frame.
        reasoning: The reasoning switch.

    Returns:
        The stream.
    """
    settings: Settings = request.app.state.settings
    turn = Turn(
        chat_id=chat_id,
        owner=owner,
        user_message_id=question_id,
        assistant_message_id=answer_id,
        call=CallFor(owner, chat_id, forward_headers(op.context)),
        request_id=op.context.request_id,
        cancel=claim,
        reasoning=reasoning,
    )
    audit = OperationTurnAudit(
        op, answer_id=answer_id, text_model=settings.text_model, reasoning=reasoning, cancel=claim
    )
    stream = TurnStream(
        agent=request.app.state.agent,
        turn=turn,
        audit=audit,
        turns=_turns(request),
        chat=_store(request).get_chat(owner, chat_id),
        user_message_id=user_message_id,
    )
    return TurnResponse(stream, request, request.app.state.turn_threads)


@router.post(
    "/{chat_id}/messages",
    response_class=StreamingResponse,
    response_model=None,
    summary="Send a message; the answer streams",
)
def send_message(
    chat_id: str,
    body: MessageIn,
    request: Request,
    user: User,
    op: Annotated[Operation, Depends(audited("collect", "chat.message", also=("ai_call", "disclose")))],
) -> TurnResponse | JSONResponse:
    """Store the user's message and stream its answer as Server-Sent Events.

    Args:
        chat_id: The chat.
        body: The message.
        request: The request.
        user: Who writes.
        op: ``collect`` / ``chat.message``, recorded with the message's id, its attachment count
            and its length before it is stored; the turn's rounds and calls are its children.

    Returns:
        The stream; or 409 ``busy`` while a turn runs in the chat, ``attachment_processing`` while
        workbench still prepares one of its attachments, before anything is recorded or stored.

    Raises:
        HTTPException: 404 for another owner's chat or an unknown one; 400 when an attachment
            is not pending in this chat.
    """
    store = _store(request)
    claim = _claim(request, user.name, chat_id)
    if claim is None:
        return _conflict(ANSWER_RUNNING, BUSY_CODE)
    try:
        owned_chat(store, user.name, chat_id)
        statuses = store.pending_statuses(chat_id)
        if not set(body.attachment_ids) <= statuses.keys():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=UNKNOWN_ATTACHMENT)
        if any(statuses[attachment_id] == "processing" for attachment_id in body.attachment_ids):
            _turns(request).end(chat_id, claim)
            return _conflict(PROCESSING, PROCESSING_CODE)
        question_id = str(uuid4())
        op.container = chat_id
        op.record(
            target=Target(kind="message", ids=(question_id,)),
            criteria={"attachments": len(body.attachment_ids), "chars": len(body.content)},
        )
        try:
            store.add_user_message(user.name, chat_id, body.content, body.attachment_ids, id=question_id)
        except AttachmentNotPending:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=UNKNOWN_ATTACHMENT) from None
        answer_id = store.start_assistant_message(chat_id)
        return _answer(
            request,
            op,
            user.name,
            chat_id,
            claim,
            question_id=question_id,
            answer_id=answer_id,
            user_message_id=question_id,
            reasoning=body.reasoning,
        )
    except BaseException:
        _turns(request).end(chat_id, claim)
        raise


@router.post(
    "/{chat_id}/regenerate",
    response_class=StreamingResponse,
    response_model=None,
    summary="Answer the last question again",
)
def regenerate(
    chat_id: str,
    request: Request,
    user: User,
    op: Annotated[Operation, Depends(audited("modify", "chat.regenerate", also=("ai_call", "disclose")))],
    body: Annotated[RegenerateIn | None, Body()] = None,
) -> TurnResponse | JSONResponse:
    """Replace the chat's last answer with a new one, streamed as Server-Sent Events.

    Args:
        chat_id: The chat.
        request: The request.
        user: Who asks.
        op: ``modify`` / ``chat.regenerate``, recorded with the replaced answer's id before it
            is dropped; the turn's rounds and calls are its children.
        body: The reasoning switch; off without a body.

    Returns:
        The stream; or 409 ``busy`` while a turn runs in the chat, ``no_answer`` when it ends in
        no answer.

    Raises:
        HTTPException: 404 for another owner's chat or an unknown one.
    """
    store = _store(request)
    claim = _claim(request, user.name, chat_id)
    if claim is None:
        return _conflict(ANSWER_RUNNING, BUSY_CODE)
    try:
        owned_chat(store, user.name, chat_id)
        replaced = store.last_answer(user.name, chat_id)
        question_id = None if replaced is None else store.question_of(chat_id, replaced)
        if replaced is None or question_id is None:
            _turns(request).end(chat_id, claim)
            return _conflict(NO_ANSWER, NO_ANSWER_CODE)
        op.container = chat_id
        op.record(target=Target(kind="message", ids=(replaced,)))
        store.drop_message(replaced)
        answer_id = store.start_assistant_message(chat_id)
        return _answer(
            request,
            op,
            user.name,
            chat_id,
            claim,
            question_id=question_id,
            answer_id=answer_id,
            user_message_id=None,
            reasoning=body is not None and body.reasoning,
        )
    except BaseException:
        _turns(request).end(chat_id, claim)
        raise


@router.post("/{chat_id}/stop", status_code=status.HTTP_204_NO_CONTENT, summary="Stop the answer being written")
def stop(chat_id: str, request: Request, user: User) -> None:
    """Ask the chat's running answer to stop; a chat without one is left as it is. Records nothing.

    Args:
        chat_id: The chat.
        request: The request.
        user: Who asks.

    Raises:
        HTTPException: 404 for another owner's chat or an unknown one.
    """
    owned_chat(_store(request), user.name, chat_id)
    _turns(request).stop(chat_id)
