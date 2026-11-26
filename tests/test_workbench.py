"""The workbench client: discovery, tool calls and files, confined to the chat by keyed pseudonyms.

The door is mocked from the shared contract alone (:class:`tests.fakes.FakeDoor`).
Every name, id and value here is invented.
"""

import base64
import hashlib
import hmac
import json
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, ClassVar, cast

import anyio
import httpx
import pytest
from loguru import logger

from ghostwriter import __version__
from ghostwriter.audit import RequestContext, forward_headers
from ghostwriter.store import STORED_MANIFEST_KEYS
from ghostwriter.utils.audit_event import Actor
from ghostwriter.utils.audit_runtime import encode_purpose
from ghostwriter.workbench import (
    ACCEPT_IMAGES,
    DERIVED_TIMEOUT_S,
    DERIVED_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_IMAGE_BYTES,
    MAX_RENDITION_BYTES,
    META_CLIENT_CAPS,
    META_CLIENT_INFO,
    META_PROTOCOL,
    PREPARE_TIMEOUT_S,
    PREVIEW_CELL_CHARS,
    PREVIEW_COLUMNS,
    PREVIEW_ROWS,
    RENDITION_TIMEOUT_S,
    CallFor,
    Manifest,
    Measure,
    OutputFile,
    Rendition,
    RenditionRequest,
    StorageFull,
    TablePreview,
    ToolFailure,
    ToolImage,
    ToolResult,
    UploadedFile,
    UploadTooLarge,
    WorkbenchClient,
    WorkbenchNotFound,
    WorkbenchUnavailable,
    chat_folder,
    serves_measure,
    session_pseudonym,
    user_pseudonym,
)
from tests.conftest import KEY, make_settings
from tests.fakes import (
    CONTRACT_TOOLS,
    DERIVED_ID,
    NOT_FOUND,
    OUTPUT_ID,
    READ_DOCUMENT_TEXT,
    TAKE_BACK,
    TINY_JPEG,
    TINY_PNG,
    UPLOAD_ID,
    XLSX,
    FakeDoor,
    RpcError,
    call_result,
    discover_result,
    image_block,
    output,
    refusal,
    stock_manifest,
    view_result,
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


PDF = f"{FOLDER}/quay-scan.pdf"
PHOTO = f"{FOLDER}/harbour-photo.jpg"
CLIP = f"{FOLDER}/harbour-tour.mp4"
#: What a clip starts with: a few bytes of an MP4 header, then nothing the door reads.
CLIP_BYTES = b"\x00\x00\x00\x18ftypmp42" + bytes(200)
TRANSCRIPT = (
    b'{"start": 0.0, "end": 4.5, "text": "welcome aboard"}\n{"start": 5.0, "end": 91.0, "text": "mind the gap"}\n'
)


def _shown(n: int, **changes: Any) -> dict[str, Any]:
    return {"label": f"page {n}", "n": n, "width": 64, "height": 48, "mime": "image/jpeg", **changes}


def _view_file(door: FakeDoor, shown: list[dict[str, Any]], blocks: list[dict[str, Any]]) -> ToolResult | ToolFailure:
    door.answers["tools/call"] = view_result(FOLDER, shown, blocks)
    return _client(door).call_tool("view_file", {"path": PDF}, call=CALL, timeout_s=40.0, images=True)


def _renditions(client: WorkbenchClient, *wanted: RenditionRequest) -> list[tuple[str, bytes] | None]:
    return client.fetch_renditions(wanted, call=CALL)


#: Every request the client makes, by the name :func:`_operate` knows it by.
OPERATIONS = [
    "discover",
    "call_tool",
    "upload",
    "view",
    "delete_chat",
    "delete_upload",
    "prepare",
    "measure",
    "derived",
    "fetch_renditions",
]


async def _operate(client: WorkbenchClient, door: FakeDoor, operation: str) -> None:
    """Make one of :data:`OPERATIONS` against a door that holds a workbook, a PDF and a clip of the chat.

    Args:
        client: The client.
        door: Its door; the files are stored first.
        operation: Which request.
    """
    door.files[f"{FOLDER}/sales.xlsx"] = (b"PK", XLSX)
    door.files[PDF] = (b"%PDF", "application/pdf")
    door.files[CLIP] = (CLIP_BYTES, "video/mp4")
    if operation == "discover":
        client.discover(call=CALL, images=True)
    elif operation == "call_tool":
        _call(client)
    elif operation == "upload":
        await _upload(client)
    elif operation == "view":
        async with client.view(call=CALL, path=f"{FOLDER}/sales.xlsx"):
            pass
    elif operation == "prepare":
        await client.prepare(call=CALL, path=PDF)
    elif operation == "measure":
        await client.measure(call=CALL, path=PDF)
    elif operation == "derived":
        assert (
            await client.derived(call=CALL, source=CLIP, role="nextext_jsonl", chunks=_chunks(TRANSCRIPT)) == DERIVED_ID
        )
    elif operation == "fetch_renditions":
        assert (
            _renditions(client, RenditionRequest(PDF, 1), RenditionRequest(PDF, 2)) == [("image/jpeg", TINY_JPEG)] * 2
        )
    elif operation == "delete_upload":
        await client.delete_upload(call=CALL, path=f"{FOLDER}/sales.xlsx")
    else:
        await client.delete_chat(call=CALL)


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
    found = WorkbenchClient(CFG, transport=httpx.MockTransport(door)).discover(call=CALL, images=True)
    assert found is not None
    assert _tool_names(found) == [
        "fill_template",
        "inspect_template",
        "list_files",
        "read_document",
        "run_python",
        "search_files",
        "view_file",
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
    assert _client(door).discover(call=CALL, images=True) is None


def test_a_tool_without_an_object_schema_is_dropped() -> None:
    """Only the tools whose inputSchema is an object are offered."""
    door = FakeDoor()
    listed = door.answers["tools/list"]
    assert isinstance(listed, dict)
    tools = [dict(tool) for tool in listed["tools"]]
    tools[0]["inputSchema"] = {"type": "array"}
    del tools[1]["inputSchema"]
    door.answers["tools/list"] = {**listed, "tools": tools}
    found = _client(door).discover(call=CALL, images=True)
    assert found is not None
    assert _tool_names(found) == sorted(CONTRACT_TOOLS)[2:]


def test_each_result_is_cached_for_its_ttl() -> None:
    """tools/list is asked again after its 300 s, server/discover after its hour."""
    door = FakeDoor()
    clock = FakeClock()
    client = _client(door, clock)
    client.discover(call=CALL, images=True)
    clock.advance(299)
    client.discover(call=CALL, images=True)
    assert door.methods() == ["server/discover", "tools/list"]
    clock.advance(2)
    assert client.discover(call=CALL, images=True) is not None
    assert door.methods() == ["server/discover", "tools/list", "tools/list"]
    clock.advance(3600)
    assert client.discover(call=CALL, images=True) is not None
    assert door.methods()[-2:] == ["server/discover", "tools/list"]
    assert len(door.methods()) == 5


def test_a_failed_discovery_is_tried_again_at_most_once_a_minute() -> None:
    """After a failure workbench answered, turns within the next 60 s get no tool and send nothing."""
    door = FakeDoor()
    door.answers["server/discover"] = httpx.Response(503, text="unavailable")
    clock = FakeClock()
    client = _client(door, clock)
    assert client.discover(call=CALL, images=True) is None
    clock.advance(59)
    assert client.discover(call=CALL, images=True) is None
    assert door.methods() == ["server/discover"]
    clock.advance(2)
    door.answers["server/discover"] = FakeDoor().answers["server/discover"]
    assert client.discover(call=CALL, images=True) is not None
    assert door.methods() == ["server/discover", "server/discover", "tools/list"]


@pytest.mark.parametrize(
    "fault",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectError("name or service not known"),
        httpx.RemoteProtocolError("server disconnected without sending a response"),
    ],
    ids=["refused", "no_such_name", "disconnected"],
)
def test_an_unreachable_workbench_is_discovered_again_at_once(fault: httpx.TransportError) -> None:
    """A failure that comes fast sets no delay: the next turn discovers again, and finds workbench once it is back."""
    door = FakeDoor()
    door.answers["server/discover"] = fault
    client = _client(door, FakeClock())
    assert client.discover(call=CALL, images=True) is None
    assert client.discover(call=CALL, images=True) is None
    assert door.methods() == ["server/discover"] * 2
    door.answers["server/discover"] = discover_result()
    assert client.discover(call=CALL, images=True) is not None
    assert door.methods() == ["server/discover"] * 3 + ["tools/list"]


@pytest.mark.parametrize(
    "fault",
    [httpx.ConnectTimeout("timed out"), httpx.ReadTimeout("timed out"), RuntimeError("door broke")],
    ids=["connect_timeout", "read_timeout", "error"],
)
def test_a_slow_or_broken_discovery_keeps_its_delay(fault: Exception) -> None:
    """A timeout or an error is tried again only after 60 s, while workbench answers nothing else."""
    door = FakeDoor()
    door.answers["server/discover"] = fault
    clock = FakeClock()
    client = _client(door, clock)
    assert client.discover(call=CALL, images=True) is None
    door.answers["server/discover"] = discover_result()
    clock.advance(59)
    assert client.discover(call=CALL, images=True) is None
    assert door.methods() == ["server/discover"]
    clock.advance(2)
    assert client.discover(call=CALL, images=True) is not None
    assert door.methods() == ["server/discover", "server/discover", "tools/list"]


@pytest.mark.parametrize("change", ["no_modern_revision", "no_usable_tool", "rpc_error"])
def test_an_answered_failure_keeps_its_delay_although_it_was_an_answer(change: str) -> None:
    """The discovery's own answers end no delay of its own: the next turn, at once, sends nothing."""
    door = FakeDoor()
    listed = door.answers["tools/list"]
    assert isinstance(listed, dict)
    if change == "no_modern_revision":
        door.answers["server/discover"] = {"supportedVersions": ["2025-11-25"], "resultType": "complete"}
    elif change == "no_usable_tool":
        none: list[dict[str, Any]] = []
        door.answers["tools/list"] = {**listed, "tools": none}
    else:
        door.answers["tools/list"] = RpcError(-32603, "internal error")
    client = _client(door, FakeClock())
    assert client.discover(call=CALL, images=True) is None
    sent = len(door.requests)
    assert client.discover(call=CALL, images=True) is None
    assert len(door.requests) == sent


@pytest.mark.parametrize("operation", [name for name in OPERATIONS if name != "discover"])
async def test_any_answer_from_workbench_ends_both_delays(operation: str) -> None:
    """Both listings timed out and wait out their delay; one answer to any request ends both, and both discover."""
    door = FakeDoor()
    door.answers["server/discover"] = httpx.ReadTimeout("timed out")
    client = _client(door, FakeClock())
    assert client.discover(call=CALL, images=True) is None
    assert client.discover(call=CALL, images=False) is None
    door.answers["server/discover"] = discover_result()
    assert client.discover(call=CALL, images=True) is None
    assert door.methods() == ["server/discover"] * 2
    await _operate(client, door, operation)
    before = len(door.methods())
    assert client.discover(call=CALL, images=True) is not None
    assert client.discover(call=CALL, images=False) is not None
    assert door.methods()[before:] == ["server/discover", "tools/list"] * 2


async def test_a_refusal_is_an_answer_too() -> None:
    """A refused upload (workbench's 503) shows workbench up all the same: the delay ends."""
    door = FakeDoor()
    door.answers["server/discover"] = httpx.ReadTimeout("timed out")
    client = _client(door, FakeClock())
    assert client.discover(call=CALL, images=False) is None
    door.answers["server/discover"] = discover_result()
    door.answers["/files/upload"] = httpx.Response(503, json={"detail": "busy, retry later"})
    with pytest.raises(WorkbenchUnavailable):
        await _upload(client)
    assert client.discover(call=CALL, images=False) is not None


def test_a_failure_is_logged_once_until_a_discovery_succeeds(loguru_caplog: pytest.LogCaptureFixture) -> None:
    """One warning when a listing goes down, none while it stays down, however it fails; again once it was up."""
    door = FakeDoor()
    clock = FakeClock()
    client = _client(door, clock)
    door.answers["server/discover"] = httpx.ConnectError("connection refused")
    assert client.discover(call=CALL, images=True) is None
    assert client.discover(call=CALL, images=True) is None
    door.answers["server/discover"] = httpx.ReadTimeout("timed out")
    assert client.discover(call=CALL, images=True) is None
    _call(client)
    door.answers["server/discover"] = httpx.Response(503, text="unavailable")
    assert client.discover(call=CALL, images=True) is None
    assert [record.getMessage() for record in loguru_caplog.records] == [
        "workbench discovery failed: unreachable. No tool is offered; the next turn tries again"
    ]
    door.answers["server/discover"] = discover_result()
    clock.advance(61)
    assert client.discover(call=CALL, images=True) is not None
    door.answers["server/discover"] = httpx.Response(503, text="unavailable")
    clock.advance(3601)
    assert client.discover(call=CALL, images=True) is None
    assert [record.getMessage() for record in loguru_caplog.records][1:] == [
        "workbench discovery failed: http:503. No tool is offered; the next try comes after 60 s,"
        " or at once when workbench answers a request"
    ]


def _description(found: Any, name: str) -> str:
    return next(tool.description for tool in found.tools if tool.name == name)


def test_each_listing_is_asked_with_its_own_header() -> None:
    """With images, both requests carry X-Accept-Images and list view_file; without, neither does, as 0.6 lists.

    The asking call's headers carry no such header either way.
    """
    assert ACCEPT_IMAGES == {"X-Accept-Images": "1"} and "x-accept-images" not in {k.lower() for k in CALL.headers}
    door = FakeDoor()
    client = _client(door)
    full = client.discover(call=CALL, images=True)
    plain = client.discover(call=CALL, images=False)
    assert full is not None and plain is not None
    assert "view_file" in _tool_names(full) and _tool_names(plain) == [n for n in _tool_names(full) if n != "view_file"]
    assert _description(full, "read_document") == CONTRACT_TOOLS["read_document"][0]
    assert _description(plain, "read_document") == READ_DOCUMENT_TEXT != CONTRACT_TOOLS["read_document"][0]
    assert door.methods() == ["server/discover", "tools/list"] * 2
    assert [request.headers.get("x-accept-images") for request, _ in door.requests] == ["1", "1", None, None]


def test_each_listing_is_cached_and_tried_again_on_its_own() -> None:
    """A listing's cache window and retry delay are its own; a call that drops the discovery drops both.

    A failure delays its own listing only, and the other's discovery, answered, ends that delay as any answer does.
    """
    door = FakeDoor()
    clock = FakeClock()
    client = _client(door, clock)
    door.answers["server/discover"] = httpx.Response(503, text="unavailable")
    assert client.discover(call=CALL, images=True) is None
    door.answers["server/discover"] = discover_result()
    assert client.discover(call=CALL, images=True) is None
    assert door.methods() == ["server/discover"]
    assert client.discover(call=CALL, images=False) is not None
    clock.advance(30)
    assert client.discover(call=CALL, images=True) is not None
    assert door.methods()[1:] == ["server/discover", "tools/list"] * 2
    clock.advance(280)
    assert client.discover(call=CALL, images=False) is not None
    assert client.discover(call=CALL, images=True) is not None
    assert door.methods()[5:] == ["tools/list"]
    assert door.requests[-1][0].headers.get("x-accept-images") is None

    door.answers["tools/call"] = httpx.ConnectError("connection refused")
    _call(client)
    assert client.discover(call=CALL, images=True) is not None
    assert client.discover(call=CALL, images=False) is not None
    assert door.methods()[6:] == ["tools/call"] + ["server/discover", "tools/list"] * 2


def test_one_listing_in_progress_does_not_hold_the_other() -> None:
    """Single-flight per listing: a slow discovery with images and one without run side by side.

    Each waits 1 s at the door, so one after the other would take 2 s.
    """
    door = FakeDoor()
    door.delays["server/discover"] = 1.0
    client = _client(door)
    found: dict[bool, Any] = {}

    def ask(images: bool) -> None:
        found[images] = client.discover(call=CALL, images=images)

    threads = [threading.Thread(target=ask, args=(images,)) for images in (True, False)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)
    assert time.monotonic() - started < 1.6
    assert found[True] is not None and found[False] is not None
    assert door.methods().count("server/discover") == 2


async def test_the_door_lists_as_workbench_0_7_does_with_and_without_images() -> None:
    """As workbench 0.7: nine tools and 0.6's read_document text without X-Accept-Images, ten with it."""
    auth = {"Authorization": f"Bearer {KEY}"}
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(FakeDoor()), base_url="http://workbench.test") as http:
        plain = (await http.post("/mcp", json=message, headers=auth)).json()
        full = (await http.post("/mcp", json=message, headers={**auth, **ACCEPT_IMAGES})).json()
    plain_tools = {tool["name"]: tool["description"] for tool in plain["result"]["tools"]}
    full_tools = {tool["name"]: tool["description"] for tool in full["result"]["tools"]}
    assert "view_file" not in plain_tools and len(plain_tools) == 9
    assert sorted(full_tools) == sorted(CONTRACT_TOOLS)
    assert (plain_tools["read_document"], full_tools["read_document"]) == (
        READ_DOCUMENT_TEXT,
        CONTRACT_TOOLS["read_document"][0],
    )
    assert plain_tools["run_python"] == full_tools["run_python"]
    assert "view_file" in CONTRACT_TOOLS and "strips" in CONTRACT_TOOLS["read_document"][1]


# --- requests ----------------------------------------------------------------


def test_every_request_states_the_modern_revision() -> None:
    """Each POST to /mcp carries the key, the content types, the revision, its method and the client's _meta."""
    door = FakeDoor()
    client = _client(door)
    client.discover(call=CALL, images=True)
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


def test_a_call_asks_for_images_only_when_told_to() -> None:
    """X-Accept-Images goes out with images=True and with nothing else; a caller's own header is not forwarded."""
    door = FakeDoor()
    client = _client(door)
    _call(client)
    client.call_tool("view_file", {"path": PDF}, call=CALL, timeout_s=40.0, images=True)
    client.call_tool("view_file", {"path": PDF}, call=CALL, timeout_s=40.0, images=False)
    asking = CallFor(CALL.owner, CALL.chat_id, {**CALL.headers, "X-Accept-Images": "1"})
    client.call_tool("run_python", {"code": SOURCE}, call=asking, timeout_s=40.0)
    assert [request.headers.get("x-accept-images") for request, _ in door.requests] == [None, "1", None, None]


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_every_request_carries_the_session_purpose_and_request_id(operation: str) -> None:
    """Discovery, tools/call and the file routes each carry the chat, the user, the purpose and the request id."""
    door = FakeDoor()
    await _operate(_client(door), door, operation)
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


def test_a_view_file_answer_keeps_its_images_with_their_labels() -> None:
    """Each image block is kept with the label, size and type of its own shown entry; the text is unchanged."""
    door = FakeDoor()
    shown = [_shown(1), _shown(2, width=1131, height=1600)]
    result = _view_file(door, shown, [image_block(TINY_JPEG), image_block(TINY_PNG, "image/png")])
    assert isinstance(result, ToolResult) and door.last_call_answer is not None
    assert result.text == door.last_call_answer["content"][0]["text"]
    assert result.images == (
        ToolImage("page 1", 64, 48, "image/jpeg", TINY_JPEG),
        ToolImage("page 2", 1131, 1600, "image/png", TINY_PNG),
    )
    assert result.outputs == ()


def test_a_tool_image_shows_no_bytes_in_its_repr() -> None:
    """The picture's bytes are not in the repr, so no log or assertion message carries them."""
    image = ToolImage("page 1", 64, 48, "image/jpeg", TINY_JPEG)
    assert repr(image) == "ToolImage(label='page 1', width=64, height=48, mime='image/jpeg')"


@pytest.mark.parametrize("fault", ["bad_base64", "gif", "png_as_jpeg", "past_1_mib", "no_shown_entry"])
def test_image_blocks_that_are_not_images_are_dropped(fault: str, loguru_caplog: pytest.LogCaptureFixture) -> None:
    """A block that is not a jpeg or png of at most 1 MiB, or has no shown entry, is dropped with one warning."""
    label = "Hafenplan Seite 1"
    shown: list[dict[str, Any]] = [_shown(1, label=label)]
    block = image_block(TINY_JPEG)
    if fault == "bad_base64":
        block = {**block, "data": "not base64 !"}
    elif fault == "gif":
        block = image_block(TINY_JPEG, "image/gif")
    elif fault == "png_as_jpeg":
        block = image_block(TINY_PNG, "image/jpeg")
    elif fault == "past_1_mib":
        block = image_block(TINY_JPEG + bytes(MAX_IMAGE_BYTES + 1 - len(TINY_JPEG)))
    else:
        shown = []
    door = FakeDoor()
    result = _view_file(door, shown, [block])
    assert isinstance(result, ToolResult) and door.last_call_answer is not None
    assert result.images == () and result.text == door.last_call_answer["content"][0]["text"]
    assert len(loguru_caplog.records) == 1 and label not in loguru_caplog.text


def test_a_block_of_exactly_1_mib_is_kept() -> None:
    """The cap is on a block's size: 1 MiB is kept."""
    data = TINY_JPEG + bytes(MAX_IMAGE_BYTES - len(TINY_JPEG))
    result = _view_file(FakeDoor(), [_shown(1)], [image_block(data)])
    assert isinstance(result, ToolResult) and [image.data for image in result.images] == [data]


@pytest.mark.parametrize(
    "changes",
    [
        {"label": "x" * 101},
        {"label": 7},
        {"width": 0},
        {"width": 4097},
        {"height": "48"},
        {"height": True},
        {"height": 48.0},
    ],
    ids=["label_101", "label_not_text", "width_0", "width_4097", "height_text", "height_bool", "height_float"],
)
def test_a_block_whose_shown_entry_is_malformed_is_dropped(changes: dict[str, Any]) -> None:
    """A label past 100 characters, or a width or height that is not an integer of 1 to 4096, drops the block."""
    result = _view_file(FakeDoor(), [_shown(1, **changes)], [image_block(TINY_JPEG)])
    assert isinstance(result, ToolResult) and result.images == ()


def test_a_block_pairs_with_the_shown_entry_at_its_own_position() -> None:
    """Block i is shown[i] whatever happened to the blocks before it: a dropped block does not shift the others."""
    shown = [_shown(1), _shown(2, label="second"), _shown(3, label="third")]
    blocks = [image_block(TINY_JPEG, "image/gif"), image_block(TINY_JPEG), image_block(TINY_PNG, "image/png")]
    result = _view_file(FakeDoor(), shown, blocks)
    assert isinstance(result, ToolResult)
    assert [(image.label, image.mime) for image in result.images] == [("second", "image/jpeg"), ("third", "image/png")]


@pytest.mark.parametrize(
    ("count", "size", "kept"), [(10, len(TINY_JPEG), 8), (4, 900 * 1024, 3)], ids=["ten_blocks", "four_of_900_kib"]
)
def test_at_most_8_images_and_3_mib_are_kept(
    count: int, size: int, kept: int, monkeypatch: pytest.MonkeyPatch, loguru_caplog: pytest.LogCaptureFixture
) -> None:
    """Keeping stops at 8 images, or before the image that would take the answer past 3 MiB; the rest is counted."""
    # Past 3 MiB of pictures an answer is past the 4 MiB cap on a whole answer, which workbench's own 2.5 MiB keeps.
    monkeypatch.setattr("ghostwriter.workbench.MAX_ANSWER_BYTES", 16 * 1024 * 1024)
    data = TINY_JPEG + bytes(size - len(TINY_JPEG))
    result = _view_file(FakeDoor(), [_shown(n) for n in range(1, count + 1)], [image_block(data) for _ in range(count)])
    assert isinstance(result, ToolResult)
    assert [image.label for image in result.images] == [f"page {n}" for n in range(1, kept + 1)]
    assert len(loguru_caplog.records) == 1 and str(count - kept) in loguru_caplog.records[0].getMessage()


def test_an_error_result_keeps_no_image() -> None:
    """A refusal is a failure with its token; whatever blocks come with it are not read."""
    door = FakeDoor()
    door.answers["tools/call"] = {
        **refusal("refused"),
        "content": [*refusal("refused")["content"], image_block(TINY_JPEG)],
    }
    result = _client(door).call_tool("view_file", {"path": PDF}, call=CALL, timeout_s=40.0, images=True)
    assert result == ToolFailure("refused", "busy, retry later", answered=True)


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
    assert client.discover(call=CALL, images=True) is not None

    door.answers["tools/call"] = httpx.ConnectError("connection refused")
    result = _call(client)
    assert isinstance(result, ToolFailure) and (result.token, result.answered) == ("unreachable", False)
    assert client.discover(call=CALL, images=True) is not None
    assert door.methods()[-2:] == ["server/discover", "tools/list"]

    door.answers["tools/call"] = refusal("invalid_arguments")
    assert _call(client) == ToolFailure("invalid_arguments", "busy, retry later", answered=True)
    assert client.discover(call=CALL, images=True) is not None
    assert door.methods()[-2:] == ["server/discover", "tools/list"]

    door.answers["tools/call"] = call_result
    door.delays = {"tools/call": 1.0}
    result = _call(client, timeout_s=0.1)
    assert isinstance(result, ToolFailure) and result.token == "timeout"
    assert client.discover(call=CALL, images=True) is not None
    assert door.methods()[-2:] == ["server/discover", "tools/list"]

    door.delays = {}
    door.answers["tools/call"] = refusal("queue_full")
    assert _call(client) == ToolFailure("queue_full", "busy, retry later", answered=True)
    assert client.discover(call=CALL, images=True) is not None
    assert door.methods()[-1] == "tools/call"
    assert door.methods().count("server/discover") == 4


@pytest.mark.parametrize("code", [-32602, -32022])
def test_an_unknown_tool_or_a_refused_revision_drops_the_discovery(code: int) -> None:
    """-32602 and -32022 mean the discovery may be stale: the next turn asks again. Other codes keep it."""
    door = FakeDoor()
    client = _client(door)
    assert client.discover(call=CALL, images=True) is not None
    door.answers["tools/call"] = RpcError(code, "unknown tool")
    assert _call(client) == ToolFailure(f"jsonrpc:{code}", "unknown tool", answered=True)
    assert client.discover(call=CALL, images=True) is not None
    assert door.methods().count("server/discover") == 2

    door.answers["tools/call"] = RpcError(-32603, "internal error", status=500)
    _call(client)
    assert client.discover(call=CALL, images=True) is not None
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


async def test_prepare_answers_the_manifest() -> None:
    """POST /files/prepare carries the path and the chat's session, no body; the manifest is read and kept."""
    door = FakeDoor()
    door.files[PDF] = (b"%PDF", "application/pdf")
    got = await _client(door).prepare(call=CALL, path=PDF)
    req = door.last("/files/prepare")
    assert req.method == "POST" and dict(req.url.params) == {"path": PDF} and req.content == b""
    assert req.headers["x-session-id"] == session_pseudonym(KEY, CHAT)
    assert req.extensions["timeout"]["read"] == PREPARE_TIMEOUT_S == 390.0
    assert got.kind == "pdf" and got.total == 3 and got.pages_without_text == (2,) and got.frames is None
    assert got.renditions == tuple(Rendition(n, 1131, 1600, "image/jpeg") for n in (1, 2, 3))
    stored = got.as_stored()
    assert tuple(stored) == STORED_MANIFEST_KEYS
    assert stored == {
        "kind": "pdf",
        "total": 3,
        "pages_without_text": [2],
        "renditions": [{"n": n, "width": 1131, "height": 1600, "mime": "image/jpeg"} for n in (1, 2, 3)],
        "frames": None,
        "unit": "page",
        "duration_s": None,
        "transcript": False,
    }
    assert json.loads(json.dumps(stored)) == stored


async def test_a_prepare_waits_390_s() -> None:
    """Workbench's 120 s of queue, two job walls of 120 s (an Office file is converted, then drawn) and 30 s."""
    door = FakeDoor()
    door.files[PDF] = (b"%PDF", "application/pdf")
    await _client(door).prepare(call=CALL, path=PDF)
    request = door.last("/files/prepare")
    assert (request.method, request.extensions["timeout"]["read"]) == ("POST", 390.0)
    assert PREPARE_TIMEOUT_S == 390.0


async def test_prepare_reads_an_office_manifest_with_its_unit() -> None:
    """A deck is kind office and counts slides; a Word file is kind office and counts pages; the unit is kept."""
    door = FakeDoor()
    deck, memo = f"{FOLDER}/harbour-deck.pptx", f"{FOLDER}/quay-memo.docx"
    for path in (deck, memo):
        door.files[path] = (b"PK", "application/octet-stream")
    client = _client(door)
    slides = await client.prepare(call=CALL, path=deck)
    assert (slides.kind, slides.unit, slides.total, slides.pages_without_text) == ("office", "slide", 3, (2,))
    assert slides.renditions == tuple(Rendition(n, 1131, 1600, "image/jpeg") for n in (1, 2, 3))
    assert slides.as_stored()["unit"] == "slide" and slides.as_stored()["kind"] == "office"
    pages = await client.prepare(call=CALL, path=memo)
    assert (pages.kind, pages.unit, pages.total, pages.pages_without_text) == ("office", "page", 3, ())
    assert tuple(pages.as_stored()) == STORED_MANIFEST_KEYS


async def test_a_manifest_without_unit_reads_as_page() -> None:
    """Workbench 0.7 sends seven keys: a pdf's unit is then a page, and a docx it answers none for stays none."""
    door = FakeDoor()
    memo = f"{FOLDER}/quay-memo.docx"
    door.files[PDF] = (b"%PDF", "application/pdf")
    door.files[memo] = (b"PK", "application/octet-stream")
    seven: dict[str, Any] = {
        "path": memo,
        "kind": "none",
        "total": 0,
        "pages_without_text": [],
        "renditions": [],
        "frames": None,
    }
    door.manifests[PDF] = {key: value for key, value in stock_manifest(PDF).items() if key != "unit"}
    door.manifests[memo] = {**seven, "hint": None}
    client = _client(door)
    scan = await client.prepare(call=CALL, path=PDF)
    assert (scan.kind, scan.unit, scan.total) == ("pdf", "page", 3)
    assert scan.as_stored()["unit"] == "page"
    word = await client.prepare(call=CALL, path=memo)
    assert (word.kind, word.unit) == ("none", "page")
    assert word == Manifest("none", 0, (), (), None)


async def test_a_prepare_that_cuts_strips_may_wait_less() -> None:
    """``strips`` goes as a query parameter only past 1, and ``timeout_s`` bounds each read instead of the default."""
    door = FakeDoor()
    door.files[PHOTO] = (b"\xff\xd8\xff", "image/jpeg")
    got = await _client(door).prepare(call=CALL, path=PHOTO, strips=2, timeout_s=60.0)
    req = door.last("/files/prepare")
    assert dict(req.url.params) == {"path": PHOTO, "strips": "2"}
    assert req.extensions["timeout"]["read"] == 60.0 and got.kind == "image"


async def test_prepare_reads_a_picture_and_a_file_that_is_neither() -> None:
    """A picture is kind image with one rendition; any other file is kind none, with nothing to show."""
    door = FakeDoor()
    door.files[PHOTO] = (b"\xff\xd8\xff", "image/jpeg")
    door.files[f"{FOLDER}/sales.xlsx"] = (b"PK", XLSX)
    door.manifests[PHOTO] = {**stock_manifest(PHOTO), "frames": 12, "hint": "a note"}
    client = _client(door)
    photo = await client.prepare(call=CALL, path=PHOTO)
    assert (photo.kind, photo.total, photo.pages_without_text, photo.frames) == ("image", 1, (), 12)
    assert photo.renditions == (Rendition(1, 64, 48, "image/jpeg"),)
    sheet = await client.prepare(call=CALL, path=f"{FOLDER}/sales.xlsx")
    assert sheet == Manifest("none", 0, (), (), None)


async def test_prepare_asks_for_strips_only_past_one() -> None:
    """The strips parameter goes out only when it is more than 1."""
    door = FakeDoor()
    door.files[PHOTO] = (b"\xff\xd8\xff", "image/jpeg")
    client = _client(door)
    for strips in (1, 2, 3):
        await client.prepare(call=CALL, path=PHOTO, strips=strips)
    assert [dict(r.url.params) for r, _ in door.requests] == [
        {"path": PHOTO},
        {"path": PHOTO, "strips": "2"},
        {"path": PHOTO, "strips": "3"},
    ]


@pytest.mark.parametrize(
    ("answer", "error", "token"),
    [
        (httpx.Response(404, json=NOT_FOUND), WorkbenchNotFound, "not_found"),
        (httpx.Response(422, json={"detail": "unreadable_file: damaged"}), WorkbenchUnavailable, "unreadable_file"),
        (httpx.Response(422, json={"detail": "unsupported_format: .xyz"}), WorkbenchUnavailable, "unsupported_format"),
        (httpx.Response(422, json={"detail": "read_timeout: 90 s"}), WorkbenchUnavailable, "read_timeout"),
        (httpx.Response(422, json={"detail": "the file could not be processed: x"}), WorkbenchUnavailable, "http:422"),
        (httpx.Response(422, json={"detail": "unreadable_file"}), WorkbenchUnavailable, "http:422"),
        (httpx.Response(422, json={"detail": [{"loc": ["query", "path"]}]}), WorkbenchUnavailable, "http:422"),
        (httpx.Response(422, text="not json"), WorkbenchUnavailable, "http:422"),
        (httpx.Response(428, json={"detail": "purpose"}), WorkbenchUnavailable, "http:428"),
        (httpx.Response(503, json={"detail": "busy, retry later"}), WorkbenchUnavailable, "http:503"),
        (httpx.Response(507, json={"detail": "storage full"}), StorageFull, None),
        (httpx.ConnectError("connection refused"), WorkbenchUnavailable, "unreachable"),
        (httpx.ReadTimeout("slow"), WorkbenchUnavailable, "timeout"),
    ],
    ids=[
        "not_found",
        "unreadable",
        "unsupported",
        "read_timeout",
        "sentence",
        "no_colon",
        "validation_list",
        "not_json",
        "no_purpose",
        "busy",
        "storage_full",
        "unreachable",
        "timeout",
    ],
)
async def test_prepare_maps_workbenchs_refusals(answer: object, error: type[Exception], token: str | None) -> None:
    """404 is not found, 507 a full store, a 422's token the text before its colon, any other status http:<status>.

    A 422 detail without a colon names no token, however token-like it reads.
    """
    door = FakeDoor()
    door.files[PDF] = (b"%PDF", "application/pdf")
    door.answers["/files/prepare"] = answer
    with pytest.raises(error) as raised:
        await _client(door).prepare(call=CALL, path=PDF)
    assert type(raised.value) is error
    assert token is None or getattr(raised.value, "token", None) == token


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "video"},
        {"kind": None},
        {"unit": "sheet"},
        {"unit": 1},
        {"total": -1},
        {"total": "3"},
        {"pages_without_text": [0]},
        {"pages_without_text": "2"},
        {"renditions": [{"n": 1, "width": 1, "height": 1, "mime": "image/gif"}]},
        {"renditions": [{"n": 1, "width": 0, "height": 1, "mime": "image/png"}]},
        {"renditions": [{"n": 1.5, "width": 1, "height": 1, "mime": "image/png"}]},
        {"renditions": ["x"]},
        {"frames": "2"},
        {"frames": -1},
    ],
)
async def test_a_malformed_manifest_is_a_bad_answer(change: dict[str, Any]) -> None:
    """A manifest whose kind, counts, pages or renditions are not what the contract says is refused whole."""
    door = FakeDoor()
    door.answers["/files/prepare"] = {**stock_manifest(PDF), **change}
    with pytest.raises(WorkbenchUnavailable) as raised:
        await _client(door).prepare(call=CALL, path=PDF)
    assert raised.value.token == "bad_answer" and not isinstance(raised.value, WorkbenchNotFound)


@pytest.mark.parametrize("answer", [[], "pdf", httpx.Response(200, text="pdf")], ids=["list", "text", "not_json"])
async def test_a_prepare_answer_that_is_not_an_object_is_a_bad_answer(answer: object) -> None:
    """Only one JSON object can be a manifest."""
    door = FakeDoor()
    door.answers["/files/prepare"] = answer
    with pytest.raises(WorkbenchUnavailable):
        await _client(door).prepare(call=CALL, path=PDF)


async def test_prepare_never_asks_outside_the_chat() -> None:
    """A path outside the chat's folder is not found, and nothing is sent."""
    door = FakeDoor()
    for path in ("/workspace/other/x.pdf", f"{FOLDER}/../x.pdf", FOLDER):
        with pytest.raises(WorkbenchNotFound):
            await _client(door).prepare(call=CALL, path=path)
    assert door.requests == []


NOTES = f"{FOLDER}/tide-notes.txt"
#: Two lines, 31 characters.
NOTES_TEXT = b"high tide 06:12\nlow tide 12:30\n"
#: A measure as workbench 0.10 answers it.
MEASURED = {"path": NOTES, "kind": "text", "unit": "line", "total": 2, "chars": 31, "capped": False}


async def test_measure_asks_for_one_file_of_the_chat() -> None:
    """POST /files/measure carries the path as its JSON body, and the chat, the purpose and the request id."""
    door = FakeDoor(server_version="0.10.0")
    door.files[NOTES] = (NOTES_TEXT, "text/plain")
    got = await _client(door).measure(call=CALL, path=NOTES)
    req = door.last("/files/measure")
    assert (req.method, dict(req.url.params)) == ("POST", {})
    assert json.loads(req.content) == {"path": NOTES}
    assert req.headers["content-type"] == "application/json"
    assert req.headers["accept-encoding"] == "identity"
    assert req.headers["x-session-id"] == session_pseudonym(KEY, CHAT)
    assert req.headers["x-user-id"] == user_pseudonym(KEY, "user-a")
    assert req.headers["x-audit-purpose"] == encode_purpose(None, "ghostwriter chat")
    assert req.headers["x-request-id"] == "req-0001"
    assert req.extensions["timeout"]["read"] == PREPARE_TIMEOUT_S == 390.0
    assert got == Measure(total=2, unit="line", chars=len(NOTES_TEXT), capped=False)
    assert got.as_stored() == {"total": 2, "unit": "line", "chars": 31, "capped": False}


@pytest.mark.parametrize(
    ("answer", "error", "token"),
    [
        (httpx.Response(404, json=NOT_FOUND), WorkbenchNotFound, "not_found"),
        (httpx.Response(422, json={"detail": "unreadable_file: damaged"}), WorkbenchUnavailable, "unreadable_file"),
        (httpx.Response(422, json={"detail": "unsupported_format: .xyz"}), WorkbenchUnavailable, "unsupported_format"),
        (httpx.Response(507, json={"detail": "storage full"}), StorageFull, None),
        (httpx.Response(503, json={"detail": "busy, retry later"}), WorkbenchUnavailable, "http:503"),
        ({**MEASURED, "unit": "chapter"}, WorkbenchUnavailable, "bad_answer"),
        ({**MEASURED, "chars": -1}, WorkbenchUnavailable, "bad_answer"),
        ({**MEASURED, "capped": "yes"}, WorkbenchUnavailable, "bad_answer"),
        ({**MEASURED, "total": True}, WorkbenchUnavailable, "bad_answer"),
        ([MEASURED], WorkbenchUnavailable, "bad_answer"),
    ],
    ids=[
        "not_found",
        "unreadable",
        "unsupported",
        "storage_full",
        "busy",
        "unit",
        "chars",
        "capped",
        "total_bool",
        "not_an_object",
    ],
)
async def test_measure_maps_refusals(answer: object, error: type[Exception], token: str | None) -> None:
    """As prepare's: 404, a 422's token, 507, http:<status>; an answer the contract does not allow is a bad answer."""
    door = FakeDoor(server_version="0.10.0")
    door.files[NOTES] = (NOTES_TEXT, "text/plain")
    door.answers["/files/measure"] = answer
    with pytest.raises(error) as raised:
        await _client(door).measure(call=CALL, path=NOTES)
    assert type(raised.value) is error
    assert token is None or getattr(raised.value, "token", None) == token


async def test_measure_never_asks_outside_the_chat() -> None:
    """A path outside the chat's folder is not found, and nothing is sent."""
    door = FakeDoor(server_version="0.10.0")
    for path in ("/workspace/other/x.txt", f"{FOLDER}/../x.txt", FOLDER):
        with pytest.raises(WorkbenchNotFound):
            await _client(door).measure(call=CALL, path=path)
    assert door.requests == []


@pytest.mark.parametrize(
    ("version", "served"),
    [("0.10.0", True), ("0.11", True), ("1.0.2", True), ("0.9.3", False), ("", False), ("dev", False)],
)
def test_serves_measure(version: str, served: bool) -> None:
    """Workbench 0.10 and later measures a document; an older one, no version and any other text do not."""
    assert serves_measure(version) is served


async def test_derived_streams_an_artifact_into_the_clips_folder() -> None:
    """The artifact goes as the raw body, typed by its role, to the clip's source, for the chat's session."""
    door = FakeDoor()
    door.files[CLIP] = (CLIP_BYTES, "video/mp4")
    client = _client(door)
    got = await client.derived(
        call=CALL, source=CLIP, role="nextext_jsonl", chunks=_chunks(TRANSCRIPT[:20], TRANSCRIPT[20:])
    )
    assert got == DERIVED_ID
    req = door.last("/files/derived")
    assert req.method == "POST" and dict(req.url.params) == {"source": CLIP, "role": "nextext_jsonl"}
    assert req.headers["content-type"] == "application/x-ndjson" == DERIVED_TYPES["nextext_jsonl"]
    assert req.headers["accept-encoding"] == "identity" and "content-length" not in req.headers
    assert req.headers["x-session-id"] == session_pseudonym(KEY, CHAT)
    assert req.extensions["timeout"]["read"] == DERIVED_TIMEOUT_S == 150.0
    assert door.derived == [(CLIP, "nextext_jsonl", TRANSCRIPT)]
    frames = b"PK\x03\x04" + bytes(64)
    assert await client.derived(call=CALL, source=CLIP, role="nextext_zip", chunks=_chunks(frames)) == DERIVED_ID
    req = door.last("/files/derived")
    assert dict(req.url.params) == {"source": CLIP, "role": "nextext_zip"}
    assert req.headers["content-type"] == "application/zip" == DERIVED_TYPES["nextext_zip"]
    assert door.derived[-1] == (CLIP, "nextext_zip", frames)


@pytest.mark.parametrize(
    ("answer", "error", "token"),
    [
        (httpx.Response(404, json=NOT_FOUND), WorkbenchNotFound, "not_found"),
        (httpx.Response(413, json={"detail": "file too large"}), WorkbenchUnavailable, "http:413"),
        (
            httpx.Response(422, json={"detail": "unreadable_file: the artifact is empty"}),
            WorkbenchUnavailable,
            "unreadable_file",
        ),
        (
            httpx.Response(422, json={"detail": "unreadable_file: the artifact is not what its role says"}),
            WorkbenchUnavailable,
            "unreadable_file",
        ),
        (httpx.Response(422, json={"detail": "unsupported_format: .mp4"}), WorkbenchUnavailable, "unsupported_format"),
        (httpx.Response(422, json={"detail": [{"loc": ["query", "role"]}]}), WorkbenchUnavailable, "http:422"),
        (httpx.Response(400, json={"detail": "invalid session"}), WorkbenchUnavailable, "http:400"),
        (httpx.Response(428, json={"detail": "purpose"}), WorkbenchUnavailable, "http:428"),
        (httpx.Response(503, json={"detail": "busy, retry later"}), WorkbenchUnavailable, "http:503"),
        (httpx.Response(507, json={"detail": "storage full"}), StorageFull, None),
        (httpx.ConnectError("connection refused"), WorkbenchUnavailable, "unreachable"),
        (httpx.ReadTimeout("slow"), WorkbenchUnavailable, "timeout"),
    ],
    ids=[
        "not_found",
        "too_large",
        "empty",
        "not_its_role",
        "unsupported",
        "validation_list",
        "bad_session",
        "no_purpose",
        "busy",
        "storage_full",
        "unreachable",
        "timeout",
    ],
)
async def test_derived_maps_refusals(answer: object, error: type[Exception], token: str | None) -> None:
    """404 is not found, 413 and any status without a token http:<status>, a 422's token the text before its colon.

    A busy workbench (503, the clip's lock held past 120 s) is http:503, as every other route maps it; the media step
    asks once more.
    """
    door = FakeDoor()
    door.files[CLIP] = (CLIP_BYTES, "video/mp4")
    door.answers["/files/derived"] = answer
    with pytest.raises(error) as raised:
        await _client(door).derived(call=CALL, source=CLIP, role="nextext_jsonl", chunks=_chunks(TRANSCRIPT))
    assert type(raised.value) is error
    assert token is None or getattr(raised.value, "token", None) == token


@pytest.mark.parametrize(
    "answer",
    [
        {"role": "nextext_jsonl", "bytes": 4, "replaced": None},
        {"id": "xyz", "role": "nextext_jsonl", "bytes": 4, "replaced": None},
        {"id": "A" * 32, "role": "nextext_jsonl", "bytes": 4, "replaced": None},
        {"id": 7, "role": "nextext_jsonl", "bytes": 4, "replaced": None},
        ["id", DERIVED_ID],
        httpx.Response(200, text="stored"),
    ],
    ids=["no_id", "id_not_32_hex", "id_upper_case", "id_not_text", "not_an_object", "not_json"],
)
async def test_a_derived_answer_without_an_id_is_refused(answer: object) -> None:
    """Only an object with a 32-hex ``id`` is an answer; anything else is unavailable, never not found."""
    door = FakeDoor()
    door.files[CLIP] = (CLIP_BYTES, "video/mp4")
    door.answers["/files/derived"] = answer
    with pytest.raises(WorkbenchUnavailable) as raised:
        await _client(door).derived(call=CALL, source=CLIP, role="nextext_jsonl", chunks=_chunks(TRANSCRIPT))
    assert not isinstance(raised.value, WorkbenchNotFound)


async def test_derived_never_asks_outside_the_chat() -> None:
    """A source outside the chat's folder is not found, and nothing is sent."""
    door = FakeDoor()
    for source in ("/workspace/other/x.mp4", f"{FOLDER}/../x.mp4", FOLDER):
        with pytest.raises(WorkbenchNotFound):
            await _client(door).derived(call=CALL, source=source, role="nextext_zip", chunks=_chunks(b"PK"))
    assert door.requests == []


async def test_derived_closes_the_callers_stream() -> None:
    """The artifact's iterator is closed when the call ends: after an answer, a refusal and a source left unsent."""
    closed: list[str] = []

    class Stream:
        """One chunk, then the end; says when it is closed, whether it was read or not."""

        def __init__(self, label: str) -> None:
            self.label, self.sent = label, False

        def __aiter__(self) -> "Stream":
            return self

        async def __anext__(self) -> bytes:
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return TRANSCRIPT

        async def aclose(self) -> None:
            closed.append(self.label)

    def stream(label: str) -> AsyncIterator[bytes]:
        return Stream(label)

    door = FakeDoor()
    door.files[CLIP] = (CLIP_BYTES, "video/mp4")
    client = _client(door)
    await client.derived(call=CALL, source=CLIP, role="nextext_jsonl", chunks=stream("answered"))
    door.answers["/files/derived"] = httpx.Response(503, json={"detail": "busy, retry later"})
    with pytest.raises(WorkbenchUnavailable):
        await client.derived(call=CALL, source=CLIP, role="nextext_jsonl", chunks=stream("refused"))
    with pytest.raises(WorkbenchNotFound):
        await client.derived(call=CALL, source="/workspace/other/x.mp4", role="nextext_jsonl", chunks=stream("unsent"))
    assert closed == ["answered", "refused", "unsent"]


class ArtifactBroke(Exception):
    """An invented failure of the stream an artifact comes in, as Nextext's own types are for the media step."""


@pytest.mark.parametrize("exception", [ArtifactBroke, ValueError], ids=["artifact_broke", "value_error"])
async def test_derived_hands_on_an_artifact_stream_that_fails_mid_body(
    exception: type[Exception],
) -> None:
    """A stream that breaks while it is sent reaches the caller as itself; the clip gets no derived file.

    workbench drops a body that ends early; the client reports no success and wraps nothing, so the media step
    sees Nextext's own failure (an artifact past its cap, Nextext gone) and its own token.
    """
    closed: list[bool] = []

    async def chunks() -> AsyncIterator[bytes]:
        try:
            yield TRANSCRIPT
            raise exception("the artifact broke off")
        finally:
            closed.append(True)

    door = FakeDoor()
    door.files[CLIP] = (CLIP_BYTES, "video/mp4")
    with pytest.raises(exception) as raised:
        await _client(door).derived(call=CALL, source=CLIP, role="nextext_jsonl", chunks=chunks())
    assert type(raised.value) is exception and not isinstance(raised.value, WorkbenchUnavailable)
    assert closed == [True] and door.derived == []


async def test_a_media_manifest_is_read() -> None:
    """A clip is kind media and counts frames; the transcript and the duration are read and kept in the stored form."""
    door = FakeDoor()
    door.files[CLIP] = (CLIP_BYTES, "video/mp4")
    client = _client(door)
    bare = await client.prepare(call=CALL, path=CLIP)
    assert bare == Manifest("media", 0, (), (), None, "frame", None, False)
    await client.derived(call=CALL, source=CLIP, role="nextext_jsonl", chunks=_chunks(TRANSCRIPT))
    await client.derived(call=CALL, source=CLIP, role="nextext_zip", chunks=_chunks(b"PK\x03\x04"))
    got = await client.prepare(call=CALL, path=CLIP)
    assert (got.kind, got.unit, got.total, got.frames) == ("media", "frame", 3, None)
    assert (got.duration_s, got.transcript, got.pages_without_text) == (91.0, True, ())
    assert got.renditions == tuple(Rendition(n, 1280, 720, "image/jpeg") for n in (1, 2, 3))
    stored = got.as_stored()
    assert tuple(stored) == STORED_MANIFEST_KEYS
    assert (stored["kind"], stored["unit"], stored["duration_s"], stored["transcript"]) == (
        "media",
        "frame",
        91.0,
        True,
    )
    assert json.loads(json.dumps(stored)) == stored


async def test_a_clip_without_speech_keeps_the_duration_its_frames_give() -> None:
    """The frames alone are a manifest: no transcript, and the last frame's time as the duration, a float.

    As workbench writes it, a duration is ``null`` only for a clip with neither speech nor frames; a whole number
    reads as a float.
    """
    door = FakeDoor()
    door.files[CLIP] = (CLIP_BYTES, "video/mp4")
    client = _client(door)
    await client.derived(call=CALL, source=CLIP, role="nextext_zip", chunks=_chunks(b"PK\x03\x04"))
    silent = await client.prepare(call=CALL, path=CLIP)
    assert (silent.total, silent.transcript, silent.duration_s) == (3, False, 8.0)
    door.manifests[CLIP] = {**stock_manifest(CLIP), "duration_s": 40}
    whole = await client.prepare(call=CALL, path=CLIP)
    assert (whole.total, whole.duration_s) == (0, 40.0) and isinstance(whole.duration_s, float)
    door.manifests[CLIP] = {**stock_manifest(CLIP), "duration_s": None}
    assert (await client.prepare(call=CALL, path=CLIP)).duration_s is None


@pytest.mark.parametrize(
    "change",
    [
        {"duration_s": "x"},
        {"duration_s": "91.0"},
        {"duration_s": -1},
        {"duration_s": -0.5},
        {"duration_s": True},
        {"duration_s": [91.0]},
        {"transcript": 1},
        {"transcript": "yes"},
        {"transcript": None},
        {"unit": "page"},
        {"unit": "slide"},
        {"unit": None},
    ],
    ids=[
        "duration_text",
        "duration_numeral",
        "duration_negative_int",
        "duration_negative_float",
        "duration_bool",
        "duration_list",
        "transcript_int",
        "transcript_text",
        "transcript_null",
        "media_unit_page",
        "media_unit_slide",
        "media_unit_null",
    ],
)
async def test_a_bad_media_manifest_is_bad_answer(change: dict[str, Any]) -> None:
    """A clip's manifest whose duration, transcript or unit is not what the contract says is refused whole."""
    door = FakeDoor()
    door.answers["/files/prepare"] = {**stock_manifest(CLIP), **change}
    with pytest.raises(WorkbenchUnavailable) as raised:
        await _client(door).prepare(call=CALL, path=CLIP)
    assert raised.value.token == "bad_answer" and not isinstance(raised.value, WorkbenchNotFound)


async def test_a_media_manifest_without_its_unit_is_a_bad_answer() -> None:
    """Workbench 0.9 names the unit of a clip; an answer that leaves it out would read as pages, so it is refused."""
    door = FakeDoor()
    door.answers["/files/prepare"] = {key: value for key, value in stock_manifest(CLIP).items() if key != "unit"}
    with pytest.raises(WorkbenchUnavailable) as raised:
        await _client(door).prepare(call=CALL, path=CLIP)
    assert raised.value.token == "bad_answer"


@pytest.mark.parametrize(
    "duration",
    [float("inf"), float("-inf"), float("nan"), 10**400, -(10**400)],
    ids=["inf", "minus_inf", "nan", "too_large_for_a_float", "too_small_for_a_float"],
)
async def test_a_duration_that_is_not_a_finite_number_is_a_bad_answer(duration: float) -> None:
    """Python's JSON reads ``Infinity``, ``NaN`` and integers of any size; none of them is a duration.

    An integer too large for a float must end as ``bad_answer`` as any other bad value does, never as an
    ``OverflowError``.
    """
    door = FakeDoor()
    text = json.dumps({**stock_manifest(CLIP), "duration_s": duration})
    door.answers["/files/prepare"] = httpx.Response(
        200, content=text.encode(), headers={"Content-Type": "application/json"}
    )
    with pytest.raises(WorkbenchUnavailable) as raised:
        await _client(door).prepare(call=CALL, path=CLIP)
    assert raised.value.token == "bad_answer"


async def test_an_older_manifest_reads_with_the_defaults() -> None:
    """Workbench 0.8's eight keys (no duration, no transcript) read as no duration and no transcript."""
    door = FakeDoor()
    door.files[PDF] = (b"%PDF", "application/pdf")
    door.manifests[PDF] = {
        key: value for key, value in stock_manifest(PDF).items() if key not in ("duration_s", "transcript")
    }
    got = await _client(door).prepare(call=CALL, path=PDF)
    assert (got.kind, got.total, got.duration_s, got.transcript) == ("pdf", 3, None, False)
    stored = got.as_stored()
    assert (stored["duration_s"], stored["transcript"]) == (None, False)
    assert tuple(stored) == STORED_MANIFEST_KEYS


async def test_a_rendition_view_carries_its_parameters() -> None:
    """With rendition, the view asks for it, and for strips only past 1; without it, only the path goes."""
    door = FakeDoor()
    door.files[PDF] = (b"%PDF", "application/pdf")
    client = _client(door)

    async def view(**parameters: Any) -> tuple[bytes, str]:
        async with client.view(call=CALL, path=PDF, **parameters) as response:
            return b"".join([chunk async for chunk in response.aiter_bytes()]), response.headers["content-type"]

    assert await view(rendition=2, strips=2, strip=1) == (TINY_JPEG + b"\x01", "image/jpeg")
    assert await view(rendition=3) == (TINY_JPEG, "image/jpeg")
    await view(rendition=1, strips=1)
    async with client.view(call=CALL, path=PDF, strips=2, strip=1) as response:
        assert response.status_code == 200
    assert [dict(r.url.params) for r, _ in door.requests] == [
        {"path": PDF, "rendition": "2", "strips": "2", "strip": "1"},
        {"path": PDF, "rendition": "3"},
        {"path": PDF, "rendition": "1"},
        {"path": PDF},
    ]


async def test_a_rendition_view_maps_a_page_that_is_not_there_and_needs_its_strip() -> None:
    """A page past the total is not found; strips past 1 without strip is refused unsent."""
    door = FakeDoor()
    door.files[PDF] = (b"%PDF", "application/pdf")
    client = _client(door)
    with pytest.raises(WorkbenchNotFound):
        async with client.view(call=CALL, path=PDF, rendition=4):
            pass
    sent = len(door.requests)
    with pytest.raises(ValueError):
        async with client.view(call=CALL, path=PDF, rendition=1, strips=2):
            pass
    assert len(door.requests) == sent


def _fetching_transport(door: FakeDoor, odd: dict[str, httpx.Response]) -> httpx.MockTransport:
    """Wrap the door so the paths in ``odd`` are answered as scripted, the others by the door."""

    async def handler(request: httpx.Request) -> httpx.Response:
        scripted = odd.get(request.url.params.get("path", ""))
        return scripted if scripted is not None else await door(request)

    return httpx.MockTransport(handler)


def test_renditions_are_fetched_together_and_checked() -> None:
    """Each answer counts only if it is a whole picture of its type; the others are None. The requests overlap."""
    door = FakeDoor()
    door.files[PHOTO] = (b"\xff\xd8\xff", "image/jpeg")
    wrong = f"{FOLDER}/wrong-type.jpg"
    door.files[wrong] = (b"x", "image/jpeg")
    odd = {wrong: httpx.Response(200, content=TINY_PNG, headers={"Content-Type": "image/jpeg"})}
    client = WorkbenchClient(CFG, transport=_fetching_transport(door, odd))
    wanted = [RenditionRequest(PHOTO), RenditionRequest(PHOTO, n=2), RenditionRequest(wrong)]
    assert client.fetch_renditions(wanted, call=CALL) == [("image/jpeg", TINY_JPEG), None, None]

    door.delays["/files/view"] = 0.3
    started = time.monotonic()
    got = _renditions(_client(door), *[RenditionRequest(PHOTO)] * 3)
    assert time.monotonic() - started < 0.8
    assert got == [("image/jpeg", TINY_JPEG)] * 3


def test_a_rendition_asks_for_its_strip() -> None:
    """The strip parameters go out when strips is past 1, and the strip comes back."""
    door = FakeDoor()
    door.files[PHOTO] = (b"\xff\xd8\xff", "image/jpeg")
    got = _renditions(_client(door), RenditionRequest(PHOTO, 1, strips=2, strip=2), RenditionRequest(PHOTO))
    assert got == [("image/jpeg", TINY_JPEG + b"\x02"), ("image/jpeg", TINY_JPEG)]
    assert [dict(r.url.params) for r, _ in door.requests] == [
        {"path": PHOTO, "rendition": "1", "strips": "2", "strip": "2"},
        {"path": PHOTO, "rendition": "1"},
    ]


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, content=TINY_JPEG, headers={"Content-Type": "text/html"}),
        httpx.Response(200, content=TINY_JPEG, headers={"Content-Type": "image/gif"}),
        httpx.Response(200, content=TINY_PNG, headers={"Content-Type": "image/jpeg"}),
        httpx.Response(200, content=TINY_JPEG, headers={"Content-Type": "image/png"}),
        httpx.Response(200, content=b"", headers={"Content-Type": "image/png"}),
        httpx.Response(200, content=TINY_JPEG + bytes(MAX_RENDITION_BYTES), headers={"Content-Type": "image/jpeg"}),
        httpx.Response(
            200,
            content=TINY_JPEG + bytes(MAX_RENDITION_BYTES - len(TINY_JPEG)),
            headers={"Content-Type": "image/jpeg"},
        ),
        httpx.Response(404, json=NOT_FOUND),
        httpx.Response(503, json={"detail": "busy, retry later"}),
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("slow"),
    ],
    ids=[
        "html",
        "gif",
        "png_as_jpeg",
        "jpeg_as_png",
        "empty",
        "past_768_kib",
        "at_768_kib",
        "gone",
        "busy",
        "unreachable",
        "timeout",
    ],
)
def test_a_rendition_that_is_not_a_whole_picture_is_none(answer: object) -> None:
    """Not 200, not a jpeg or png, 768 KiB or more, or without the signature of its type: None."""
    door = FakeDoor()
    door.files[PHOTO] = (b"\xff\xd8\xff", "image/jpeg")
    door.answers["/files/view"] = answer
    assert _renditions(_client(door), RenditionRequest(PHOTO)) == [None]


def test_a_png_rendition_under_768_kib_is_kept() -> None:
    """A png under its own type is accepted, a byte under the cap."""
    data = TINY_PNG + bytes(MAX_RENDITION_BYTES - 1 - len(TINY_PNG))
    door = FakeDoor()
    door.files[PHOTO] = (b"x", "image/jpeg")
    door.answers["/files/view"] = httpx.Response(
        200, content=data, headers={"Content-Type": "image/png; charset=binary"}
    )
    assert _renditions(_client(door), RenditionRequest(PHOTO)) == [("image/png", data)]


def test_a_rendition_outside_the_chat_is_none_and_unsent() -> None:
    """A path outside the chat's folder is not asked for; without a key, or with nothing wanted, nothing is sent."""
    door = FakeDoor()
    door.files[PHOTO] = (b"x", "image/jpeg")
    wanted = [RenditionRequest("/workspace/other/x.jpg"), RenditionRequest(f"{FOLDER}/../x.jpg")]
    assert _renditions(_client(door), *wanted) == [None, None]
    assert _renditions(_client(door)) == []
    keyless = _client(door, settings=make_settings(Path("unused"), workbench_api_key=None))
    assert _renditions(keyless, RenditionRequest(PHOTO)) == [None]
    assert door.requests == []


@pytest.mark.parametrize("fault", ["transport", "connect", "timeout"])
def test_fetch_renditions_never_raises(fault: str) -> None:
    """A transport that raises, whatever it raises, gives None for every rendition."""
    error: Exception = {
        "transport": RuntimeError("boom"),
        "connect": httpx.ConnectError("refused"),
        "timeout": httpx.ReadTimeout("slow"),
    }[fault]

    def boom(request: httpx.Request) -> httpx.Response:
        raise error

    client = WorkbenchClient(CFG, transport=httpx.MockTransport(boom))
    assert _renditions(client, RenditionRequest(PHOTO), RenditionRequest(PHOTO, n=2)) == [None, None]


def test_fetch_renditions_never_raises_when_the_exchange_itself_fails(
    monkeypatch: pytest.MonkeyPatch, loguru_caplog: pytest.LogCaptureFixture
) -> None:
    """A failure outside any one request, here the exchange itself, gives None for every rendition and one warning."""

    async def broken(*args: object) -> None:
        raise RuntimeError("exchange broke")

    monkeypatch.setattr(WorkbenchClient, "_fetch_renditions", broken)
    door = FakeDoor()
    door.files[PHOTO] = (b"x", "image/jpeg")
    assert _renditions(_client(door), RenditionRequest(PHOTO), RenditionRequest(PHOTO, n=2)) == [None, None]
    assert "workbench renditions raised RuntimeError" in loguru_caplog.text


def test_a_malformed_rendition_request_fails_alone(loguru_caplog: pytest.LogCaptureFixture) -> None:
    """A request whose strips name no strip is None, unsent; the others of the fetch, still in flight, come."""
    door = FakeDoor()
    door.files[PHOTO] = (b"x", "image/jpeg")
    door.delays["/files/view"] = 0.05
    malformed = RenditionRequest(PHOTO, 1, strips=2, strip=cast(int, None))
    assert _renditions(_client(door), malformed, RenditionRequest(PHOTO)) == [None, ("image/jpeg", TINY_JPEG)]
    assert [dict(r.url.params) for r, _ in door.requests] == [{"path": PHOTO, "rendition": "1"}]
    assert "renditions raised" not in loguru_caplog.text


def test_a_rendition_fetch_is_given_up_at_its_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past RENDITION_TIMEOUT_S the pictures that have not come are None, and the caller is not held."""
    assert RENDITION_TIMEOUT_S == 30.0
    monkeypatch.setattr("ghostwriter.workbench.RENDITION_TIMEOUT_S", 0.3)
    door = FakeDoor()
    door.files[PHOTO] = (b"x", "image/jpeg")
    door.delays["/files/view"] = 3.0
    started = time.monotonic()
    assert _renditions(_client(door), RenditionRequest(PHOTO)) == [None]
    assert time.monotonic() - started < 2.0


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


async def test_taking_back_an_upload_deletes_it_in_its_chat() -> None:
    """DELETE /files/upload names the upload by its path; a file workbench does not hold is not found."""
    door = FakeDoor()
    door.files[f"{FOLDER}/sales.xlsx"] = (b"PK", XLSX)
    client = _client(door)
    await client.delete_upload(call=CALL, path=f"{FOLDER}/sales.xlsx")
    request = door.last("/files/upload", "DELETE")
    assert request.url.params["path"] == f"{FOLDER}/sales.xlsx" and door.files == {}
    with pytest.raises(WorkbenchNotFound):
        await client.delete_upload(call=CALL, path=f"{FOLDER}/sales.xlsx")


async def test_an_upload_outside_the_chats_folder_is_not_asked_for() -> None:
    """A path that is not below the chat's folder never reaches workbench."""
    door = FakeDoor()
    client = _client(door)
    for path in ("/workspace/elsewhere/sales.xlsx", f"{FOLDER}/../sales.xlsx", f"{FOLDER}/sales\x01.xlsx"):
        with pytest.raises(WorkbenchNotFound):
            await client.delete_upload(call=CALL, path=path)
    assert door.requests == []


@pytest.mark.parametrize(
    ("answer", "token"),
    [
        (httpx.Response(405, json={"detail": "Method Not Allowed"}), "http:405"),
        (httpx.Response(503, json={"detail": "busy, retry later"}), "http:503"),
        ({"deleted": 0}, "bad_answer"),
        (httpx.TimeoutException("synthetic timeout"), "timeout"),
    ],
    ids=["a workbench before 0.8.1", "busy", "a bad answer", "a timeout"],
)
async def test_an_upload_workbench_could_not_take_back_is_unavailable(answer: object, token: str) -> None:
    """Anything but a deleted upload or a 404 is unavailable, a workbench without the route among them."""
    door = FakeDoor()
    door.answers[TAKE_BACK] = answer
    client = _client(door)
    with pytest.raises(WorkbenchUnavailable) as caught:
        await client.delete_upload(call=CALL, path=f"{FOLDER}/sales.xlsx")
    assert caught.value.token == token and not isinstance(caught.value, WorkbenchNotFound)


async def test_without_a_key_nothing_is_sent() -> None:
    """With workbench off, discovery offers nothing, a call fails and the file routes are unavailable."""
    door = FakeDoor()
    client = _client(door, settings=make_settings(Path("unused"), workbench_api_key=None))
    assert client.discover(call=CALL, images=True) is None
    result = _call(client)
    assert isinstance(result, ToolFailure) and result.token == "not_configured"
    with pytest.raises(WorkbenchUnavailable):
        await _upload(client)
    with pytest.raises(WorkbenchUnavailable):
        async with client.view(call=CALL, path=f"{FOLDER}/sales.xlsx"):
            pass
    with pytest.raises(WorkbenchUnavailable):
        await client.delete_chat(call=CALL)
    with pytest.raises(WorkbenchUnavailable):
        await client.delete_upload(call=CALL, path=f"{FOLDER}/sales.xlsx")
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
    door.files[PHOTO] = (b"\xff\xd8\xff", "image/jpeg")
    client = _client(door)
    assert client.discover(call=CALL, images=True) is not None
    assert isinstance(_call(client), ToolResult)

    async def files() -> None:
        await _upload(client)
        async with client.view(call=CALL, path=f"{FOLDER}/sales.xlsx"):
            pass
        await client.prepare(call=CALL, path=PHOTO)
        assert _renditions(client, RenditionRequest(PHOTO)) == [("image/jpeg", TINY_JPEG)]
        await client.delete_chat(call=CALL)

    anyio.run(files)
    assert len(_SpyClient.built) == 7
    assert all(built.trust_env is False for built in _SpyClient.built)
    rest = httpx.Timeout(CFG.workbench_timeout_s, connect=3.0, write=10.0, pool=3.0)
    assert [built.timeout for built in _SpyClient.built] == [
        httpx.Timeout(5.0, connect=3.0, write=10.0, pool=3.0),
        httpx.Timeout(40.0, connect=3.0, write=10.0, pool=3.0),
        rest,
        rest,
        httpx.Timeout(PREPARE_TIMEOUT_S, connect=3.0, write=10.0, pool=3.0),
        httpx.Timeout(RENDITION_TIMEOUT_S, connect=3.0, write=10.0, pool=3.0),
        rest,
    ]


def test_the_log_holds_no_names_or_content(log_lines: list[str]) -> None:
    """No file name, argument, output text, user or chat reaches a log line, on success or failure."""
    secret = "Hafenfest"
    door = FakeDoor()
    door.answers["server/discover"] = httpx.Response(503, text=secret)
    client = _client(door)
    client.discover(call=CALL, images=True)
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


def test_the_log_holds_no_image_bytes(log_lines: list[str]) -> None:
    """No picture's base64, label or file name reaches a log line, whether the pictures are kept, dropped or fetched."""
    label = "Hafenplan Seite 1"
    encoded = base64.b64encode(TINY_JPEG).decode("ascii")
    door = FakeDoor()
    door.files[PHOTO] = (b"\xff\xd8\xff", "image/jpeg")
    client = _client(door)
    answers = [
        view_result(FOLDER, [_shown(1, label=label)], [image_block(TINY_JPEG)]),
        view_result(FOLDER, [_shown(1, label=label)], [image_block(TINY_PNG, "image/jpeg")]),
        view_result(FOLDER, [], [image_block(TINY_JPEG)]),
    ]
    kept: list[ToolResult] = []
    for answer in answers:
        door.answers["tools/call"] = answer
        result = client.call_tool("view_file", {"path": PHOTO}, call=CALL, timeout_s=5.0, images=True)
        if isinstance(result, ToolResult):
            kept.append(result)

    async def files() -> None:
        await client.prepare(call=CALL, path=PHOTO)
        door.answers["/files/prepare"] = httpx.Response(422, json={"detail": f"unreadable_file: {label}"})
        with pytest.raises(WorkbenchUnavailable):
            await client.prepare(call=CALL, path=PHOTO)
        door.answers["/files/prepare"] = httpx.Response(200, json={"kind": label})
        with pytest.raises(WorkbenchUnavailable):
            await client.prepare(call=CALL, path=PHOTO)

    anyio.run(files)
    assert _renditions(client, RenditionRequest(PHOTO), RenditionRequest(PHOTO, 2)) == [("image/jpeg", TINY_JPEG), None]
    door.answers["/files/view"] = httpx.Response(200, content=TINY_JPEG, headers={"Content-Type": "image/png"})
    assert _renditions(client, RenditionRequest(PHOTO)) == [None]
    assert [len(result.images) for result in kept] == [1, 0, 0]
    assert log_lines
    forbidden = (encoded, label, label.lower(), "harbour-photo", "user-a", CHAT, KEY)
    assert not [line for line in log_lines if any(value in line for value in forbidden)]
    reprs = [repr(image) for result in kept for image in result.images]
    assert reprs and not [text for text in reprs if encoded in text or repr(TINY_JPEG) in text]


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
