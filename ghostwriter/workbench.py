"""Ghostwriter's client for workbench: tools over its MCP door, files over its REST door.

Tools. The MCP transport is docint's (``docint/utils/workbench_client.py``): revision
``2026-07-28`` only, one JSON-RPC message per ``POST {WORKBENCH_API_BASE}/mcp``, no
MCP session. Each exchange (a discovery or a call) is an ``anyio.run`` over a fresh
``httpx.AsyncClient`` that takes no proxy from the environment, on a daemon thread of
its own; the caller waits at most the exchange's deadline plus half a second
(:func:`_detached`), so a slow name lookup never holds a turn, and the client may be
called from any context, a running event loop included.

Discovery (``server/discover``, then ``tools/list``, within :data:`DISCOVERY_TIMEOUT_S`
together) is single-flight and cached per result for its ``ttlMs``, for every chat. It
succeeds when workbench serves this revision and lists at least one tool whose
``inputSchema`` is an object; other tools are dropped. A failure offers no tool and is
tried again at most once per :data:`DISCOVERY_RETRY_S`. A call without an answer, refused
arguments, ``-32022`` or ``-32602`` drops the cache. workbench's discovery reads no
principal; it carries the asking turn's headers only because every request does.

The model reads a result's ``content[0].text``, which workbench writes without the table
previews; the cards come from ``structuredContent.outputs``, kept only inside the chat's
folder, each preview clamped. A refusal is read by its ``structuredContent.reason`` token.
:meth:`WorkbenchClient.call_tool` never raises.

Files. Uploads stream into the chat's folder as multipart, cut off past
:data:`MAX_ATTACHMENT_BYTES`; views stream back; a chat's folder is deleted whole. These
run on the caller's event loop, each over a fresh client that takes no proxy either.

Every request carries the key, the user and the chat as keyed pseudonyms (HMAC-SHA256
under ``WORKBENCH_API_KEY`` over a label, a NUL byte and the owner or the chat id, as 64
hex digits), the purpose and the request id; the chat's pseudonym names its folder. The
log holds tool names, ids, counts, statuses and tokens, never a file name, an argument,
an output or the key.
"""

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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, TypeGuard, TypeVar

import anyio
import httpx
from loguru import logger

from ghostwriter import __version__
from ghostwriter.config import Settings
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
#: Seconds after a failed discovery before the next try.
DISCOVERY_RETRY_S = 60.0
#: The largest answer read; past it the answer is refused.
MAX_ANSWER_BYTES = 4 * 1024 * 1024
#: The largest attachment, as workbench's ``WORKBENCH_UPLOAD_MAX_MB`` counts it.
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
#: A workbench file id.
FILE_ID = re.compile(r"[0-9a-f]{32}")
#: A table preview's most columns, rows and characters a cell, as workbench's executor cuts them.
PREVIEW_COLUMNS = 20
PREVIEW_ROWS = 5
PREVIEW_CELL_CHARS = 100

_PATH = "/mcp"
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
class ToolResult:
    """A call that completed.

    Attributes:
        text: What the model reads, without the previews.
        outputs: The files the call wrote, for the cards.
    """

    text: str
    outputs: tuple[OutputFile, ...]


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
        The text and the outputs, or the failure the answer names.
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
    return ToolResult(text, outputs)


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
        self._flight = threading.Lock()
        self._ids = itertools.count(1)
        self._server: _Server | None = None
        self._tools: _Tools | None = None
        self._retry_at: float | None = None
        self._up: bool | None = None

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
        return httpx.AsyncClient(
            base_url=self._settings.workbench_api_base,
            transport=self._transport,
            trust_env=False,
            timeout=self._timeout(t),
        )

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

    def discover(self, *, call: CallFor) -> Discovery | None:
        """Return the cached discovery, discovering first when a result has expired.

        Concurrent callers wait for the one discovery in progress, never longer than its limit
        plus half a second; a caller that would is offered no tool on this turn.

        Args:
            call: The asking turn's chat. Its headers go with the requests, as with every request
                to workbench; workbench's discovery reads no principal from them.

        Returns:
            The tools and the version, or ``None`` when workbench is not usable on this turn.
        """
        key = self._key()
        if key is None:
            return None
        if not self._flight.acquire(timeout=DISCOVERY_TIMEOUT_S + _GRACE_S):
            return None
        try:
            return self._discover_held(self._headers(key, call))
        finally:
            self._flight.release()

    def _discover_held(self, headers: Mapping[str, str]) -> Discovery | None:
        """Discover, holding the single-flight lock.

        Args:
            headers: The asking turn's headers.

        Returns:
            As :meth:`discover`.
        """
        now = self._clock()
        server, tools = self._server, self._tools
        server = server if server is not None and now < server.expires_at else None
        tools = tools if tools is not None and now < tools.expires_at else None
        if server is not None and tools is not None:
            return Discovery(tools=tools.tools, server_version=server.version)
        if self._retry_at is not None and now < self._retry_at:
            return None
        try:
            fresh_server, fresh_tools = _detached(
                lambda: self._discover(server, tools, now, headers), DISCOVERY_TIMEOUT_S + _GRACE_S
            )
        except _Unusable as exc:
            return self._discovery_failed(now, exc.token)
        except (TimeoutError, httpx.TimeoutException):
            return self._discovery_failed(now, "timeout")
        except httpx.TransportError:
            return self._discovery_failed(now, "unreachable")
        except Exception as exc:
            logger.warning("workbench discovery raised {}", type(exc).__name__)
            return self._discovery_failed(now, "error")
        # Only what was fetched is written back: a forget() meanwhile keeps its effect.
        if fresh_server is not server:
            self._server = fresh_server
        if fresh_tools is not tools:
            self._tools = fresh_tools
        self._retry_at = None
        found = Discovery(tools=fresh_tools.tools, server_version=fresh_server.version)
        if self._up is not True:
            self._up = True
            logger.info(
                "workbench is discovered: {} tools are offered (workbench '{}')",
                len(found.tools),
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

    def _discovery_failed(self, now: float, token: str) -> None:
        self._retry_at = now + DISCOVERY_RETRY_S
        if self._up is not False:
            self._up = False
            logger.warning(
                "workbench discovery failed: {}. No tool is offered; the next try comes after {} s",
                token,
                int(DISCOVERY_RETRY_S),
            )

    def forget(self) -> None:
        """Drop both cached results, so the next turn discovers again."""
        self._server = None
        self._tools = None

    # --- tools -------------------------------------------------------------

    def call_tool(
        self, name: str, arguments: Mapping[str, Any], *, call: CallFor, timeout_s: float
    ) -> ToolResult | ToolFailure:
        """Call a tool for a chat, within one deadline. Never raises.

        Args:
            name: The tool.
            arguments: Its arguments, as the model wrote them.
            call: The chat.
            timeout_s: The deadline for the whole call, name lookup to last byte. The caller
                is answered within it plus half a second.

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
                headers = {**self._headers(key, call), "Mcp-Name": name}
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

    @contextlib.asynccontextmanager
    async def view(self, *, call: CallFor, path: str) -> AsyncIterator[httpx.Response]:
        """Stream a file of the chat back.

        Args:
            call: The chat.
            path: The file's path in the chat's folder.

        Yields:
            workbench's answer, its body not read yet.

        Raises:
            WorkbenchNotFound: When the chat has no such file; a path outside its folder is not asked for.
            WorkbenchUnavailable: On any other failure.
        """
        with _rest_errors("view"):
            key = self._required_key()
            if _inside(chat_folder(key, call.chat_id), path) is None:
                raise WorkbenchNotFound("not_found")
            headers = {**self._headers(key, call), "Accept-Encoding": "identity"}
            async with (
                self._http(self._settings.workbench_timeout_s) as client,
                client.stream("GET", "/files/view", params={"path": path}, headers=headers) as response,
            ):
                if response.status_code == 404:
                    raise WorkbenchNotFound("not_found")
                if response.status_code != 200:
                    raise WorkbenchUnavailable(f"http:{response.status_code}")
                yield response

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
