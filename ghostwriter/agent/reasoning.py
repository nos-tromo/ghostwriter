"""Strip reasoning from a reply while it streams.

:class:`ReasoningStripper` and the helpers it uses are copied unchanged from docint
(``docint/utils/llm_sanitize.py``). Think blocks in the content (paired, or left open at the
end) are read out and never shown; Harmony channels are not read. The text has no length
limit and can carry a user's own words, so each scan stays linear in its length.
"""

from __future__ import annotations

import re
from typing import Literal

# An open tag runs from ``<think`` to the first ``>`` after it: see :func:`_find_open_think`.
_OPEN_THINK_START_RE: re.Pattern[str] = re.compile(
    r"<think\b",
    re.IGNORECASE,
)
_CLOSE_THINK_RE: re.Pattern[str] = re.compile(
    r"</think\s*>",
    re.IGNORECASE,
)


def _find_open_think(text: str, pos: int = 0) -> tuple[int, int] | None:
    """Find the first ``<think ...>`` open tag at or after *pos*.

    The tag runs from ``<think`` to the first ``>`` after it. When no ``>``
    follows the first ``<think``, none follows a later one either, so one
    look settles it; a regex search would scan to the end again from every
    later ``<think``.

    Args:
        text (str): The text to search.
        pos (int): Where to start.

    Returns:
        tuple[int, int] | None: The tag's start and end, or ``None`` when there is none.
    """
    start = _OPEN_THINK_START_RE.search(text, pos)
    if start is None:
        return None
    bracket = text.find(">", start.end())
    if bracket < 0:
        return None
    return start.start(), bracket + 1


# A tail that could still grow into an open or a close tag, so a stream holds it back.
_OPEN_THINK_TAIL_RE: re.Pattern[str] = re.compile(r"<(?:t(?:h(?:i(?:n(?:k(?:[^\w>][^>]*)?)?)?)?)?)?\Z", re.IGNORECASE)
_CLOSE_THINK_TAIL_RE: re.Pattern[str] = re.compile(r"<(?:/(?:t(?:h(?:i(?:n(?:k\s*)?)?)?)?)?)?\Z", re.IGNORECASE)


def _tail_start(text: str, tail: re.Pattern[str]) -> int:
    """Return where a tag that may still be forming begins at the end of *text*.

    Args:
        text (str): The text held so far.
        tail (re.Pattern[str]): The partial-tag pattern to look for.

    Returns:
        int: Its start, or ``len(text)`` when the text cannot end inside a tag.
    """
    match = tail.search(text)
    return match.start() if match else len(text)


class ReasoningStripper:
    """Strip reasoning from a reply while it streams: the streaming counterpart of :func:`strip_reasoning`.

    Feed the reply's content a piece at a time; each call returns the visible text that piece
    completes. Held back is only what may still turn out otherwise: a tail that could grow
    into a tag, trailing whitespace, and, for a reply that may open in reasoning, everything up
    to its first close tag. Joined, the returned text is ``strip_reasoning(reply)[0]`` and
    :attr:`reasoning` its second value, for paired blocks, a block left open at the end, and,
    when the reply may open in reasoning, a close tag with no open tag before it.

    Harmony channels are not read. Without ``reasoning_open``, a close tag with no open tag
    before it is passed on as text: the text before it may already have been shown.
    """

    def __init__(self, *, reasoning_open: bool = False) -> None:
        """Start before any content.

        Args:
            reasoning_open (bool): Whether the reply may open inside a think block the chat
                template began in the prompt, so that only a close tag shows where it ends.
        """
        self._state: Literal["held", "text", "think"] = "held" if reasoning_open else "text"
        self._buffer = ""
        self._scan = 0
        self._think = ""
        self._paired: list[str] = []
        self._dangling = ""
        self._unclosed = ""
        self._started = False
        self._space = ""

    @property
    def reasoning(self) -> str | None:
        """Return the reasoning read so far, as :func:`strip_reasoning` joins it.

        Returns:
            str | None: Paired blocks in order, then the text before a close tag with no open
            tag, then a block left open; each stripped, joined by newlines. ``None`` when empty.
        """
        return "\n".join(part for part in (*self._paired, self._dangling, self._unclosed) if part) or None

    def feed(self, text: str) -> str:
        """Accept the next piece of the reply.

        Args:
            text (str): The piece.

        Returns:
            str: The visible text it completes; empty while all of it is held.
        """
        self._buffer += text
        return self._drain()

    def cancel_open_reasoning(self) -> str:
        """Read held content as ordinary text, because the endpoint sends reasoning apart from it.

        Returns:
            str: The visible text the held content yields; empty when nothing was held.
        """
        if self._state != "held":
            return ""
        self._state = "text"
        return self._drain()

    def flush(self) -> str:
        """Release what is still held once the reply has ended.

        A reply still waiting for a close tag never opened in reasoning, so its held content is
        read as text; a block left open is reasoning; a tail that never became a tag is text;
        trailing whitespace is dropped.

        Returns:
            str: The visible text that remained.
        """
        out = self.cancel_open_reasoning()
        if self._state == "think":
            self._unclosed = (self._think + self._buffer).strip()
            self._think = ""
        else:
            out += self._visible(self._buffer)
        self._buffer = self._space = ""
        return out

    def _drain(self) -> str:
        """Read the buffer as far as it can be read.

        Returns:
            str: The visible text read.
        """
        out: list[str] = []
        while self._buffer:
            if self._state == "held":
                close = _CLOSE_THINK_RE.search(self._buffer, self._scan)
                if close is None:
                    # A close tag can only begin at the last "<" or after it.
                    last = self._buffer.rfind("<")
                    self._scan = last if last >= 0 else len(self._buffer)
                    break
                self._end_held(self._buffer[: close.start()])
                self._buffer, self._state = self._buffer[close.end() :], "text"
            elif self._state == "think":
                close = _CLOSE_THINK_RE.search(self._buffer)
                if close is None:
                    cut = _tail_start(self._buffer, _CLOSE_THINK_TAIL_RE)
                    self._think += self._buffer[:cut]
                    self._buffer = self._buffer[cut:]
                    break
                body = (self._think + self._buffer[: close.start()]).strip()
                if body:
                    self._paired.append(body)
                self._think = ""
                self._buffer, self._state = self._buffer[close.end() :], "text"
            else:
                opening = _find_open_think(self._buffer)
                if opening is None:
                    cut = _tail_start(self._buffer, _OPEN_THINK_TAIL_RE)
                    out.append(self._visible(self._buffer[:cut]))
                    self._buffer = self._buffer[cut:]
                    break
                out.append(self._visible(self._buffer[: opening[0]]))
                self._buffer, self._state = self._buffer[opening[1] :], "think"
        return "".join(out)

    def _end_held(self, held: str) -> None:
        """Record what came before the first close tag of a reply that opened in reasoning.

        Args:
            held (str): The held content. If the model wrote its own open tag in it, the tag
                is no part of the reasoning and the block counts as a paired one.
        """
        opening = _find_open_think(held)
        if opening is None:
            self._dangling = held.strip()
            return
        body = (held[: opening[0]] + held[opening[1] :]).strip()
        if body:
            self._paired.append(body)

    def _visible(self, text: str) -> str:
        """Pass visible text on, dropping the reply's leading whitespace and holding trailing whitespace.

        Args:
            text (str): Visible text just read.

        Returns:
            str: What may be shown now.
        """
        if not self._started:
            text = text.lstrip()
            if not text:
                return ""
            self._started = True
        text = self._space + text
        shown = text.rstrip()
        self._space = text[len(shown) :]
        return shown
