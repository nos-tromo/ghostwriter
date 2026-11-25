"""Stream one model round: its visible text as it arrives, and its tool calls joined.

Text goes out as it arrives, with no hold, also when the round then turns to tool calls: a
preface such as "I'll read the file." is shown and kept. Reasoning never goes out: think
blocks in the content are read out by :class:`~ghostwriter.agent.reasoning.ReasoningStripper`,
and a reasoning field the endpoint sends apart from the content (``reasoning_content``, or
``reasoning``) is dropped. Neither is kept, logged or returned.

A reasoning round (the message's switch) may open inside a think block the chat template
began in the prompt, so its content is held until a close tag (what came before it is
reasoning), a separate reasoning field (the endpoint keeps reasoning apart, so the content is
the answer), or the end of the reply (the held content was the answer), as docint's
``stream_round`` does. It may answer in up to ``REASONING_ANSWER_TOKENS``.

A continuation round (Continue) goes on in the answer so far, the conversation's last message: it
asks the model for that (:data:`~ghostwriter.inference.CONTINUE_BODY` on vllm), and the whitespace its
reply opens with, which an answer's start drops, is kept, as it joins the reply to the answer so far.

:class:`ToolCall` and :class:`ToolCallAccumulator`, with the helpers they use, are copied
unchanged from docint (``docint/agents/turn/stream.py``); here they read the SDK's chunks
directly.

A malformed stream may carry an unpaired surrogate (an escaped U+D800), which no store,
frame or later request can encode; :func:`_scalar` replaces it with U+FFFD in the text, the
calls' ids and names, the served model's name and the finish reason. Valid text is unchanged.

A round stops once ``cancel`` is set: the next chunk is not read on, the stream is closed,
and the round asks for no call, as none of its calls may be whole. A round dropped by its
consumer closes its stream too. The model's errors propagate: a refused request on the first
pull, before anything goes out; a reply cut off where it breaks.
"""

import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from loguru import logger

from ghostwriter.agent.limits import MAX_ANSWER_TOKENS, MODEL_TIMEOUT_S, REASONING_ANSWER_TOKENS
from ghostwriter.agent.reasoning import ReasoningStripper
from ghostwriter.inference import ModelClient


@dataclass(frozen=True)
class ToolCall:
    """One tool call a model round asked for.

    Attributes:
        id (str): The id its reply names; unique within the round.
        name (str): The tool's name as the model wrote it.
        arguments (str): The arguments as raw JSON text, unparsed and unchecked.
    """

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class TextDelta:
    """Visible answer text, as it arrived; never reasoning.

    Attributes:
        text (str): Text not sent before.
    """

    text: str


@dataclass(frozen=True)
class RoundDone:
    """How a round ended; always its last event.

    Attributes:
        text (str): Every :class:`TextDelta` of the round, joined: reasoning left out, and
            leading and trailing whitespace too; a continuation keeps its leading whitespace.
        calls (list[ToolCall]): The tool calls, in the order the model began them; empty when
            the text is the answer, and for a stopped round.
        finish_reason (str | None): The endpoint's finish reason, or ``None`` if it sent none.
        model_id (str | None): The model the router says served the round.
        input_tokens (int | None): The prompt's tokens, when the endpoint reported them.
        output_tokens (int | None): The reply's tokens, likewise.
        stopped (bool): Whether ``cancel`` ended the round before the reply did.
    """

    text: str
    calls: list[ToolCall]
    finish_reason: str | None
    model_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    stopped: bool


def _field(value: Any, name: str) -> Any:
    """Read one field of a raw chunk part, whether the client parsed it or not.

    Args:
        value (Any): An SDK object, a mapping, or ``None``.
        name (str): The field.

    Returns:
        Any: Its value, or ``None`` when absent.
    """
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _text(value: Any) -> str:
    """Return a string field's value, or ``""`` for a missing or malformed one.

    Args:
        value (Any): The field's value.

    Returns:
        str: The value when it is a string.
    """
    return value if isinstance(value, str) else ""


def _choice(raw_chunk: Any) -> Any:
    """Return a raw chunk's first choice.

    Args:
        raw_chunk (Any): The chunk as the client received it.

    Returns:
        Any: The choice, or ``None`` for a chunk without choices, such as a usage chunk.
    """
    choices = _field(raw_chunk, "choices")
    return choices[0] if isinstance(choices, Sequence) and choices else None


@dataclass
class _PartialCall:
    """A tool call still being joined from its fragments."""

    id: str = ""
    name: str = ""
    arguments: str = ""


class ToolCallAccumulator:
    """Join the tool-call fragments in one round's raw chunks into whole calls.

    Every entry of a chunk's ``delta.tool_calls`` is read, so several whole calls in one
    chunk all survive. A fragment joins the call open at its ``index``, unless it carries
    an id other than that call's, which starts a new call there. A fragment without an
    ``index`` joins the call holding its id, or starts one if the id is new; with neither,
    it continues the call fed last.

    Feed each chunk as it arrives: the client grows an earlier chunk's call objects in
    place as later fragments come in, so a chunk read late would count them twice.
    """

    def __init__(self) -> None:
        """Start with no calls."""
        self._calls: list[_PartialCall] = []
        self._by_index: dict[int, _PartialCall] = {}
        self._by_id: dict[str, _PartialCall] = {}
        self._last: _PartialCall | None = None

    def feed(self, raw_chunk: Any) -> bool:
        """Read the tool-call fragments of one raw chunk.

        Args:
            raw_chunk (Any): The chunk on ``ChatResponse.raw``: the SDK's object, or the same
                shape as a mapping; anything else carries no call.

        Returns:
            bool: Whether the chunk carried any tool-call fragment.
        """
        entries = _field(_field(_choice(raw_chunk), "delta"), "tool_calls")
        if not isinstance(entries, Sequence) or not entries:
            return False
        for entry in entries:
            self._join(entry)
        return True

    def _join(self, entry: Any) -> None:
        """Add one fragment to the call it belongs to.

        Args:
            entry (Any): One entry of a chunk's ``delta.tool_calls``.
        """
        index = _field(entry, "index")
        call_id = _text(_field(entry, "id"))
        call = self._target(index if isinstance(index, int) and not isinstance(index, bool) else None, call_id)
        if call_id and not call.id:
            call.id = call_id
            self._by_id.setdefault(call_id, call)
        function = _field(entry, "function")
        call.name += _text(_field(function, "name"))
        call.arguments += _text(_field(function, "arguments"))
        self._last = call

    def _target(self, index: int | None, call_id: str) -> _PartialCall:
        """Find the call a fragment continues, or start the one it begins.

        Args:
            index (int | None): The fragment's index, if it has one.
            call_id (str): Its id; ``""`` when it has none.

        Returns:
            _PartialCall: The call to extend.
        """
        if index is not None:
            call = self._by_index.get(index)
            if call is None or (call_id and call.id and call_id != call.id):
                call = self._start()
                self._by_index[index] = call
            return call
        if call_id:
            return self._by_id.get(call_id) or self._start()
        return self._last or self._start()

    def _start(self) -> _PartialCall:
        """Begin a new call.

        Returns:
            _PartialCall: The empty call, appended in arrival order.
        """
        call = _PartialCall()
        self._calls.append(call)
        return call

    @property
    def dropped(self) -> int:
        """Count the calls :meth:`calls` leaves out because they never received a name.

        Returns:
            int: The count.
        """
        return sum(1 for call in self._calls if not call.name.strip())

    def calls(self) -> list[ToolCall]:
        """Return the calls joined so far, those without a name left out.

        A call without an id, or with one an earlier call already holds, is given
        ``call_<position>``, suffixed until no other call holds it, so every call can be
        answered by its own id.

        Returns:
            list[ToolCall]: The calls, in the order they began.
        """
        named = [call for call in self._calls if call.name.strip()]
        taken = {call.id for call in named if call.id}
        given: set[str] = set()
        calls: list[ToolCall] = []
        for position, call in enumerate(named):
            call_id = call.id
            if not call_id or call_id in given:
                call_id = base = f"call_{position}"
                suffix = 0
                while call_id in taken or call_id in given:
                    suffix += 1
                    call_id = f"{base}_{suffix}"
            given.add(call_id)
            calls.append(ToolCall(id=call_id, name=call.name, arguments=call.arguments))
        return calls


def _scalar(text: str) -> str:
    """Replace each unpaired surrogate with U+FFFD; a pair that stands apart is joined, valid text is unchanged.

    Args:
        text (str): Text read from the stream.

    Returns:
        str: Text that encodes as UTF-8.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return text


def _finish_reason(raw_chunk: Any) -> str | None:
    """Read the finish reason a raw chunk carries.

    Args:
        raw_chunk (Any): The chunk as the client received it.

    Returns:
        str | None: The reason, or ``None`` when the chunk has none.
    """
    return _text(_field(_choice(raw_chunk), "finish_reason")) or None


def _count(value: Any) -> int | None:
    """Return a token count, or ``None`` for a missing or malformed one.

    Args:
        value (Any): The field's value.

    Returns:
        int | None: The count when it is an integer.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class _Lead:
    """The whitespace a continuation opens with, which joins it to the answer so far.

    The reader drops a reply's leading whitespace, as an answer starts without it. A continuation goes on where
    the answer stopped, so a space or a line break it opens with is the seam: it is held while the reply's content
    is whitespace alone, and put before the reply's first visible text. For any other round it passes everything on.
    """

    def __init__(self, *, kept: bool) -> None:
        """Start before any content.

        Args:
            kept (bool): Whether the reply's leading whitespace is kept: only a continuation's is.
        """
        self._held = ""
        self._open = kept
        self._shown = not kept

    def take(self, content: str) -> str:
        """Hold the content's leading whitespace while the reply's content so far is whitespace alone.

        Args:
            content (str): The next piece of the reply's content.

        Returns:
            str: The piece for the reader, without what was held.
        """
        if not self._open:
            return content
        rest = content.lstrip()
        self._held += content[: len(content) - len(rest)]
        self._open = not rest
        return rest

    def give(self, text: str) -> str:
        """Put what was held before the reply's first visible text.

        Args:
            text (str): Visible text the reader passed on.

        Returns:
            str: The text, the held whitespace before it if it is the first.
        """
        if self._shown or not text:
            return text
        self._shown = True
        return self._held + text


def stream_round(
    model: ModelClient,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]],
    headers: Mapping[str, str],
    cancel: threading.Event,
    reasoning: bool = False,
    continuation: bool = False,
) -> Iterator[TextDelta | RoundDone]:
    """Stream one model round, bounded in tokens, each wait on the router by ``MODEL_TIMEOUT_S``.

    Args:
        model (ModelClient): The chat model.
        messages (list[dict[str, Any]]): The conversation so far.
        tools (list[dict[str, Any]]): The tool schemas offered; none, and the request carries
            neither ``tools`` nor ``tool_choice``.
        headers (Mapping[str, str]): The extra headers, ``inference_headers``.
        cancel (threading.Event): Set to stop the round.
        reasoning (bool): Whether the model may think first: the round asks for it and answers
            in up to ``REASONING_ANSWER_TOKENS``, else ``MAX_ANSWER_TOKENS``.
        continuation (bool): Whether the round goes on in the last message, the answer so far: it asks
            for that, and keeps the whitespace its reply opens with.

    Yields:
        TextDelta | RoundDone: The visible text as it arrives, then exactly one :class:`RoundDone`.

    Raises:
        openai.OpenAIError: Whatever the model call raises.
    """
    stream = model.stream(
        messages,
        tools=tools,
        headers=headers,
        max_tokens=REASONING_ANSWER_TOKENS if reasoning else MAX_ANSWER_TOKENS,
        timeout_s=MODEL_TIMEOUT_S,
        reasoning=reasoning,
        continuation=continuation,
    )
    accumulator = ToolCallAccumulator()
    reader = ReasoningStripper(reasoning_open=reasoning)
    lead = _Lead(kept=continuation)
    shown: list[str] = []
    finish_reason: str | None = None
    model_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    stopped = False
    try:
        for chunk in stream:
            if cancel.is_set():
                stopped = True
                break
            accumulator.feed(chunk)
            finish_reason = _scalar(_finish_reason(chunk) or "") or finish_reason
            model_id = _scalar(_text(_field(chunk, "model"))) or model_id
            usage = _field(chunk, "usage")
            if usage is not None:
                input_tokens = _count(_field(usage, "prompt_tokens"))
                output_tokens = _count(_field(usage, "completion_tokens"))
            delta = _field(_choice(chunk), "delta")
            text = ""
            if _text(_field(delta, "reasoning_content")) or _text(_field(delta, "reasoning")):
                text = reader.cancel_open_reasoning()
            content = lead.take(_scalar(_text(_field(delta, "content"))))
            text = lead.give(text + reader.feed(content))
            if text:
                shown.append(text)
                yield TextDelta(text)
    finally:
        stream.close()
    calls: list[ToolCall] = []
    if not stopped:
        tail = lead.give(reader.flush())
        if tail:
            shown.append(tail)
            yield TextDelta(tail)
        calls = [ToolCall(_scalar(c.id), _scalar(c.name), c.arguments) for c in accumulator.calls()]
        if accumulator.dropped:
            logger.warning("A model round sent tool calls without a name; dropped: {}", accumulator.dropped)
    yield RoundDone(
        text="".join(shown),
        calls=calls,
        finish_reason=finish_reason,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stopped=stopped,
    )
