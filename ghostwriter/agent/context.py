"""The messages a round sends: the system prompt, earlier turns that fit, and the current turn.

The budget is counted as characters of the messages' compact JSON over ``CHARS_PER_TOKEN``,
the tool schemas included. The current turn is always sent; while it alone is over budget,
its oldest tool results are cleared, one at a time. Earlier turns are then added newest
first, whole, while they fit: each as its question, per round an assistant message with its
calls and one tool message per call, then its answer. A replayed call's arguments stay a
JSON object, which chat templates parse, and are cut to ``HISTORY_ARGUMENT_CHARS``; its
result is cut to ``HISTORY_RESULT_CHARS``.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from loguru import logger

from ghostwriter.agent.limits import CHARS_PER_TOKEN, HISTORY_ARGUMENT_CHARS, HISTORY_RESULT_CHARS, PROMPT_TOKENS
from ghostwriter.agent.tools import CUT_MARK, compact, cut, echoed_arguments, parse_arguments
from ghostwriter.schemas import AttachmentOut

#: What a current tool result reads once it was cleared to make room.
CLEARED: Final = "[cleared to make room; call the tool again if you need it]"


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
    """

    user: str
    rounds: tuple[tuple[PastCall, ...], ...]
    answer: str
    expired: bool


def estimate_tokens(value: str | Sequence[Mapping[str, Any]]) -> int:
    """Estimate the tokens of a text, or of messages as their compact JSON.

    Args:
        value: The text, or the messages.

    Returns:
        Its characters over ``CHARS_PER_TOKEN``, rounded up.
    """
    text = value if isinstance(value, str) else compact(list(value))
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def user_content(text: str, attachments: Sequence[AttachmentOut], *, path_of: Callable[[AttachmentOut], str]) -> str:
    """Return a user's message as the model reads it: the text, then a line per attachment.

    Args:
        text: The message.
        attachments: The files it sent.
        path_of: Where an attachment is in the chat's folder.

    Returns:
        The text and ``[Attached: name (path, size bytes)]``, or ``[Attached: name; expired]``.
    """
    lines = [text] if text else []
    for attachment in attachments:
        if attachment.expired:
            lines.append(f"[Attached: {attachment.name}; expired]")
        else:
            lines.append(f"[Attached: {attachment.name} ({path_of(attachment)}, {attachment.size} bytes)]")
    return "\n".join(lines)


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


def _replayed(turn: PastTurn) -> list[dict[str, Any]]:
    """Return an earlier turn as the messages that replay it.

    Args:
        turn: The turn.

    Returns:
        Its question, each round's calls and results, and its answer.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": turn.user}]
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


class _Size:
    """The characters of a message list's compact JSON, kept as messages are added.

    ``[`` and ``]``, each message's JSON, and a comma between two: the sum is the length of
    :func:`compact` of the whole list, without serialising it again.
    """

    def __init__(self, messages: Sequence[Mapping[str, Any]]) -> None:
        self.count = len(messages)
        self.chars = sum(len(compact(m)) for m in messages)

    def tokens(self, *, more: int = 0, chars: int = 0) -> int:
        count = self.count + more
        return math.ceil((2 + self.chars + chars + max(0, count - 1)) / CHARS_PER_TOKEN)


def fit(
    system: str,
    tools: list[dict[str, Any]],
    past: Sequence[PastTurn],
    current: list[dict[str, Any]],
    *,
    budget: int = PROMPT_TOKENS,
) -> list[dict[str, Any]]:
    """Return the messages of a round, within the budget as far as the current turn allows.

    Args:
        system: The system prompt.
        tools: The tool schemas the round offers; they count against the budget.
        past: The earlier turns, oldest first.
        current: The current turn: the question, then each round's calls and results. Not changed.
        budget: The tokens the messages and the tool schemas may take.

    Returns:
        The system message, the earlier turns that fit, and the current turn, in order.
    """
    fixed = estimate_tokens(tools) if tools else 0
    kept = [{"role": "system", "content": system}, *(dict(m) for m in current)]
    size = _Size(kept)
    for message in kept[1:]:
        if size.tokens() + fixed <= budget:
            break
        if message["role"] == "tool" and message.get("content") != CLEARED:
            size.chars += len(compact({**message, "content": CLEARED})) - len(compact(message))
            message["content"] = CLEARED
    if size.tokens() + fixed > budget:
        logger.warning("The current turn alone is over the prompt budget: about {} tokens", size.tokens() + fixed)
    replayed: list[dict[str, Any]] = []
    for turn in reversed(past):
        messages = _replayed(turn)
        chars = sum(len(compact(m)) for m in messages)
        if size.tokens(more=len(messages), chars=chars) + fixed > budget:
            break
        size.count += len(messages)
        size.chars += chars
        replayed[:0] = messages
    return [kept[0], *replayed, *kept[1:]]
