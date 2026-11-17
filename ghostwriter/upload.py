"""Uploads: the file part of a multipart request read as a stream, and the names and types an attachment may have.

:func:`read_file_part` feeds ``request.stream()`` to python-multipart's ``MultipartParser`` and
returns as soon as the headers of the first part named ``field`` are read; the part's bytes go on
from the same stream as they arrive. Nothing is written to disk or kept whole. A part past
``max_bytes``, or a body past ``max_bytes`` and :data:`MULTIPART_SLACK`, is :class:`TooLarge`; a
body that is no such form, or ends inside the part, is :class:`BadUpload`. The file name is read
as a browser writes it, its ``%22``, ``%0A`` and ``%0D`` undone.

An attachment's type is decided by its extension alone, in any case, through
:data:`ATTACHMENT_TYPES` to the MIME type sent on; its name is :func:`safe_filename`'s, and
:func:`name_problem` refuses what workbench would.
"""

import re
import unicodedata
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Final

from python_multipart import MultipartParser
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import parse_options_header
from starlette.requests import ClientDisconnect, Request

from ghostwriter.body_limit import MULTIPART_SLACK, BodyTooLarge

#: The types an attachment may have, by extension, and the MIME type each is sent on as.
ATTACHMENT_TYPES: Final[dict[str, str]] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".eml": "message/rfc822",
    ".html": "text/html",
    ".htm": "text/html",
}
#: The most attachments a chat may hold before a message sends them.
MAX_PENDING: Final = 10
#: The longest file name, in characters and in UTF-8 bytes, as workbench takes it.
NAME_CHARS: Final = 200
NAME_BYTES: Final = 255

#: A file part's type when it names none.
UNTYPED: Final = "application/octet-stream"
#: The Unicode categories dropped from a name: control and format characters, line and paragraph separators.
_DROPPED: Final = frozenset({"Cc", "Cf", "Zl", "Zp"})
_REPLACED: Final = re.compile(r'["*:<>?|]')
_SEPARATORS: Final = re.compile(r"[/\\]")
_BROWSER_ESCAPES: Final = {"%22": '"', "%0A": "\n", "%0D": "\r"}
_BROWSER_ESCAPE: Final = re.compile("%22|%0A|%0D")


class BadUpload(Exception):
    """The request is not a multipart form with the file part, or ended inside it (400)."""


class TooLarge(Exception):
    """The file part, or the whole body, is past its cap (413)."""


@dataclass(frozen=True)
class FilePart:
    """The file part of a request, its bytes still to come.

    Attributes:
        filename: Its file name as the user had it; ``""`` when it names none.
        content_type: Its ``Content-Type`` as sent, or :data:`UNTYPED`.
        chunks: Its bytes, as they arrive; ends with the part, raises :class:`TooLarge` or
            :class:`BadUpload`, and closes the request's stream when it ends, or is closed after
            it started. :meth:`aclose` closes the stream also when it never started.
    """

    filename: str
    content_type: str
    chunks: AsyncIterator[bytes]
    _body: "_Body" = field(repr=False)

    async def aclose(self) -> None:
        """Close :attr:`chunks` and the request's stream, whether the part was read or not."""
        aclose = getattr(self.chunks, "aclose", None)
        if aclose is not None:
            await aclose()
        await self._body.aclose()


def safe_filename(raw: str) -> str | None:
    """Reduce a file name as a user's system sent it to one safe name.

    It keeps the text after the last ``/`` or backslash, drops control and format characters
    (bidi overrides, zero-width characters) and the line and paragraph separators U+2028 and
    U+2029, replaces ``"*:<>?|`` with ``_``, NFCs it and strips spaces and dots at both ends; the
    stem is cut so the whole stays within :data:`NAME_CHARS` characters, keeping the extension.

    Args:
        raw: The name sent.

    Returns:
        The name, or ``None`` when no stem is left.
    """
    name = _SEPARATORS.split(raw)[-1]
    name = "".join(char for char in name if unicodedata.category(char) not in _DROPPED)
    name = unicodedata.normalize("NFC", _REPLACED.sub("_", name)).rstrip(" .")
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    else:
        extension = dot + extension
    stem = stem.lstrip(" .")[: max(NAME_CHARS - len(extension), 0)]
    return stem + extension if stem else None


def attachment_type(name: str) -> str | None:
    """Return the MIME type an attachment of this name is sent on as, by its extension in any case.

    Args:
        name: A name :func:`safe_filename` returned.

    Returns:
        The type, or ``None`` when the extension is not in :data:`ATTACHMENT_TYPES`.
    """
    stem, dot, extension = name.rpartition(".")
    return ATTACHMENT_TYPES.get(f".{extension.lower()}") if dot and stem else None


def name_problem(name: str) -> str | None:
    """Say why workbench would refuse a file name, so it is never sent to it.

    Args:
        name: The name.

    Returns:
        The message for the user, or ``None`` when the name is allowed.
    """
    if name in {"", ".", ".."} or _SEPARATORS.search(name) or any(ord(char) < 32 for char in name):
        return "The file name must be a single name, without folders."
    if name.endswith(".part"):
        return "A file name may not end in .part."
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError:
        return "The file name cannot be stored."
    if len(name) > NAME_CHARS or len(encoded) > NAME_BYTES:
        return f"The file name is too long; at most {NAME_CHARS} characters and {NAME_BYTES} bytes are allowed."
    return None


def _decoded(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


@dataclass
class _Parts:
    """The parser's callbacks: each part's headers, and the data of the part sought."""

    sought: str
    header_name: bytearray = field(default_factory=bytearray)
    header_value: bytearray = field(default_factory=bytearray)
    disposition: bytes = b""
    content_type: bytes = b""
    found: tuple[str, str] | None = None
    reading: bool = False
    ended: bool = False
    data: deque[bytes] = field(default_factory=deque)

    def on_part_begin(self) -> None:
        self.disposition = self.content_type = b""

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self.header_name += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self.header_value += data[start:end]

    def on_header_end(self) -> None:
        name = bytes(self.header_name).lower()
        if name == b"content-disposition":
            self.disposition = bytes(self.header_value)
        elif name == b"content-type":
            self.content_type = bytes(self.header_value)
        self.header_name.clear()
        self.header_value.clear()

    def on_headers_finished(self) -> None:
        if self.found is not None:
            return
        _, options = parse_options_header(self.disposition)
        name = options.get(b"name")
        if name is None or _decoded(name) != self.sought:
            return
        filename = _BROWSER_ESCAPE.sub(lambda m: _BROWSER_ESCAPES[m.group(0)], _decoded(options.get(b"filename", b"")))
        self.found = (filename, self.content_type.decode("latin-1").strip() or UNTYPED)
        self.reading = True

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self.reading:
            self.data.append(data[start:end])

    def on_part_end(self) -> None:
        if self.reading:
            self.reading, self.ended = False, True


class _Body:
    """The request's stream as the parser is fed from it, counted against the body's cap."""

    def __init__(self, request: Request, parser: MultipartParser, cap: int) -> None:
        self._stream = request.stream()
        self._parser = parser
        self._cap = cap
        self._read = 0

    async def feed(self) -> bool:
        """Feed the parser the next piece of the body.

        Returns:
            ``False`` once the body has ended.

        Raises:
            TooLarge: Past the body's cap.
            BadUpload: When the client went away, or the body is no valid multipart form.
        """
        try:
            chunk = await anext(self._stream)
        except StopAsyncIteration:
            return False
        except ClientDisconnect:
            raise BadUpload("the upload broke off") from None
        except BodyTooLarge:
            raise TooLarge from None
        self._read += len(chunk)
        if self._read > self._cap:
            raise TooLarge
        try:
            self._parser.write(chunk)
        except FormParserError:
            raise BadUpload("invalid multipart form") from None
        return True

    async def aclose(self) -> None:
        await self._stream.aclose()


async def _part_bytes(parts: _Parts, body: _Body, max_bytes: int) -> AsyncGenerator[bytes]:
    """Yield the sought part's bytes as the body arrives.

    Args:
        parts: The parser's state, the part's headers read.
        body: The body.
        max_bytes: The most bytes the part may have.

    Yields:
        The part's bytes.

    Raises:
        TooLarge: Before the bytes that would pass ``max_bytes`` are yielded.
        BadUpload: When the body ends inside the part.
    """
    sent = 0
    try:
        while True:
            while parts.data:
                data = parts.data.popleft()
                sent += len(data)
                if sent > max_bytes:
                    raise TooLarge
                if data:
                    yield data
            if parts.ended:
                return
            if not await body.feed():
                raise BadUpload("the body ended inside the file part")
    finally:
        await body.aclose()


async def read_file_part(request: Request, *, field: str = "file", max_bytes: int) -> FilePart:
    """Read a multipart request up to the headers of its first part named ``field``.

    Args:
        request: The request.
        field: The part's form field.
        max_bytes: The most bytes the part may have; the body may have :data:`MULTIPART_SLACK` more.

    Returns:
        The part, its bytes still to come from the request's stream.

    Raises:
        BadUpload: When the request is no multipart form, or holds no such part.
        TooLarge: When the body passes its cap before the part begins.
    """
    media_type, params = parse_options_header(request.headers.get("content-type"))
    boundary = params.get(b"boundary")
    if media_type != b"multipart/form-data" or not boundary:
        raise BadUpload("not a multipart form")
    parts = _Parts(field)
    try:
        parser = MultipartParser(
            boundary,
            {
                "on_part_begin": parts.on_part_begin,
                "on_header_field": parts.on_header_field,
                "on_header_value": parts.on_header_value,
                "on_header_end": parts.on_header_end,
                "on_headers_finished": parts.on_headers_finished,
                "on_part_data": parts.on_part_data,
                "on_part_end": parts.on_part_end,
            },
        )
    except FormParserError:
        raise BadUpload("invalid boundary") from None
    body = _Body(request, parser, max_bytes + MULTIPART_SLACK)
    try:
        while parts.found is None:
            if not await body.feed():
                raise BadUpload(f"no part named {field}")
    except BaseException:
        await body.aclose()
        raise
    filename, content_type = parts.found
    return FilePart(filename, content_type, _part_bytes(parts, body, max_bytes), body)
