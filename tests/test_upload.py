"""Upload helpers: safe file names, and a multipart file part read as a stream.

Every name, id and value here is invented.
"""

from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from starlette.types import Message
from typing_extensions import override

from ghostwriter.upload import (
    IMAGE_TYPES,
    PREPARED_SUFFIXES,
    BadUpload,
    TooLarge,
    attachment_kind,
    attachment_type,
    name_problem,
    needs_prepare,
    read_file_part,
    safe_filename,
)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BOUNDARY = "gwtest"
KIB = 1024


class TrackedRequest(Request):
    """A request that tells when its body stream was closed.

    Attributes:
        closed: Whether the stream's ``finally`` ran.
    """

    closed: bool = False

    @override
    async def stream(self) -> AsyncGenerator[bytes]:
        """Yield the body as Starlette does, noting when the stream ends or is closed.

        Yields:
            The body's pieces.
        """
        try:
            async for chunk in super().stream():
                yield chunk
        finally:
            self.closed = True


class Body:
    """A request body handed to the app in pieces, as a server receives it.

    Attributes:
        pieces: The pieces not received yet.
        received: How many pieces were received so far.
    """

    def __init__(self, body: bytes, piece: int) -> None:
        """Cut the body into pieces.

        Args:
            body: The whole body.
            piece: The size of each piece.
        """
        self.pieces = [body[i : i + piece] for i in range(0, len(body), piece)]
        self.total = len(self.pieces)
        self.received = 0

    async def receive(self) -> Message:
        """Hand out the next piece.

        Returns:
            The ASGI message.
        """
        body = self.pieces.pop(0) if self.pieces else b""
        self.received += 1
        return {"type": "http.request", "body": body, "more_body": bool(self.pieces)}


def _form(name: str, data: bytes, mime: str = XLSX) -> tuple[bytes, str]:
    """Render a multipart body with one file field, as httpx writes it."""
    request = httpx.Request("POST", "http://upload.test", files={"file": (name, data, mime)})
    return request.read(), request.headers["content-type"]


def _request(body: bytes, content_type: str, *, piece: int = 64 * KIB) -> tuple[TrackedRequest, Body]:
    fed = Body(body, piece)
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/api/chats/c-1/attachments",
        "query_string": b"",
        "headers": [(b"content-type", content_type.encode("latin-1"))],
    }
    return TrackedRequest(scope, fed.receive), fed


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("../../x/report.pdf", "report.pdf"),
        ("C:\\Users\\x\\Bericht März.PDF", "Bericht März.PDF"),
        ("re\u0301sume\u0301.docx", "résumé.docx"),
        ('Q3 "final".xlsx', "Q3 _final_.xlsx"),
        ("line\nbreak.csv", "linebreak.csv"),
        ("in\u202evoice\u200b.pdf", "invoice.pdf"),
        ("line\u2028sep\u2029arated.txt", "lineseparated.txt"),
        (" .hidden notes.txt. ", "hidden notes.txt"),
        ("..", None),
        (".pdf", None),
        ("", None),
    ],
)
def test_upload_names_are_reduced_to_a_safe_basename(raw: str, safe: str | None) -> None:
    """Review Focus 1: a basename, NFC, control and format characters dropped, ``"*:<>?|`` replaced, ends stripped."""
    assert safe_filename(raw) == safe


def test_a_long_name_keeps_its_extension() -> None:
    """A name cut to 200 characters keeps its extension."""
    safe = safe_filename("x" * 300 + ".pdf")
    assert safe is not None and len(safe) == 200 and safe.endswith(".pdf")


async def test_the_file_part_streams_without_being_kept_whole() -> None:
    """3 MiB fed in 64 KiB pieces: the first bytes come out before the body has all been received."""
    data = bytes(range(256)) * (3 * KIB * KIB // 256)
    body, content_type = _form("sales.xlsx", data)
    request, fed = _request(body, content_type)
    part = await read_file_part(request, max_bytes=50 * KIB * KIB)
    assert (part.filename, part.content_type) == ("sales.xlsx", XLSX)
    got = bytearray()
    async for chunk in part.chunks:
        if not got:
            assert fed.received < fed.total
        got += chunk
    assert bytes(got) == data and fed.received == fed.total


async def test_a_part_closed_unread_closes_the_request_body_at_once() -> None:
    """The route's 415 and 422 close the part unread; the request's stream closes then, not once collected."""
    body, content_type = _form("tool.exe", b"\x00" * (2 * KIB))
    request, _ = _request(body, content_type, piece=256)
    part = await read_file_part(request, max_bytes=KIB)
    assert not request.closed
    await part.aclose()
    assert request.closed


async def test_a_body_past_the_cap_stops_reading() -> None:
    """A part past ``max_bytes`` raises TooLarge, and the rest of the body is never asked for."""
    body, content_type = _form("sales.xlsx", b"\x00" * (2 * KIB))
    request, fed = _request(body, content_type, piece=256)
    part = await read_file_part(request, max_bytes=KIB)
    with pytest.raises(TooLarge):
        async for _ in part.chunks:
            pass
    assert fed.received < fed.total


async def test_a_body_without_the_file_field_is_refused() -> None:
    """A form with only a field ``x`` holds no file part."""
    body = f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="x"\r\n\r\n1\r\n--{BOUNDARY}--\r\n'.encode()
    request, _ = _request(body, f"multipart/form-data; boundary={BOUNDARY}")
    with pytest.raises(BadUpload):
        await read_file_part(request, max_bytes=KIB)


async def test_a_body_that_is_not_a_multipart_form_is_refused() -> None:
    """JSON, or a multipart type without its boundary, is no upload."""
    for content_type in ("application/json", "multipart/form-data"):
        request, _ = _request(b'{"file": "x"}', content_type)
        with pytest.raises(BadUpload):
            await read_file_part(request, max_bytes=KIB)


async def test_a_file_part_cut_short_is_refused() -> None:
    """A body that ends inside the file part is never taken for the whole file."""
    body, content_type = _form("sales.xlsx", b"\x00" * (2 * KIB))
    request, _ = _request(body[:-100], content_type, piece=512)
    part = await read_file_part(request, max_bytes=50 * KIB)
    with pytest.raises(BadUpload):
        async for _ in part.chunks:
            pass


async def test_a_browsers_escapes_in_the_file_name_are_undone() -> None:
    """Browsers send ``"``, LF and CR in a file name as ``%22``, ``%0A`` and ``%0D``; the name is read as it was."""
    body = (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="Q3 %22final%22%0A.xlsx"\r\n'
        f"Content-Type: {XLSX}\r\n\r\nPK\r\n--{BOUNDARY}--\r\n"
    ).encode()
    request, _ = _request(body, f"multipart/form-data; boundary={BOUNDARY}")
    part = await read_file_part(request, max_bytes=KIB)
    assert part.filename == 'Q3 "final"\n.xlsx' and safe_filename(part.filename) == "Q3 _final_.xlsx"
    assert [chunk async for chunk in part.chunks] == [b"PK"]


@pytest.mark.parametrize("name", ["ä" * 150 + ".pdf", "report.pdf.part", "a/b.pdf"])
def test_a_name_over_255_bytes_or_ending_in_part_is_a_problem(name: str) -> None:
    """Workbench refuses these names, so none is sent to it; a long name with umlauts and spaces is fine."""
    assert isinstance(name_problem(name), str)
    assert name_problem("Übersicht März #1.xlsx") is None


def test_images_are_attachable_only_when_offered() -> None:
    """A picture's type is known only while images are offered, in any case; ``.svg`` is never one."""
    assert attachment_type("photo.HEIC") is None
    assert attachment_type("photo.HEIC", images=True) == "image/heic"
    assert attachment_type("quay-scan.PDF") == "application/pdf"
    assert attachment_type("quay-scan.pdf", images=True) == "application/pdf"
    assert attachment_type("drawing.svg") is None
    assert attachment_type("drawing.svg", images=True) is None
    assert attachment_type(".png", images=True) is None
    assert attachment_type("png", images=True) is None


def test_the_image_types_are_the_spec_s() -> None:
    """Ten suffixes, each on its MIME type; ``.jpg`` and ``.jpeg``, ``.tif`` and ``.tiff`` share one."""
    assert IMAGE_TYPES == {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }
    assert PREPARED_SUFFIXES == frozenset({".pdf", *IMAGE_TYPES})


@pytest.mark.parametrize(
    ("name", "kind", "prepared"),
    [
        ("quay-scan.pdf", "document", True),
        ("deck.pptx", "document", False),
        ("harbour-photo.jpg", "image", True),
        ("Harbour-Photo.JPEG", "image", True),
        ("notes.txt", "document", False),
    ],
)
def test_kinds_and_preparation_follow_the_extension(name: str, kind: str, prepared: bool) -> None:
    """A picture or a pdf is prepared when it is stored; a picture is the one image kind."""
    assert attachment_kind(name) == kind
    assert needs_prepare(name) is prepared
