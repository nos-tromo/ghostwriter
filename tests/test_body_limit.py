"""The backend's own cap on request bodies, per route as nginx's ``client_max_body_size``.

Every name, id and value here is invented.
"""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from ghostwriter.body_limit import BodyLimit
from ghostwriter.inference import ModelClient
from ghostwriter.main import create_app
from tests.conftest import CapturedAuditStream, as_user, events, make_settings
from tests.fakes import FakeDoor, FakeOpenAI

MIB = 1024 * 1024
TOO_LARGE = (413, {"detail": "Request too large."})


@pytest.fixture
def fake_openai() -> FakeOpenAI:
    """Return the router, with no round scripted."""
    return FakeOpenAI()


@pytest.fixture
def client(tmp_path: Path, audit_stream: CapturedAuditStream, fake_openai: FakeOpenAI) -> Iterator[TestClient]:
    """Serve the app with workbench behind a fake door and the router behind ``fake_openai``."""
    settings = make_settings(tmp_path)
    model = ModelClient(settings, client=fake_openai.as_client())
    app = create_app(
        settings, audit_stream=audit_stream, workbench_transport=httpx.MockTransport(FakeDoor()), model=model
    )
    with TestClient(app) as test_client:
        yield test_client


def _new(client: TestClient) -> str:
    response = client.post("/api/chats", headers=as_user())
    assert response.status_code == 201
    return str(response.json()["id"])


def _messages(client: TestClient, cid: str) -> list[object]:
    app = client.app
    assert isinstance(app, FastAPI)
    return list(app.state.store.chat_detail("user-a", cid).messages)


def test_a_message_over_1_mib_is_refused_before_the_handler_runs(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """A declared 2 MiB body is 413 before anything reads it: before identity, record, store or model."""
    cid = _new(client)
    before = len(events(audit_stream))
    body = json.dumps({"content": "x" * (2 * MIB)})
    anonymous: dict[str, str] = {}
    for headers in (as_user(), anonymous):
        r = client.post(
            f"/api/chats/{cid}/messages", content=body, headers={**headers, "Content-Type": "application/json"}
        )
        assert (r.status_code, r.json()) == TOO_LARGE
    assert events(audit_stream)[before:] == [] and _messages(client, cid) == [] and fake_openai.calls == []


def test_a_chunked_body_past_the_cap_is_refused(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """A body without Content-Length is counted as it arrives and refused past 1 MiB."""
    cid = _new(client)
    before = len(events(audit_stream))
    pieces = [b'{"content": "', b"x" * MIB, b'"}']

    def body() -> Iterator[bytes]:
        yield from pieces

    r = client.post(
        f"/api/chats/{cid}/messages", content=body(), headers={**as_user(), "Content-Type": "application/json"}
    )
    assert r.request.headers.get("content-length") is None
    assert (r.status_code, r.json()) == TOO_LARGE
    assert events(audit_stream)[before:] == [] and _messages(client, cid) == [] and fake_openai.calls == []


def test_an_attachment_within_its_own_limit_passes(client: TestClient) -> None:
    """A 2 MiB upload is over the general cap but within the attachment route's, so it is attached."""
    cid = _new(client)
    files = {"file": ("sales.xlsx", b"\x00" * (2 * MIB), "application/octet-stream")}
    r = client.post(f"/api/chats/{cid}/attachments", files=files, headers=as_user())
    assert (r.status_code, r.json()["size"]) == (201, 2 * MIB)


class Drive:
    """What happened when a request went through ``BodyLimit`` in front of an app that reads its whole body.

    Attributes:
        app_calls: How often the wrapped app was called.
        receives: How often a body chunk was asked for.
        sent: The messages sent to the client.
        error: The error that came out of the middleware, if any.
    """

    def __init__(self) -> None:
        """Start with nothing having happened."""
        self.app_calls = 0
        self.receives = 0
        self.sent: list[Message] = []
        self.error: HTTPException | None = None

    @property
    def status(self) -> int | None:
        """The status the client was sent, if a response was started."""
        starts = [m["status"] for m in self.sent if m["type"] == "http.response.start"]
        return starts[0] if starts else None

    @property
    def answer(self) -> dict[str, str] | None:
        """The JSON answer of a 413, if one was sent."""
        bodies = [m["body"] for m in self.sent if m["type"] == "http.response.body"]
        return json.loads(bodies[0]) if self.status == 413 and bodies else None


def drive(path: str, *, declared: int | None, chunks: list[bytes], method: str = "POST") -> Drive:
    """Send one request through ``BodyLimit``."""
    result = Drive()
    pending = list(chunks)

    async def inner(_scope: Scope, receive: Receive, send: Send) -> None:
        result.app_calls += 1
        more = True
        while more:
            more = (await receive()).get("more_body", False)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive() -> Message:
        result.receives += 1
        body = pending.pop(0) if pending else b""
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def send(message: Message) -> None:
        result.sent.append(message)

    headers = [] if declared is None else [(b"content-length", str(declared).encode())]
    scope: Scope = {"type": "http", "method": method, "path": path, "headers": headers}

    async def run() -> None:
        try:
            await BodyLimit(inner)(scope, receive, send)
        except HTTPException as exc:
            result.error = exc

    asyncio.run(run())
    return result


@pytest.mark.parametrize(
    ("path", "cap", "answer"),
    [
        ("/api/chats/c-1/messages", MIB, {"detail": "Request too large."}),
        ("/api/chats", MIB, {"detail": "Request too large."}),
        ("/api/chats/c-1/attachments", 51 * MIB, {"detail": "The file is larger than 50 MB.", "code": "too_large"}),
        ("/api/transcribe", 11 * MIB, {"detail": "Request too large."}),
    ],
)
def test_each_route_class_has_the_cap_nginx_gives_it(path: str, cap: int, answer: dict[str, str]) -> None:
    """At its cap a body passes; one byte more is refused unread; a streamed body stops at the chunk past it."""
    r = drive(path, declared=cap, chunks=[b"x" * cap])
    assert (r.status, r.app_calls, r.error) == (200, 1, None)
    r = drive(path, declared=cap + 1, chunks=[b"x" * (cap + 1)])
    assert (r.status, r.answer, r.app_calls, r.receives) == (413, answer, 0, 0)
    half = cap // 2
    r = drive(path, declared=None, chunks=[b"x" * half, b"x" * half, b"x", b"x" * half])
    assert r.error is not None and (r.error.status_code, r.error.detail) == (413, answer["detail"])
    assert (r.receives, r.status) == (3, None)


def test_a_scope_that_is_not_http_is_left_alone() -> None:
    """Lifespan and websocket scopes go straight to the app."""
    seen: list[str] = []

    async def inner(scope: Scope, _receive: Receive, _send: Send) -> None:
        seen.append(scope["type"])

    async def never_received() -> Message:
        raise AssertionError("not called")

    async def never_sent(_message: Message) -> None:
        raise AssertionError("not called")

    asyncio.run(BodyLimit(inner)({"type": "lifespan"}, never_received, never_sent))
    assert seen == ["lifespan"]
