"""The messages a round sends: the system prompt, earlier turns that fit, and the current turn.

The budget is counted as characters of the messages' compact JSON over ``CHARS_PER_TOKEN``,
the tool schemas included. The current turn is always sent; while it alone is over budget,
its oldest tool results are cleared, one at a time, and then, still over, its oldest calls'
arguments are cut as a replayed call's are. Earlier turns are then added newest
first, whole, while they fit: each as its question, per round an assistant message with its
calls and one tool message per call, then its answer. A replayed call's arguments stay a
JSON object, which chat templates parse, and are cut to ``HISTORY_ARGUMENT_CHARS``; its
result is cut to ``HISTORY_RESULT_CHARS``. A :class:`FitReport` counts the earlier turns and
those replayed.

With an :class:`~ghostwriter.agent.images.ImageBudget`, an image ref counts as its label's
characters plus :func:`~ghostwriter.agent.images.estimate_image_tokens`, and a request carries
at most ``max_images``. While the current turn is over the budget or the cap, it gives way in
this order:

1. the tool images of its earlier rounds retire, oldest first;
2. past the cap, the question's own pictures give way, last first, so the newest tool images (the
   latest round's, while they end the turn: see :func:`~ghostwriter.agent.images.newest_tool_images`),
   which the model has not seen yet, stay within the cap; after a round without images, every tool
   image is an earlier round's;
3. tool results are cleared, oldest first, except the latest round's (the tool messages after the
   turn's last assistant message with calls);
4. arguments are cut;
5. the question's pictures give way, last first, each with a note that ``view_file`` shows it;
6. the newest tool images give way, oldest first;
7. the latest round's own results are cleared, oldest first.

So the document the model just asked for outlasts the pictures of the question, which it has seen
or can look at, and the images it just asked for outrank text it has already read. Each image
leaves a note in its place, and a picture's strips give way together. A question's picture that
gives way stays a ref in the turn: a later round sends it when there is room, and never says it
was shown before. An earlier turn with images is replayed with them while the budget and the cap
allow, else with a note per image that ``view_file`` shows it; a tool image is never replayed.
In a round that does not offer ``view_file`` (``view=False``), every note keeps what happened and
leaves the tool out (:data:`~ghostwriter.agent.images.WITHOUT_VIEW`).
Without an image budget, the current turn's results are cleared oldest first and then arguments
cut, as before; an earlier turn is replayed as its text, and the current turn must hold no image ref.

At the end of an answer :func:`next_prompt_tokens` estimates what the next message's prompt starts with: the
system prompt, the tool schemas and every turn as :func:`fit` would replay it, the answer just finished
(:func:`finished_turn`) among them. It is sized by the same code and counts what could no longer be sent: no
budget, no image cap.

A continuation (Continue) sends its turn as :func:`continuation` gives it: the turn replayed as an earlier one is,
the answer so far its last message, and for a provider that does not go on in that message, an instruction after it.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Final, Literal, TypeGuard

from loguru import logger

from ghostwriter.agent.images import (
    LABEL,
    NO_ROOM,
    NOT_RESENT,
    OVER_CAP,
    RETIRED,
    UNSEEN,
    AttachedImage,
    ImageBudget,
    attachment_parts,
    estimate_image_tokens,
    is_ref,
    newest_tool_images,
    note,
    parts_of,
    ref_places,
    refs,
    rendition_size,
    siblings,
    told,
)
from ghostwriter.agent.limits import CHARS_PER_TOKEN, HISTORY_ARGUMENT_CHARS, HISTORY_RESULT_CHARS, PROMPT_TOKENS
from ghostwriter.agent.prompt import CONTINUE
from ghostwriter.agent.tools import CUT_MARK, compact, cut, echoed_arguments, parse_arguments
from ghostwriter.schemas import AttachmentOut
from ghostwriter.upload import media_kind

#: What a current tool result reads once it was cleared to make room.
CLEARED: Final = "[cleared to make room; call the tool again if you need it]"
#: How an earlier turn is replayed: with its images, with a note per image, or as its text alone.
ReplayMode = Literal["images", "notes", "none"]
#: The most pages or slides without text an attachment's line lists.
LISTED_PAGES: Final = 10
#: What an Office attachment's line calls its format, by the name's suffix: the keys are
#: :data:`~ghostwriter.upload.OFFICE_SUFFIXES`.
OFFICE_FORMATS: Final[Mapping[str, str]] = {
    ".docx": "Word document",
    ".dotx": "Word document",
    ".pptx": "PowerPoint",
    ".potx": "PowerPoint",
    ".odt": "OpenDocument text",
}
#: What a picture's line says while the turn does not see, whatever the cause: vision off, a model
#: that takes no images, or a workbench that shows none.
UNSEEN_PICTURE: Final = "not shown to the model"


@dataclass(frozen=True)
class PastCall:
    """A call of an earlier turn whose content has not expired.

    Attributes:
        call_id: The model's id for the call.
        tool: The tool.
        arguments: The arguments as the model wrote them.
        result: What the model read back.
    """

    call_id: str
    tool: str
    arguments: str
    result: str


@dataclass(frozen=True)
class PastTurn:
    """An earlier question and its answer.

    Attributes:
        user: The question, its attachments named.
        rounds: The calls of each round that called tools, in order; only those not expired.
        answer: The answer's text.
        expired: Whether any of its files or tool contents have expired.
        images: The pictures its question attached that are still ready to be seen.
    """

    user: str
    rounds: tuple[tuple[PastCall, ...], ...]
    answer: str
    expired: bool
    images: tuple[AttachedImage, ...] = ()


def estimate_tokens(value: str | Sequence[Mapping[str, Any]]) -> int:
    """Estimate the tokens of a text, or of messages as their compact JSON.

    Args:
        value: The text, or the messages.

    Returns:
        Its characters over ``CHARS_PER_TOKEN``, rounded up.
    """
    text = value if isinstance(value, str) else compact(list(value))
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def user_content(
    text: str,
    attachments: Sequence[AttachmentOut],
    *,
    path_of: Callable[[AttachmentOut], str],
    facts_of: Callable[[AttachmentOut], str | None] = lambda _: None,
) -> str:
    """Return a user's message as the model reads it: the text, then a line per attachment.

    Args:
        text: The message.
        attachments: The files it sent.
        path_of: Where an attachment is in the chat's folder.
        facts_of: What an attachment is (:func:`attachment_facts`), or ``None`` to say nothing.

    Returns:
        The text and ``[Attached: name (path, size bytes)]``, its facts after the size
        (``[Attached: name (path, size bytes; facts)]``); ``[Attached: name; expired]``, or
        ``[Attached: name; could not be read]`` for one that failed.
    """
    lines = [text] if text else []
    for attachment in attachments:
        if attachment.expired:
            lines.append(f"[Attached: {attachment.name}; expired]")
        elif attachment.status == "failed":
            lines.append(f"[Attached: {attachment.name}; could not be read]")
        else:
            facts = facts_of(attachment)
            told = f"; {facts}" if facts else ""
            lines.append(f"[Attached: {attachment.name} ({path_of(attachment)}, {attachment.size} bytes{told})]")
    return "\n".join(lines)


def attachment_facts(kind: str, manifest: Mapping[str, Any] | None, *, seen: bool, name: str) -> str | None:
    """Say what an attachment is, from what workbench reported of it.

    Args:
        kind: The attachment's kind: ``document``, ``image`` or ``media``.
        manifest: Its manifest as the chat keeps it, or ``None``.
        seen: Whether the turn sees images.
        name: The attachment's name, whose suffix says an Office file's format and whether a clip is video or
            audio.

    Returns:
        For a picture ``image`` and its first rendition's size, followed by :data:`UNSEEN_PICTURE`
        when not ``seen``; for a PDF its pages, for an Office file (``PowerPoint``, ``Word document`` or
        ``OpenDocument text``) its slides or pages as the manifest's ``unit`` says, and for either those
        without text (:func:`_pages_told`), whether ``seen`` or not; for a clip (``media``) its length,
        frames and transcript (:func:`_clip_told`); else ``None``, which a manifest of kind ``none`` also gives.
    """
    if kind == "image":
        size = rendition_size(manifest)
        facts = "image" if size is None else f"image {size[0]}\N{MULTIPLICATION SIGN}{size[1]}"
        return facts if seen else f"{facts}; {UNSEEN_PICTURE}"
    if manifest is None:
        return None
    if kind == "media":
        return _clip_told(manifest, name, seen=seen)
    if manifest.get("kind") == "pdf":
        return _pages_told(manifest, "PDF", "page")
    if manifest.get("kind") == "office":
        office = OFFICE_FORMATS.get(PurePosixPath(name).suffix.lower())
        if office is None:
            return None
        return _pages_told(manifest, office, "slide" if manifest.get("unit") == "slide" else "page")
    return None


def _is_length(value: object) -> TypeGuard[int | float]:
    """Tell whether a manifest's value is a clip's length in seconds.

    Args:
        value: The value.

    Returns:
        ``True`` for a finite number of zero or more that is no ``bool``; ``False`` for any other, an integer too
        large for a float included.
    """
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _clip_told(manifest: Mapping[str, Any], name: str, *, seen: bool) -> str | None:
    """Say what a video or audio clip holds: its length, its frames and its transcript.

    Args:
        manifest: Its manifest as the chat keeps it.
        name: The clip's name, whose suffix says video or audio.
        seen: Whether the turn sees images, which a video's frames are shown to.

    Returns:
        ``video 02:14, 8 frames; transcript``: ``video`` or ``audio``, then its length as ``mm:ss``
        (``h:mm:ss`` from one hour; whole seconds, cut) when the manifest has one, then for a video its frames
        (``, 1 frame``, ``, no frames``), followed by :data:`UNSEEN_PICTURE` when it has some and the turn is not
        ``seen``, and last ``; transcript`` or ``; no speech``. A figure that is not a number is left out.
        ``None`` for a manifest that is not a clip's.
    """
    if manifest.get("kind") != "media":
        return None
    clip = media_kind(name)
    facts = clip
    duration = manifest.get("duration_s")
    if _is_length(duration):
        seconds = int(duration)
        hours, rest = divmod(seconds, 3600)
        clock = f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60:02d}:{rest % 60:02d}"
        facts = f"{facts} {clock}"
    total = manifest.get("total")
    if clip == "video" and isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        if total == 0:
            facts = f"{facts}, no frames"
        else:
            facts = f"{facts}, {total} frame" if total == 1 else f"{facts}, {total} frames"
            facts = facts if seen else f"{facts} {UNSEEN_PICTURE}"
    return f"{facts}; transcript" if manifest.get("transcript") is True else f"{facts}; no speech"


def _pages_told(manifest: Mapping[str, Any], format_name: str, unit: str) -> str | None:
    """Say how many pages or slides a document has, and which have no text.

    Args:
        manifest: Its manifest as the chat keeps it.
        format_name: What the line calls the format: ``PDF``, ``PowerPoint``.
        unit: The noun for one of them: ``page`` or ``slide``.

    Returns:
        ``PDF, 12 pages`` or ``PowerPoint, 1 slide``, then ``; page 3 has no text`` for one without
        text, ``; pages 3, 7 have no text`` for more, at most :data:`LISTED_PAGES` of them and how many
        more, or ``; no page has text`` when that holds for more than one; ``None`` without a count
        of at least one.
    """
    total = manifest.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 1:
        return None
    facts = f"{format_name}, 1 {unit}" if total == 1 else f"{format_name}, {total} {unit}s"
    listed = manifest.get("pages_without_text")
    blank: list[int] = [page for page in listed if isinstance(page, int)] if isinstance(listed, list) else []
    distinct = {page for page in blank if 1 <= page <= total}
    if total > 1 and len(distinct) == total:
        return f"{facts}; no {unit} has text"
    if len(blank) == 1:
        return f"{facts}; {unit} {blank[0]} has no text"
    if not blank:
        return facts
    numbers = ", ".join(str(page) for page in blank[:LISTED_PAGES])
    more = f", and {len(blank) - LISTED_PAGES} more" if len(blank) > LISTED_PAGES else ""
    return f"{facts}; {unit}s {numbers}{more} have no text"


def _cut_arguments(raw: str, limit: int) -> str:
    """Return a call's arguments as a JSON object of at most ``limit`` characters.

    The longest string values are cut first, each marked; arguments that still do not fit
    become ``{}``.

    Args:
        raw: The arguments as the model wrote them.
        limit: The most characters.

    Returns:
        The arguments.
    """
    echoed = echoed_arguments(raw)
    if len(echoed) <= limit:
        return echoed
    value = dict(parse_arguments(raw) or {})
    for key in sorted((k for k, v in value.items() if isinstance(v, str)), key=lambda k: -len(value[k])):
        over = len(compact(value)) - limit
        if over <= 0:
            break
        text: str = value[key]
        value[key] = text[: max(0, len(text) - over - len(CUT_MARK))] + CUT_MARK
    shortened = compact(value)
    return shortened if len(shortened) <= limit else "{}"


def _with_cut_arguments(call: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of one of the current turn's ``tool_calls``, its arguments cut as a replayed call's are.

    Args:
        call: The entry; never changed.

    Returns:
        The entry, its arguments a JSON object of at most ``HISTORY_ARGUMENT_CHARS``.
    """
    function = call["function"]
    return {
        **call,
        "function": {**function, "arguments": _cut_arguments(function["arguments"], HISTORY_ARGUMENT_CHARS)},
    }


def _question(turn: PastTurn, mode: ReplayMode, images: ImageBudget | None, *, view: bool = True) -> dict[str, Any]:
    """Return an earlier turn's question as it is replayed.

    Args:
        turn: The turn.
        mode: ``images``: the text and its images' refs; ``notes``: the text and a
            :data:`~ghostwriter.agent.images.NOT_RESENT` line per image; ``none``: the text.
        images: What images cost; read in ``images`` mode, for the strips.
        view: Whether the round offers ``view_file``, which a ``notes`` line names.

    Returns:
        The user's message.
    """
    if mode == "images":
        fixed = None if images is None else images.fixed
        parts = attachment_parts(turn.images, fixed=fixed)
        return {"role": "user", "content": [{"type": "text", "text": turn.user}, *parts]}
    if mode == "notes":
        lines = [turn.user, *(told(NOT_RESENT, view=view).format(label=image.name) for image in turn.images)]
        return {"role": "user", "content": "\n".join(line for line in lines if line)}
    return {"role": "user", "content": turn.user}


def _replayed(
    turn: PastTurn, *, mode: ReplayMode = "none", images: ImageBudget | None = None, view: bool = True
) -> list[dict[str, Any]]:
    """Return an earlier turn as the messages that replay it.

    Args:
        turn: The turn.
        mode: How its question's images are replayed (:func:`_question`).
        images: What images cost.
        view: Whether the round offers ``view_file``.

    Returns:
        Its question, each round's calls and results, and its answer.
    """
    messages: list[dict[str, Any]] = [_question(turn, mode, images, view=view)]
    for calls in turn.rounds:
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.tool,
                            "arguments": _cut_arguments(call.arguments, HISTORY_ARGUMENT_CHARS),
                        },
                    }
                    for call in calls
                ],
            }
        )
        messages.extend(
            {"role": "tool", "tool_call_id": call.call_id, "content": cut(call.result, HISTORY_RESULT_CHARS)}
            for call in calls
        )
    messages.append({"role": "assistant", "content": turn.answer})
    return messages


def continuation(this: PastTurn, *, instruct: bool) -> list[dict[str, Any]]:
    """Return the current turn of a continuation: the turn so far, its answer the last assistant message.

    The turn is replayed as an earlier one is (:func:`_replayed`), its text alone: its calls' arguments and
    results in the history form, and no image.

    Args:
        this: The turn whose answer goes on, as the store replays it.
        instruct: Whether a user message asks the model to go on (:data:`~ghostwriter.agent.prompt.CONTINUE`),
            for a provider that does not go on in the last assistant message itself; vllm does.

    Returns:
        The question, each round's calls and results, then the answer so far; with ``instruct``, the instruction
        after it.
    """
    messages = _replayed(this)
    if instruct:
        messages.append({"role": "user", "content": CONTINUE})
    return messages


def _latest_round(messages: Sequence[Mapping[str, Any]]) -> list[int]:
    """Return where the latest round's results are: the tool messages after the last assistant message with calls.

    Args:
        messages: The messages.

    Returns:
        Their places, in order; none before a round has called a tool.
    """
    asked = [
        place for place, message in enumerate(messages) if message["role"] == "assistant" and message.get("tool_calls")
    ]
    if not asked:
        return []
    return [place for place in range(asked[-1] + 1, len(messages)) if messages[place]["role"] == "tool"]


def _copied(message: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a message of the current turn that ``fit`` may change.

    Args:
        message: The message; never changed.

    Returns:
        The copy, with its own list of content parts when it has one.
    """
    content = message.get("content")
    return {**message, "content": list(content)} if isinstance(content, list) else dict(message)


class _Size:
    """The tokens of a message list: its compact JSON's characters, and its images' estimates.

    ``[`` and ``]``, each message's JSON, and a comma between two: the sum is the length of
    :func:`compact` of the whole list, without serialising it again. An image ref counts as
    its :data:`~ghostwriter.agent.images.LABEL` text part in the characters, and as
    :func:`~ghostwriter.agent.images.estimate_image_tokens` in :attr:`image_tokens`; :attr:`images`
    counts the refs.
    """

    def __init__(self, messages: Sequence[Mapping[str, Any]], images: ImageBudget | None = None) -> None:
        self.fixed = None if images is None else images.fixed
        self.count = len(messages)
        self.chars = 0
        self.image_tokens = 0
        self.images = 0
        for message in messages:
            chars, image_tokens, count = self.measure(message)
            self.chars += chars
            self.image_tokens += image_tokens
            self.images += count

    def measure(self, message: Mapping[str, Any]) -> tuple[int, int, int]:
        """Measure one message.

        Args:
            message: The message.

        Returns:
            Its characters, with each ref as its label; its images' tokens; its images.
        """
        found = [part for part in parts_of(message) if is_ref(part)]
        if not found:
            return len(compact(message)), 0, 0
        counted = {**message, "content": [note(p, LABEL) if is_ref(p) else p for p in message["content"]]}
        image_tokens = sum(estimate_image_tokens(r["width"], r["height"], fixed=self.fixed) for r in found)
        return len(compact(counted)), image_tokens, len(found)

    def total(self, messages: Sequence[Mapping[str, Any]]) -> tuple[int, int, int]:
        """Measure messages not yet counted.

        Args:
            messages: The messages.

        Returns:
            Their characters, their images' tokens and their images, as :meth:`measure` counts them.
        """
        sizes = [self.measure(message) for message in messages]
        return sum(s[0] for s in sizes), sum(s[1] for s in sizes), sum(s[2] for s in sizes)

    def tokens(self, *, more: int = 0, chars: int = 0, image_tokens: int = 0) -> int:
        count = self.count + more
        text = math.ceil((2 + self.chars + chars + max(0, count - 1)) / CHARS_PER_TOKEN)
        return text + self.image_tokens + image_tokens

    def replace(self, message: dict[str, Any], index: int, part: dict[str, Any]) -> None:
        """Put a part in place of one of a counted message's parts, keeping the counts in step.

        Args:
            message: The message, one of those counted.
            index: The part's index in its content.
            part: The new part.
        """
        chars, image_tokens, images = self.measure(message)
        message["content"][index] = part
        now = self.measure(message)
        self.chars += now[0] - chars
        self.image_tokens += now[1] - image_tokens
        self.images += now[2] - images


@dataclass
class FitReport:
    """How many earlier turns a round's messages replay, as :func:`fit` fills it.

    Attributes:
        past_turns: The earlier turns there were.
        replayed_turns: Those replayed, the newest of them.
    """

    past_turns: int = 0
    replayed_turns: int = 0

    @property
    def trimmed(self) -> bool:
        """Whether earlier turns were left out."""
        return self.replayed_turns < self.past_turns


def fit(
    system: str,
    tools: list[dict[str, Any]],
    past: Sequence[PastTurn],
    current: list[dict[str, Any]],
    *,
    budget: int = PROMPT_TOKENS,
    images: ImageBudget | None = None,
    view: bool = True,
    report: FitReport | None = None,
) -> list[dict[str, Any]]:
    """Return the messages of a round, within the budget as far as the current turn allows.

    A report, when given, is filled once the replay ends: how many earlier turns there were and how many
    were replayed. It changes nothing of the messages. The loop gives one to the message's first round only,
    and its try without images, whose trim is the chat's (``context_trimmed``).

    Args:
        system: The system prompt.
        tools: The tool schemas the round offers; they count against the budget.
        past: The earlier turns, oldest first.
        current: The current turn: the question, then each round's calls and results, and the
            messages that carry its tool images. Not changed.
        budget: The tokens the messages and the tool schemas may take.
        images: What an image costs and how many a request carries; ``None`` when the model is
            sent no images, and earlier turns are replayed as their text.
        view: Whether the round offers ``view_file``; without it no note names the tool.
        report: Filled with the earlier turns there were and those replayed; ``None`` for none.

    Returns:
        The system message, the earlier turns that fit, and the current turn, in order; their
        image refs still refs (:func:`~ghostwriter.agent.images.materialise` makes them images).

    Raises:
        ValueError: If ``current`` holds an image ref while ``images`` is ``None``.
    """
    if images is None and refs(current):
        raise ValueError("the current turn holds images, but fit was given no image budget")
    fixed = estimate_tokens(tools) if tools else 0
    kept: list[dict[str, Any]] = [{"role": "system", "content": system}, *(_copied(m) for m in current)]
    size = _Size(kept, images)

    def over() -> bool:
        return size.tokens() + fixed > budget or (images is not None and size.images > images.max_images)

    tool_places = ref_places(kept, origin="tool")
    newest = newest_tool_images(kept)

    def give_way(message: dict[str, Any], index: int, template: str, **values: Any) -> None:
        if is_ref(message["content"][index]):
            for sibling in siblings(message, index):
                size.replace(message, sibling, note(message["content"][sibling], template, **values))

    def clear(places: Sequence[int]) -> None:
        for place in places:
            if size.tokens() + fixed <= budget:
                break
            message = kept[place]
            if message["role"] == "tool" and message.get("content") != CLEARED:
                size.chars += len(compact({**message, "content": CLEARED})) - len(compact(message))
                message["content"] = CLEARED

    # With images, the latest round's results are cleared last of all (step 7); without, as any other.
    latest: list[int] = _latest_round(kept) if images is not None else []
    if images is not None:
        for message, index in tool_places:
            if message is newest or not over():
                break
            give_way(message, index, told(RETIRED, view=view))
        for message, index in reversed(ref_places(kept[1:2], origin="attachment")):
            if size.images <= images.max_images:
                break
            give_way(message, index, told(OVER_CAP, view=view), max=images.max_images)
    clear([place for place in range(1, len(kept)) if place not in latest])
    for index, message in enumerate(kept):
        if size.tokens() + fixed <= budget:
            break
        if message["role"] != "assistant" or not message.get("tool_calls"):
            continue
        shortened = {**message, "tool_calls": [_with_cut_arguments(call) for call in message["tool_calls"]]}
        size.chars += len(compact(shortened)) - len(compact(message))
        kept[index] = shortened
    if images is not None:
        for message, index in reversed(ref_places(kept[1:2], origin="attachment")):
            if not over():
                break
            give_way(message, index, told(NO_ROOM, view=view))
        for message, index in tool_places:
            if message is not newest:
                continue
            if not over():
                break
            give_way(message, index, UNSEEN)
        clear(latest)
    if size.tokens() + fixed > budget:
        logger.warning("The current turn alone is over the prompt budget: about {} tokens", size.tokens() + fixed)
    replayed: list[dict[str, Any]] = []
    taken = 0
    for turn in reversed(past):
        modes: tuple[ReplayMode, ...] = ("none",) if images is None or not turn.images else ("images", "notes")
        for mode in modes:
            messages = _replayed(turn, mode=mode, images=images, view=view)
            chars, image_tokens, count = size.total(messages)
            if size.tokens(more=len(messages), chars=chars, image_tokens=image_tokens) + fixed > budget:
                continue
            if mode == "images" and images is not None and size.images + count > images.max_images:
                continue
            size.count += len(messages)
            size.chars += chars
            size.image_tokens += image_tokens
            size.images += count
            replayed[:0] = messages
            taken += 1
            break
        else:
            break
    if report is not None:
        report.past_turns, report.replayed_turns = len(past), taken
    return [kept[0], *replayed, *kept[1:]]


def next_prompt_tokens(
    system: str,
    tools: list[dict[str, Any]],
    past: Sequence[PastTurn],
    *,
    images: ImageBudget | None = None,
) -> int:
    """Estimate the tokens the prompt of the next message starts with.

    The system message and every turn as :func:`fit` replays one, sized as :func:`fit` sizes its messages, plus the
    tool schemas. Nothing is left out for a budget or an image cap: the estimate counts what could no longer be
    sent, so that a chat past its budget shows as past it.

    Args:
        system: The system prompt.
        tools: The tool schemas the next round is offered; none counts none.
        past: The turns, oldest first, the one just finished the last of them.
        images: What an image costs when the turn sees, which counts the pictures a question attached; ``None``
            when it does not, and every turn counts as its text.

    Returns:
        The estimated tokens.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for turn in past:
        mode: ReplayMode = "images" if images is not None and turn.images else "none"
        messages.extend(_replayed(turn, mode=mode, images=images))
    fixed = estimate_tokens(tools) if tools else 0
    return _Size(messages, images).tokens() + fixed


def finished_turn(
    question: str, images: tuple[AttachedImage, ...], current: Sequence[Mapping[str, Any]], answer: str
) -> PastTurn:
    """Return the current turn as the store will replay it once its answer is stored.

    Each round of the turn that called tools is one round of calls, each with the result the model read back; a
    message that carries a tool's images is no part of it, as no tool image is replayed. A call no tool message
    answers is left out, as a step is only stored with its result, and so is a round left with no call.

    Args:
        question: The question as the model reads it: its text and the lines naming its attachments.
        images: The pictures its question attached that are ready to be seen.
        current: The turn's messages as the loop holds them: the question, then each round's calls and results.
        answer: The answer's text, as it is stored.

    Returns:
        The turn, none of its content expired.
    """
    results = {message["tool_call_id"]: message["content"] for message in current if message["role"] == "tool"}
    rounds: list[tuple[PastCall, ...]] = []
    for message in current:
        if message["role"] != "assistant" or not message.get("tool_calls"):
            continue
        calls = tuple(
            PastCall(call["id"], call["function"]["name"], call["function"]["arguments"], results[call["id"]])
            for call in message["tool_calls"]
            if call["id"] in results
        )
        if calls:
            rounds.append(calls)
    return PastTurn(question, tuple(rounds), answer, False, images)
