"""Stand-ins for Ghostwriter's neighbours, built from the shared contract alone.

:class:`FakeDoor` is workbench as Ghostwriter's key reaches it: ``/mcp`` and the
REST routes ``/files/upload``, ``/files/prepare``, ``/files/derived``, ``/files/view`` and ``/files/chat``.
:class:`FakeOpenAI` is the router as the OpenAI SDK reaches it, fed by the chunk
builders :func:`text_chunk`, :func:`reasoning_chunk`, :func:`call_chunk` and
:func:`usage_chunk`. :class:`FakeWorkbench` and :class:`RecordingAudit` are workbench
and a turn's audit as the agent loop reaches them. :class:`FakeNextext` is Nextext's
``/api/v1`` as Ghostwriter's service token reaches it. Every name, id and value here is
invented.
"""

import base64
import dataclasses
import json
import re
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Final, cast
from urllib.parse import quote

import anyio
import httpx
import openai
from openai.types.audio import Transcription
from openai.types.chat import ChatCompletionChunk
from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartParser

from ghostwriter.agent.stream import RoundDone
from ghostwriter.utils.audit_event import AuditWriteError
from ghostwriter.workbench import (
    META_SERVER_INFO,
    CallFor,
    Discovery,
    McpTool,
    RenditionRequest,
    ToolFailure,
    ToolResult,
    WorkbenchClient,
)
from tests.conftest import KEY, NEXTEXT_KEY

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
#: The version the fakes' workbench names itself, unless a test sets another.
SERVER_VERSION = "0.3.0"
SERVER_META = {META_SERVER_INFO: {"name": "workbench", "version": SERVER_VERSION}}
#: The id workbench gives the output of :func:`call_result`.
OUTPUT_ID = "0" * 32
#: The id the door gives every upload, unless a test sets another.
UPLOAD_ID = "1" * 32
#: The id the door gives every derived file, unless a test scripts another answer.
DERIVED_ID = "2" * 32
NOT_FOUND = {"detail": "not found"}
#: Bytes that pass the signature check of a jpeg and of a png, and no more.
TINY_JPEG = b"\xff\xd8\xff\xe0" + bytes(60)
TINY_PNG = b"\x89PNG\r\n\x1a\n" + bytes(56)
#: The header workbench 0.7 lists ``view_file`` and attaches images for.
ACCEPT_IMAGES_HEADER = "x-accept-images"
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif")
_MEDIA_SUFFIXES = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac")
#: The roles workbench 0.9 takes a derived file for: a clip's transcript and its keyframes.
_DERIVED_ROLES = frozenset({"nextext_jsonl", "nextext_zip"})

#: What workbench 0.7 tells a call that takes no images of ``read_document``: 0.6's text, which promises none.
READ_DOCUMENT_TEXT = "Read a document as text, from its text layer only."
#: The ten workspace tools of the contract: name, description (as a call that takes images is told) and input
#: properties.
CONTRACT_TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "list_files": ("List the files in the chat's folder.", {"path": {"type": "string"}}),
    "read_document": (
        "Read a document as text; pages without text come back as images.",
        {"path": {"type": "string"}, "strips": {"type": "integer"}},
    ),
    "search_files": ("Search the chat's files for a text.", {"query": {"type": "string"}}),
    "run_python": ("Run Python on the chat's files.", {"code": {"type": "string"}}),
    "inspect_template": ("List a template's fields.", {"path": {"type": "string"}}),
    "fill_template": ("Fill a template's fields.", {"path": {"type": "string"}, "values": {"type": "object"}}),
    "write_table": ("Write a table as csv or xlsx.", {"name": {"type": "string"}, "rows": {"type": "array"}}),
    "write_document": ("Write a document.", {"name": {"type": "string"}, "text": {"type": "string"}}),
    "view_file": (
        "Show the pages or the picture of a file.",
        {"path": {"type": "string"}, "part": {"type": "string"}, "strips": {"type": "integer"}},
    ),
    "write_presentation": (
        "Build a presentation in a slide master.",
        {"path": {"type": "string"}, "name": {"type": "string"}, "slides": {"type": "array"}},
    ),
}

_ROUTES = frozenset(
    {
        ("POST", "/mcp"),
        ("POST", "/files/upload"),
        ("DELETE", "/files/upload"),
        ("POST", "/files/prepare"),
        ("POST", "/files/derived"),
        ("GET", "/files/view"),
        ("DELETE", "/files/chat"),
    }
)
_DISCOVERY = frozenset({"server/discover", "tools/list"})
#: The key a ``DELETE`` of an upload is scripted and delayed by in :attr:`FakeDoor.answers`, apart from its ``POST``.
TAKE_BACK: Final = "DELETE /files/upload"


@dataclass(frozen=True)
class RpcError:
    """A JSON-RPC error the door answers with.

    Attributes:
        code: The error code.
        message: Its text.
        status: The HTTP status it comes with.
    """

    code: int
    message: str
    status: int = 400


@dataclass(frozen=True)
class Upload:
    """A file part the door parsed out of an upload, as workbench's FastAPI parser reads it.

    Attributes:
        filename: The part's file name.
        content_type: The part's media type.
        data: Its bytes.
    """

    filename: str
    content_type: str
    data: bytes


def discover_result(server_version: str = SERVER_VERSION) -> dict[str, Any]:
    """Return workbench's ``server/discover`` result.

    Args:
        server_version: The version workbench names itself.

    Returns:
        The result, serving 2026-07-28 for an hour.
    """
    return {
        "supportedVersions": ["2026-07-28", "2025-11-25", "2025-06-18"],
        "capabilities": {"tools": {"listChanged": False}},
        "ttlMs": 3600000,
        "cacheScope": "private",
        "resultType": "complete",
        "_meta": {META_SERVER_INFO: {"name": "workbench", "version": server_version}},
    }


def tools_result() -> dict[str, Any]:
    """Return workbench's ``tools/list`` result for the ghostwriter caller.

    Returns:
        The result, the ten contract tools by name, as workbench lists them to a call that accepts
        images (``view_file`` included), cached for 300 s.
    """
    tools = [
        {
            "name": name,
            "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": sorted(properties)},
        }
        for name, (description, properties) in sorted(CONTRACT_TOOLS.items())
    ]
    return {"tools": tools, "ttlMs": 300000, "cacheScope": "private", "resultType": "complete", "_meta": SERVER_META}


def output(folder: str, **changes: Any) -> dict[str, Any]:
    """Return one entry of ``structuredContent.outputs``: an xlsx with its preview.

    Args:
        folder: The chat's folder.
        **changes: Fields to set instead.

    Returns:
        The entry.
    """
    preview = {"columns": ["month", "sum"], "rows": [["2026-01", "1200"]], "row_count": 12, "truncated": True}
    entry = {
        "id": OUTPUT_ID,
        "path": f"{folder}/out/sums.xlsx",
        "name": "sums.xlsx",
        "mime": XLSX,
        "size": 48213,
        "preview": preview,
    }
    return {**entry, **changes}


def call_result(folder: str, outputs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return a successful ``tools/call`` result: the text without previews, the outputs with them.

    Args:
        folder: The chat's folder.
        outputs: The outputs; one xlsx with its preview when omitted.

    Returns:
        The result.
    """
    listed = [output(folder)] if outputs is None else outputs
    structured = {"stdout": "12 months summed", "stderr": "", "truncated": False, "outputs": listed}
    told = {**structured, "outputs": [{k: v for k, v in item.items() if k != "preview"} for item in listed]}
    return {
        "resultType": "complete",
        "isError": False,
        "content": [{"type": "text", "text": json.dumps(told)}],
        "structuredContent": structured,
        "_meta": SERVER_META,
    }


def refusal(token: str) -> dict[str, Any]:
    """Return a refused ``tools/call`` result.

    Args:
        token: The reason.

    Returns:
        The result, its text the one every refusal carries.
    """
    return {
        "resultType": "complete",
        "isError": True,
        "content": [{"type": "text", "text": "busy, retry later"}],
        "structuredContent": {"reason": token},
        "_meta": SERVER_META,
    }


def image_block(data: bytes, mime: str = "image/jpeg") -> dict[str, Any]:
    """Return one image block of a ``tools/call`` result, as workbench writes it.

    Args:
        data: The picture's bytes.
        mime: Its media type.

    Returns:
        The block, its bytes as base64 and addressed to the assistant.
    """
    return {
        "type": "image",
        "data": base64.b64encode(data).decode("ascii"),
        "mimeType": mime,
        "annotations": {"audience": ["assistant"]},
    }


def view_result(folder: str, shown: list[dict[str, Any]], blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a successful ``view_file`` result: the text, then one image block for each entry of ``shown``.

    Args:
        folder: The chat's folder.
        shown: The entries of ``structuredContent.shown``, in the order of the blocks.
        blocks: The image blocks, see :func:`image_block`.

    Returns:
        The result, with the contract's eight structured keys and no outputs.
    """
    structured = {
        "path": f"{folder}/quay-scan.pdf",
        "kind": "pdf",
        "total": max(len(shown), 3),
        "shown": shown,
        "truncated": False,
        "next_part": None,
        "hint": None,
        "outputs": list[Any](),
    }
    return {
        "resultType": "complete",
        "isError": False,
        "content": [{"type": "text", "text": json.dumps(structured)}, *blocks],
        "structuredContent": structured,
        "_meta": SERVER_META,
    }


def stock_manifest(path: str, roles: Collection[str] = ()) -> dict[str, Any]:
    """Return the manifest the door gives a stored file, by its suffix: workbench 0.9's ten keys.

    Args:
        path: The file's path.
        roles: The roles of the derived files posted for it; read for a clip only.

    Returns:
        A picture: one 64 x 48 rendition. A pdf: three pages of 1131 x 1600, the second without
        text. A deck (``.pptx``, ``.potx``): kind ``office``, unit ``slide``, three slides drawn
        as a pdf's pages are, the second without text. A Word or OpenDocument text file (``.docx``,
        ``.dotx``, ``.odt``): kind ``office``, three pages, none without text. A clip (the 11 media
        suffixes): kind ``media``, unit ``frame``, and from what was posted for it: a ``nextext_jsonl``
        gives ``transcript`` true and a ``duration_s`` of 91.0, a ``nextext_zip`` three frames of 1280 x 720
        at 0, 4 and 8 s and a ``duration_s`` of 8.0 (the later of the two when both were posted), neither no
        frames and a ``duration_s`` of ``None``. Anything else: kind ``none``. The unit is ``page`` for every kind
        but a deck and a clip, and ``duration_s`` is ``None`` and ``transcript`` false for every kind but a clip.
    """
    lower = path.lower()
    manifest: dict[str, Any] = {
        "path": path,
        "kind": "none",
        "total": 0,
        "pages_without_text": [],
        "renditions": [],
        "frames": None,
        "unit": "page",
        "hint": None,
        "duration_s": None,
        "transcript": False,
    }
    if lower.endswith(_MEDIA_SUFFIXES):
        frames = [{"n": n, "width": 1280, "height": 720, "mime": "image/jpeg", "bytes": 48213} for n in (1, 2, 3)]
        spoken, framed = "nextext_jsonl" in roles, "nextext_zip" in roles
        # As workbench writes it: the later of the last speech's end and the last frame's time (frames at 0, 4 and 8 s).
        times = [*([91.0] if spoken else []), *([8.0] if framed else [])]
        return {
            **manifest,
            "kind": "media",
            "unit": "frame",
            "total": 3 if framed else 0,
            "renditions": frames if framed else [],
            "duration_s": max(times) if times else None,
            "transcript": spoken,
        }
    if lower.endswith(_IMAGE_SUFFIXES):
        rendition = {"n": 1, "width": 64, "height": 48, "mime": "image/jpeg", "bytes": len(TINY_JPEG)}
        return {**manifest, "kind": "image", "total": 1, "renditions": [rendition]}
    pages = [{"n": n, "width": 1131, "height": 1600, "mime": "image/jpeg", "bytes": 182311} for n in (1, 2, 3)]
    if lower.endswith(".pdf"):
        return {**manifest, "kind": "pdf", "total": 3, "pages_without_text": [2], "renditions": pages}
    if lower.endswith((".pptx", ".potx")):
        return {
            **manifest,
            "kind": "office",
            "unit": "slide",
            "total": 3,
            "pages_without_text": [2],
            "renditions": pages,
        }
    if lower.endswith((".docx", ".dotx", ".odt")):
        return {**manifest, "kind": "office", "total": 3, "renditions": pages}
    return manifest


async def _parse_upload(request: httpx.Request) -> Upload | None:
    """Read the ``file`` part of an upload with Starlette's parser, which workbench's route uses.

    Args:
        request: The upload.

    Returns:
        The part, or ``None`` when the body holds no ``file`` part.
    """

    async def body() -> Any:
        yield request.content

    try:
        form = await MultiPartParser(Headers({"content-type": request.headers["content-type"]}), body()).parse()
    except Exception:
        return None
    try:
        part = form.get("file")
        if not isinstance(part, UploadFile):
            return None
        return Upload(part.filename or "", part.content_type or "", await part.read())
    finally:
        await form.close()


class FakeDoor:
    """workbench's doors as Ghostwriter's key reaches them, for ``httpx.MockTransport``.

    It refuses a wrong key (401), any other route (404) and a missing or ``default``
    session (400, or ``-32600`` on ``tools/call``); as workbench's, its discovery reads
    no session. It answers ``/mcp`` by method and the REST
    routes by path, from :attr:`answers`: a result or JSON body, an ``httpx.Response``,
    an :class:`RpcError`, an exception to raise, or a callable that takes the chat's
    folder and returns one of those. ``None`` for ``/files/upload``, :data:`TAKE_BACK`, ``/files/prepare``,
    ``/files/derived`` and ``/files/view`` is the default: an upload is parsed, stored in :attr:`files` and answered
    with its path, size and :attr:`upload_id`; a take-back deletes a stored file of the chat (404 for none); a
    prepare answers the file's manifest, a clip's from the derived files posted for it
    (:func:`stock_manifest`); a derived file is recorded in :attr:`derived` and answered with its id, role and size
    (404 for a source the chat does not hold, 422 for another role or an empty body); a view serves a stored file
    of the chat, or with ``rendition`` one of its renders.

    As workbench 0.7's, its ``tools/list`` answer holds ``view_file`` and the image text of
    ``read_document`` only for a request that carries ``X-Accept-Images: 1``; without it, it is
    :func:`listed_without_images`. Every request is kept with its headers in :attr:`requests`.

    Attributes:
        key: The key the door accepts.
        requests: Every request, with its body: the JSON-RPC message, the parsed upload, or ``None``.
        answers: The scripted answers, by MCP method or REST path.
        delays: Seconds to wait before answering, by MCP method or REST path.
        files: The stored files, by path: their bytes and media type.
        derived: The derived files posted, in order: the source's path, the role and the body.
        manifests: The manifest to answer for a path, instead of :func:`stock_manifest`'s.
        upload_id: The id every upload gets.
        last_call_answer: The last result ``tools/call`` was answered with.
        on_delete: Called when a ``DELETE /files/chat`` the door accepts arrives, before it is answered.
    """

    def __init__(self, key: str = KEY, *, server_version: str = SERVER_VERSION) -> None:
        """Answer as a healthy workbench does, without delay.

        Args:
            key: The key the door accepts.
            server_version: The version it names itself in ``server/discover``.
        """
        self.key = key
        self.requests: list[tuple[httpx.Request, Any]] = []
        self.answers: dict[str, object] = {
            "server/discover": discover_result(server_version),
            "tools/list": tools_result(),
            "tools/call": call_result,
            "/files/upload": None,
            TAKE_BACK: None,
            "/files/prepare": None,
            "/files/derived": None,
            "/files/view": None,
            "/files/chat": {"deleted": 3},
        }
        self.delays: dict[str, float] = {}
        self.files: dict[str, tuple[bytes, str]] = {}
        self.derived: list[tuple[str, str, bytes]] = []
        self.manifests: dict[str, dict[str, Any]] = {}
        self.upload_id: str = UPLOAD_ID
        self.last_call_answer: dict[str, Any] | None = None
        self.on_delete: Callable[[], None] | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record the request and answer it.

        Args:
            request: The request the client sent, its body read.

        Returns:
            The answer.

        Raises:
            Exception: The scripted exception, when one is set.
        """
        path = request.url.path
        body: Any = None
        if path == "/mcp":
            body = json.loads(request.content)
        elif path == "/files/upload" and request.method == "POST":
            body = await _parse_upload(request)
        self.requests.append((request, body))
        route = body["method"] if path == "/mcp" else path
        if (request.method, path) == ("DELETE", "/files/upload"):
            route = TAKE_BACK
        if route in self.delays:
            await anyio.sleep(self.delays[route])
        if request.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"detail": "unauthorized"})
        if (request.method, path) not in _ROUTES:
            return httpx.Response(404, json=NOT_FOUND)
        session = request.headers.get("x-session-id", "")
        if session in ("", "default") and route not in _DISCOVERY:
            if path == "/mcp":
                return _rpc_error(body, RpcError(-32600, "invalid session"))
            return httpx.Response(400, json={"detail": "invalid session"})
        folder = f"/workspace/{session}"
        if route == "/files/chat" and self.on_delete is not None:
            self.on_delete()
        answer = self.answers.get(route)
        if callable(answer):
            answer = answer(folder)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        if path == "/mcp":
            if isinstance(answer, RpcError):
                return _rpc_error(body, answer)
            if route == "tools/call" and isinstance(answer, dict):
                self.last_call_answer = answer
            if route == "tools/list" and isinstance(answer, dict) and not _accepts_images(request):
                answer = listed_without_images(answer)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": answer})
        if answer is None and route == TAKE_BACK:
            return self._take_back(request, folder)
        if answer is None and path == "/files/upload":
            return self._store(request, body, folder)
        if answer is None and path == "/files/prepare":
            return self._prepare(request, folder)
        if answer is None and path == "/files/derived":
            return self._derive(request, folder)
        if answer is None and path == "/files/view":
            return self._serve(request, folder)
        return httpx.Response(200, json=answer)

    def _store(self, request: httpx.Request, upload: Upload | None, folder: str) -> httpx.Response:
        """Store an upload into the chat's folder, as workbench does for Ghostwriter.

        Args:
            request: The upload.
            upload: Its parsed file part.
            folder: The chat's folder.

        Returns:
            The path, size and id; 404 for another folder, 400 without a file part.
        """
        if upload is None:
            return httpx.Response(400, json={"detail": "invalid upload"})
        if request.url.params.get("directory") != folder:
            return httpx.Response(404, json=NOT_FOUND)
        path = f"{folder}/{upload.filename}"
        self.files[path] = (upload.data, upload.content_type)
        return httpx.Response(200, json={"path": path, "size": len(upload.data), "id": self.upload_id})

    def _take_back(self, request: httpx.Request, folder: str) -> httpx.Response:
        """Delete a stored upload of the chat, as workbench 0.8.1 does for Ghostwriter.

        Args:
            request: The delete, its ``path`` a query parameter.
            folder: The chat's folder.

        Returns:
            ``deleted`` 1 and no renders; 404 for a file the chat does not hold.
        """
        path = request.url.params.get("path", "")
        if not path.startswith(f"{folder}/") or path not in self.files:
            return httpx.Response(404, json=NOT_FOUND)
        del self.files[path]
        return httpx.Response(200, json={"deleted": 1, "renders": 0})

    def _serve(self, request: httpx.Request, folder: str) -> httpx.Response:
        """Serve a stored file of the chat.

        Args:
            request: The view.
            folder: The chat's folder.

        Returns:
            The file; 404 for a path outside the chat or a missing file alike.
        """
        path = request.url.params.get("path", "")
        if "rendition" in request.url.params:
            return self._render(request, path, folder)
        stored = self.files.get(path) if path.startswith(f"{folder}/") else None
        if stored is None:
            return httpx.Response(404, json=NOT_FOUND)
        data, mime = stored
        name = quote(path.rsplit("/", 1)[-1], safe="")
        disposition = f"inline; filename*=UTF-8''{name}"
        return httpx.Response(200, content=data, headers={"Content-Type": mime, "Content-Disposition": disposition})

    def _manifest_of(self, path: str, folder: str) -> dict[str, Any] | None:
        """Return the manifest of a file of the chat.

        Args:
            path: The file's path.
            folder: The chat's folder.

        Returns:
            The scripted manifest of the path, else the stock one of a stored file; ``None`` for
            another chat's path and for an unknown one.
        """
        if not path.startswith(f"{folder}/"):
            return None
        if path in self.manifests:
            return self.manifests[path]
        if path not in self.files:
            return None
        return stock_manifest(path, [role for source, role, _ in self.derived if source == path])

    def _prepare(self, request: httpx.Request, folder: str) -> httpx.Response:
        """Answer a prepare with the file's manifest.

        Args:
            request: The prepare.
            folder: The chat's folder.

        Returns:
            The manifest; 404 for a path outside the chat or an unknown one.
        """
        manifest = self._manifest_of(request.url.params.get("path", ""), folder)
        return httpx.Response(404, json=NOT_FOUND) if manifest is None else httpx.Response(200, json=manifest)

    def _derive(self, request: httpx.Request, folder: str) -> httpx.Response:
        """Take a derived file of a stored file of the chat, as workbench 0.9 does for Ghostwriter.

        Args:
            request: The post: ``source`` and ``role`` query parameters, the artifact as the raw body.
            folder: The chat's folder.

        Returns:
            ``id``, ``role``, ``bytes`` and ``replaced`` (``None``); 404 for a source the chat does not hold, 422
            for a role it does not know and for an empty body.
        """
        source, role = request.url.params.get("source", ""), request.url.params.get("role", "")
        if not source.startswith(f"{folder}/") or source not in self.files:
            return httpx.Response(404, json=NOT_FOUND)
        if role not in _DERIVED_ROLES:
            return httpx.Response(422, json={"detail": [{"loc": ["query", "role"], "msg": "unknown role"}]})
        if not request.content:
            return httpx.Response(422, json={"detail": "unreadable_file: the artifact is empty"})
        self.derived.append((source, role, request.content))
        return httpx.Response(
            200, json={"id": DERIVED_ID, "role": role, "bytes": len(request.content), "replaced": None}
        )

    def _render(self, request: httpx.Request, path: str, folder: str) -> httpx.Response:
        """Serve one render of a file of the chat: a page, a picture or one of its strips.

        Args:
            request: The view, with ``rendition`` and, for strips, ``strips`` and ``strip``.
            path: The file's path.
            folder: The chat's folder.

        Returns:
            The picture, as ``image/jpeg``: the same bytes for every page, and for strip ``i`` of
            ``k > 1`` those bytes and ``i`` after them; 404 outside ``1..total``, 422 for a bad
            ``strips`` and ``strip`` pair.
        """
        manifest = self._manifest_of(path, folder)
        params = request.url.params
        number, strips, strip = params["rendition"], params.get("strips", "1"), params.get("strip")
        if manifest is None or not number.isdigit() or not 1 <= int(number) <= manifest["total"]:
            return httpx.Response(404, json=NOT_FOUND)
        cut = int(strips) if strips.isdigit() else 0
        if (
            not 1 <= cut <= 3
            or (cut > 1) != (strip is not None)
            or (strip is not None and strip not in tuple("123"[:cut]))
        ):
            return httpx.Response(422, json={"detail": "bad strips"})
        data = TINY_JPEG if strip is None else TINY_JPEG + bytes([int(strip)])
        return httpx.Response(200, content=data, headers={"Content-Type": "image/jpeg"})

    def methods(self) -> list[str]:
        """Return the MCP method of every ``/mcp`` request so far, in order.

        Returns:
            The methods.
        """
        return [body["method"] for request, body in self.requests if request.url.path == "/mcp"]

    def last(self, path: str, method: str | None = None) -> httpx.Request:
        """Return the last request to a path, by any method or the one given.

        Args:
            path: The route, such as ``/files/upload``.
            method: The method, such as ``DELETE``; any when left out.

        Returns:
            The request.

        Raises:
            LookupError: When no request went there.
        """
        for request, _ in reversed(self.requests):
            if request.url.path == path and method in (None, request.method):
                return request
        raise LookupError(path)

    @property
    def uploads(self) -> list[Upload]:
        """Every upload's parsed file part, in order."""
        return [body for _, body in self.requests if isinstance(body, Upload)]


def _accepts_images(request: httpx.Request) -> bool:
    """Tell whether a request asks for images, as workbench 0.7 reads ``X-Accept-Images``.

    Args:
        request: The request.

    Returns:
        ``True`` when the header is ``1``.
    """
    return request.headers.get(ACCEPT_IMAGES_HEADER) == "1"


def listed_without_images(listing: dict[str, Any]) -> dict[str, Any]:
    """Return a ``tools/list`` result as workbench 0.7 lists it to a call that takes no images.

    Args:
        listing: The result, as a call that takes images gets it.

    Returns:
        A copy without ``view_file``, and ``read_document`` told as 0.6 told it (:data:`READ_DOCUMENT_TEXT`).
    """
    tools = [
        {**tool, "description": READ_DOCUMENT_TEXT} if tool.get("name") == "read_document" else tool
        for tool in listing["tools"]
        if tool.get("name") != "view_file"
    ]
    return {**listing, "tools": tools}


def _rpc_error(body: dict[str, Any], error: RpcError) -> httpx.Response:
    """Answer a JSON-RPC request with an error.

    Args:
        body: The request.
        error: The error.

    Returns:
        The answer.
    """
    payload = {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": error.code, "message": error.message}}
    return httpx.Response(error.status, json=payload)


# --- the router, as the OpenAI SDK reaches it ---------------------------------

#: The model every chunk names, unless a usage chunk names another.
SERVED_MODEL = "served-model"
#: One chunk as the router streams it: the JSON of a ``ChatCompletionChunk``.
Chunk = dict[str, Any]
#: A chunk that comes late: called when the reply reaches it, it returns the chunk or raises,
#: as a router that stalls and then answers or times out.
Gate = Callable[[], Chunk]
#: A scripted round: its chunks, any of them an exception raised once it is reached or a
#: :data:`Gate`, or an exception the request itself raises.
Round = Sequence[Chunk | Exception | Gate] | Exception


def _chunk(choices: list[dict[str, Any]], **fields: Any) -> Chunk:
    """Return a chunk with the fields every chunk carries.

    Args:
        choices: Its choices.
        **fields: Further fields, or defaults replaced.

    Returns:
        The chunk.
    """
    head = {"id": "chatcmpl-0001", "object": "chat.completion.chunk", "created": 1_790_000_000, "model": SERVED_MODEL}
    return {**head, "choices": choices, **fields}


def _delta(delta: dict[str, Any]) -> Chunk:
    """Return a chunk whose one choice carries ``delta``.

    Args:
        delta: The choice's delta.

    Returns:
        The chunk.
    """
    return _chunk([{"index": 0, "delta": delta, "finish_reason": None}])


def text_chunk(t: str) -> Chunk:
    """Return a chunk carrying a piece of the reply's content.

    Args:
        t: The piece.

    Returns:
        The chunk.
    """
    return _delta({"content": t})


def reasoning_chunk(t: str, *, field: str = "reasoning_content") -> Chunk:
    """Return a chunk carrying a piece of reasoning apart from the content, as a router with a parser sends it.

    Args:
        t: The piece.
        field: The delta's field: ``reasoning_content`` (vLLM, LiteLLM) or ``reasoning``.

    Returns:
        The chunk.
    """
    return _delta({field: t})


def call_chunk(index: int, id: str | None, name: str | None, args: str) -> Chunk:
    """Return a chunk carrying one tool-call fragment.

    Args:
        index: The call's index in the round.
        id: Its id, which only a call's first fragment carries; ``None`` for a continuation.
        name: Its name, likewise.
        args: A piece of its JSON arguments.

    Returns:
        The chunk.
    """
    function: dict[str, Any] = {"arguments": args}
    if name is not None:
        function["name"] = name
    entry: dict[str, Any] = {"index": index, "function": function}
    if id is not None:
        entry |= {"id": id, "type": "function"}
    return _delta({"tool_calls": [entry]})


def finish_chunk(reason: str) -> Chunk:
    """Return the chunk that ends a round's choice with its finish reason.

    Args:
        reason: The finish reason: ``stop``, ``tool_calls`` or ``length``.

    Returns:
        The chunk, its delta empty.
    """
    return _chunk([{"index": 0, "delta": {}, "finish_reason": reason}])


def usage_chunk(prompt: int, completion: int, model: str) -> Chunk:
    """Return the last chunk, which ``stream_options.include_usage`` asks for: no choice, the token counts.

    Args:
        prompt: The prompt's tokens.
        completion: The reply's tokens.
        model: The model that served.

    Returns:
        The chunk.
    """
    usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
    return _chunk([], model=model, usage=usage)


class FakeStream:
    """A streamed reply as the SDK's ``openai.Stream`` hands it out: read once, closable.

    Each chunk goes out parsed into the SDK's ``ChatCompletionChunk``; an exception in a
    chunk's place is raised there, as a reply cut off mid-stream is, and a :data:`Gate` is
    called there. Reading on after :meth:`close` fails, as it does on the SDK's closed response.

    Attributes:
        sent: How many chunks went out.
        closed: Whether :meth:`close` was called.
    """

    def __init__(self, chunks: Sequence[Chunk | Exception | Gate]) -> None:
        """Hold the reply.

        Args:
            chunks: Its chunks, in order.
        """
        self.sent = 0
        self.closed = False
        self._chunks = self._read(list(chunks))

    def _read(self, chunks: list[Chunk | Exception | Gate]) -> Iterator[ChatCompletionChunk]:
        """Hand out the chunks.

        Args:
            chunks: The chunks.

        Yields:
            Each chunk, parsed.

        Raises:
            Exception: The one scripted in a chunk's place, or the one a gate raises.
            RuntimeError: When read after :meth:`close`.
        """
        for chunk in chunks:
            if self.closed:
                raise RuntimeError("read after close")
            if isinstance(chunk, Exception):
                raise chunk
            if callable(chunk):
                chunk = chunk()
            self.sent += 1
            yield ChatCompletionChunk.model_validate(chunk)

    def __iter__(self) -> Iterator[ChatCompletionChunk]:
        """Read the reply.

        Returns:
            The chunks not read yet.
        """
        return self._chunks

    def close(self) -> None:
        """Close the reply, as the SDK releases its connection."""
        self.closed = True


class FakeOpenAI:
    """``openai.OpenAI`` as Ghostwriter calls it: ``chat.completions.create`` and ``audio.transcriptions.create``.

    Each chat call answers the next scripted round: a :class:`FakeStream` of its chunks, or
    the round's exception raised, as the SDK raises a request the router refused. A call the
    script has no round for fails the test. Every call's keyword arguments are recorded.

    Attributes:
        script: The rounds not answered yet, in order.
        calls: Every chat call's keyword arguments, in order.
        streams: The stream each answered chat call returned.
        transcript: What every transcription answers: its text, or an exception to raise.
        transcript_model: The model a transcription names, as a router may add it; ``None`` names
            none, as the SDK's own result has no such field.
        transcriptions: Every transcription call's keyword arguments, in order.
        chat: Holds ``completions.create``.
        audio: Holds ``transcriptions.create``.
    """

    def __init__(self, rounds: Sequence[Round] = (), *, transcript: str | Exception = "Summe der Spalte B") -> None:
        """Script the router.

        Args:
            rounds: The rounds to answer, in order.
            transcript: What every transcription answers.
        """
        self.script: list[Round] = list(rounds)
        self.calls: list[dict[str, Any]] = []
        self.streams: list[FakeStream] = []
        self.transcript = transcript
        self.transcript_model: str | None = None
        self.transcriptions: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._complete))
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self._transcribe))

    def as_client(self) -> openai.OpenAI:
        """Return this fake typed as the SDK's client, for ``ModelClient(settings, client=...)``.

        Returns:
            The fake.
        """
        return cast(openai.OpenAI, self)

    def _complete(self, **kwargs: Any) -> FakeStream:
        """Answer a chat call with the next scripted round.

        Args:
            **kwargs: The call's keyword arguments.

        Returns:
            The round's stream.

        Raises:
            Exception: The round's exception.
            AssertionError: When the script has no round left.
        """
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("a model round the test did not script")
        answer = self.script.pop(0)
        if isinstance(answer, Exception):
            raise answer
        stream = FakeStream(answer)
        self.streams.append(stream)
        return stream

    def _transcribe(self, **kwargs: Any) -> Transcription:
        """Answer a transcription call.

        Args:
            **kwargs: The call's keyword arguments.

        Returns:
            The transcript.

        Raises:
            Exception: The scripted exception.
        """
        self.transcriptions.append(kwargs)
        if isinstance(self.transcript, Exception):
            raise self.transcript
        if self.transcript_model is None:
            return Transcription(text=self.transcript)
        return Transcription.model_validate({"text": self.transcript, "model": self.transcript_model})


# --- workbench and the audit, as the agent loop reaches them ------------------

#: What :class:`FakeWorkbench` answers a call with: an outcome, or a callable that returns one
#: (it may move a clock, set a stop, or raise to stand for a fault).
Answer = ToolResult | ToolFailure | Callable[[], ToolResult | ToolFailure]
#: What a call answers when the script has no answer left.
PLAIN_RESULT = ToolResult('{"text": "Umsatz 42"}', ())


def contract_tools() -> tuple[McpTool, ...]:
    """Return the ten contract tools as discovery hands them out.

    Returns:
        The tools, in workbench's order.
    """
    return tuple(McpTool(t["name"], t["description"], t["inputSchema"]) for t in tools_result()["tools"])


@dataclass(frozen=True)
class SentCall:
    """A call the loop sent to workbench.

    Attributes:
        name: The tool.
        arguments: Its arguments.
        call: The chat it was made for.
        timeout_s: Its deadline.
        images: Whether it asked workbench for images (``X-Accept-Images: 1``).
    """

    name: str
    arguments: dict[str, Any]
    call: CallFor
    timeout_s: float
    images: bool = False


class RecordingAudit:
    """A turn's audit that records each write as a line, or fails it.

    Attributes:
        log: ``disclose:<tool>:<round>:<attempt>`` and ``model:<round>:<outcome>``, in order.
        rounds: Each ``model_called``'s round, as the loop reported it.
        images: Each ``model_called``'s count of the images the round sent.
        call_ids: Each ``disclosed``'s call id, in order.
        failing: The writes that fail, ``disclose`` or ``model``.
        on_model: Called with the round after each ``model_called`` was written.
    """

    def __init__(self) -> None:
        """Record nothing yet, and fail nothing."""
        self.log: list[str] = []
        self.rounds: list[RoundDone | None] = []
        self.images: list[int] = []
        self.call_ids: list[str] = []
        self.failing: set[str] = set()
        self.on_model: Callable[[int], None] | None = None

    def disclosed(self, *, tool: str, round: int, call_id: str, attempt: int) -> None:
        """Record a disclosure to workbench.

        Args:
            tool: The tool.
            round: The model round that asked for it.
            call_id: The call's id in the turn.
            attempt: 1, or 2 for the retry after a busy answer.

        Raises:
            AuditWriteError: While ``disclose`` is failing.
        """
        if "disclose" in self.failing:
            raise AuditWriteError("synthetic audit failure")
        self.log.append(f"disclose:{tool}:{round}:{attempt}")
        self.call_ids.append(call_id)

    def model_called(self, *, round: int, done: RoundDone | None, outcome: str, images: int = 0) -> None:
        """Record a model round.

        Args:
            round: The round.
            done: How it ended; ``None`` when it failed.
            outcome: ``ok`` or ``error``.
            images: How many images the round sent.

        Raises:
            AuditWriteError: While ``model`` is failing.
        """
        if "model" in self.failing:
            raise AuditWriteError("synthetic audit failure")
        self.log.append(f"model:{round}:{outcome}")
        self.rounds.append(done)
        self.images.append(images)
        if self.on_model is not None:
            self.on_model(round)


class FakeWorkbench:
    """``WorkbenchClient`` as the agent loop calls it: ``discover``, ``call_tool`` and ``fetch_renditions``.

    Discovery answers :attr:`discovery` to a call that takes images and, as workbench 0.7 does, the
    same without ``view_file`` and with 0.6's ``read_document`` text to one that does not
    (:data:`READ_DOCUMENT_TEXT`). Each call answers the next of :attr:`answers`, or
    :data:`PLAIN_RESULT` once they are used up, and is recorded with the audit line written
    last before it went out. A rendition is :data:`TINY_JPEG`, unless its path is in :attr:`missing`.

    Attributes:
        discovery: What every discovery that takes images answers; ``None`` is a workbench that is not usable.
        answers: The scripted answers, in order.
        calls: Every call, in order.
        discoveries: The chat of every discovery, and whether it asked for images, in order.
        sent_after: For every call, the audit's last line when it was sent.
        audit: The audit whose log :attr:`sent_after` reads.
        missing: The paths whose renditions are not there.
        fetched: The renditions each ``fetch_renditions`` asked for, in order.
    """

    def __init__(
        self,
        answers: Sequence[Answer] = (),
        *,
        tools: Sequence[McpTool] | None = None,
        audit: RecordingAudit | None = None,
    ) -> None:
        """Script workbench.

        Args:
            answers: The calls' answers, in order.
            tools: The tools discovery finds; the ten contract tools when omitted, none
                at all (``()``) for a workbench that is not usable.
            audit: The audit whose log :attr:`sent_after` reads.
        """
        found = contract_tools() if tools is None else tuple(tools)
        self.discovery: Discovery | None = Discovery(tools=found, server_version=SERVER_VERSION) if found else None
        self.answers: list[Answer] = list(answers)
        self.calls: list[SentCall] = []
        self.discoveries: list[tuple[CallFor, bool]] = []
        self.sent_after: list[str | None] = []
        self.audit = audit
        self.missing: set[str] = set()
        self.fetched: list[tuple[RenditionRequest, ...]] = []

    def as_client(self) -> WorkbenchClient:
        """Return this fake typed as the client, for ``AgentLoop(workbench=...)``.

        Returns:
            The fake.
        """
        return cast(WorkbenchClient, self)

    def discover(self, *, call: CallFor, images: bool) -> Discovery | None:
        """Answer a discovery.

        Args:
            call: The asking turn's chat.
            images: Whether it asks for the listing of a call that takes images.

        Returns:
            :attr:`discovery`, or without images its tools as workbench 0.7 lists them then.
        """
        self.discoveries.append((call, images))
        if images or self.discovery is None:
            return self.discovery
        tools = tuple(
            dataclasses.replace(tool, description=READ_DOCUMENT_TEXT) if tool.name == "read_document" else tool
            for tool in self.discovery.tools
            if tool.name != "view_file"
        )
        return dataclasses.replace(self.discovery, tools=tools)

    def call_tool(
        self, name: str, arguments: Mapping[str, Any], *, call: CallFor, timeout_s: float, images: bool = False
    ) -> ToolResult | ToolFailure:
        """Record a call and answer it from the script.

        Args:
            name: The tool.
            arguments: Its arguments.
            call: The chat.
            timeout_s: Its deadline.
            images: Whether it asks for images; the script answers the same either way.

        Returns:
            The next scripted answer.
        """
        self.calls.append(SentCall(name, dict(arguments), call, timeout_s, images))
        self.sent_after.append(self.audit.log[-1] if self.audit is not None and self.audit.log else None)
        answer = self.answers.pop(0) if self.answers else PLAIN_RESULT
        return answer() if callable(answer) else answer

    def fetch_renditions(self, wanted: Sequence[RenditionRequest], *, call: CallFor) -> list[tuple[str, bytes] | None]:
        """Record a fetch and answer each rendition.

        Args:
            wanted: The renditions.
            call: The chat.

        Returns:
            ``("image/jpeg", TINY_JPEG)`` for each, ``None`` for a path in :attr:`missing`.
        """
        self.fetched.append(tuple(wanted))
        return [None if item.path in self.missing else ("image/jpeg", TINY_JPEG) for item in wanted]


# --- Nextext -----------------------------------------------------------------

#: The id of the first job a :class:`FakeNextext` creates.
JOB_ID = "3f9c1d2e7a4b4c58b1e60d93a7f20c14"
#: When every job of the fake was created, as Nextext writes it.
JOB_CREATED_AT = "2026-11-22T22:54:00.912174Z"
#: The error text a failed job of the fake carries: the client must never log it.
FAILURE_TEXT = "The speech endpoint refused the clip at 00:12"
_NEXTEXT_PATH = "/api/v1"
_ARTIFACT_PATH = re.compile(r"/jobs/(?P<job>[^/]+)/artifacts/(?P<name>[^/]+)")
_JOB_PATH = re.compile(r"/jobs/(?P<job>[^/]+)")
_ARTIFACT_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class NextextUpload:
    """What a create sent, for assertions only.

    Attributes:
        headers: The request's headers.
        body: The whole body.
    """

    headers: httpx.Headers
    body: bytes


@dataclass(frozen=True)
class JobUpload:
    """A create's multipart body, parsed as Nextext's route (Starlette on python-multipart) reads it.

    Attributes:
        options: The decoded ``options`` field.
        filename: The ``file`` part's file name.
        content_type: Its media type.
        data: Its bytes.
    """

    options: dict[str, Any]
    filename: str
    content_type: str
    data: bytes


async def parse_job_upload(upload: NextextUpload) -> JobUpload:
    """Parse the body of a create.

    Args:
        upload: What the fake recorded.

    Returns:
        The options, and the file part.

    Raises:
        AssertionError: When the body holds no ``options`` text or no ``file`` part.
    """

    async def body() -> Any:
        yield upload.body

    headers = Headers({"content-type": upload.headers["content-type"]})
    form = await MultiPartParser(headers, body()).parse()
    try:
        options, part = form.get("options"), form.get("file")
        assert isinstance(options, str) and isinstance(part, UploadFile)
        return JobUpload(json.loads(options), part.filename or "", part.content_type or "", await part.read())
    finally:
        await form.close()


class FakeNextext:
    """Nextext 1.22's ``/api/v1`` as Ghostwriter's service token reaches it, for ``httpx.MockTransport``.

    It refuses a wrong key (401, ``/health`` and ``/ready`` apart), a list without ``X-Audit-Purpose`` (428, as
    Nextext's default does) and any other route (404). A create answers ``201 {job_id, status, created_at}``; each job
    then answers every ``GET /jobs`` with the next of :attr:`statuses`, the last repeated, until it is deleted or
    has left the list after :attr:`vanish_after` polls. A list item has the keys Nextext 1.21 serves, and the list
    holds no job of another caller but those in :attr:`listed`. An artifact answers ``200`` with the
    ``Content-Length`` Nextext's in-memory render has, or its scripted status; a job that is gone (deleted, or out of
    the list after :attr:`vanish_after` polls, as after a restart) answers ``404``. A ``DELETE`` answers ``204`` for a
    job created or in :attr:`listed`, which then leaves the list, or ``404`` for a job that is not there.

    Attributes:
        key: The service token the fake accepts.
        statuses: The status of a job at each poll, in order; the last one repeats.
        error_code: The ``error_code`` of a list item.
        skipped: The ``skipped`` flag of a list item.
        skip_reason_code: The ``skip_reason_code`` of a list item.
        artifacts: By artifact name, the bytes served, or an ``int`` that is the status answered instead.
        declared: By artifact name, the ``Content-Length`` to claim; ``None`` claims none (a chunked body).
        create_status: The status a create answers; ``201`` creates a job.
        create_body: A body to answer a create with instead of the job's, with ``201``.
        list_status: The status ``GET /jobs`` answers.
        delete_status: A status every ``DELETE`` answers with, instead of ``204`` or ``404``.
        health: The status ``GET /health`` answers.
        ready: The status ``GET /ready`` answers; ``None`` follows :attr:`health`, as a Nextext whose liveness fails
            cannot transcribe: ``200`` while it answers ``200``, else ``503``; ``404`` is a Nextext before 1.22,
            without the route.
        vanish_after: The polls a job is listed for before it leaves the list (Nextext restarted); ``None`` never.
        listed: Other jobs, as list items.
        raises: An exception to raise on every request, as a connection that fails does.
        requests: Every request, in order.
        uploads: Every create's headers and body.
        deleted: The ids of the jobs a ``DELETE`` removed, in order.
        events: ``"create"``, ``"list"`` and ``"delete:<id>"``, in order.
        served: The names of the artifacts whose body the fake started to send.
        jobs: The jobs created, by id: how many times each was polled.
        created: The ``created_at`` a created job is listed with, by id; :data:`JOB_CREATED_AT`, a day before, for one
            not named.
    """

    def __init__(self, key: str = NEXTEXT_KEY) -> None:
        """Answer as a healthy Nextext does: one job that completes at its first poll, with both artifacts.

        Args:
            key: The service token the fake accepts.
        """
        self.key = key
        self.statuses: list[str] = ["completed"]
        self.error_code: str | None = None
        self.skipped = False
        self.skip_reason_code: str | None = None
        self.artifacts: dict[str, bytes | int] = {
            "docint.jsonl": b'{"text": "hello"}\n',
            "keyframes.zip": b"PK\x03\x04",
        }
        self.declared: dict[str, int | None] = {}
        self.create_status = 201
        self.create_body: dict[str, Any] | None = None
        self.list_status = 200
        self.delete_status: int | None = None
        self.health = 200
        self.ready: int | None = None
        self.vanish_after: int | None = None
        self.listed: list[Any] = []
        self.raises: Exception | None = None
        self.requests: list[httpx.Request] = []
        self.uploads: list[NextextUpload] = []
        self.deleted: list[str] = []
        self.events: list[str] = []
        self.served: list[str] = []
        self.jobs: dict[str, int] = {}
        self.created: dict[str, str] = {}

    def _status(self, polls: int) -> str:
        """Return a job's status at its poll number ``polls``, counted from 0.

        Args:
            polls: How many times the job was listed before.

        Returns:
            The scripted status; the last repeats.
        """
        return self.statuses[min(polls, len(self.statuses) - 1)]

    def item(self, job_id: str, status: str, **changes: Any) -> dict[str, Any]:
        """Build a list item, with the keys Nextext 1.21 serves.

        Args:
            job_id: The job.
            status: Its status.
            **changes: Keys to set.

        Returns:
            The item.
        """
        failed = status == "failed"
        item: dict[str, Any] = {
            "created_at": JOB_CREATED_AT,
            "error": FAILURE_TEXT if failed else None,
            "error_code": self.error_code,
            "file_name": "media.mp4",
            "finished_at": None if status in ("queued", "running") else JOB_CREATED_AT,
            "job_id": job_id,
            "progress": 1.0 if status == "completed" else 0.0,
            "skip_reason_code": self.skip_reason_code,
            "skipped": self.skipped,
            "stage": None,
            "started_at": None if status == "queued" else JOB_CREATED_AT,
            "status": status,
            "task": "transcribe",
        }
        return {**item, **changes}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record the request and answer it.

        Args:
            request: The request the client sent, its body read.

        Returns:
            The answer.

        Raises:
            Exception: :attr:`raises`, when it is set.
        """
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        path = request.url.path
        if not path.startswith(_NEXTEXT_PATH + "/"):
            return httpx.Response(404, json=NOT_FOUND)
        route = path.removeprefix(_NEXTEXT_PATH)
        if (request.method, route) == ("GET", "/health"):
            return httpx.Response(self.health, json={"status": "ok", "inference": True})
        if (request.method, route) == ("GET", "/ready"):
            return self._ready()
        if request.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"detail": "unauthorized"})
        if (request.method, route) == ("POST", "/jobs"):
            return self._create(request)
        if (request.method, route) == ("GET", "/jobs"):
            return self._list(request)
        if (found := _ARTIFACT_PATH.fullmatch(route)) and request.method == "GET":
            return self._artifact(found["job"], found["name"])
        if (found := _JOB_PATH.fullmatch(route)) and request.method == "DELETE":
            return self._delete(found["job"])
        return httpx.Response(404, json=NOT_FOUND)

    def _ready(self) -> httpx.Response:
        """Answer ``GET /ready`` as Nextext 1.22 does: ``200`` when it can transcribe, else ``503``, or as scripted.

        Returns:
            The answer, from :attr:`ready` or, when that is ``None``, from :attr:`health`.
        """
        status = self.ready if self.ready is not None else (200 if self.health == 200 else 503)
        if status == 404:
            return httpx.Response(404, json=NOT_FOUND)
        ready = status == 200
        body = {"status": "ready" if ready else "unavailable", "inference": ready, "transcription": ready}
        return httpx.Response(status, json={**body, "version": "1.22.0"})

    def _create(self, request: httpx.Request) -> httpx.Response:
        self.events.append("create")
        self.uploads.append(NextextUpload(request.headers, request.content))
        if self.create_status != 201:
            return httpx.Response(self.create_status, json={"detail": "refused"})
        if self.create_body is not None:
            return httpx.Response(201, json=self.create_body)
        job_id = JOB_ID if not self.jobs else f"{JOB_ID[:-4]}{len(self.jobs):04x}"
        self.jobs[job_id] = 0
        return httpx.Response(201, json={"job_id": job_id, "status": "queued", "created_at": JOB_CREATED_AT})

    def _list(self, request: httpx.Request) -> httpx.Response:
        self.events.append("list")
        if "x-audit-purpose" not in request.headers:
            return httpx.Response(428, json={"detail": "purpose required"})
        if self.list_status != 200:
            return httpx.Response(self.list_status, json={"detail": "refused"})
        jobs: list[dict[str, Any]] = []
        for job_id, polls in list(self.jobs.items()):
            if job_id in self.deleted:
                continue
            self.jobs[job_id] = polls + 1
            if self.vanish_after is not None and polls >= self.vanish_after:
                continue
            jobs.append(self.item(job_id, self._status(polls), created_at=self.created.get(job_id, JOB_CREATED_AT)))
        return httpx.Response(200, json={"jobs": [*jobs, *self.listed]})

    def _gone(self, job_id: str) -> bool:
        """Tell whether a job is not there: never created, deleted, or out of the list (Nextext restarted).

        Args:
            job_id: The job.

        Returns:
            Whether Nextext holds it no more.
        """
        polls = self.jobs.get(job_id)
        return polls is None or job_id in self.deleted or (self.vanish_after is not None and polls > self.vanish_after)

    def _artifact(self, job_id: str, name: str) -> httpx.Response:
        if self._gone(job_id):
            return httpx.Response(404, json=NOT_FOUND)
        served = self.artifacts.get(name, 404)
        if isinstance(served, int):
            return httpx.Response(served, json={"detail": "no such artifact"} if served == 404 else {})
        headers: dict[str, str] = {"Content-Type": "application/octet-stream"}
        declared = self.declared.get(name, len(served))
        if declared is not None:
            headers["Content-Length"] = str(declared)

        async def body() -> Any:
            self.served.append(name)
            for start in range(0, len(served), _ARTIFACT_CHUNK):
                yield served[start : start + _ARTIFACT_CHUNK]

        return httpx.Response(200, headers=headers, content=body())

    def _delete(self, job_id: str) -> httpx.Response:
        self.events.append(f"delete:{job_id}")
        if self.delete_status is not None:
            return httpx.Response(self.delete_status, json={"detail": "refused"})
        kept = [item for item in self.listed if not (isinstance(item, dict) and item.get("job_id") == job_id)]
        if len(kept) < len(self.listed):
            self.listed = kept
            self.deleted.append(job_id)
            return httpx.Response(204)
        if self._gone(job_id):
            return httpx.Response(404, json=NOT_FOUND)
        self.deleted.append(job_id)
        return httpx.Response(204)
