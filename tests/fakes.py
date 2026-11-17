"""Stand-ins for Ghostwriter's neighbours, built from the shared contract alone.

:class:`FakeDoor` is workbench as Ghostwriter's key reaches it: ``/mcp`` and the
REST routes ``/files/upload``, ``/files/view`` and ``/files/chat``.
:class:`FakeOpenAI` is the router as the OpenAI SDK reaches it, fed by the chunk
builders :func:`text_chunk`, :func:`reasoning_chunk`, :func:`call_chunk` and
:func:`usage_chunk`. :class:`FakeWorkbench` and :class:`RecordingAudit` are workbench
and a turn's audit as the agent loop reaches them. Every name, id and value here is
invented.
"""

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast
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
    ToolFailure,
    ToolResult,
    WorkbenchClient,
)
from tests.conftest import KEY

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SERVER_META = {META_SERVER_INFO: {"name": "workbench", "version": "0.3.0"}}
#: The id workbench gives the output of :func:`call_result`.
OUTPUT_ID = "0" * 32
#: The id the door gives every upload, unless a test sets another.
UPLOAD_ID = "1" * 32
NOT_FOUND = {"detail": "not found"}

#: The eight workspace tools of the contract: name, description and input properties.
CONTRACT_TOOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "list_files": ("List the files in the chat's folder.", {"path": {"type": "string"}}),
    "read_document": ("Read a document as text.", {"path": {"type": "string"}}),
    "search_files": ("Search the chat's files for a text.", {"query": {"type": "string"}}),
    "run_python": ("Run Python on the chat's files.", {"code": {"type": "string"}}),
    "inspect_template": ("List a template's fields.", {"path": {"type": "string"}}),
    "fill_template": ("Fill a template's fields.", {"path": {"type": "string"}, "values": {"type": "object"}}),
    "write_table": ("Write a table as csv or xlsx.", {"name": {"type": "string"}, "rows": {"type": "array"}}),
    "write_document": ("Write a document.", {"name": {"type": "string"}, "text": {"type": "string"}}),
}

_ROUTES = frozenset({("POST", "/mcp"), ("POST", "/files/upload"), ("GET", "/files/view"), ("DELETE", "/files/chat")})
_DISCOVERY = frozenset({"server/discover", "tools/list"})


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


def discover_result() -> dict[str, Any]:
    """Return workbench's ``server/discover`` result.

    Returns:
        The result, serving 2026-07-28 for an hour.
    """
    return {
        "supportedVersions": ["2026-07-28", "2025-11-25", "2025-06-18"],
        "capabilities": {"tools": {"listChanged": False}},
        "ttlMs": 3600000,
        "cacheScope": "private",
        "resultType": "complete",
        "_meta": SERVER_META,
    }


def tools_result() -> dict[str, Any]:
    """Return workbench's ``tools/list`` result for the ghostwriter caller.

    Returns:
        The result, the eight contract tools by name, as workbench lists them, cached for 300 s.
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
    folder and returns one of those. ``None`` for ``/files/upload`` and ``/files/view``
    is the default: an upload is parsed, stored in :attr:`files` and answered with
    its path, size and :attr:`upload_id`; a view serves a stored file of the chat.

    Attributes:
        key: The key the door accepts.
        requests: Every request, with its body: the JSON-RPC message, the parsed upload, or ``None``.
        answers: The scripted answers, by MCP method or REST path.
        delays: Seconds to wait before answering, by MCP method or REST path.
        files: The stored files, by path: their bytes and media type.
        upload_id: The id every upload gets.
        last_call_answer: The last result ``tools/call`` was answered with.
        on_delete: Called when a ``DELETE /files/chat`` the door accepts arrives, before it is answered.
    """

    def __init__(self, key: str = KEY) -> None:
        """Answer as a healthy workbench does, without delay.

        Args:
            key: The key the door accepts.
        """
        self.key = key
        self.requests: list[tuple[httpx.Request, Any]] = []
        self.answers: dict[str, object] = {
            "server/discover": discover_result(),
            "tools/list": tools_result(),
            "tools/call": call_result,
            "/files/upload": None,
            "/files/view": None,
            "/files/chat": {"deleted": 3},
        }
        self.delays: dict[str, float] = {}
        self.files: dict[str, tuple[bytes, str]] = {}
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
        elif path == "/files/upload":
            body = await _parse_upload(request)
        self.requests.append((request, body))
        route = body["method"] if path == "/mcp" else path
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
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": answer})
        if answer is None and path == "/files/upload":
            return self._store(request, body, folder)
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

    def _serve(self, request: httpx.Request, folder: str) -> httpx.Response:
        """Serve a stored file of the chat.

        Args:
            request: The view.
            folder: The chat's folder.

        Returns:
            The file; 404 for a path outside the chat or a missing file alike.
        """
        path = request.url.params.get("path", "")
        stored = self.files.get(path) if path.startswith(f"{folder}/") else None
        if stored is None:
            return httpx.Response(404, json=NOT_FOUND)
        data, mime = stored
        name = quote(path.rsplit("/", 1)[-1], safe="")
        disposition = f"inline; filename*=UTF-8''{name}"
        return httpx.Response(200, content=data, headers={"Content-Type": mime, "Content-Disposition": disposition})

    def methods(self) -> list[str]:
        """Return the MCP method of every ``/mcp`` request so far, in order.

        Returns:
            The methods.
        """
        return [body["method"] for request, body in self.requests if request.url.path == "/mcp"]

    def last(self, path: str) -> httpx.Request:
        """Return the last request to a path.

        Args:
            path: The route, such as ``/files/upload``.

        Returns:
            The request.

        Raises:
            LookupError: When no request went there.
        """
        for request, _ in reversed(self.requests):
            if request.url.path == path:
                return request
        raise LookupError(path)

    @property
    def uploads(self) -> list[Upload]:
        """Every upload's parsed file part, in order."""
        return [body for _, body in self.requests if isinstance(body, Upload)]


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
        return Transcription(text=self.transcript)


# --- workbench and the audit, as the agent loop reaches them ------------------

#: What :class:`FakeWorkbench` answers a call with: an outcome, or a callable that returns one
#: (it may move a clock, set a stop, or raise to stand for a fault).
Answer = ToolResult | ToolFailure | Callable[[], ToolResult | ToolFailure]
#: What a call answers when the script has no answer left.
PLAIN_RESULT = ToolResult('{"text": "Umsatz 42"}', ())


def contract_tools() -> tuple[McpTool, ...]:
    """Return the eight contract tools as discovery hands them out.

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
    """

    name: str
    arguments: dict[str, Any]
    call: CallFor
    timeout_s: float


class RecordingAudit:
    """A turn's audit that records each write as a line, or fails it.

    Attributes:
        log: ``disclose:<tool>:<round>:<attempt>`` and ``model:<round>:<outcome>``, in order.
        rounds: Each ``model_called``'s round, as the loop reported it.
        failing: The writes that fail, ``disclose`` or ``model``.
        on_model: Called with the round after each ``model_called`` was written.
    """

    def __init__(self) -> None:
        """Record nothing yet, and fail nothing."""
        self.log: list[str] = []
        self.rounds: list[RoundDone | None] = []
        self.failing: set[str] = set()
        self.on_model: Callable[[int], None] | None = None

    def disclosed(self, *, tool: str, round: int, call_id: str, attempt: int) -> None:
        """Record a disclosure to workbench.

        Args:
            tool: The tool.
            round: The model round that asked for it.
            call_id: The model's id for the call.
            attempt: 1, or 2 for the retry after a busy answer.

        Raises:
            AuditWriteError: While ``disclose`` is failing.
        """
        if "disclose" in self.failing:
            raise AuditWriteError("synthetic audit failure")
        self.log.append(f"disclose:{tool}:{round}:{attempt}")

    def model_called(self, *, round: int, done: RoundDone | None, outcome: str) -> None:
        """Record a model round.

        Args:
            round: The round.
            done: How it ended; ``None`` when it failed.
            outcome: ``ok`` or ``error``.

        Raises:
            AuditWriteError: While ``model`` is failing.
        """
        if "model" in self.failing:
            raise AuditWriteError("synthetic audit failure")
        self.log.append(f"model:{round}:{outcome}")
        self.rounds.append(done)
        if self.on_model is not None:
            self.on_model(round)


class FakeWorkbench:
    """``WorkbenchClient`` as the agent loop calls it: ``discover`` and ``call_tool``.

    Discovery answers :attr:`discovery`. Each call answers the next of :attr:`answers`, or
    :data:`PLAIN_RESULT` once they are used up, and is recorded with the audit line written
    last before it went out.

    Attributes:
        discovery: What every discovery answers; ``None`` is a workbench that is not usable.
        answers: The scripted answers, in order.
        calls: Every call, in order.
        discoveries: The chat of every discovery, in order.
        sent_after: For every call, the audit's last line when it was sent.
        audit: The audit whose log :attr:`sent_after` reads.
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
            tools: The tools discovery finds; the eight contract tools when omitted, none
                at all (``()``) for a workbench that is not usable.
            audit: The audit whose log :attr:`sent_after` reads.
        """
        found = contract_tools() if tools is None else tuple(tools)
        self.discovery: Discovery | None = Discovery(tools=found, server_version="0.3.0") if found else None
        self.answers: list[Answer] = list(answers)
        self.calls: list[SentCall] = []
        self.discoveries: list[CallFor] = []
        self.sent_after: list[str | None] = []
        self.audit = audit

    def as_client(self) -> WorkbenchClient:
        """Return this fake typed as the client, for ``AgentLoop(workbench=...)``.

        Returns:
            The fake.
        """
        return cast(WorkbenchClient, self)

    def discover(self, *, call: CallFor) -> Discovery | None:
        """Answer a discovery.

        Args:
            call: The asking turn's chat.

        Returns:
            :attr:`discovery`.
        """
        self.discoveries.append(call)
        return self.discovery

    def call_tool(
        self, name: str, arguments: Mapping[str, Any], *, call: CallFor, timeout_s: float
    ) -> ToolResult | ToolFailure:
        """Record a call and answer it from the script.

        Args:
            name: The tool.
            arguments: Its arguments.
            call: The chat.
            timeout_s: Its deadline.

        Returns:
            The next scripted answer.
        """
        self.calls.append(SentCall(name, dict(arguments), call, timeout_s))
        self.sent_after.append(self.audit.log[-1] if self.audit is not None and self.audit.log else None)
        answer = self.answers.pop(0) if self.answers else PLAIN_RESULT
        return answer() if callable(answer) else answer
