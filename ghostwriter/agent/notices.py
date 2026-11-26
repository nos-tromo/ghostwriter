"""What an answer tells the user of itself, live as a ``notice`` frame and stored with the answer.

A notice is a code (:data:`~ghostwriter.schemas.NoticeCode`) and, for a notice about a file, its details:
workbench's file id, the file's name, and how much of it was read, of how much, in which unit. The live frame
carries every detail set, the name included; what is stored carries no name, which is read from the chat's
attachment row when the answer is served. A notice holds ids and counts, never content.

The notices about a file come from :class:`Reading`, what one message's calls opened and read. A file is opened by
any call workbench answered whose arguments name its full path, at any depth, alone or inside a text: read, viewed,
a template filled, a master or a picture a deck was built with. What a ``read_document`` answer read is the units of
its parts, as the model was given them, a part cut not counted (:func:`covered` joins them), and a page or slide
without text, which was looked at; no other tool reads any of a file. ``document_partial`` names an attachment of
the chat whose parts read cover less than its total: the measure's, or where a part is one unit, the answer's
``total_parts`` (:data:`TOTAL_PARTS_UNITS`). A part cut, or one whose ``units`` are no span, was read by an amount
the units do not say: the notice then names the file alone, with no number, never a wrong one. ``document_unread``
names a ``ready`` attachment of the question, a document or a clip, or a picture while the turn does not see, that no
call opened. Once a call of :data:`BLIND_TOOLS` ran, which reads files without naming what it read, neither is given:
a notice never says what is not known to be true.
"""

from __future__ import annotations

import dataclasses
import json
import posixpath
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, TypeGuard, cast, get_args

from ghostwriter.agent.tools import Refusal
from ghostwriter.schemas import AttachmentKind, AttachmentStatus, MeasureUnit, NoticeCode
from ghostwriter.workbench import ToolFailure, ToolResult

#: The codes this version knows; a stored notice with another reads as none.
NOTICE_CODES: Final[frozenset[str]] = frozenset(get_args(NoticeCode))
#: The units a length is counted in.
MEASURE_UNITS: Final[frozenset[str]] = frozenset(get_args(MeasureUnit))
#: The tools that read the chat's files without naming what they read: once one ran, no file notice is given.
BLIND_TOOLS: Final = frozenset({"run_python", "search_files"})
#: The kinds of ``read_document``'s answer whose ``total_parts`` counts one unit per part, and that unit.
TOTAL_PARTS_UNITS: Final[Mapping[str, MeasureUnit]] = {
    "pdf": "page",
    "pptx": "slide",
    "docx": "paragraph",
    "odt": "paragraph",
    "text": "line",
}
#: The units in which a page or slide without text, looked at, counts as read.
_PAGED_UNITS: Final[frozenset[str]] = frozenset({"page", "slide"})
#: The kinds of attachment a question's answer is told it did not read, sight or not; a picture only without sight.
_UNREAD_KINDS: Final[frozenset[str]] = frozenset({"document", "media"})


@dataclass(frozen=True)
class NoticeEvent:
    """Something the user is told about the answer.

    Attributes:
        code: ``images_dropped``: the model refused a round's images, and the round went again without them;
            ``context_trimmed``: the message's first round left earlier turns out; ``answer_cut``: the last
            round ended at its token cap; ``document_partial``: a file was read only in part;
            ``document_unread``: a file attached to the question was not read.
        file_id: workbench's id of the file the notice is about.
        name: The file's name, for the live frame only; never stored.
        read: How many units of the file were read.
        total: How many units the file has.
        unit: What ``read`` and ``total`` count.
    """

    code: NoticeCode
    file_id: str | None = None
    name: str | None = None
    read: int | None = None
    total: int | None = None
    unit: MeasureUnit | None = None

    def frame(self) -> dict[str, Any]:
        """Return the notice as its live frame carries it.

        Returns:
            The code, and every other field that is set, the name included.
        """
        details = {field.name: getattr(self, field.name) for field in dataclasses.fields(self)}
        return {key: value for key, value in details.items() if key == "code" or value is not None}

    def stored(self) -> dict[str, Any]:
        """Return the notice as it is stored with the answer.

        Returns:
            :meth:`frame` without the name, which is never stored.
        """
        return {key: value for key, value in self.frame().items() if key != "name"}

    @classmethod
    def from_stored(cls, value: Mapping[str, Any]) -> NoticeEvent | None:
        """Read a stored notice back.

        A name is never read from it, as none is ever stored, and keys this version does not know are left out.
        A known code whose details are not all this version's (a file id that is not text, a count that is not a
        whole number of zero or more, a unit not known) keeps what it says without them, as the code alone: the
        form the sweep leaves once the details' deadline has passed, so no wrong number is ever served.

        Args:
            value: The notice, as :meth:`stored` wrote it.

        Returns:
            The notice, or the code alone when a detail is malformed; ``None`` for a value that is not a mapping
            or a code this version does not know.
        """
        if not isinstance(value, Mapping):
            return None
        code, file_id, unit = value.get("code"), value.get("file_id"), value.get("unit")
        read, total = value.get("read"), value.get("total")
        if not _known_code(code):
            return None
        if (
            (file_id is not None and not isinstance(file_id, str))
            or not all(count is None or _count(count) for count in (read, total))
            or (unit is not None and not _known_unit(unit))
        ):
            return cls(code)
        return cls(code, file_id=file_id, read=read, total=total, unit=unit)


def _known_code(value: object) -> TypeGuard[NoticeCode]:
    """Tell whether a stored value is a code this version knows.

    Args:
        value: The value.

    Returns:
        Whether it is one of :data:`NOTICE_CODES`.
    """
    return isinstance(value, str) and value in NOTICE_CODES


def _known_unit(value: object) -> TypeGuard[MeasureUnit]:
    """Tell whether a stored value is a unit this version knows.

    Args:
        value: The value.

    Returns:
        Whether it is one of :data:`MEASURE_UNITS`; a value that is not text, a list among them, is none.
    """
    return isinstance(value, str) and value in MEASURE_UNITS


def _count(value: object) -> bool:
    """Tell whether a stored value is a count.

    Args:
        value: The value.

    Returns:
        Whether it is a whole number of zero or more, and not a boolean.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class SentFile:
    """An attachment of the chat, as the notices about it read it.

    Attributes:
        file_id: workbench's id of the file.
        path: Its path in the chat's folder.
        name: The file name.
        kind: ``document``, ``image`` or ``media``.
        status: ``processing``, ``ready`` or ``failed``.
        total: How many units the measure counted, while it is ``measured`` and the file has not expired; else
            ``None``.
        unit: What ``total`` counts, on the same terms.
    """

    file_id: str
    path: str
    name: str
    kind: AttachmentKind
    status: AttachmentStatus
    total: int | None
    unit: MeasureUnit | None


def to_open(attached: Sequence[SentFile], *, sees: bool) -> list[SentFile]:
    """Return the attachments of a question the model reads only by calling a tool.

    Args:
        attached: The question's attachments, in order.
        sees: Whether the turn sees: then a picture is in the question as an image, and needs no call.

    Returns:
        Each ``ready`` document or clip, and each ``ready`` picture while the turn does not see, in order.
    """
    return [
        sent
        for sent in attached
        if sent.status == "ready" and (sent.kind in _UNREAD_KINDS or (sent.kind == "image" and not sees))
    ]


def covered(spans: Iterable[tuple[int, int]]) -> int:
    """Count the units a set of spans covers, each once.

    Args:
        spans: First and last unit of each span, inclusive, the first not after the last.

    Returns:
        How many units are in at least one span.
    """
    count, end = 0, 0
    for first, last in sorted(spans):
        if last > end:
            count += last - max(first, end + 1) + 1
            end = last
    return count


@dataclass
class _Read:
    """What the ``read_document`` answers for one path read.

    Attributes:
        spans: The units of each part given to the model that was not cut and whose ``units`` are a span.
        blank: The pages or slides the answers named without text.
        kind: The kind the last answer named.
        total_parts: The ``total_parts`` the last answer named.
        units: Whether any part carried ``units``, as workbench 0.10's do.
        uncounted: Whether a part given to the model was cut, or carried no span as ``units`` (``null`` or not two
            whole numbers). What was read of it is then not known to the unit, so no number is told of the file.
    """

    spans: list[tuple[int, int]] = field(default_factory=list)
    blank: set[int] = field(default_factory=set)
    kind: str | None = None
    total_parts: int | None = None
    units: bool = False
    uncounted: bool = False

    def length(self, sent: SentFile) -> tuple[int, MeasureUnit] | None:
        """Return how long the file is, and in which unit.

        Args:
            sent: The attachment read.

        Returns:
            The measure's total and unit; else the answer's ``total_parts`` where a part is one unit
            (:data:`TOTAL_PARTS_UNITS`); else ``None``.
        """
        if sent.total is not None and sent.unit is not None:
            return sent.total, sent.unit
        unit = TOTAL_PARTS_UNITS.get(self.kind) if self.kind is not None else None
        if unit is not None and self.total_parts is not None:
            return self.total_parts, unit
        return None


class Reading:
    """What one message's calls opened, and what of it was read."""

    def __init__(self) -> None:
        """Start with nothing opened."""
        self._blind = False
        self._opened: set[str] = set()
        self._said: list[str] = []
        self._read: dict[str, _Read] = {}

    def call(
        self,
        tool: str,
        arguments: Mapping[str, Any] | None,
        outcome: ToolResult | ToolFailure | Refusal,
        content: str,
    ) -> None:
        """Record one call of the message.

        A call of :data:`BLIND_TOOLS` that was sent, answered or not, ends every notice of the message. Any other call
        workbench answered opens every path its arguments name (:func:`_texts`), alone or inside a text, as a picture
        written ``{image: <path>}`` or a block's JSON is; a ``read_document`` answer also opens its own ``path`` and
        records what it read, from ``content``'s first line, the JSON the model was given.

        Args:
            tool: The tool called.
            arguments: Its arguments, as the model wrote them; ``None`` for arguments that are not an object.
            outcome: What workbench answered, or why the call was not sent.
            content: What the model read back, as the loop wrote it (:func:`~ghostwriter.agent.tools.tool_message`).
        """
        if tool in BLIND_TOOLS and not isinstance(outcome, Refusal):
            self._blind = True
            return
        if not isinstance(outcome, ToolResult):
            return
        texts = _texts(arguments)
        normalised = {posixpath.normpath(text) for text in texts}
        self._opened.update(normalised)
        self._said.extend({*texts, *normalised})
        if tool == "read_document":
            self._answer(_path(arguments.get("path") if arguments is not None else None), content)

    def _answer(self, asked: str | None, content: str) -> None:
        """Record what a ``read_document`` answer read.

        Args:
            asked: The path the call named, normalised; ``None`` for none.
            content: What the model read back.
        """
        try:
            body = json.loads(content.split("\n", 1)[0])
        except (ValueError, RecursionError):
            return
        if not isinstance(body, dict):
            return
        answer = cast(dict[str, Any], body)
        path = _path(answer.get("path")) or asked
        if path is None:
            return
        self._opened.add(path)
        read = self._read.setdefault(path, _Read())
        kind, total_parts = answer.get("kind"), answer.get("total_parts")
        if isinstance(kind, str):
            read.kind = kind
        if _count(total_parts):
            read.total_parts = total_parts
        parts = answer.get("parts")
        for part in cast(list[Any], parts) if isinstance(parts, list) else []:
            if not isinstance(part, dict):
                continue
            piece = cast(dict[str, Any], part)
            read.units = read.units or "units" in piece
            span, cut = _span(piece.get("units")), piece.get("cut") is True
            if span is None or cut:
                read.uncounted = True
            else:
                read.spans.append(span)
        blank = answer.get("pages_without_text")
        if isinstance(blank, list):
            read.blank.update(n for n in cast(list[Any], blank) if _count(n) and n > 0)

    def partial(self, files: Mapping[str, SentFile]) -> list[NoticeEvent]:
        """Return a ``document_partial`` for each attachment of the chat read only in part.

        Args:
            files: Every attachment of the chat, by path.

        Returns:
            Per path read, in the order first read: a notice when it is an attachment of the chat, an answer for it
            carried ``units``, its length is known (:meth:`_Read.length`) and the units read, a page or slide without
            text among them and none past the length counted, are fewer. The notice says how many of how many, in
            which unit, unless a part given to the model was cut or had no ``units`` span (:attr:`_Read.uncounted`):
            then what was read is not known to the unit, and it carries the file alone. None once a blind tool ran.
        """
        if self._blind:
            return []
        by_path = {posixpath.normpath(path): sent for path, sent in files.items()}
        notices: list[NoticeEvent] = []
        for path, read in self._read.items():
            sent = by_path.get(path)
            length = read.length(sent) if sent is not None and read.units else None
            if sent is None or length is None:
                continue
            total, unit = length
            spans = [*read.spans, *((n, n) for n in read.blank)] if unit in _PAGED_UNITS else read.spans
            count = covered((first, min(last, total)) for first, last in spans if first <= total)
            if count >= total:
                continue
            if read.uncounted:
                notices.append(NoticeEvent("document_partial", sent.file_id, sent.name))
            else:
                notices.append(NoticeEvent("document_partial", sent.file_id, sent.name, count, total, unit))
        return notices

    def unread(self, attached: Sequence[SentFile], *, sees: bool) -> list[NoticeEvent]:
        """Return a ``document_unread`` for each attachment of the question no call opened.

        Args:
            attached: The question's attachments, in order.
            sees: Whether the turn saw as it began: then a picture went to the model as an image, and is not counted.

        Returns:
            A notice per ``ready`` document or clip, or picture while the turn does not see, whose path no call
            workbench answered named (:meth:`_used`); none once a blind tool ran.
        """
        if self._blind:
            return []
        return [
            NoticeEvent("document_unread", file_id=sent.file_id, name=sent.name)
            for sent in to_open(attached, sees=sees)
            if not self._used(sent.path)
        ]

    def _used(self, path: str) -> bool:
        """Tell whether a call workbench answered named a file.

        Args:
            path: The file's path.

        Returns:
            Whether its full path, normalised, was opened, or is inside a text an answered call's arguments held, as
            written or normalised: a match that only ever takes a notice away.
        """
        full = posixpath.normpath(path)
        return full in self._opened or any(full in text for text in self._said)


def _path(value: object) -> str | None:
    """Read a path a call named or an answer gave.

    Args:
        value: The value.

    Returns:
        It normalised (:func:`posixpath.normpath`), as workbench reads it; ``None`` for anything but a non-empty text.
    """
    return posixpath.normpath(value) if isinstance(value, str) and value else None


def _texts(arguments: Mapping[str, Any] | None) -> list[str]:
    """Return every text a call's arguments hold, each a path it may name or hold.

    Args:
        arguments: The arguments, as the model wrote them; ``None`` for none.

    Returns:
        Each non-empty text among their values, in objects and lists at any depth, as written.
    """
    texts: list[str] = []
    values: list[Any] = [arguments] if arguments is not None else []
    while values:
        value = values.pop()
        if isinstance(value, Mapping):
            values.extend(cast(Mapping[str, Any], value).values())
        elif isinstance(value, list):
            values.extend(cast(list[Any], value))
        elif isinstance(value, str) and value:
            texts.append(value)
    return texts


def _span(value: object) -> tuple[int, int] | None:
    """Read a part's ``units``.

    Args:
        value: The value.

    Returns:
        ``(first, last)`` for two whole numbers from 1, the first not after the last; else ``None``.
    """
    if not isinstance(value, list) or len(cast(list[Any], value)) != 2:
        return None
    first, last = cast(list[Any], value)
    if not (_count(first) and _count(last)) or not 1 <= first <= last:
        return None
    return first, last
