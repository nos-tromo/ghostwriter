"""Ghostwriter's client for workbench: tools over its MCP door, files over its REST door.

Tools. The MCP transport is docint's (``docint/utils/workbench_client.py``): revision
``2026-07-28`` only, one JSON-RPC message per ``POST {WORKBENCH_API_BASE}/mcp``, no
MCP session. Each exchange (a discovery or a call) is an ``anyio.run`` over a fresh
``httpx.AsyncClient`` that takes no proxy from the environment, on a daemon thread of
its own; the caller waits at most the exchange's deadline plus half a second
(:func:`_detached`), so a slow name lookup never holds a turn, and the client may be
called from any context, a running event loop included.

Discovery (``server/discover``, then ``tools/list``, within :data:`DISCOVERY_TIMEOUT_S`
together) is single-flight and cached per result for its ``ttlMs``, for every chat, once
for each listing: a call that takes images, and one that does not. It succeeds when
workbench serves this revision and lists at least one tool whose ``inputSchema`` is an
object; other tools are dropped. A failure offers no tool. An ``unreachable`` one (refused,
no such name), which comes fast, sets no delay: the next turn discovers again. Any other (a
timeout, an error, an answer of no use) is tried again after :data:`DISCOVERY_RETRY_S`, or
sooner: an answer workbench gives any request of this client, whatever its status, ends both
listings' delays. Each listing keeps its own cache, lock and retry delay. A call without an
answer, refused arguments, ``-32022`` or ``-32602`` drops both caches. workbench's
discovery reads no principal; it carries the asking turn's headers only because every request does.

The model reads a result's ``content[0].text``, which workbench writes without the table
previews; the cards come from ``structuredContent.outputs``, kept only inside the chat's
folder, each preview clamped. A refusal is read by its ``structuredContent.reason`` token.
:meth:`WorkbenchClient.call_tool` never raises.

Images. workbench 0.7 lists ``view_file`` and attaches images to an answer only for a request
that carries ``X-Accept-Images: 1`` (:data:`ACCEPT_IMAGES`); without it, it lists as 0.6 did, with
``read_document`` told in 0.6's words, which promise no image. A discovery and a call send it only
when asked (``images=True``), so each listing is the one its calls get. The
image blocks of an answer, ``content[1:]``, are kept paired by position with
``structuredContent.shown``: a block that is no jpeg or png of its type, is too large, or has no
valid entry is dropped, and so is whatever passes the caps on one answer.

Files. Uploads stream into the chat's folder as multipart, cut off past
:data:`MAX_ATTACHMENT_BYTES`; views stream back; a chat's folder is deleted whole. A file is
prepared (:meth:`WorkbenchClient.prepare`: its manifest, and its renders made) and its renders
are viewed one by one (``view(rendition=...)``) or several at once
(:meth:`WorkbenchClient.fetch_renditions`, from a thread). A clip's transcript and keyframes, which
Nextext made, stream in as derived files of the clip (:meth:`WorkbenchClient.derived`, workbench 0.9),
and its manifest then counts frames and says whether it has a transcript. A document is measured
(:meth:`WorkbenchClient.measure`, workbench 0.10, :func:`serves_measure`): its units and the characters a full
read shows. The REST routes run on the caller's event loop, each over a fresh client that takes no proxy either.

Every request carries the key, the user and the chat as keyed pseudonyms (HMAC-SHA256
under ``WORKBENCH_API_KEY`` over a label, a NUL byte and the owner or the chat id, as 64
hex digits), the purpose and the request id; the chat's pseudonym names its folder. The
log holds tool names, ids, counts, statuses and tokens, never a file name, an argument,
an output, a picture's label or bytes, or the key.
"""

import base64
import concurrent.futures
import contextlib
import hashlib
import hmac
import itertools
import json
import math
import posixpath
import re
import secrets
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, TypeGuard, TypeVar

import anyio
import httpx
from loguru import logger

from ghostwriter import __version__
from ghostwriter.config import Settings
from ghostwriter.schemas import MeasureUnit
from ghostwriter.utils.audit_runtime import PURPOSE_HEADER, REQUEST_ID_HEADER

#: The one MCP revision this client speaks.
PROTOCOL_VERSION = "2026-07-28"

# The ``_meta`` key names, here and nowhere else.
META_PROTOCOL = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

#: Seconds both discovery requests get together. Never spent from a call's time.
DISCOVERY_TIMEOUT_S = 5.0
#: Seconds after a failed discovery before the next try, unless it was ``unreachable`` (no delay) or workbench answers
#: a request meanwhile (the delay ends).
DISCOVERY_RETRY_S = 60.0
#: The discovery failure that sets no delay: refused or no such name, which fails fast and is tried again at once.
_NO_DELAY: Final = "unreachable"
#: The largest answer read; past it the answer is refused.
MAX_ANSWER_BYTES = 4 * 1024 * 1024
#: The largest attachment, as workbench's ``WORKBENCH_UPLOAD_MAX_MB`` counts it.
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
#: A workbench file id.
FILE_ID = re.compile(r"[0-9a-f]{32}")
#: The header workbench 0.7 lists ``view_file`` and attaches images for.
ACCEPT_IMAGES: Final = {"X-Accept-Images": "1"}
#: What a picture's bytes start with, by its media type: a jpeg and a png are the only ones shown.
IMAGE_SIGNATURES: Final = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}
#: The largest picture kept from a tool's answer, and the most pictures and bytes kept from one answer.
MAX_IMAGE_BYTES: Final = 1024 * 1024
MAX_IMAGES_BYTES: Final = 3 * 1024 * 1024
MAX_TOOL_IMAGES: Final = 8
#: A rendition fetched must be smaller than this.
MAX_RENDITION_BYTES: Final = 768 * 1024
#: Seconds a prepare is waited for: workbench's 120 s of queue, two job walls of 120 s (an Office file is
#: converted, then drawn, in one turn) and 30 s. A measure waits as long: workbench 0.10's measure gate takes 120 s,
#: the caller's wall and 30 s, 270 s at its defaults; the wait must stay above that sum if its limits are raised.
PREPARE_TIMEOUT_S: Final = 390.0
#: The first workbench that measures a document, as ``(major, minor)``: 0.10.
MEASURE_WORKBENCH: Final = (0, 10)
#: Seconds the renditions of one fetch get together.
RENDITION_TIMEOUT_S: Final = 30.0
#: A table preview's most columns, rows and characters a cell, as workbench's executor cuts them.
PREVIEW_COLUMNS = 20
PREVIEW_ROWS = 5
PREVIEW_CELL_CHARS = 100

#: What a file is to the tools that show it: a picture, a PDF, an Office file drawn as pages or slides, a video or
#: audio clip shown as its frames, or none.
Kind = Literal["image", "pdf", "office", "media", "none"]
#: What a file's renders count: the pages of a document, the slides of a deck, or the frames of a clip.
Unit = Literal["page", "slide", "frame"]
#: What Nextext's artifact of a clip is to workbench: its transcript (JSON lines) or its keyframes (a zip).
DerivedRole = Literal["nextext_jsonl", "nextext_zip"]

#: The media type each role's artifact is sent as.
DERIVED_TYPES: Final[dict[DerivedRole, str]] = {
    "nextext_jsonl": "application/x-ndjson",
    "nextext_zip": "application/zip",
}
#: Seconds workbench is waited for once it holds a whole artifact: the clip's lock for up to 120 s (a prepare of the
#: same clip may hold it) and the work itself. The transfer comes before it and is bounded by the caller's own budget.
DERIVED_TIMEOUT_S: Final = 150.0

_PATH = "/mcp"
# A picture's label, as workbench's ``shown`` writes it, and the largest side it names.
_LABEL_CHARS = 100
_MAX_SIDE = 4096
# The base64 of a picture at the cap, so a longer string is not decoded.
_MAX_IMAGE_BASE64 = (MAX_IMAGE_BYTES + 2) // 3 * 4
# The most of a refusal's body read for its token.
_MAX_REFUSAL_BYTES = 16 * 1024
_KINDS: Final[dict[str, Kind]] = {
    "image": "image",
    "pdf": "pdf",
    "office": "office",
    "media": "media",
    "none": "none",
}
_UNITS: Final[dict[str, Unit]] = {"page": "page", "slide": "slide", "frame": "frame"}
_MEASURE_UNITS: Final[dict[str, MeasureUnit]] = {
    "page": "page",
    "slide": "slide",
    "paragraph": "paragraph",
    "row": "row",
    "line": "line",
}
# A version workbench names itself as offered: dotted numbers, at least two, of ASCII digits.
_RELEASE = re.compile(r"([0-9]{1,6})\.([0-9]{1,6})(?:\.[0-9]{1,6})*")
_ACCEPT = "application/json, text/event-stream"
_USER_LABEL = b"workbench-user/v1"
_SESSION_LABEL = b"workbench-session/v1"
_CONNECT_S = 3.0
_WRITE_S = 10.0
_POOL_S = 3.0
# How long past an exchange's deadline its caller waits for the exchange's thread.
_GRACE_S = 0.5
# A call without an answer may mean workbench moved; refused arguments may mean a changed schema;
# -32022 is a revision no longer served, and -32602 covers an unknown tool (mapped by code, never text).
_DROPS_DISCOVERY = frozenset({"timeout", "unreachable", "invalid_arguments", "jsonrpc:-32022", "jsonrpc:-32602"})
# A reason is logged, so only a token's shape passes: no sentence a server might put there.
_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,39}")
_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,64}")
# A tool name goes into a header, the log and the model's tool list: the function-name shape only.
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MEDIA_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}")
_FORWARDED = frozenset({PURPOSE_HEADER.lower(), REQUEST_ID_HEADER.lower()})
# httpx's HTML5 escapes for a multipart parameter (httpx/_multipart.py).
_FORM_ESCAPES = {'"': "%22", "\\": "\\\\", **{chr(c): f"%{c:02X}" for c in range(0x20) if c != 0x1B}}
_FORM_ESCAPE = re.compile("|".join(re.escape(c) for c in _FORM_ESCAPES))
_TEXTS = {
    "timeout": "workbench did not answer in time.",
    "unreachable": "workbench could not be reached.",
    "not_configured": "workbench is not configured.",
    "unknown_tool": "there is no such tool.",
}
_UNREADABLE = "workbench's answer could not be read."

_T = TypeVar("_T")


@dataclass(frozen=True)
class McpTool:
    """A tool workbench lists.

    Attributes:
        name: Its name.
        description: What it does, for the model.
        input_schema: Its arguments, a JSON Schema object.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class Discovery:
    """What a successful discovery found.

    Attributes:
        tools: The usable tools, in workbench's order.
        server_version: workbench's version, or ``""`` when it named none.
    """

    tools: tuple[McpTool, ...]
    server_version: str


@dataclass(frozen=True)
class TablePreview:
    """The head of a table a tool wrote.

    Attributes:
        columns: The header, at most :data:`PREVIEW_COLUMNS`.
        rows: At most :data:`PREVIEW_ROWS` rows, cells cut to :data:`PREVIEW_CELL_CHARS`.
        row_count: The data rows, when workbench knows them.
        truncated: Whether rows, columns or the end of a cell were left out.
    """

    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    row_count: int | None
    truncated: bool


@dataclass(frozen=True)
class OutputFile:
    """A file a tool wrote in the chat's folder.

    Attributes:
        id: workbench's file id, 32 hex digits.
        path: Its path, inside the chat's folder.
        name: Its name.
        mime: Its media type.
        size: Its size in bytes.
        preview: The head of a csv or xlsx table, when workbench read one.
    """

    id: str
    path: str
    name: str
    mime: str
    size: int
    preview: TablePreview | None


@dataclass(frozen=True)
class ToolImage:
    """A picture a tool showed: a page, or a whole picture, or one of its strips.

    Attributes:
        label: What workbench calls it, at most 100 characters; for the model, never for a log.
        width: Its width in pixels, 1 to 4096.
        height: Its height in pixels, 1 to 4096.
        mime: ``image/jpeg`` or ``image/png``.
        data: Its bytes, at most :data:`MAX_IMAGE_BYTES`; not in the repr.
    """

    label: str
    width: int
    height: int
    mime: str
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class ToolResult:
    """A call that completed.

    Attributes:
        text: What the model reads, without the previews.
        outputs: The files the call wrote, for the cards.
        images: The pictures the call showed, at most :data:`MAX_TOOL_IMAGES` and
            :data:`MAX_IMAGES_BYTES`, in workbench's order.
    """

    text: str
    outputs: tuple[OutputFile, ...]
    images: tuple[ToolImage, ...] = ()


@dataclass(frozen=True)
class ToolFailure:
    """A call that returned nothing usable.

    Attributes:
        token: Why, as a token; safe to log.
        text: What the model may read about it.
        answered: Whether a JSON-RPC message came back.
    """

    token: str
    text: str
    answered: bool


@dataclass(frozen=True)
class CallFor:
    """The chat a request to workbench is made for.

    Attributes:
        owner: The chat's owner; sent as its pseudonym only.
        chat_id: The chat; sent as its pseudonym only.
        headers: ``X-Audit-Purpose`` and ``X-Request-ID``, from :func:`ghostwriter.audit.forward_headers`.
    """

    owner: str
    chat_id: str
    headers: Mapping[str, str]


@dataclass(frozen=True)
class UploadedFile:
    """An attachment workbench stored.

    Attributes:
        id: workbench's file id, 32 hex digits.
        path: Its path in the chat's folder.
        size: Its size in bytes.
    """

    id: str
    path: str
    size: int


@dataclass(frozen=True)
class Rendition:
    """One render of a file that workbench keeps: a page of a PDF, or a picture.

    Attributes:
        n: Its place, counted from 1.
        width: Its width in pixels.
        height: Its height in pixels.
        mime: ``image/jpeg`` or ``image/png``.
    """

    n: int
    width: int
    height: int
    mime: str


@dataclass(frozen=True)
class Manifest:
    """What workbench says a file is to the tools that show it, as :meth:`WorkbenchClient.prepare` reads it.

    Attributes:
        kind: ``image``, ``pdf``, ``office`` for a Word, PowerPoint or OpenDocument text file drawn as pages
            or slides, ``media`` for a video or audio clip, or ``none`` for a file that is not shown as pictures.
        total: The pages of a PDF or an Office file, or its slides, or the frames of a clip (0 for audio); 1 for a
            picture; 0 for ``none``.
        pages_without_text: The pages or slides with no text of their own, counted from 1.
        renditions: The renders that exist now.
        frames: The frames of a picture that has several, of which only the first is shown; else ``None``.
        unit: What ``total`` and the renders count: ``slide`` for a deck, ``frame`` for a clip, else ``page``; an
            answer of workbench 0.7 names none, and reads as ``page``.
        duration_s: A clip's length in seconds, when workbench knows it; ``None`` for any other file, and for a
            manifest of workbench before 0.9.
        transcript: Whether a clip has a transcript, which ``read_document`` reads; false for any other file, and
            for a manifest of workbench before 0.9.
    """

    kind: Kind
    total: int
    pages_without_text: tuple[int, ...]
    renditions: tuple[Rendition, ...]
    frames: int | None
    unit: Unit = "page"
    duration_s: float | None = None
    transcript: bool = False

    def as_stored(self) -> dict[str, Any]:
        """Return what a chat keeps of the manifest: its eight counts and kinds, never a path or a hint.

        Returns:
            The keys of ``STORED_MANIFEST_KEYS`` as plain JSON values.
        """
        return {
            "kind": self.kind,
            "total": self.total,
            "pages_without_text": list(self.pages_without_text),
            "renditions": [
                {"n": item.n, "width": item.width, "height": item.height, "mime": item.mime} for item in self.renditions
            ],
            "frames": self.frames,
            "unit": self.unit,
            "duration_s": self.duration_s,
            "transcript": self.transcript,
        }


@dataclass(frozen=True)
class Measure:
    """How long a document is, as :meth:`WorkbenchClient.measure` reads it (workbench 0.10).

    Attributes:
        total: How many units it has.
        unit: What ``total`` counts, in its reader's own unit: ``page``, ``slide``, ``paragraph``, ``row`` or ``line``.
        chars: The characters a full read shows, counted up to workbench's cap.
        capped: Whether the count stopped at that cap, so the document is longer still.
    """

    total: int
    unit: MeasureUnit
    chars: int
    capped: bool

    def as_stored(self) -> dict[str, Any]:
        """Return what a chat keeps of the measure: its four counts, never a path or a kind.

        Returns:
            ``total``, ``unit``, ``chars`` and ``capped`` as plain JSON values.
        """
        return {"total": self.total, "unit": self.unit, "chars": self.chars, "capped": self.capped}


def version_at_least(version: str, floor: tuple[int, int]) -> bool:
    """Say whether a version workbench names itself is at least ``floor``.

    Args:
        version: The version, as workbench's discovery carries it.
        floor: The first ``(major, minor)`` that counts.

    Returns:
        ``True`` for ``X.Y`` or ``X.Y.Z...`` of ASCII digits with ``(X, Y)`` at ``floor`` or later; ``False`` for an
        older one, an empty one, and any other text.
    """
    found = _RELEASE.fullmatch(version)
    return found is not None and (int(found[1]), int(found[2])) >= floor


def serves_measure(version: str) -> bool:
    """Say whether a workbench of this version measures a document: 0.10.0 and later.

    Args:
        version: The version workbench names itself, as its discovery carries it.

    Returns:
        Whether it is :data:`MEASURE_WORKBENCH` or later (:func:`version_at_least`).
    """
    return version_at_least(version, MEASURE_WORKBENCH)


@dataclass(frozen=True)
class RenditionRequest:
    """One rendition to fetch.

    Attributes:
        path: The file's path in the chat's folder.
        n: The page or picture, counted from 1.
        strips: Into how many strips the render is cut: 1 for the whole render, up to 3.
        strip: Which strip, counted from 1; read only when ``strips`` is more than 1.
    """

    path: str
    n: int = 1
    strips: int = 1
    strip: int = 1


class WorkbenchUnavailable(Exception):
    """workbench could not do what was asked; ``token`` says why."""

    def __init__(self, token: str) -> None:
        """Name the failure.

        Args:
            token: Why, as a token; safe to log.
        """
        super().__init__(token)
        self.token = token


class WorkbenchNotFound(WorkbenchUnavailable):
    """workbench has no such file in the chat."""


class UploadTooLarge(Exception):
    """The attachment is larger than :data:`MAX_ATTACHMENT_BYTES`, or than workbench accepts (its ``413``)."""


class StorageFull(Exception):
    """The user's files fill the space workbench allows (its ``507``)."""


@dataclass(frozen=True)
class _Server:
    version: str
    expires_at: float


@dataclass(frozen=True)
class _Tools:
    tools: tuple[McpTool, ...]
    expires_at: float


@dataclass
class _Listing:
    """One listing's discovery: its lock, its cached results, its retry delay and what was last logged."""

    flight: threading.Lock = field(default_factory=threading.Lock)
    server: _Server | None = None
    tools: _Tools | None = None
    retry_at: float | None = None
    up: bool | None = None


class _Unusable(Exception):
    """The door's answer, or its absence, cannot be used; ``token`` says why."""

    def __init__(self, token: str, *, answered: bool = False) -> None:
        super().__init__(token)
        self.token = token
        self.answered = answered


def _pseudonym(key: str, label: bytes, value: str) -> str:
    return hmac.new(key.encode("utf-8"), label + b"\x00" + value.encode("utf-8"), hashlib.sha256).hexdigest()


def user_pseudonym(key: str, owner: str) -> str:
    """Return the ``X-User-Id`` workbench sees for a chat's owner.

    Args:
        key: ``WORKBENCH_API_KEY``.
        owner: The owner, as the edge names them.

    Returns:
        HMAC-SHA256 over ``workbench-user/v1``, NUL and the owner, as 64 hex digits.
    """
    return _pseudonym(key, _USER_LABEL, owner)


def session_pseudonym(key: str, chat_id: str) -> str:
    """Return the ``X-Session-Id`` workbench sees for a chat.

    Args:
        key: ``WORKBENCH_API_KEY``.
        chat_id: The chat's uuid.

    Returns:
        HMAC-SHA256 over ``workbench-session/v1``, NUL and the chat id, as 64 hex digits.
    """
    return _pseudonym(key, _SESSION_LABEL, chat_id)


def chat_folder(key: str, chat_id: str) -> str:
    """Return a chat's folder in workbench.

    Args:
        key: ``WORKBENCH_API_KEY``.
        chat_id: The chat's uuid.

    Returns:
        ``/workspace/<the chat's pseudonym>``.
    """
    return f"/workspace/{session_pseudonym(key, chat_id)}"


def _client_meta() -> dict[str, Any]:
    return {
        META_PROTOCOL: PROTOCOL_VERSION,
        META_CLIENT_INFO: {"name": "ghostwriter", "version": __version__},
        META_CLIENT_CAPS: {},
    }


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _seconds(value: object) -> TypeGuard[int | float]:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:  # an integer too large for a float is no duration either
        return False


def _ttl_s(result: Mapping[str, Any]) -> float:
    ttl = result.get("ttlMs")
    if isinstance(ttl, int | float) and not isinstance(ttl, bool) and math.isfinite(ttl) and ttl > 0:
        return ttl / 1000.0
    return 0.0


def _server_version(result: Mapping[str, Any]) -> str | None:
    meta = result.get("_meta")
    info = meta.get(META_SERVER_INFO) if isinstance(meta, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    return version if isinstance(version, str) and _VERSION.fullmatch(version) else None


def _usable_tools(result: Mapping[str, Any]) -> tuple[McpTool, ...]:
    """Return the listed tools with a function's name and an object schema, the first of each name.

    Args:
        result: The ``tools/list`` result.

    Returns:
        The tools, in workbench's order.
    """
    listed = result.get("tools")
    found: dict[str, McpTool] = {}
    for tool in listed if isinstance(listed, list) else []:
        if not isinstance(tool, dict):
            continue
        name, description, schema = tool.get("name"), tool.get("description"), tool.get("inputSchema")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name) or name in found:
            continue
        if isinstance(schema, dict) and schema.get("type") == "object":
            found[name] = McpTool(name, description if isinstance(description, str) else "", schema)
    return tuple(found.values())


def _message(raw: bytes, request_id: int) -> dict[str, Any]:
    """Return the one JSON-RPC message ``raw`` holds, answering ``request_id``.

    Args:
        raw: The answer's body.
        request_id: The id the request carried.

    Returns:
        A message with either a ``result`` object or an ``error`` object with an integer ``code``.

    Raises:
        _Unusable: ``not_json_rpc``, when it is anything else.
    """
    try:
        message = json.loads(raw)
    except (ValueError, RecursionError):
        raise _Unusable("not_json_rpc") from None
    if (
        not isinstance(message, dict)
        or message.get("jsonrpc") != "2.0"
        or ("result" in message) == ("error" in message)
    ):
        raise _Unusable("not_json_rpc")
    if "result" in message:
        if message.get("id") != request_id or not isinstance(message["result"], dict):
            raise _Unusable("not_json_rpc")
    else:
        error = message["error"]
        if not isinstance(error, dict) or not _is_int(error.get("code")) or message.get("id") not in (request_id, None):
            raise _Unusable("not_json_rpc")
    return message


def _result(message: Mapping[str, Any]) -> dict[str, Any]:
    """Return a message's complete result.

    Args:
        message: A message from :func:`_message`.

    Returns:
        Its result.

    Raises:
        _Unusable: ``jsonrpc:<code>`` for an error, ``incomplete`` for a result that asks
            for more than this client gives.
    """
    if "error" in message:
        raise _Unusable(f"jsonrpc:{int(message['error']['code'])}", answered=True)
    result: dict[str, Any] = message["result"]
    if result.get("resultType", "complete") != "complete":
        raise _Unusable("incomplete", answered=True)
    return result


def _detached(work: Callable[[], Awaitable[_T]], wait_s: float) -> _T:
    """Run ``work`` in an event loop of its own on a daemon thread, and wait at most ``wait_s``.

    Args:
        work: The exchange, bounded by its own deadline.
        wait_s: Seconds to wait for the thread: the deadline plus :data:`_GRACE_S`.

    Returns:
        What ``work`` returned.

    Raises:
        TimeoutError: When the thread has not finished in time. It is abandoned and its late result dropped.
    """
    future: concurrent.futures.Future[_T] = concurrent.futures.Future()

    def exchange() -> None:
        try:
            future.set_result(anyio.run(work))
        except BaseException as exc:
            future.set_exception(exc)

    # A fresh context, not the caller's: a copy would carry the running loop's sniffio marker, and
    # anyio.run would refuse. The exchange reads no context variable; its headers are built before.
    threading.Thread(target=exchange, name="workbench-exchange", daemon=True).start()
    return future.result(timeout=wait_s)


def _inside(folder: str, path: object) -> str | None:
    """Return a path if it is a plain path below a chat's folder.

    Args:
        folder: The chat's folder.
        path: The path workbench named.

    Returns:
        The path when it is normalized, below the folder and without control characters; else ``None``.
    """
    if not isinstance(path, str) or not path.startswith(f"{folder}/") or posixpath.normpath(path) != path:
        return None
    return None if any(ord(char) < 32 for char in path) else path


def _flat_name(value: object) -> str | None:
    if not isinstance(value, str) or value in {"", ".", ".."} or "/" in value or "\\" in value:
        return None
    return None if any(ord(char) < 32 for char in value) else value


def _strings(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _side(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _MAX_SIDE


def _positive(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _cells(values: list[str]) -> tuple[str, ...]:
    return tuple(value[:PREVIEW_CELL_CHARS] for value in values[:PREVIEW_COLUMNS])


def _preview(value: object) -> TablePreview | None:
    """Read an output's preview, clamped to the preview's limits.

    Args:
        value: ``preview`` as workbench sent it.

    Returns:
        The preview; ``None`` when there is none or it is malformed.
    """
    if not isinstance(value, dict):
        return None
    columns, rows = value.get("columns"), value.get("rows")
    row_count, truncated = value.get("row_count"), value.get("truncated")
    if not _strings(columns) or not isinstance(rows, list) or not all(_strings(row) for row in rows):
        return None
    if not (row_count is None or _count(row_count) is not None) or not isinstance(truncated, bool):
        return None
    cut = len(rows) > PREVIEW_ROWS or any(
        len(row) > PREVIEW_COLUMNS or any(len(cell) > PREVIEW_CELL_CHARS for cell in row[:PREVIEW_COLUMNS])
        for row in [columns, *rows[:PREVIEW_ROWS]]
    )
    return TablePreview(_cells(columns), tuple(_cells(row) for row in rows[:PREVIEW_ROWS]), row_count, truncated or cut)


def _output(entry: object, folder: str) -> OutputFile | None:
    """Read one of ``structuredContent.outputs``.

    Args:
        entry: The entry.
        folder: The chat's folder.

    Returns:
        The file; ``None`` when it is not a file of the chat's folder with an id, a name, a type and a size.
    """
    if not isinstance(entry, dict):
        return None
    file_id, path, mime = entry.get("id"), _inside(folder, entry.get("path")), entry.get("mime")
    name, size = _flat_name(entry.get("name")), _count(entry.get("size"))
    if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id) or path is None:
        return None
    if name is None or not isinstance(mime, str) or size is None:
        return None
    return OutputFile(file_id, path, name, mime, size, _preview(entry.get("preview")))


def _without_previews(structured: Mapping[str, Any]) -> str:
    outputs = structured.get("outputs")
    if isinstance(outputs, list):
        bare = [
            {k: v for k, v in item.items() if k != "preview"} if isinstance(item, dict) else item for item in outputs
        ]
        structured = {**structured, "outputs": bare}
    return json.dumps(structured, ensure_ascii=False, separators=(",", ":"))


def _error_text(message: Mapping[str, Any]) -> str:
    error = message.get("error")
    text = error.get("message") if isinstance(error, dict) else None
    return text if isinstance(text, str) else _UNREADABLE


def _decode(message: Mapping[str, Any], folder: str) -> ToolResult | ToolFailure:
    """Read a ``tools/call`` answer.

    Args:
        message: One JSON-RPC message.
        folder: The chat's folder, which every output must be in.

    Returns:
        The text, the outputs and the pictures, or the failure the answer names.
    """
    try:
        result = _result(message)
    except _Unusable as exc:
        return ToolFailure(exc.token, _error_text(message), answered=True)
    structured = result.get("structuredContent")
    structured = structured if isinstance(structured, dict) else None
    content = result.get("content")
    first: Mapping[str, Any] = (
        content[0] if isinstance(content, list) and content and isinstance(content[0], dict) else {}
    )
    text = first.get("text") if first.get("type") == "text" else None
    if result.get("isError") is True:
        reason = structured.get("reason") if structured is not None else None
        token = reason if isinstance(reason, str) and _TOKEN.fullmatch(reason) else "bad_reason"
        return ToolFailure(token, text if isinstance(text, str) else "", answered=True)
    if not isinstance(text, str):
        if structured is None:
            return ToolFailure("bad_result", _UNREADABLE, answered=True)
        text = _without_previews(structured)
    listed = structured.get("outputs") if structured is not None else None
    entries: list[Any] = listed if isinstance(listed, list) else []
    outputs = tuple(item for item in (_output(entry, folder) for entry in entries) if item is not None)
    if len(outputs) < len(entries):
        logger.warning(
            "workbench listed {} outputs that are not files of the chat; they are dropped", len(entries) - len(outputs)
        )
    return ToolResult(text, outputs, _images(content, structured))


def _image(block: object, entry: object) -> ToolImage | None:
    """Read one image block with the entry of ``shown`` at its position.

    Args:
        block: An element of ``content[1:]``.
        entry: ``shown[i]`` for the block's position ``i``, or ``None`` when there is none.

    Returns:
        The picture; ``None`` when the block is no jpeg or png of its type with valid base64 of at most
        :data:`MAX_IMAGE_BYTES`, or the entry has no label of at most 100 characters and no sides of 1 to 4096.
    """
    if not isinstance(block, dict) or block.get("type") != "image" or not isinstance(entry, dict):
        return None
    mime, encoded = block.get("mimeType"), block.get("data")
    label, width, height = entry.get("label"), entry.get("width"), entry.get("height")
    if not isinstance(mime, str) or mime not in IMAGE_SIGNATURES or not isinstance(encoded, str):
        return None
    if not isinstance(label, str) or len(label) > _LABEL_CHARS or not _side(width) or not _side(height):
        return None
    if len(encoded) > _MAX_IMAGE_BASE64:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError:
        return None
    if len(data) > MAX_IMAGE_BYTES or not data.startswith(IMAGE_SIGNATURES[mime]):
        return None
    return ToolImage(label, width, height, mime, data)


def _images(content: object, structured: Mapping[str, Any] | None) -> tuple[ToolImage, ...]:
    """Keep the pictures of an answer, block ``i`` of ``content[1:]`` paired with ``shown[i]``.

    Keeping stops at :data:`MAX_TOOL_IMAGES` pictures, or before the one that would take the answer past
    :data:`MAX_IMAGES_BYTES`. The blocks that are not kept give one warning with their count.

    Args:
        content: The result's ``content``.
        structured: Its ``structuredContent``.

    Returns:
        The pictures, in order.
    """
    blocks: list[Any] = content[1:] if isinstance(content, list) else []
    if not blocks:
        return ()
    listed = structured.get("shown") if structured is not None else None
    shown: list[Any] = listed if isinstance(listed, list) else []
    kept: list[ToolImage] = []
    size = 0
    for index, block in enumerate(blocks):
        if len(kept) >= MAX_TOOL_IMAGES:
            break
        image = _image(block, shown[index] if index < len(shown) else None)
        if image is None:
            continue
        if size + len(image.data) > MAX_IMAGES_BYTES:
            break
        kept.append(image)
        size += len(image.data)
    if len(kept) < len(blocks):
        logger.warning("workbench sent {} image blocks that are not kept", len(blocks) - len(kept))
    return tuple(kept)


def _failure(token: str, *, answered: bool = False) -> ToolFailure:
    return ToolFailure(token, _TEXTS.get(token, _UNREADABLE), answered)


def _form_param(name: str, value: str) -> bytes:
    escaped = _FORM_ESCAPE.sub(lambda match: _FORM_ESCAPES[match.group(0)], value)
    return f'{name}="{escaped}"'.encode()


async def _multipart(boundary: str, filename: str, mime: str, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Stream one multipart field ``file``, cut off past :data:`MAX_ATTACHMENT_BYTES`.

    Args:
        boundary: The body's boundary.
        filename: The file's name.
        mime: Its media type.
        chunks: Its bytes.

    Yields:
        The part's headers, the bytes, then the closing boundary.

    Raises:
        UploadTooLarge: Before the chunk that would pass the cap is sent.
    """
    disposition = b'Content-Disposition: form-data; name="file"; ' + _form_param("filename", filename)
    yield f"--{boundary}\r\n".encode() + disposition + f"\r\nContent-Type: {mime}\r\n\r\n".encode()
    sent = 0
    async for chunk in chunks:
        sent += len(chunk)
        if sent > MAX_ATTACHMENT_BYTES:
            raise UploadTooLarge
        if chunk:
            yield chunk
    yield f"\r\n--{boundary}--\r\n".encode()


async def _json_answer(response: httpx.Response) -> object:
    """Read a REST answer: ``200``, one JSON document of at most :data:`MAX_ANSWER_BYTES`.

    Args:
        response: The streamed answer.

    Returns:
        The document.

    Raises:
        WorkbenchUnavailable: ``http:<status>``, ``not_json`` or ``answer_too_large``.
    """
    if response.status_code != 200:
        raise WorkbenchUnavailable(f"http:{response.status_code}")
    if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise WorkbenchUnavailable("not_json")
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        raw += chunk
        if len(raw) > MAX_ANSWER_BYTES:
            raise WorkbenchUnavailable("answer_too_large")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise WorkbenchUnavailable("not_json") from None


async def _refusal_token(response: httpx.Response) -> str:
    """Read the token of a ``422``: the text of its ``detail`` before the first ``:``, when that is a token.

    Args:
        response: The streamed answer.

    Returns:
        The token, such as ``unreadable_file``; ``http:<status>`` when the body is no JSON object
        with a ``detail`` text that starts with one and a ``:``, or is longer than :data:`_MAX_REFUSAL_BYTES`.
    """
    fallback = f"http:{response.status_code}"
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        raw += chunk
        if len(raw) > _MAX_REFUSAL_BYTES:
            return fallback
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        return fallback
    detail = body.get("detail") if isinstance(body, dict) else None
    head, colon, _ = detail.partition(":") if isinstance(detail, str) else ("", "", "")
    return head if colon and _TOKEN.fullmatch(head) else fallback


def _rendition(entry: object) -> Rendition | None:
    if not isinstance(entry, dict):
        return None
    n, width, height, mime = entry.get("n"), entry.get("width"), entry.get("height"), entry.get("mime")
    if not (_positive(n) and _positive(width) and _positive(height)):
        return None
    return Rendition(n, width, height, mime) if isinstance(mime, str) and mime in IMAGE_SIGNATURES else None


def _manifest(answer: object) -> Manifest | None:
    """Read a prepare's answer.

    Args:
        answer: The JSON document.

    Returns:
        The manifest; ``None`` when it is not an object with a known ``kind``, a count for ``total``,
        pages counted from 1, renditions of a jpeg or png, a frame count or ``null``, a ``unit`` of
        ``page``, ``slide`` or ``frame``, which reads as ``page`` when the answer names none (workbench 0.7),
        a ``duration_s`` that is ``null`` or a finite number from 0 and a ``transcript`` that is a bool (both
        read as ``None`` and false when the answer names none, as before workbench 0.9). A clip (kind
        ``media``) counts ``frame`` units. Its ``path`` and ``hint`` are not read.
    """
    if not isinstance(answer, dict):
        return None
    kind, total, frames = answer.get("kind"), _count(answer.get("total")), answer.get("frames")
    pages, listed, unit = answer.get("pages_without_text"), answer.get("renditions"), answer.get("unit", "page")
    duration, transcript = answer.get("duration_s"), answer.get("transcript", False)
    if not isinstance(kind, str) or kind not in _KINDS or total is None:
        return None
    if not isinstance(unit, str) or unit not in _UNITS or (kind == "media" and unit != "frame"):
        return None
    if not (duration is None or _seconds(duration)) or not isinstance(transcript, bool):
        return None
    if (
        not isinstance(pages, list)
        or not isinstance(listed, list)
        or not (frames is None or _count(frames) is not None)
    ):
        return None
    numbers = tuple(page for page in pages if _positive(page))
    renditions = tuple(item for item in (_rendition(entry) for entry in listed) if item is not None)
    if len(numbers) < len(pages) or len(renditions) < len(listed):
        return None
    return Manifest(
        _KINDS[kind],
        total,
        numbers,
        renditions,
        frames,
        _UNITS[unit],
        None if duration is None else float(duration),
        transcript,
    )


def _measure_answer(answer: object) -> Measure | None:
    """Read a measure's answer.

    Args:
        answer: The JSON document.

    Returns:
        The measure; ``None`` when it is not an object with a ``unit`` of the five, a count (an int from 0, never a
        bool) for ``total`` and for ``chars``, and a bool for ``capped``. Its ``path`` and ``kind`` are not read.
    """
    if not isinstance(answer, dict):
        return None
    unit, capped = answer.get("unit"), answer.get("capped")
    total, chars = _count(answer.get("total")), _count(answer.get("chars"))
    if not isinstance(unit, str) or unit not in _MEASURE_UNITS or total is None or chars is None:
        return None
    return Measure(total, _MEASURE_UNITS[unit], chars, capped) if isinstance(capped, bool) else None


def _view_params(path: str, rendition: int | None, strips: int, strip: int | None) -> dict[str, str | int]:
    """Return the query of a view.

    Args:
        path: The file's path.
        rendition: The render asked for; ``None`` for the file itself.
        strips: Into how many strips the render is cut; read with ``rendition`` only.
        strip: Which strip; read when ``strips`` is more than 1.

    Returns:
        ``path``, and with a rendition its number and, past one strip, ``strips`` and ``strip``.

    Raises:
        ValueError: When ``strips`` is more than 1 and ``strip`` is ``None``.
    """
    params: dict[str, str | int] = {"path": path}
    if rendition is None:
        return params
    params["rendition"] = rendition
    if strips > 1:
        if strip is None:
            raise ValueError("strip is required when strips is more than 1")
        params["strips"] = strips
        params["strip"] = strip
    return params


async def _picture(response: httpx.Response) -> tuple[str, bytes] | None:
    """Read a rendition's answer as a stream.

    Args:
        response: The streamed answer.

    Returns:
        Its media type and bytes when it is ``200``, a jpeg or png under :data:`MAX_RENDITION_BYTES` that starts
        as its type does; else ``None``. The stream is cut at the cap.
    """
    if response.status_code != 200:
        return None
    mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    signature = IMAGE_SIGNATURES.get(mime)
    if signature is None:
        return None
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        raw += chunk
        if len(raw) >= MAX_RENDITION_BYTES:
            return None
    return (mime, bytes(raw)) if raw.startswith(signature) else None


@contextlib.contextmanager
def _rest_errors(operation: str) -> Iterator[None]:
    """Turn httpx's errors into :class:`WorkbenchUnavailable`, and log each failure by its token.

    Args:
        operation: What is being done, for the log.

    Yields:
        Nothing.

    Raises:
        WorkbenchUnavailable: ``timeout`` or ``unreachable`` for httpx's errors, or as raised inside.
        UploadTooLarge: As raised inside.
        StorageFull: As raised inside.
    """
    try:
        yield
    except httpx.TimeoutException:
        token = "timeout"
    except httpx.TransportError:
        token = "unreachable"
    except httpx.HTTPError:
        token = "error"
    except WorkbenchNotFound:
        logger.info("workbench {}: not found", operation)
        raise
    except WorkbenchUnavailable as exc:
        logger.warning("workbench {} failed: {}", operation, exc.token)
        raise
    except (UploadTooLarge, StorageFull) as exc:
        logger.info("workbench {} refused: {}", operation, type(exc).__name__)
        raise
    else:
        return
    logger.warning("workbench {} failed: {}", operation, token)
    raise WorkbenchUnavailable(token) from None


class WorkbenchClient:
    """Reach workbench for one chat at a time: tools over MCP, files over REST."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the client.

        Args:
            settings: The base URL, the key and the timeout.
            transport: Injected transport (tests pass an ``httpx.MockTransport``); ``None`` uses httpx's own.
            clock: The monotonic clock the cache and the retry delay read.
        """
        self._settings = settings
        self._transport = transport
        self._clock = clock
        self._ids = itertools.count(1)
        # By whether the listing is the one of a call that takes images.
        self._listings = {True: _Listing(), False: _Listing()}

    def _key(self) -> str | None:
        key = self._settings.workbench_api_key
        return key if key is not None and self._settings.workbench_enabled else None

    def _timeout(self, t: float) -> httpx.Timeout:
        """Return the per-phase limits inside a deadline of ``t`` seconds.

        Args:
            t: The exchange's deadline, which also bounds each read.

        Returns:
            ``t`` for reads; connect 3 s, write 10 s, pool 3 s.
        """
        return httpx.Timeout(t, connect=_CONNECT_S, write=_WRITE_S, pool=_POOL_S)

    def _http(self, t: float) -> httpx.AsyncClient:
        """Return a fresh client for one exchange or one REST request; every request to workbench goes through one.

        Args:
            t: The deadline, as :meth:`_timeout`.

        Returns:
            A client that takes no proxy from the environment and ends the discovery delays on every answer.
        """
        return httpx.AsyncClient(
            base_url=self._settings.workbench_api_base,
            transport=self._transport,
            trust_env=False,
            timeout=self._timeout(t),
            event_hooks={"response": [self._answered]},
        )

    async def _answered(self, response: httpx.Response) -> None:
        """End both listings' retry delay: workbench answered a request, whatever its status.

        It runs as the answer's headers arrive, so a discovery's own answer comes before its failure, whose
        delay then stands. Without the listings' locks: from an exchange's thread or the caller's loop, an
        answer racing a failure may end the delay just set (one more discovery) or come before it (the delay
        stands until the next answer); either is benign.

        Args:
            response: The answer; only its arrival counts.
        """
        del response
        for listing in self._listings.values():
            listing.retry_at = None

    def _headers(self, key: str, call: CallFor) -> dict[str, str]:
        """Return the headers every request carries for a chat.

        Args:
            key: ``WORKBENCH_API_KEY``.
            call: The chat.

        Returns:
            The key, the user's and the chat's pseudonyms, the purpose and the request id.
        """
        forwarded = {name: value for name, value in call.headers.items() if name.lower() in _FORWARDED}
        return {
            **forwarded,
            "Authorization": f"Bearer {key}",
            "X-User-Id": user_pseudonym(key, call.owner),
            "X-Session-Id": session_pseudonym(key, call.chat_id),
        }

    async def _post(
        self, client: httpx.AsyncClient, method: str, params: Mapping[str, Any], headers: Mapping[str, str]
    ) -> dict[str, Any]:
        """Send one JSON-RPC request and read its answer as a stream.

        Args:
            client: The exchange's client.
            method: The MCP method.
            params: Its parameters, without ``_meta``.
            headers: The chat's headers, and any beyond the common ones.

        Returns:
            The answer, one JSON-RPC message.

        Raises:
            _Unusable: When the answer is not one ``application/json`` message of at most :data:`MAX_ANSWER_BYTES`.
        """
        request_id = next(self._ids)
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {**params, "_meta": _client_meta()}}
        sent = {
            "Content-Type": "application/json",
            "Accept": _ACCEPT,
            "Accept-Encoding": "identity",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
            **headers,
        }
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        async with client.stream("POST", _PATH, content=payload, headers=sent) as response:
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                raise _Unusable(f"http:{response.status_code}" if response.is_error else "not_json")
            if response.headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}:
                raise _Unusable("encoded")
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw += chunk
                if len(raw) > MAX_ANSWER_BYTES:
                    raise _Unusable("answer_too_large")
        return _message(bytes(raw), request_id)

    # --- discovery -------------------------------------------------------

    def discover(self, *, call: CallFor, images: bool) -> Discovery | None:
        """Return a listing's cached discovery, discovering first when a result has expired.

        Concurrent callers for one listing wait for its one discovery in progress, never longer than
        its limit plus half a second; a caller that would is offered no tool on this turn. The two
        listings never wait for each other. After a failure the listing sends nothing while its retry
        delay holds (:meth:`_discovery_failed`): none after ``unreachable``, else :data:`DISCOVERY_RETRY_S`
        or until workbench answers any request (:meth:`_answered`).

        Args:
            call: The asking turn's chat. Its headers go with the requests, as with every request
                to workbench; workbench's discovery reads no principal from them.
            images: Whether to ask for the listing of a call that takes images: both requests then carry
                ``X-Accept-Images: 1``, and workbench 0.7 lists ``view_file`` and its image texts. Without
                it, the listing is the one workbench 0.6 gave.

        Returns:
            The tools and the version, or ``None`` when workbench is not usable on this turn.
        """
        key = self._key()
        if key is None:
            return None
        listing = self._listings[images]
        if not listing.flight.acquire(timeout=DISCOVERY_TIMEOUT_S + _GRACE_S):
            return None
        try:
            return self._discover_held(listing, {**self._headers(key, call), **(ACCEPT_IMAGES if images else {})})
        finally:
            listing.flight.release()

    def _discover_held(self, listing: _Listing, headers: Mapping[str, str]) -> Discovery | None:
        """Discover one listing, holding its single-flight lock.

        Args:
            listing: The listing.
            headers: The asking turn's headers, and the listing's.

        Returns:
            As :meth:`discover`.
        """
        now = self._clock()
        server, tools = listing.server, listing.tools
        server = server if server is not None and now < server.expires_at else None
        tools = tools if tools is not None and now < tools.expires_at else None
        if server is not None and tools is not None:
            return Discovery(tools=tools.tools, server_version=server.version)
        if listing.retry_at is not None and now < listing.retry_at:
            return None
        try:
            fresh_server, fresh_tools = _detached(
                lambda: self._discover(server, tools, now, headers), DISCOVERY_TIMEOUT_S + _GRACE_S
            )
        except _Unusable as exc:
            return self._discovery_failed(listing, now, exc.token)
        except (TimeoutError, httpx.TimeoutException):
            return self._discovery_failed(listing, now, "timeout")
        except httpx.TransportError:
            return self._discovery_failed(listing, now, "unreachable")
        except Exception as exc:
            logger.warning("workbench discovery raised {}", type(exc).__name__)
            return self._discovery_failed(listing, now, "error")
        # Only what was fetched is written back: a forget() meanwhile keeps its effect.
        if fresh_server is not server:
            listing.server = fresh_server
        if fresh_tools is not tools:
            listing.tools = fresh_tools
        listing.retry_at = None
        found = Discovery(tools=fresh_tools.tools, server_version=fresh_server.version)
        if listing.up is not True:
            listing.up = True
            logger.info(
                "workbench is discovered: {} tools are offered to calls {} images (workbench '{}')",
                len(found.tools),
                "with" if listing is self._listings[True] else "without",
                found.server_version,
            )
        return found

    async def _discover(
        self, server: _Server | None, tools: _Tools | None, now: float, headers: Mapping[str, str]
    ) -> tuple[_Server, _Tools]:
        with anyio.fail_after(DISCOVERY_TIMEOUT_S):
            async with self._http(DISCOVERY_TIMEOUT_S) as client:
                if server is None:
                    result = _result(await self._post(client, "server/discover", {}, headers))
                    versions = result.get("supportedVersions")
                    if not isinstance(versions, list) or PROTOCOL_VERSION not in versions:
                        raise _Unusable("no_modern_revision", answered=True)
                    server = _Server(version=_server_version(result) or "", expires_at=now + _ttl_s(result))
                if tools is None:
                    result = _result(await self._post(client, "tools/list", {}, headers))
                    usable = _usable_tools(result)
                    if not usable:
                        raise _Unusable("no_usable_tool", answered=True)
                    tools = _Tools(tools=usable, expires_at=now + _ttl_s(result))
        return server, tools

    def _discovery_failed(self, listing: _Listing, now: float, token: str) -> None:
        """Set a failed listing's retry delay, and warn once each time it goes down.

        ``unreachable`` sets none, so the next caller discovers again at once; every other token (``timeout``,
        ``error``, an answer that is no use) delays it :data:`DISCOVERY_RETRY_S`, until workbench answers
        another request (:meth:`_answered`).

        Args:
            listing: The listing.
            now: When its discovery began.
            token: Why it failed.
        """
        listing.retry_at = None if token == _NO_DELAY else now + DISCOVERY_RETRY_S
        if listing.up is not False:
            listing.up = False
            if listing.retry_at is None:
                logger.warning("workbench discovery failed: {}. No tool is offered; the next turn tries again", token)
            else:
                logger.warning(
                    "workbench discovery failed: {}. No tool is offered; the next try comes after {} s,"
                    " or at once when workbench answers a request",
                    token,
                    int(DISCOVERY_RETRY_S),
                )

    def forget(self) -> None:
        """Drop both listings' cached results, so the next turn discovers again."""
        for listing in self._listings.values():
            listing.server = None
            listing.tools = None

    # --- tools -------------------------------------------------------------

    def call_tool(
        self, name: str, arguments: Mapping[str, Any], *, call: CallFor, timeout_s: float, images: bool = False
    ) -> ToolResult | ToolFailure:
        """Call a tool for a chat, within one deadline. Never raises.

        Args:
            name: The tool.
            arguments: Its arguments, as the model wrote them.
            call: The chat.
            timeout_s: The deadline for the whole call, name lookup to last byte. The caller
                is answered within it plus half a second.
            images: Whether to send ``X-Accept-Images: 1``, so that workbench may attach pictures to
                the answer. Without it the request is the one workbench 0.6 knew.

        Returns:
            What the call returned, or why there is nothing.
        """
        shown = name if isinstance(name, str) and _TOOL_NAME.fullmatch(name) else "?"
        try:
            key = self._key()
            if key is None:
                outcome = _failure("not_configured")
            elif shown == "?":
                outcome = _failure("unknown_tool")
            else:
                headers = {**self._headers(key, call), "Mcp-Name": name, **(ACCEPT_IMAGES if images else {})}
                params = {"name": name, "arguments": dict(arguments)}
                folder = chat_folder(key, call.chat_id)
                outcome = _detached(lambda: self._call(params, headers, timeout_s, folder), timeout_s + _GRACE_S)
        except TimeoutError:
            outcome = _failure("timeout")
        except Exception as exc:
            logger.warning("workbench {} raised {}", shown, type(exc).__name__)
            outcome = _failure("error")
        return self._settle(shown, outcome)

    async def _call(
        self, params: Mapping[str, Any], headers: Mapping[str, str], timeout_s: float, folder: str
    ) -> ToolResult | ToolFailure:
        try:
            with anyio.fail_after(timeout_s):
                async with self._http(timeout_s) as client:
                    message = await self._post(client, "tools/call", params, headers)
        except (TimeoutError, httpx.TimeoutException):
            return _failure("timeout")
        except httpx.TransportError:
            return _failure("unreachable")
        except _Unusable as exc:
            return _failure(exc.token, answered=exc.answered)
        return _decode(message, folder)

    def _settle(self, name: str, outcome: ToolResult | ToolFailure) -> ToolResult | ToolFailure:
        if isinstance(outcome, ToolResult):
            logger.debug("workbench ran {}: {} outputs", name, len(outcome.outputs))
            return outcome
        if outcome.token in _DROPS_DISCOVERY:
            self.forget()
        if outcome.answered and not outcome.token.startswith("jsonrpc:"):
            logger.info("workbench refused {}: {}", name, outcome.token)
        else:
            logger.warning(
                "workbench {} failed: {} ({})", name, outcome.token, "answered" if outcome.answered else "no answer"
            )
        return outcome

    # --- files -------------------------------------------------------------

    def _required_key(self) -> str:
        key = self._key()
        if key is None:
            raise WorkbenchUnavailable("not_configured")
        return key

    async def upload(self, *, call: CallFor, filename: str, mime: str, chunks: AsyncIterator[bytes]) -> UploadedFile:
        """Stream an attachment into the chat's folder.

        Args:
            call: The chat.
            filename: The file's name, one that passed ``name_problem``.
            mime: Its media type, without parameters.
            chunks: Its bytes, as they arrive; closed when the upload returns or raises.

        Returns:
            The stored file.

        Raises:
            ValueError: If ``filename`` is not one flat name or ``mime`` not a bare media type; nothing is sent.
            UploadTooLarge: Past :data:`MAX_ATTACHMENT_BYTES`, or on workbench's ``413``.
            StorageFull: On workbench's ``507``.
            WorkbenchUnavailable: On any other failure, an answer without a valid id included.
        """
        try:
            return await self._upload(call, filename, mime, chunks)
        finally:
            aclose = getattr(chunks, "aclose", None)
            if aclose is not None:
                await aclose()

    async def _upload(self, call: CallFor, filename: str, mime: str, chunks: AsyncIterator[bytes]) -> UploadedFile:
        if _flat_name(filename) is None:
            raise ValueError("filename must be one flat name")
        if not _MEDIA_TYPE.fullmatch(mime):
            raise ValueError("mime must be a bare media type")
        with _rest_errors("upload"):
            key = self._required_key()
            folder = chat_folder(key, call.chat_id)
            boundary = secrets.token_hex(16)
            headers = {
                **self._headers(key, call),
                "Accept-Encoding": "identity",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            }
            body = _multipart(boundary, filename, mime, chunks)
            async with (
                self._http(self._settings.workbench_timeout_s) as client,
                client.stream(
                    "POST", "/files/upload", params={"directory": folder}, content=body, headers=headers
                ) as response,
            ):
                if response.status_code == 413:
                    raise UploadTooLarge
                if response.status_code == 507:
                    raise StorageFull
                answer = await _json_answer(response)
            got = answer if isinstance(answer, dict) else {}
            file_id, path, size = got.get("id"), _inside(folder, got.get("path")), _count(got.get("size"))
            if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id) or path is None or size is None:
                raise WorkbenchUnavailable("bad_answer")
        logger.debug("workbench stored upload '{}': {} bytes", file_id, size)
        return UploadedFile(file_id, path, size)

    async def prepare(
        self, *, call: CallFor, path: str, strips: int = 1, timeout_s: float = PREPARE_TIMEOUT_S
    ) -> Manifest:
        """Have workbench make a file's renders, and say what they are. Idempotent.

        Args:
            call: The chat.
            path: The file's path in the chat's folder.
            strips: How many strips a picture is cut into on this prepare as well; sent only when more than 1.
            timeout_s: The longest wait for each read of the answer; :data:`PREPARE_TIMEOUT_S` unless set.

        Returns:
            The file's manifest.

        Raises:
            WorkbenchNotFound: When the chat has no such file; a path outside its folder is not asked for.
            StorageFull: On workbench's ``507``.
            WorkbenchUnavailable: On any other failure. Its token is the one a ``422`` names
                (``unreadable_file``, ``unsupported_format``, ``read_timeout``), ``http:<status>``,
                ``bad_answer`` for a manifest that cannot be read, or ``timeout`` and ``unreachable``.
        """
        with _rest_errors("prepare"):
            key = self._required_key()
            if _inside(chat_folder(key, call.chat_id), path) is None:
                raise WorkbenchNotFound("not_found")
            params: dict[str, str | int] = {"path": path, **({"strips": strips} if strips > 1 else {})}
            headers = {**self._headers(key, call), "Accept-Encoding": "identity"}
            async with (
                self._http(timeout_s) as client,
                client.stream("POST", "/files/prepare", params=params, headers=headers) as response,
            ):
                if response.status_code == 404:
                    raise WorkbenchNotFound("not_found")
                if response.status_code == 507:
                    raise StorageFull
                if response.status_code == 422:
                    raise WorkbenchUnavailable(await _refusal_token(response))
                answer = await _json_answer(response)
            manifest = _manifest(answer)
            if manifest is None:
                raise WorkbenchUnavailable("bad_answer")
        logger.debug(
            "workbench prepared a file: kind {}, {} pages, {} renditions",
            manifest.kind,
            manifest.total,
            len(manifest.renditions),
        )
        return manifest

    async def measure(self, *, call: CallFor, path: str, timeout_s: float = PREPARE_TIMEOUT_S) -> Measure:
        """Have workbench measure a document of the chat (workbench 0.10): its units and its characters.

        The path goes as the JSON body ``{"path": ...}``, with the headers and the chat of
        :meth:`prepare`.

        Args:
            call: The chat.
            path: The file's path in the chat's folder.
            timeout_s: The longest wait for each read of the answer; :data:`PREPARE_TIMEOUT_S` unless set.

        Returns:
            The measure.

        Raises:
            WorkbenchNotFound: When the chat has no such file; a path outside its folder is not asked for.
            StorageFull: On workbench's ``507``.
            WorkbenchUnavailable: On any other failure. Its token is the one a ``422`` names
                (``unreadable_file``, ``unsupported_format``, ``read_timeout``), ``http:<status>``,
                ``bad_answer`` for a measure that cannot be read, or ``timeout`` and ``unreachable``.
        """
        with _rest_errors("measure"):
            key = self._required_key()
            if _inside(chat_folder(key, call.chat_id), path) is None:
                raise WorkbenchNotFound("not_found")
            headers = {**self._headers(key, call), "Accept-Encoding": "identity"}
            async with (
                self._http(timeout_s) as client,
                client.stream("POST", "/files/measure", json={"path": path}, headers=headers) as response,
            ):
                if response.status_code == 404:
                    raise WorkbenchNotFound("not_found")
                if response.status_code == 507:
                    raise StorageFull
                if response.status_code == 422:
                    raise WorkbenchUnavailable(await _refusal_token(response))
                answer = await _json_answer(response)
            measure = _measure_answer(answer)
            if measure is None:
                raise WorkbenchUnavailable("bad_answer")
        logger.debug(
            "workbench measured a file: {} {} units, {} characters, capped {}",
            measure.total,
            measure.unit,
            measure.chars,
            measure.capped,
        )
        return measure

    async def derived(self, *, call: CallFor, source: str, role: DerivedRole, chunks: AsyncIterator[bytes]) -> str:
        """Stream one of Nextext's artifacts into workbench, as a derived file of a clip of the chat (workbench 0.9).

        Args:
            call: The chat.
            source: The clip's path in the chat's folder.
            role: What the artifact is: ``nextext_jsonl`` (the transcript) or ``nextext_zip`` (the keyframes); it
                is sent as that role's media type.
            chunks: The artifact's bytes, as they arrive; closed when the call returns or raises.

        Returns:
            The derived file's id, 32 hex digits.

        Raises:
            WorkbenchNotFound: When the chat has no such clip, or workbench has none meanwhile; a source outside its
                folder is not asked for.
            StorageFull: On workbench's ``507``.
            WorkbenchUnavailable: On any other failure. Its token is the one a ``422`` names (``unreadable_file``,
                ``unsupported_format``), ``http:<status>`` (``http:413`` for an artifact too large, ``http:503``
                for a clip whose lock was not free in time), ``bad_answer`` for an answer without a valid id, or
                ``timeout`` and ``unreachable``.
        """
        try:
            return await self._derived(call, source, role, chunks)
        finally:
            aclose = getattr(chunks, "aclose", None)
            if aclose is not None:
                await aclose()

    async def _derived(self, call: CallFor, source: str, role: DerivedRole, chunks: AsyncIterator[bytes]) -> str:
        with _rest_errors("derived"):
            key = self._required_key()
            if _inside(chat_folder(key, call.chat_id), source) is None:
                raise WorkbenchNotFound("not_found")
            headers = {**self._headers(key, call), "Accept-Encoding": "identity", "Content-Type": DERIVED_TYPES[role]}
            # The read limit starts when the whole body is sent, so the call is the transfer plus DERIVED_TIMEOUT_S.
            async with (
                self._http(DERIVED_TIMEOUT_S) as client,
                client.stream(
                    "POST", "/files/derived", params={"source": source, "role": role}, content=chunks, headers=headers
                ) as response,
            ):
                if response.status_code == 404:
                    raise WorkbenchNotFound("not_found")
                if response.status_code == 507:
                    raise StorageFull
                if response.status_code == 422:
                    raise WorkbenchUnavailable(await _refusal_token(response))
                answer = await _json_answer(response)
            file_id = answer.get("id") if isinstance(answer, dict) else None
            if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
                raise WorkbenchUnavailable("bad_answer")
        logger.debug("workbench took a derived file '{}' of role {}", file_id, role)
        return file_id

    @contextlib.asynccontextmanager
    async def view(
        self, *, call: CallFor, path: str, rendition: int | None = None, strips: int = 1, strip: int | None = None
    ) -> AsyncIterator[httpx.Response]:
        """Stream a file of the chat back, or one of its renders.

        Args:
            call: The chat.
            path: The file's path in the chat's folder.
            rendition: The page or picture to show as an image, counted from 1; ``None`` for the file itself.
            strips: Into how many strips the render is cut; sent with ``rendition`` only, and only when more than 1.
            strip: Which strip; sent when ``strips`` is more than 1.

        Yields:
            workbench's answer, its body not read yet.

        Raises:
            ValueError: If ``strips`` is more than 1 with a ``rendition`` and ``strip`` is ``None``; nothing is sent.
            WorkbenchNotFound: When the chat has no such file or render; a path outside its folder is not asked for.
            WorkbenchUnavailable: On any other failure.
        """
        params = _view_params(path, rendition, strips, strip)
        with _rest_errors("view"):
            key = self._required_key()
            if _inside(chat_folder(key, call.chat_id), path) is None:
                raise WorkbenchNotFound("not_found")
            headers = {**self._headers(key, call), "Accept-Encoding": "identity"}
            async with (
                self._http(self._settings.workbench_timeout_s) as client,
                client.stream("GET", "/files/view", params=params, headers=headers) as response,
            ):
                if response.status_code == 404:
                    raise WorkbenchNotFound("not_found")
                if response.status_code != 200:
                    raise WorkbenchUnavailable(f"http:{response.status_code}")
                yield response

    def fetch_renditions(self, wanted: Sequence[RenditionRequest], *, call: CallFor) -> list[tuple[str, bytes] | None]:
        """Fetch several renditions at once, for a turn's thread. Never raises.

        One exchange within :data:`RENDITION_TIMEOUT_S`, over one client, all the requests together.
        A picture counts only when workbench answered ``200`` with a jpeg or png under
        :data:`MAX_RENDITION_BYTES` that starts as its type does.

        Args:
            wanted: The renditions, of the chat's own files.
            call: The chat.

        Returns:
            One entry for each of ``wanted``, in order: the media type and bytes, or ``None`` for a
            picture outside the chat's folder, not there, not whole, not in time, or asked for with a
            ``strips`` past 1 and no ``strip``.
        """
        found: list[tuple[str, bytes] | None] = [None] * len(wanted)
        key = self._key()
        if key is not None and wanted:
            try:
                folder = chat_folder(key, call.chat_id)
                headers = {**self._headers(key, call), "Accept-Encoding": "identity"}
                asked = [(i, item) for i, item in enumerate(wanted) if _inside(folder, item.path) is not None]
                _detached(lambda: self._fetch_renditions(found, asked, headers), RENDITION_TIMEOUT_S + _GRACE_S)
            except Exception as exc:
                logger.warning("workbench renditions raised {}", type(exc).__name__)
        got = sum(item is not None for item in found)
        (logger.debug if got == len(wanted) else logger.info)("workbench fetched {} of {} renditions", got, len(wanted))
        return list(found)

    async def _fetch_renditions(
        self,
        found: list[tuple[str, bytes] | None],
        asked: Sequence[tuple[int, RenditionRequest]],
        headers: Mapping[str, str],
    ) -> None:
        """Fetch the renditions together, each into its place of ``found``; what is not in time stays ``None``.

        Args:
            found: The answers, by the position of the request.
            asked: The requests to send, with their positions.
            headers: The chat's headers.
        """
        with contextlib.suppress(TimeoutError), anyio.fail_after(RENDITION_TIMEOUT_S):
            async with self._http(RENDITION_TIMEOUT_S) as client, anyio.create_task_group() as group:
                for index, item in asked:
                    group.start_soon(self._fetch_one, client, found, index, item, headers)

    async def _fetch_one(
        self,
        client: httpx.AsyncClient,
        found: list[tuple[str, bytes] | None],
        index: int,
        item: RenditionRequest,
        headers: Mapping[str, str],
    ) -> None:
        """Fetch one rendition into ``found[index]``; a failure leaves ``None`` and never reaches the task group.

        Args:
            client: The exchange's client.
            found: The answers.
            index: The request's position.
            item: The request.
            headers: The chat's headers.
        """
        try:
            params = _view_params(item.path, item.n, item.strips, item.strip)
            async with client.stream("GET", "/files/view", params=params, headers=headers) as response:
                found[index] = await _picture(response)
        except Exception:
            found[index] = None

    async def delete_upload(self, *, call: CallFor, path: str) -> None:
        """Delete one upload of the chat that its user takes back before sending it, with its renders (workbench 0.8.1).

        Args:
            call: The chat.
            path: The upload's path in the chat's folder.

        Raises:
            WorkbenchNotFound: When the chat has no such upload; a path outside its folder is not asked for.
            WorkbenchUnavailable: On any other failure, a workbench before 0.8.1 among them (``http:405``).
        """
        with _rest_errors("upload delete"):
            key = self._required_key()
            if _inside(chat_folder(key, call.chat_id), path) is None:
                raise WorkbenchNotFound("not_found")
            headers = {**self._headers(key, call), "Accept-Encoding": "identity"}
            async with (
                self._http(self._settings.workbench_timeout_s) as client,
                client.stream("DELETE", "/files/upload", params={"path": path}, headers=headers) as response,
            ):
                if response.status_code == 404:
                    raise WorkbenchNotFound("not_found")
                answer = await _json_answer(response)
            if not isinstance(answer, dict) or _count(answer.get("deleted")) != 1:
                raise WorkbenchUnavailable("bad_answer")
        logger.debug("workbench deleted an upload with {} renders", _count(answer.get("renders")))

    async def delete_chat(self, *, call: CallFor) -> int:
        """Delete the chat's folder in workbench, with its files and jobs.

        Args:
            call: The chat.

        Returns:
            How many files the chat held.

        Raises:
            WorkbenchUnavailable: On any failure.
        """
        with _rest_errors("chat delete"):
            key = self._required_key()
            headers = {**self._headers(key, call), "Accept-Encoding": "identity"}
            async with (
                self._http(self._settings.workbench_timeout_s) as client,
                client.stream("DELETE", "/files/chat", headers=headers) as response,
            ):
                answer = await _json_answer(response)
            deleted = _count(answer.get("deleted")) if isinstance(answer, dict) else None
            if deleted is None:
                raise WorkbenchUnavailable("bad_answer")
        logger.debug("workbench deleted a chat: {} files", deleted)
        return deleted
