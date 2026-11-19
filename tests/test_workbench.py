"""The workbench client: discovery, tool calls and files, confined to the chat by keyed pseudonyms.

The door is mocked from the shared contract alone (:class:`tests.fakes.FakeDoor`).
Every name, id and value here is invented.
"""

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, ClassVar

import anyio
import httpx
import pytest
from loguru import logger

from ghostwriter import __version__
from ghostwriter.audit import RequestContext, forward_headers
from ghostwriter.utils.audit_event import Actor
from ghostwriter.utils.audit_runtime import encode_purpose
from ghostwriter.workbench import (
    MAX_ATTACHMENT_BYTES,
    META_CLIENT_CAPS,
    META_CLIENT_INFO,
    META_PROTOCOL,
    PREVIEW_CELL_CHARS,
    PREVIEW_COLUMNS,
    PREVIEW_ROWS,
    CallFor,
    OutputFile,
    StorageFull,
    TablePreview,
    ToolFailure,
    ToolResult,
    UploadedFile,
    UploadTooLarge,
    WorkbenchClient,
    WorkbenchNotFound,
    WorkbenchUnavailable,
    chat_folder,
    session_pseudonym,
    user_pseudonym,
)
from tests.conftest import KEY, make_settings
from tests.fakes import (
    CONTRACT_TOOLS,
    OUTPUT_ID,
    UPLOAD_ID,
    XLSX,
    FakeDoor,
    RpcError,
    call_result,
    output,
    refusal,
)

CFG = make_settings(Path("unused"), workbench_api_key=KEY, workbench_api_base="http://workbench.test")
CHAT = "3f2a7c1e-8b4d-4e6f-9a0b-1c2d3e4f5a6b"
CALL = CallFor("user-a", CHAT, forward_headers(RequestContext(Actor("user-a", "edge"), None, "req-0001")))
FOLDER = chat_folder(KEY, CHAT)
SOURCE = "import pandas as pd\nprint(pd.read_excel('sales.xlsx').sum())"


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        """Start at an arbitrary instant."""
        self.now = 1_000.0

    def __call__(self) -> float:
        """Return the current instant.

        Returns:
            Seconds on the fake scale.
        """
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Args:
            seconds: How far.
        """
        self.now += seconds


def _client(door: FakeDoor, clock: FakeClock | None = None, settings: Any = CFG) -> WorkbenchClient:
    return WorkbenchClient(settings, transport=httpx.MockTransport(door), clock=clock or FakeClock())


def _call(client: WorkbenchClient, name: str = "run_python", timeout_s: float = 40.0) -> ToolResult | ToolFailure:
    return client.call_tool(name, {"code": SOURCE}, call=CALL, timeout_s=timeout_s)


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def _upload(client: WorkbenchClient, filename: str = "sales.xlsx", *parts: bytes) -> UploadedFile:
    return await client.upload(call=CALL, filename=filename, mime=XLSX, chunks=_chunks(*(parts or (b"PK\x03\x04",))))


def _tool_names(found: Any) -> list[str]:
    return [tool.name for tool in found.tools]


@pytest.fixture
def log_lines() -> Iterator[list[str]]:
    """Capture every loguru line, DEBUG included.

    Yields:
        The lines written while the test runs.
    """
    lines: list[str] = []
    handler_id = logger.add(lambda message: lines.append(str(message)), level="DEBUG", format="{message}")
    try:
        yield lines
    finally:
        logger.remove(handler_id)


# --- discovery ---------------------------------------------------------------


def test_discovery_offers_the_tools_workbench_lists() -> None:
    """Discovery asks server/discover, then tools/list, and offers every listed tool, in workbench's order."""
    door = FakeDoor()
    found = WorkbenchClient(CFG, transport=httpx.MockTransport(door)).discover(call=CALL)
    assert found is not None
    assert _tool_names(found) == [
        "fill_template",
        "inspect_template",
        "list_files",
        "read_document",
        "run_python",
        "search_files",
        "write_document",
        "write_presentation",
        "write_table",
    ]
    assert door.methods() == ["server/discover", "tools/list"]
    assert found.server_version == "0.3.0"
    read = next(tool for tool in found.tools if tool.name == "read_document")
    assert read.description == CONTRACT_TOOLS["read_document"][0]
    assert read.input_schema["type"] == "object" and "path" in read.input_schema["properties"]


@pytest.mark.parametrize("change", ["no_modern_revision", "no_tools", "no_object_schema", "not_json_rpc"])
def test_discovery_offers_nothing_without_the_revision_and_a_usable_tool(change: str) -> None:
    """Without 2026-07-28 and at least one tool with an object schema, there is no tool to offer."""
    door = FakeDoor()
    listed = door.answers["tools/list"]
    assert isinstance(listed, dict)
    if change == "no_modern_revision":
        door.answers["server/discover"] = {"supportedVersions": ["2025-11-25"], "resultType": "complete"}
    elif change == "no_tools":
        none: list[dict[str, Any]] = []
        door.answers["tools/list"] = {**listed, "tools": none}
    elif change == "no_object_schema":
        tools = [{**tool, "inputSchema": {"type": "string"}} for tool in listed["tools"]]
        door.answers["tools/list"] = {**listed, "tools": [*tools, {"name": "x", "inputSchema": "object"}]}
    else:
        door.answers["server/discover"] = httpx.Response(404, json={"detail": "not found"})
    assert _client(door).discover(call=CALL) is None


def test_a_tool_without_an_object_schema_is_dropped() -> None:
    """Only the tools whose inputSchema is an object are offered."""
    door = FakeDoor()
    listed = door.answers["tools/list"]
    assert isinstance(listed, dict)
    tools = [dict(tool) for tool in listed["tools"]]
    tools[0]["inputSchema"] = {"type": "array"}
    del tools[1]["inputSchema"]
    door.answers["tools/list"] = {**listed, "tools": tools}
    found = _client(door).discover(call=CALL)
    assert found is not None
    assert _tool_names(found) == sorted(CONTRACT_TOOLS)[2:]


def test_each_result_is_cached_for_its_ttl() -> None:
    """tools/list is asked again after its 300 s, server/discover after its hour."""
    door = FakeDoor()
    clock = FakeClock()
    client = _client(door, clock)
    client.discover(call=CALL)
    clock.advance(299)
    client.discover(call=CALL)
    assert door.methods() == ["server/discover", "tools/list"]
    clock.advance(2)
    assert client.discover(call=CALL) is not None
    assert door.methods() == ["server/discover", "tools/list", "tools/list"]
    clock.advance(3600)
    assert client.discover(call=CALL) is not None
    assert door.methods()[-2:] == ["server/discover", "tools/list"]
    assert len(door.methods()) == 5


def test_a_failed_discovery_is_tried_again_at_most_once_a_minute() -> None:
    """After a failure, turns within the next 60 s get no tool and send nothing."""
    door = FakeDoor()
    door.answers["server/discover"] = httpx.Response(503, text="unavailable")
    clock = FakeClock()
    client = _client(door, clock)
    assert client.discover(call=CALL) is None
    clock.advance(59)
    assert client.discover(call=CALL) is None
    assert door.methods() == ["server/discover"]
    clock.advance(2)
    door.answers["server/discover"] = FakeDoor().answers["server/discover"]
    assert client.discover(call=CALL) is not None
    assert door.methods() == ["server/discover", "server/discover", "tools/list"]


# --- requests ----------------------------------------------------------------


def test_every_request_states_the_modern_revision() -> None:
    """Each POST to /mcp carries the key, the content types, the revision, its method and the client's _meta."""
    door = FakeDoor()
    client = _client(door)
    client.discover(call=CALL)
    _call(client)
    assert len(door.requests) == 3
    for request, body in door.requests:
        assert (request.method, str(request.url)) == ("POST", "http://workbench.test/mcp")
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["accept"] == "application/json, text/event-stream"
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["mcp-protocol-version"] == "2026-07-28"
        assert request.headers["mcp-method"] == body["method"]
        assert body["jsonrpc"] == "2.0"
        assert body["params"]["_meta"] == {
            META_PROTOCOL: "2026-07-28",
            META_CLIENT_INFO: {"name": "ghostwriter", "version": __version__},
            META_CLIENT_CAPS: {},
        }
    assert [("mcp-name" in r.headers) for r, _ in door.requests] == [False, False, True]
    assert "mcp-session-id" not in door.requests[-1][0].headers
    assert door.requests[-1][1]["params"]["name"] == "run_python"
    assert door.requests[-1][1]["params"]["arguments"] == {"code": SOURCE}


def test_a_call_carries_the_chats_pseudonyms_purpose_and_request_id() -> None:
    """The user and the chat reach workbench only as HMACs under the key; the purpose and request id go along."""
    door = FakeDoor()
    _client(door).call_tool("read_document", {"path": f"{FOLDER}/sales.xlsx"}, call=CALL, timeout_s=40.0)
    req, body = door.requests[-1]
    assert (
        req.headers["x-user-id"] == hmac.new(KEY.encode(), b"workbench-user/v1\x00user-a", hashlib.sha256).hexdigest()
    )
    assert (
        req.headers["x-session-id"]
        == hmac.new(KEY.encode(), b"workbench-session/v1\x00" + CHAT.encode(), hashlib.sha256).hexdigest()
    )
    assert req.headers["mcp-name"] == "read_document" and req.headers["x-request-id"] == "req-0001"
    # Values, not str(headers): the name of httpx's own user-agent header starts with "user-a".
    sent = " ".join(req.headers.values()) + json.dumps(body)
    assert "user-a" not in sent and CHAT not in sent
    assert FOLDER == f"/workspace/{session_pseudonym(KEY, CHAT)}"
    assert user_pseudonym(KEY, CHAT) != session_pseudonym(KEY, CHAT)


@pytest.mark.parametrize("operation", ["discover", "call_tool", "upload", "view", "delete_chat"])
async def test_every_request_carries_the_session_purpose_and_request_id(operation: str) -> None:
    """Discovery, tools/call and the three file routes each carry the chat, the user, the purpose and the request id."""
    door = FakeDoor()
    door.files[f"{FOLDER}/sales.xlsx"] = (b"PK", XLSX)
    client = _client(door)
    if operation == "discover":
        client.discover(call=CALL)
    elif operation == "call_tool":
        _call(client)
    elif operation == "upload":
        await _upload(client)
    elif operation == "view":
        async with client.view(call=CALL, path=f"{FOLDER}/sales.xlsx"):
            pass
    else:
        await client.delete_chat(call=CALL)
    assert door.requests
    for request, _ in door.requests:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.headers["x-session-id"] == session_pseudonym(KEY, CHAT)
        assert request.headers["x-user-id"] == user_pseudonym(KEY, "user-a")
        assert request.headers["x-request-id"] == "req-0001"
        assert request.headers["x-audit-purpose"] == encode_purpose(None, "ghostwriter chat")


async def test_the_door_reads_no_session_on_discovery() -> None:
    """As workbench's MCP door, the fake answers discovery without a session and refuses only the call."""
    auth = {"Authorization": f"Bearer {KEY}"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeDoor()), base_url="http://workbench.test") as http:
        for method in ("server/discover", "tools/list", "tools/call"):
            message: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}
            answer = (await http.post("/mcp", json=message, headers=auth)).json()
            if method == "tools/call":
                assert answer["error"]["code"] == -32600
            else:
                assert "result" in answer


# --- results -----------------------------------------------------------------


def test_the_model_gets_the_text_and_the_cards_get_the_outputs() -> None:
    """The text is workbench's content[0], without previews; the outputs carry them."""
    door = FakeDoor()
    result = _call(_client(door))
    assert isinstance(result, ToolResult)
    assert door.last_call_answer is not None
    assert result.text == door.last_call_answer["content"][0]["text"]
    assert "preview" not in result.text
    assert result.outputs == (
        OutputFile(
            OUTPUT_ID,
            f"{FOLDER}/out/sums.xlsx",
            "sums.xlsx",
            XLSX,
            48213,
            TablePreview(("month", "sum"), (("2026-01", "1200"),), 12, True),
        ),
    )


def test_without_a_text_the_model_reads_the_structured_content_without_previews() -> None:
    """A result with no content[0] text gives the model the compact structuredContent, each preview removed."""
    door = FakeDoor()
    answer = call_result(FOLDER)
    answer["content"] = list[Any]()
    door.answers["tools/call"] = answer
    result = _call(_client(door))
    assert isinstance(result, ToolResult)
    told = json.loads(result.text)
    assert told["stdout"] == "12 months summed"
    assert told["outputs"] == [{k: v for k, v in output(FOLDER).items() if k != "preview"}]
    assert ", " not in result.text and len(result.outputs) == 1 and result.outputs[0].preview is not None


def test_a_missing_preview_or_row_count_is_kept_as_none() -> None:
    """A csv without a preview keeps its card without one; a preview without a row count keeps None."""
    door = FakeDoor()
    unknown = {"columns": ["a"], "rows": [["1"]], "row_count": None, "truncated": True}
    outputs = [
        output(FOLDER, path=f"{FOLDER}/out/plain.csv", name="plain.csv", mime="text/csv", preview=None),
        output(FOLDER, path=f"{FOLDER}/out/rows.csv", name="rows.csv", mime="text/csv", preview=unknown),
    ]
    door.answers["tools/call"] = call_result(FOLDER, outputs)
    result = _call(_client(door))
    assert isinstance(result, ToolResult)
    assert [item.preview for item in result.outputs] == [None, TablePreview(("a",), (("1",),), None, True)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "/workspace/other/x.csv"),
        ("path", f"{FOLDER}/../x.csv"),
        ("path", FOLDER),
        ("id", "xyz"),
        ("size", -1),
    ],
    ids=["another_folder", "dot_dot", "the_folder_itself", "id_not_32_hex", "negative_size"],
)
def test_an_output_outside_the_chats_folder_is_dropped(field: str, value: object) -> None:
    """An output outside the chat's folder, or without a 32-hex id or a size, is no card; the others stay."""
    door = FakeDoor()
    door.answers["tools/call"] = call_result(FOLDER, [output(FOLDER, **{field: value}), output(FOLDER)])
    result = _call(_client(door))
    assert isinstance(result, ToolResult)
    assert [item.path for item in result.outputs] == [f"{FOLDER}/out/sums.xlsx"]


def test_a_preview_is_clamped_or_dropped() -> None:
    """Past 20 columns, 5 rows or 100 characters a cell, the preview is cut; a malformed one is dropped."""
    wide = {"columns": [f"c{i}" for i in range(25)], "rows": [["1"] * 25], "row_count": 1, "truncated": False}
    tall = {"columns": ["a"], "rows": [[str(i)] for i in range(7)], "row_count": 7, "truncated": False}
    long = {"columns": ["a"], "rows": [["x" * 150]], "row_count": 1, "truncated": False}
    mixed = {"columns": ["a", 2], "rows": [["1", "2"]], "row_count": 1, "truncated": False}
    door = FakeDoor()
    door.answers["tools/call"] = call_result(FOLDER, [output(FOLDER, preview=p) for p in (wide, tall, long, mixed)])
    result = _call(_client(door))
    assert isinstance(result, ToolResult)
    first, second, third, fourth = (item.preview for item in result.outputs)
    assert first is not None and len(first.columns) == PREVIEW_COLUMNS == 20 and first.truncated
    assert len(first.rows[0]) == PREVIEW_COLUMNS
    assert second is not None and len(second.rows) == PREVIEW_ROWS == 5 and second.truncated
    assert second.row_count == 7
    assert third is not None and third.rows[0][0] == "x" * PREVIEW_CELL_CHARS and PREVIEW_CELL_CHARS == 100
    assert third.truncated
    assert fourth is None and len(result.outputs) == 4


@pytest.mark.parametrize(
    "token", ["queue_full", "wait_timeout", "executor_busy", "not_completed", "invalid_arguments", "something_new"]
)
def test_a_refused_call_is_read_by_its_token(token: str) -> None:
    """A refusal is its structuredContent.reason token with workbench's text, answered."""
    door = FakeDoor()
    door.answers["tools/call"] = refusal(token)
    assert _call(_client(door)) == ToolFailure(token, "busy, retry later", answered=True)


def test_one_deadline_bounds_the_call() -> None:
    """Past timeout_s the call is given up, unanswered."""
    door = FakeDoor()
    door.delays = {"tools/call": 2.0}
    started = time.monotonic()
    result = _call(_client(door), timeout_s=0.3)
    assert time.monotonic() - started < 1.5
    assert isinstance(result, ToolFailure)
    assert (result.token, result.answered) == ("timeout", False)


@pytest.mark.parametrize(
    ("content_type", "body", "token"),
    [
        ("text/event-stream", b"data: {}\n\n", "not_json"),
        ("application/json", b"x" * (4 * 1024 * 1024 + 1), "answer_too_large"),
        ("application/json", b"not json", "not_json_rpc"),
    ],
    ids=["sse", "past_4_mib", "not_json"],
)
def test_an_answer_that_is_not_one_small_json_rpc_message_fails(content_type: str, body: bytes, token: str) -> None:
    """SSE, an answer past 4 MiB, and anything but one JSON-RPC message fail, unanswered."""
    door = FakeDoor()
    door.answers["tools/call"] = httpx.Response(200, headers={"content-type": content_type}, content=body)
    result = _call(_client(door))
    assert isinstance(result, ToolFailure)
    assert (result.token, result.answered) == (token, False)


def test_an_unreachable_call_drops_the_discovery() -> None:
    """No answer, a timeout or invalid_arguments: the next turn discovers again. A busy door keeps it."""
    door = FakeDoor()
    client = _client(door)
    assert client.discover(call=CALL) is not None

    door.answers["tools/call"] = httpx.ConnectError("connection refused")
    result = _call(client)
    assert isinstance(result, ToolFailure) and (result.token, result.answered) == ("unreachable", False)
    assert client.discover(call=CALL) is not None
    assert door.methods()[-2:] == ["server/discover", "tools/list"]

    door.answers["tools/call"] = refusal("invalid_arguments")
    assert _call(client) == ToolFailure("invalid_arguments", "busy, retry later", answered=True)
    assert client.discover(call=CALL) is not None
    assert door.methods()[-2:] == ["server/discover", "tools/list"]

    door.answers["tools/call"] = call_result
    door.delays = {"tools/call": 1.0}
    result = _call(client, timeout_s=0.1)
    assert isinstance(result, ToolFailure) and result.token == "timeout"
    assert client.discover(call=CALL) is not None
    assert door.methods()[-2:] == ["server/discover", "tools/list"]

    door.delays = {}
    door.answers["tools/call"] = refusal("queue_full")
    assert _call(client) == ToolFailure("queue_full", "busy, retry later", answered=True)
    assert client.discover(call=CALL) is not None
    assert door.methods()[-1] == "tools/call"
    assert door.methods().count("server/discover") == 4


@pytest.mark.parametrize("code", [-32602, -32022])
def test_an_unknown_tool_or_a_refused_revision_drops_the_discovery(code: int) -> None:
    """-32602 and -32022 mean the discovery may be stale: the next turn asks again. Other codes keep it."""
    door = FakeDoor()
    client = _client(door)
    assert client.discover(call=CALL) is not None
    door.answers["tools/call"] = RpcError(code, "unknown tool")
    assert _call(client) == ToolFailure(f"jsonrpc:{code}", "unknown tool", answered=True)
    assert client.discover(call=CALL) is not None
    assert door.methods().count("server/discover") == 2

    door.answers["tools/call"] = RpcError(-32603, "internal error", status=500)
    _call(client)
    assert client.discover(call=CALL) is not None
    assert door.methods().count("server/discover") == 2


# --- files -------------------------------------------------------------------


async def test_an_upload_streams_into_the_chats_folder() -> None:
    """The file goes into the chat's folder as the multipart field file; workbench's id comes back."""
    door = FakeDoor()
    client = _client(door)
    got = await client.upload(call=CALL, filename="sales.xlsx", mime=XLSX, chunks=_chunks(b"PK\x03\x04", b"rest"))
    assert got == UploadedFile(UPLOAD_ID, f"{FOLDER}/sales.xlsx", 8)
    req = door.last("/files/upload")
    assert req.method == "POST"
    assert req.url.params["directory"] == FOLDER and b'name="file"; filename="sales.xlsx"' in req.content
    assert req.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert door.uploads[-1].content_type == XLSX and door.uploads[-1].data == b"PK\x03\x04rest"


async def test_an_upload_name_with_umlauts_and_spaces_round_trips() -> None:
    """The name is written as httpx writes a multipart filename, and workbench's parser reads it back."""
    name = "Übersicht März #1.xlsx"
    door = FakeDoor()
    got = await _upload(_client(door), name)
    assert door.uploads[-1].filename == name and got.path == f"{FOLDER}/{name}"
    rendered = httpx.Request("POST", "http://x", files={"file": (name, b"", XLSX)}).read()
    part_headers = rendered.split(b"\r\n", 1)[1].split(b"\r\n\r\n", 1)[0]
    assert part_headers in door.last("/files/upload").content


async def test_an_upload_past_50_mib_is_cut_off() -> None:
    """Past MAX_ATTACHMENT_BYTES the stream stops; workbench never gets a whole request."""
    mib = b"\x00" * (1024 * 1024)
    door = FakeDoor()
    with pytest.raises(UploadTooLarge):
        await _upload(_client(door), "sales.xlsx", *([mib] * (MAX_ATTACHMENT_BYTES // len(mib))), b"\x00")
    assert door.requests == []


async def test_the_callers_stream_is_closed_on_a_cut_off() -> None:
    """On the cut-off the caller's iterator is closed at once, so its own cleanup runs."""
    mib = b"\x00" * (1024 * 1024)
    closed: list[bool] = []

    async def chunks() -> AsyncIterator[bytes]:
        try:
            while True:
                yield mib
        finally:
            closed.append(True)

    with pytest.raises(UploadTooLarge):
        await _client(FakeDoor()).upload(call=CALL, filename="sales.xlsx", mime=XLSX, chunks=chunks())
    assert closed == [True]


@pytest.mark.parametrize(
    "name", ["../sales.xlsx", "in/sales.xlsx", "in\\sales.xlsx", ".", "..", "", "sales\n.xlsx", "sales\x00.xlsx"]
)
async def test_an_upload_name_that_is_not_flat_is_refused_unsent(name: str) -> None:
    """A name with a separator or a control character, or one that is no name, is refused before anything is sent."""
    door = FakeDoor()
    with pytest.raises(ValueError):
        await _upload(_client(door), name)
    assert door.requests == []


@pytest.mark.parametrize(("status", "error"), [(413, UploadTooLarge), (507, StorageFull)])
async def test_workbenchs_413_and_507_are_told_apart(status: int, error: type[Exception]) -> None:
    """Workbench's own size limit and a full quota each have their exception."""
    door = FakeDoor()
    door.answers["/files/upload"] = httpx.Response(status, json={"detail": "refused"})
    with pytest.raises(error):
        await _upload(_client(door))


@pytest.mark.parametrize(
    "answer",
    [
        {"path": f"{FOLDER}/sales.xlsx", "size": 4},
        {"path": f"{FOLDER}/sales.xlsx", "size": 4, "id": "xyz"},
        {"path": "/workspace/other/sales.xlsx", "size": 4, "id": UPLOAD_ID},
        {"path": f"{FOLDER}/sales.xlsx", "size": "4", "id": UPLOAD_ID},
        httpx.Response(200, text="stored"),
        httpx.Response(404, json={"detail": "not found"}),
        httpx.ConnectError("connection refused"),
    ],
    ids=["no_id", "id_not_32_hex", "another_folder", "size_not_int", "not_json", "chat_deleted", "unreachable"],
)
async def test_an_upload_answer_without_an_id_is_refused(answer: object) -> None:
    """An answer without a 32-hex id, a path in the chat's folder and a size is unavailable, as is any failure."""
    door = FakeDoor()
    door.answers["/files/upload"] = answer
    with pytest.raises(WorkbenchUnavailable) as raised:
        await _upload(_client(door))
    assert not isinstance(raised.value, WorkbenchNotFound)


async def test_a_view_streams_the_file_and_maps_404() -> None:
    """A file of the chat streams with workbench's headers; a missing one is not found, a failure unavailable."""
    door = FakeDoor()
    door.files[f"{FOLDER}/out/sums.xlsx"] = (b"PK\x03\x04sums", XLSX)
    client = _client(door)
    async with client.view(call=CALL, path=f"{FOLDER}/out/sums.xlsx") as response:
        body = b"".join([chunk async for chunk in response.aiter_bytes()])
        assert response.headers["content-type"] == XLSX
        assert response.headers["content-disposition"] == "inline; filename*=UTF-8''sums.xlsx"
        assert response.headers["content-length"] == "8"
    assert body == b"PK\x03\x04sums"
    assert door.last("/files/view").url.params["path"] == f"{FOLDER}/out/sums.xlsx"

    with pytest.raises(WorkbenchNotFound):
        async with client.view(call=CALL, path=f"{FOLDER}/out/gone.csv"):
            pass
    door.answers["/files/view"] = httpx.Response(503, json={"detail": "busy, retry later"})
    with pytest.raises(WorkbenchUnavailable) as raised:
        async with client.view(call=CALL, path=f"{FOLDER}/out/sums.xlsx"):
            pass
    assert not isinstance(raised.value, WorkbenchNotFound)


async def test_a_path_outside_the_chat_is_never_asked_for() -> None:
    """A view of another folder is not found without a request."""
    door = FakeDoor()
    for path in ("/workspace/other/x.csv", f"{FOLDER}/../x.csv", FOLDER):
        with pytest.raises(WorkbenchNotFound):
            async with _client(door).view(call=CALL, path=path):
                pass
    assert door.requests == []


async def test_deleting_a_chat_returns_workbenchs_count() -> None:
    """DELETE /files/chat answers how many files the chat held; a failure is unavailable."""
    door = FakeDoor()
    arrived: list[int] = []
    door.on_delete = lambda: arrived.append(len(door.requests))
    client = _client(door)
    assert await client.delete_chat(call=CALL) == 3
    assert door.last("/files/chat").method == "DELETE" and arrived == [1]
    door.answers["/files/chat"] = httpx.Response(503, json={"detail": "busy, retry later"})
    with pytest.raises(WorkbenchUnavailable):
        await client.delete_chat(call=CALL)
    door.answers["/files/chat"] = {"deleted": -1}
    with pytest.raises(WorkbenchUnavailable):
        await client.delete_chat(call=CALL)


async def test_without_a_key_nothing_is_sent() -> None:
    """With workbench off, discovery offers nothing, a call fails and the file routes are unavailable."""
    door = FakeDoor()
    client = _client(door, settings=make_settings(Path("unused"), workbench_api_key=None))
    assert client.discover(call=CALL) is None
    result = _call(client)
    assert isinstance(result, ToolFailure) and result.token == "not_configured"
    with pytest.raises(WorkbenchUnavailable):
        await _upload(client)
    with pytest.raises(WorkbenchUnavailable):
        async with client.view(call=CALL, path=f"{FOLDER}/sales.xlsx"):
            pass
    with pytest.raises(WorkbenchUnavailable):
        await client.delete_chat(call=CALL)
    assert door.requests == []


# --- hygiene -----------------------------------------------------------------


class _SpyClient(httpx.AsyncClient):
    """An ``AsyncClient`` that remembers every instance built."""

    built: ClassVar[list[httpx.AsyncClient]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build the client and remember it.

        Args:
            *args: Passed through.
            **kwargs: Passed through.
        """
        super().__init__(*args, **kwargs)
        _SpyClient.built.append(self)


def test_the_client_takes_no_proxy_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy set in the environment never carries a request: every exchange reaches workbench directly."""
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.setattr(_SpyClient, "built", [])
    monkeypatch.setattr(httpx, "AsyncClient", _SpyClient)
    door = FakeDoor()
    door.files[f"{FOLDER}/sales.xlsx"] = (b"PK", XLSX)
    client = _client(door)
    assert client.discover(call=CALL) is not None
    assert isinstance(_call(client), ToolResult)

    async def files() -> None:
        await _upload(client)
        async with client.view(call=CALL, path=f"{FOLDER}/sales.xlsx"):
            pass
        await client.delete_chat(call=CALL)

    anyio.run(files)
    assert len(_SpyClient.built) == 5
    assert all(built.trust_env is False for built in _SpyClient.built)
    rest = httpx.Timeout(CFG.workbench_timeout_s, connect=3.0, write=10.0, pool=3.0)
    assert [built.timeout for built in _SpyClient.built] == [
        httpx.Timeout(5.0, connect=3.0, write=10.0, pool=3.0),
        httpx.Timeout(40.0, connect=3.0, write=10.0, pool=3.0),
        rest,
        rest,
        rest,
    ]


def test_the_log_holds_no_names_or_content(log_lines: list[str]) -> None:
    """No file name, argument, output text, user or chat reaches a log line, on success or failure."""
    secret = "Hafenfest"
    door = FakeDoor()
    door.answers["server/discover"] = httpx.Response(503, text=secret)
    client = _client(door)
    client.discover(call=CALL)
    success = call_result(FOLDER)
    success["content"][0]["text"] = f"{secret} sales.xlsx"
    answers: list[object] = [
        success,
        {**refusal("not_completed"), "content": [{"type": "text", "text": secret}]},
        {**refusal(secret), "content": [{"type": "text", "text": secret}]},
        call_result(FOLDER, [output(FOLDER, path=f"/workspace/{secret}/sales.xlsx")]),
        RpcError(-32602, secret),
        httpx.Response(502, text=secret),
        httpx.ConnectError(secret),
    ]
    for answer in answers:
        door.answers["tools/call"] = answer
        client.call_tool("read_document", {"path": f"{FOLDER}/sales.xlsx", "note": secret}, call=CALL, timeout_s=5.0)

    async def files() -> None:
        await _upload(client, "sales.xlsx", secret.encode())
        door.answers["/files/upload"] = httpx.Response(507, text=secret)
        with pytest.raises(StorageFull):
            await _upload(client, "sales.xlsx", secret.encode())
        with pytest.raises(WorkbenchNotFound):
            async with client.view(call=CALL, path=f"{FOLDER}/out/sales.xlsx"):
                pass
        door.answers["/files/chat"] = httpx.Response(500, text=secret)
        with pytest.raises(WorkbenchUnavailable):
            await client.delete_chat(call=CALL)

    anyio.run(files)
    assert log_lines
    forbidden = (secret, secret.lower(), "sales.xlsx", "user-a", CHAT, KEY)
    assert not [line for line in log_lines if any(value in line for value in forbidden)]


def test_the_key_is_in_no_log_line_exception_or_repr(log_lines: list[str]) -> None:
    """The key stays in the Authorization header: no log line, exception text or repr holds it."""
    door = FakeDoor()
    client = _client(door)
    raised: list[BaseException] = []

    async def files() -> None:
        for answer in (httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.Response(500, json={})):
            door.answers["/files/upload"] = answer
            door.answers["/files/chat"] = answer
            for attempt in (_upload(client), client.delete_chat(call=CALL)):
                try:
                    await attempt
                except WorkbenchUnavailable as exc:
                    raised.append(exc)

    anyio.run(files)
    door.answers["tools/call"] = httpx.ConnectError("refused")
    failure = _call(client)
    assert len(raised) == 6
    shown = [str(exc) + repr(exc) for exc in raised] + [repr(failure), repr(client), repr(CFG), *log_lines]
    assert not [text for text in shown if KEY in text]
