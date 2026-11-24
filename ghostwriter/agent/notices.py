"""What an answer tells the user of itself, live as a ``notice`` frame and stored with the answer.

A notice is a code (:data:`~ghostwriter.schemas.NoticeCode`) and, for a notice about a file, its details:
workbench's file id, the file's name, and how much of it was read, of how much, in which unit. The live frame
carries every detail set, the name included; what is stored carries no name, which is read from the chat's
attachment row when the answer is served. A notice holds ids and counts, never content.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, TypeGuard, get_args

from ghostwriter.schemas import MeasureUnit, NoticeCode

#: The codes this version knows; a stored notice with another reads as none.
NOTICE_CODES: Final[frozenset[str]] = frozenset(get_args(NoticeCode))
#: The units a length is counted in.
MEASURE_UNITS: Final[frozenset[str]] = frozenset(get_args(MeasureUnit))


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
