"""The messages a round sends: the system prompt, earlier turns that fit, and the current turn.

The budget is counted as characters of the messages' compact JSON over ``CHARS_PER_TOKEN``,
the tool schemas included. The current turn is always sent; while it alone is over budget,
its oldest tool results are cleared, one at a time, and then, still over, its oldest calls'
arguments are cut as a replayed call's are. Earlier turns are then added newest
first, whole, while they fit: each as its question, per round an assistant message with its
calls and one tool message per call, then its answer. A replayed call's arguments stay a
JSON object, which chat templates parse, and are cut to ``HISTORY_ARGUMENT_CHARS``; its
result is cut to ``HISTORY_RESULT_CHARS``.

With an :class:`~ghostwriter.agent.images.ImageBudget`, an image ref counts as its label's
characters plus :func:`~ghostwriter.agent.images.estimate_image_tokens`, and a request carries
at most ``max_images``. While the current turn is over the budget or the cap, the tool images
of its earlier rounds retire first, oldest first; past the cap, the question's own images give
way, last first, so the newest tool images (the last message that holds any), which the model
has not seen yet, stay within the cap. Then tool results are cleared and arguments cut, and only
while still over do the newest tool images give way, oldest first: the images the model just
asked for outrank text it has already read. Each image leaves a note in its place, and a
picture's strips give way together. An earlier turn with images is replayed with them while the
budget and the cap allow, else with a note per image that ``view_file`` shows it; a tool image
is never replayed. Without an image budget, an earlier turn is replayed as its text, as before,
and the current turn must hold no image ref.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from loguru import logger

from ghostwriter.agent.images import (
    LABEL,
    NOT_RESENT,
    OVER_CAP,
    RETIRED,
    UNSEEN,
    AttachedImage,
    ImageBudget,
    attachment_parts,
    estimate_image_tokens,
    is_ref,
    note,
    parts_of,
    ref_places,
    refs,
    rendition_size,
    siblings,
)
from ghostwriter.agent.limits import CHARS_PER_TOKEN, HISTORY_ARGUMENT_CHARS, HISTORY_RESULT_CHARS, PROMPT_TOKENS
from ghostwriter.agent.tools import CUT_MARK, compact, cut, echoed_arguments, parse_arguments
from ghostwriter.schemas import AttachmentOut

#: What a current tool result reads once it was cleared to make room.
CLEARED: Final = "[cleared to make room; call the tool again if you need it]"
#: How an earlier turn is replayed: with its images, with a note per image, or as its text alone.
ReplayMode = Literal["images", "notes", "none"]
#: The most pages without text an attachment's line lists.
LISTED_PAGES: Final = 10


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


def attachment_facts(kind: str, manifest: Mapping[str, Any] | None, *, seen: bool) -> str | None:
    """Say what an attachment is, from what workbench reported of it.

    Args:
        kind: The attachment's kind: ``document``, ``image`` or ``media``.
        manifest: Its manifest as the chat keeps it, or ``None``.
        seen: Whether the model sees images.

    Returns:
        For a picture ``image`` and its first rendition's size, followed by that the model does
        not see it when not ``seen``; for a PDF its pages, and those without text: at most
        :data:`LISTED_PAGES` of them and how many more, or that no page has text when that holds
        for more than one page; else ``None``.
    """
    if kind == "image":
        size = rendition_size(manifest)
        facts = "image" if size is None else f"image {size[0]}\N{MULTIPLICATION SIGN}{size[1]}"
        return facts if seen else f"{facts}; not shown: this model does not take images"
    if manifest is None or manifest.get("kind") != "pdf":
        return None
    total = manifest.get("total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 1:
        return None
    facts = "PDF, 1 page" if total == 1 else f"PDF, {total} pages"
    listed = manifest.get("pages_without_text")
    blank: list[int] = [page for page in listed if isinstance(page, int)] if isinstance(listed, list) else []
    if total > 1 and set(blank) >= set(range(1, total + 1)):
        return f"{facts}; no page has text"
    if len(blank) == 1:
        return f"{facts}; page {blank[0]} has no text"
    if not blank:
        return facts
    pages = ", ".join(str(page) for page in blank[:LISTED_PAGES])
    more = f", and {len(blank) - LISTED_PAGES} more" if len(blank) > LISTED_PAGES else ""
    return f"{facts}; pages {pages}{more} have no text"


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


def _question(turn: PastTurn, mode: ReplayMode, images: ImageBudget | None) -> dict[str, Any]:
    """Return an earlier turn's question as it is replayed.

    Args:
        turn: The turn.
        mode: ``images``: the text and its images' refs; ``notes``: the text and a
            :data:`~ghostwriter.agent.images.NOT_RESENT` line per image; ``none``: the text.
        images: What images cost; read in ``images`` mode, for the strips.

    Returns:
        The user's message.
    """
    if mode == "images":
        fixed = None if images is None else images.fixed
        parts = attachment_parts(turn.images, fixed=fixed)
        return {"role": "user", "content": [{"type": "text", "text": turn.user}, *parts]}
    if mode == "notes":
        lines = [turn.user, *(NOT_RESENT.format(label=image.name) for image in turn.images)]
        return {"role": "user", "content": "\n".join(line for line in lines if line)}
    return {"role": "user", "content": turn.user}


def _replayed(turn: PastTurn, *, mode: ReplayMode = "none", images: ImageBudget | None = None) -> list[dict[str, Any]]:
    """Return an earlier turn as the messages that replay it.

    Args:
        turn: The turn.
        mode: How its question's images are replayed (:func:`_question`).
        images: What images cost.

    Returns:
        Its question, each round's calls and results, and its answer.
    """
    messages: list[dict[str, Any]] = [_question(turn, mode, images)]
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


def fit(
    system: str,
    tools: list[dict[str, Any]],
    past: Sequence[PastTurn],
    current: list[dict[str, Any]],
    *,
    budget: int = PROMPT_TOKENS,
    images: ImageBudget | None = None,
) -> list[dict[str, Any]]:
    """Return the messages of a round, within the budget as far as the current turn allows.

    Args:
        system: The system prompt.
        tools: The tool schemas the round offers; they count against the budget.
        past: The earlier turns, oldest first.
        current: The current turn: the question, then each round's calls and results, and the
            messages that carry its tool images. Not changed.
        budget: The tokens the messages and the tool schemas may take.
        images: What an image costs and how many a request carries; ``None`` when the model is
            sent no images, and earlier turns are replayed as their text.

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
    newest = tool_places[-1][0] if tool_places else None

    def give_way(message: dict[str, Any], index: int, template: str, **values: Any) -> None:
        if is_ref(message["content"][index]):
            for sibling in siblings(message, index):
                size.replace(message, sibling, note(message["content"][sibling], template, **values))

    if images is not None:
        for message, index in tool_places:
            if message is newest or not over():
                break
            give_way(message, index, RETIRED)
        for message, index in reversed(ref_places(kept[1:2], origin="attachment")):
            if size.images <= images.max_images:
                break
            give_way(message, index, OVER_CAP, max=images.max_images)
    for message in kept[1:]:
        if size.tokens() + fixed <= budget:
            break
        if message["role"] == "tool" and message.get("content") != CLEARED:
            size.chars += len(compact({**message, "content": CLEARED})) - len(compact(message))
            message["content"] = CLEARED
    for index, message in enumerate(kept):
        if size.tokens() + fixed <= budget:
            break
        if message["role"] != "assistant" or not message.get("tool_calls"):
            continue
        shortened = {**message, "tool_calls": [_with_cut_arguments(call) for call in message["tool_calls"]]}
        size.chars += len(compact(shortened)) - len(compact(message))
        kept[index] = shortened
    if images is not None:
        for message, index in tool_places:
            if message is not newest:
                continue
            if not over():
                break
            give_way(message, index, UNSEEN)
    if size.tokens() + fixed > budget:
        logger.warning("The current turn alone is over the prompt budget: about {} tokens", size.tokens() + fixed)
    replayed: list[dict[str, Any]] = []
    for turn in reversed(past):
        modes: tuple[ReplayMode, ...] = ("none",) if images is None or not turn.images else ("images", "notes")
        for mode in modes:
            messages = _replayed(turn, mode=mode, images=images)
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
            break
        else:
            break
    return [kept[0], *replayed, *kept[1:]]
