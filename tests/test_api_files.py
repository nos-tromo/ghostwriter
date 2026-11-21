"""Attachments stream into the chat's folder in workbench; downloads stream back, for the chat's owner only.

Every name, id and value here is invented.
"""

import logging
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import anyio
import httpx
import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ghostwriter import processing
from ghostwriter.api.files import FILE_HEADERS, _read_after_wait
from ghostwriter.config import Settings
from ghostwriter.db import init_db, make_engine, utcnow
from ghostwriter.main import create_app
from ghostwriter.processing import AttachmentProcessor
from ghostwriter.store import ChatStore
from ghostwriter.workbench import chat_folder, session_pseudonym
from tests.conftest import KEY, CapturedAuditStream, as_user, events, make_settings, unrecorded_child
from tests.fakes import TINY_JPEG, UPLOAD_ID, XLSX, FakeDoor, stock_manifest

MIB = 1024 * 1024
TOO_LARGE = {"detail": "The file is larger than 50 MB.", "code": "too_large"}
BOUNDARY = "gwtest"
TOO_MANY_PENDING = {"detail": "Send the attached files before adding more.", "code": "too_many_pending"}
NOT_FOUND = (404, {"detail": "File not found."})
GONE = (410, {"detail": "The file has expired."})
FID = UPLOAD_ID
NO_ATTACHMENT = (404, {"detail": "Attachment not found."})
PHOTO = "harbour-photo.jpg"
SCAN = "quay-scan.pdf"
UNREADABLE = {"detail": "unreadable_file: damaged"}
SYSTEM = {"user": None, "via": "system"}


class Clock:
    """The store's clock, set by hand.

    Attributes:
        now: The time it reads.
    """

    def __init__(self) -> None:
        """Start at the real time."""
        self.now = utcnow()

    def __call__(self) -> datetime:
        """Read the clock.

        Returns:
            The time set.
        """
        return self.now


@pytest.fixture
def door() -> FakeDoor:
    """Return workbench's doors, answering as a healthy workbench does."""
    return FakeDoor()


@pytest.fixture
def clock() -> Clock:
    """Return the store's clock."""
    return Clock()


@pytest.fixture
def client(tmp_path: Path, audit_stream: CapturedAuditStream, door: FakeDoor, clock: Clock) -> Iterator[TestClient]:
    """Serve the app with workbench behind ``door`` and the store on ``clock``."""
    app = create_app(
        make_settings(tmp_path), audit_stream=audit_stream, workbench_transport=httpx.MockTransport(door), now=clock
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeing(tmp_path: Path, audit_stream: CapturedAuditStream, door: FakeDoor, clock: Clock) -> Iterator[TestClient]:
    """Serve the app with pictures offered: vision on, and a workbench that lists ``view_file``."""
    app = create_app(
        make_settings(tmp_path, vision=True),
        audit_stream=audit_stream,
        workbench_transport=httpx.MockTransport(door),
        now=clock,
    )
    with TestClient(app) as test_client:
        yield test_client


def _state(client: TestClient) -> Any:
    app = client.app
    assert isinstance(app, FastAPI)
    return app.state


def _new(client: TestClient, user: str = "user-a") -> str:
    response = client.post("/api/chats", headers=as_user(user))
    assert response.status_code == 201
    return str(response.json()["id"])


def _attach(
    client: TestClient,
    cid: str,
    name: str = "sales.xlsx",
    data: bytes = b"SENTINEL-BYTES",
    headers: dict[str, str] | None = None,
) -> httpx2.Response:
    files = {"file": (name, data, "application/octet-stream")}
    return client.post(f"/api/chats/{cid}/attachments", files=files, headers=headers or as_user())


def _since(stream: CapturedAuditStream, before: int) -> list[dict[str, Any]]:
    return events(stream)[before:]


def _status(client: TestClient, cid: str, aid: str, wait: int = 0, user: str = "user-a") -> httpx2.Response:
    return client.get(f"/api/chats/{cid}/attachments/{aid}", params={"wait": wait}, headers=as_user(user))


def _settled(client: TestClient, cid: str, aid: str) -> str:
    """Follow an attachment by long-poll until it is no longer processing, and return its status."""
    r = _status(client, cid, aid, wait=20)
    assert r.status_code == 200, r.text
    status: str = r.json()["status"]
    return status


def _preview(client: TestClient, cid: str, aid: str, page: int = 1, user: str = "user-a") -> httpx2.Response:
    return client.get(f"/api/chats/{cid}/attachments/{aid}/preview", params={"page": page}, headers=as_user(user))


def _prepares(door: FakeDoor) -> list[httpx.Request]:
    return [request for request, _ in door.requests if request.url.path == "/files/prepare"]


def _views(door: FakeDoor) -> list[httpx.Request]:
    return [request for request, _ in door.requests if request.url.path == "/files/view"]


def _unreadable(folder: str) -> httpx.Response:
    """Answer a prepare as workbench does a damaged file: a fresh 422 each time."""
    return httpx.Response(422, json=UNREADABLE)


def _wait_until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "the condition never held"
        time.sleep(0.01)


def _stranded(settings: Settings, *names: str) -> tuple[str, list[str]]:
    """Write a chat whose attachments were still being prepared when the last process ended.

    Args:
        settings: The settings the app will start with, for its database.
        names: The files, each ``processing``; a picture by its suffix, any other a document.

    Returns:
        The chat's id and the attachments' ids.
    """
    engine = make_engine(settings.db_path)
    init_db(engine)
    try:
        store = ChatStore(engine, file_days=settings.file_days, notice_days=settings.notice_days)
        cid = store.create_chat("user-a").id
        ids = [
            store.add_attachment(
                cid,
                file_id=f"{n + 7:032x}",
                path=f"{chat_folder(KEY, cid)}/{name}",
                name=name,
                size=64,
                mime="image/jpeg" if name.endswith(".jpg") else "application/pdf",
                kind="image" if name.endswith(".jpg") else "document",
                status="processing",
            ).id
            for n, name in enumerate(names)
        ]
    finally:
        engine.dispose()
    return cid, ids


# --- attachments -------------------------------------------------------------


def test_an_attachment_streams_to_workbench_and_is_kept_by_reference(
    client: TestClient, door: FakeDoor, tmp_path: Path
) -> None:
    """The file goes into the chat's folder; the chat keeps its reference and none of its bytes."""
    cid = _new(client)
    r = _attach(client, cid)
    assert r.status_code == 201
    assert r.json() | {"id": None, "created_at": None} == {
        "id": None,
        "file_id": FID,
        "name": "sales.xlsx",
        "size": 14,
        "mime": XLSX,
        "kind": "document",
        "status": "ready",
        "created_at": None,
        "expired": False,
    }
    upload = door.last("/files/upload")
    assert upload.url.params["directory"] == chat_folder(KEY, cid)
    assert door.uploads[-1].content_type == XLSX and door.uploads[-1].data == b"SENTINEL-BYTES"
    pending = client.get(f"/api/chats/{cid}", headers=as_user()).json()["pending_attachments"]
    assert [a["id"] for a in pending] == [r.json()["id"]]
    for kept in tmp_path.rglob("*"):
        assert not kept.is_file() or b"SENTINEL-BYTES" not in kept.read_bytes()


@pytest.mark.parametrize("name", ["tool.exe", "image.png", "noext"])
def test_other_types_are_refused_with_415(
    client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream, name: str
) -> None:
    """The type is decided by the extension alone; any other is refused before anything is recorded or sent."""
    cid = _new(client)
    before = len(events(audit_stream))
    r = _attach(client, cid, name)
    assert (r.status_code, r.json()) == (415, {"detail": "This file type is not supported."})
    assert door.uploads == [] and _since(audit_stream, before) == []


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("orders.json", "application/json"),
        ("events.jsonl", "application/x-ndjson"),
        ("events.ndjson", "application/x-ndjson"),
    ],
)
def test_json_files_are_attached(client: TestClient, door: FakeDoor, name: str, mime: str) -> None:
    """JSON and JSON Lines go to workbench like any document, on their own MIME types."""
    r = _attach(client, _new(client), name, b'{"order": "A-0001", "total": 12.5}')
    assert (r.status_code, r.json()["name"], r.json()["mime"]) == (201, name, mime)
    assert door.uploads[-1].content_type == mime


def test_a_potx_is_sent_on_as_a_template(client: TestClient, door: FakeDoor) -> None:
    """A PowerPoint slide master is attached, and goes to workbench on the template MIME type."""
    mime = "application/vnd.openxmlformats-officedocument.presentationml.template"
    r = _attach(client, _new(client), "deck.potx")
    assert (r.status_code, r.json()["name"], r.json()["mime"]) == (201, "deck.potx", mime)
    assert door.uploads[-1].content_type == mime


def test_a_dotx_is_sent_on_as_a_template(client: TestClient, door: FakeDoor) -> None:
    """A Word template is attached, and goes to workbench on the template MIME type."""
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.template"
    r = _attach(client, _new(client), "form.dotx")
    assert (r.status_code, r.json()["name"], r.json()["mime"]) == (201, "form.dotx", mime)
    assert door.uploads[-1].content_type == mime


def test_extensions_match_in_any_case(client: TestClient, door: FakeDoor) -> None:
    """``.PDF`` is a pdf; the name keeps its case."""
    r = _attach(client, _new(client), "Bericht März.PDF")
    assert (r.status_code, r.json()["name"], r.json()["mime"]) == (201, "Bericht März.PDF", "application/pdf")
    assert door.uploads[-1].content_type == "application/pdf"


def test_a_file_over_50_mib_is_refused_with_413(
    client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """Past the cap as declared or before the part begins, refused unrecorded; past 50 MiB cut off; one answer."""
    cid = _new(client)
    before = len(events(audit_stream))
    r = _attach(client, cid, headers={**as_user(), "Content-Length": str(52 * MIB)})
    assert (r.status_code, r.json()) == (413, TOO_LARGE)
    assert door.requests == [] and _since(audit_stream, before) == []

    head = f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="x"\r\n\r\n'.encode()
    tail = f'\r\n--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="sales.xlsx"\r\n\r\nPK'.encode()

    def body() -> Iterator[bytes]:
        yield head
        yield from (b"x" * MIB for _ in range(52))
        yield tail + f"\r\n--{BOUNDARY}--\r\n".encode()

    form = {**as_user(), "Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}
    r = client.post(f"/api/chats/{cid}/attachments", content=body(), headers=form)
    assert (r.status_code, r.json()) == (413, TOO_LARGE)
    assert door.requests == [] and _since(audit_stream, before) == []

    r = _attach(client, cid, data=b"\x00" * (50 * MIB + 1))
    assert (r.status_code, r.json()) == (413, TOO_LARGE)
    assert door.uploads == [] and _state(client).store.pending_count(cid) == 0
    disclosed = [e for e in _since(audit_stream, before) if e["action"] == "disclose"]
    assert [(e["outcome"], e.get("criteria")) for e in disclosed] == [("ok", None), ("error", {"reason": "too_large"})]


def test_a_name_that_workbench_would_refuse_is_422(
    client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """154 characters but 304 bytes: refused with a message, before anything is recorded or sent."""
    cid = _new(client)
    before = len(events(audit_stream))
    r = _attach(client, cid, "ä" * 150 + ".pdf")
    assert r.status_code == 422 and isinstance(r.json()["detail"], str) and r.json()["detail"]
    assert door.uploads == [] and _since(audit_stream, before) == []


def test_a_name_with_umlauts_and_spaces_round_trips(client: TestClient, door: FakeDoor) -> None:
    """The name reaches workbench, and comes back, as the user sent it."""
    name = "Übersicht März #1.xlsx"
    r = _attach(client, _new(client), name)
    assert (r.status_code, r.json()["name"]) == (201, name)
    assert door.uploads[-1].filename == name


@pytest.mark.parametrize(("status", "code"), [(413, "too_large"), (507, "storage_full")])
def test_workbenchs_refusals_reach_the_user_with_their_code(
    client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream, status: int, code: str
) -> None:
    """Workbench's own size limit and a full quota are told apart; no attachment row, the hand-over recorded failed."""
    cid = _new(client)
    before = len(events(audit_stream))
    door.answers["/files/upload"] = httpx.Response(status, json={"detail": "refused"})
    r = _attach(client, cid)
    detail = {
        413: "The file is larger than 50 MB.",
        507: "Your files fill the space workbench allows; delete a chat or wait for files to expire.",
    }[status]
    assert (r.status_code, r.json()) == (status, {"detail": detail, "code": code})
    assert _state(client).store.pending_count(cid) == 0
    [failed] = [e for e in _since(audit_stream, before) if e["outcome"] == "error"]
    assert (failed["action"], failed["operation"], failed["criteria"]) == (
        "disclose",
        "chat.attachment",
        {"reason": code},
    )


def test_any_other_failure_of_workbench_is_502(
    client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """An unreachable workbench: 502, no row, the hand-over recorded failed with its token."""
    cid = _new(client)
    before = len(events(audit_stream))
    door.answers["/files/upload"] = httpx.ConnectError("connection refused")
    r = _attach(client, cid)
    assert (r.status_code, r.json()) == (502, {"detail": "The file could not be handed to workbench."})
    assert _state(client).store.pending_count(cid) == 0
    [failed] = [e for e in _since(audit_stream, before) if e["outcome"] == "error"]
    assert failed["criteria"] == {"reason": "unreachable"}
    # The hand-over was attempted: the chat may hold a file, so its delete asks workbench.
    assert _state(client).store.workbench_chats([cid]) == {cid}


def test_a_chat_deleted_before_the_hand_over_is_not_found(
    client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chat is marked before its file goes out; a chat gone by then gets 404, nothing sent or recorded."""
    cid = _new(client)
    store = _state(client).store
    mark = store.mark_files

    def deleted_first(chat_id: str) -> None:
        store.delete_chat_rows(chat_id)
        mark(chat_id)

    monkeypatch.setattr(store, "mark_files", deleted_first)
    before = len(events(audit_stream))
    r = _attach(client, cid)
    assert (r.status_code, r.json()) == (404, {"detail": "Chat not found."})
    assert door.requests == [] and _since(audit_stream, before) == []


def test_a_chat_deleted_during_the_upload_is_not_found(
    client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream, loguru_caplog: pytest.LogCaptureFixture
) -> None:
    """A chat deleted meanwhile: 404, no row; gone once workbench stored the file, its folder is deleted too.

    The folder's delete is recorded first, as a child of the upload, and its outcome after it.
    """
    cid = _new(client)
    door.answers["/files/upload"] = httpx.Response(404, json={"detail": "not found"})
    r = _attach(client, cid)
    assert (r.status_code, r.json()) == (404, {"detail": "Chat not found."})

    for answer, criteria, outcome in (
        ({"deleted": 1}, {"files_deleted": 1}, "ok"),
        (httpx.ConnectError("connection refused"), {"reason": "unreachable"}, "error"),
    ):
        other = _new(client)
        door.answers["/files/upload"] = lambda folder, chat=other: _state(client).store.delete_chat_rows(chat)
        door.answers["/files/chat"] = answer
        before = len(events(audit_stream))
        r = _attach(client, other)
        assert (r.status_code, r.json()) == (404, {"detail": "Chat not found."})
        assert door.uploads[-1].filename == "sales.xlsx"
        assert door.last("/files/chat").headers["x-session-id"] == session_pseudonym(KEY, other)
        collect, _, recorded, settled = _since(audit_stream, before)
        folder = {"kind": "file", "container": other}
        for child in (recorded, settled):
            assert (child["action"], child["operation"], child["parent_event_id"], child["target"]) == (
                "delete",
                "chat.workbench",
                collect["event_id"],
                folder,
            )
        assert (recorded["criteria"], recorded["outcome"]) == ({"reason": "chat_gone"}, "ok")
        assert (settled["criteria"], settled["outcome"]) == (criteria, outcome)
    assert "WorkbenchUnavailable" in loguru_caplog.text and other not in loguru_caplog.text


@pytest.mark.parametrize(("writes", "sent"), [(2, ["/files/upload"]), (3, ["/files/upload", "/files/chat"])])
def test_an_orphan_delete_whose_record_fails_never_raises(
    client: TestClient,
    door: FakeDoor,
    audit_stream: CapturedAuditStream,
    loguru_caplog: pytest.LogCaptureFixture,
    writes: int,
    sent: list[str],
) -> None:
    """Unrecorded, the folder's delete is not sent and is left to workbench's retention; its outcome unrecorded, 404."""
    cid = _new(client)
    door.answers["/files/upload"] = lambda folder: _state(client).store.delete_chat_rows(cid)
    audit_stream.writes_left = writes
    r = _attach(client, cid)
    audit_stream.writes_left = None
    assert (r.status_code, r.json()) == (404, {"detail": "Chat not found."})
    assert [request.url.path for request, _ in door.requests] == sent
    assert "AuditWriteError" in loguru_caplog.text and cid not in loguru_caplog.text


def test_uploads_need_workbench(tmp_path: Path, audit_stream: CapturedAuditStream) -> None:
    """Without workbench's key there is nowhere to put a file: 503, nothing recorded."""
    settings = make_settings(tmp_path, workbench_api_key=None)
    with TestClient(create_app(settings, audit_stream=audit_stream)) as client:
        cid = _new(client)
        before = len(events(audit_stream))
        r = _attach(client, cid)
        assert (r.status_code, r.json()) == (503, {"detail": "File processing is not available."})
        assert _since(audit_stream, before) == []


def test_more_than_ten_pending_attachments_wait(client: TestClient, door: FakeDoor) -> None:
    """Ten files may wait for a message; the eleventh is refused until they are sent."""
    cid = _new(client)
    for n in range(10):
        door.upload_id = f"{n:032x}"
        assert _attach(client, cid, f"sales-{n}.xlsx").status_code == 201
    r = _attach(client, cid, "sales-10.xlsx")
    assert (r.status_code, r.json()) == (409, TOO_MANY_PENDING)
    assert len(door.uploads) == 10


def test_expired_pending_attachments_leave_room(client: TestClient, door: FakeDoor, clock: Clock) -> None:
    """Ten waiting files whose 7 days are over do not block an eleventh."""
    cid = _new(client)
    store = _state(client).store
    for n in range(10):
        store.add_attachment(cid, file_id=f"{n:032x}", path=f"/w/{n}", name=f"{n}.xlsx", size=1, mime=XLSX)
    r = _attach(client, cid)
    assert (r.status_code, r.json()) == (409, TOO_MANY_PENDING)
    clock.now += timedelta(days=7)
    assert _attach(client, cid).status_code == 201
    assert len(door.uploads) == 1


def test_two_parallel_uploads_at_nine_pending_leave_ten(
    tmp_path: Path, audit_stream: CapturedAuditStream, door: FakeDoor
) -> None:
    """Both pass the first count before either is stored; the count under the store's write lock refuses one."""
    arrived = 0

    async def together(request: httpx.Request) -> httpx.Response:
        nonlocal arrived
        if request.url.path == "/files/upload":
            arrived += 1
            with anyio.fail_after(5):
                while arrived < 2:
                    await anyio.sleep(0.01)
        return await door(request)

    transport = httpx.MockTransport(together)
    with TestClient(
        create_app(make_settings(tmp_path), audit_stream=audit_stream, workbench_transport=transport)
    ) as client:
        cid = _new(client)
        store = _state(client).store
        for n in range(9):
            store.add_attachment(cid, file_id=f"{n:032x}", path=f"/w/{n}", name=f"{n}.xlsx", size=1, mime=XLSX)
        with ThreadPoolExecutor(2) as pool:
            statuses = sorted(pool.map(lambda n: _attach(client, cid, f"sales-{n}.xlsx").status_code, range(2)))
        assert (statuses, arrived, store.pending_count(cid)) == ([201, 409], 2, 10)


def test_an_upload_is_recorded_as_collect_then_disclose(client: TestClient, audit_stream: CapturedAuditStream) -> None:
    """The attachment by its id, the hand-over to workbench as its child; never the file's name."""
    cid = _new(client)
    before = len(events(audit_stream))
    attachment = _attach(client, cid).json()
    recorded = _since(audit_stream, before)
    assert [(e["action"], e["operation"]) for e in recorded] == [
        ("collect", "chat.attachment"),
        ("disclose", "chat.attachment"),
    ]
    collect, disclose = recorded
    target = {"kind": "attachment", "ids": [attachment["id"]], "container": cid}
    assert (collect["target"], collect["criteria"]) == (target, {"mime": XLSX})
    assert (disclose["parent_event_id"], disclose["recipient"], disclose["outcome"]) == (
        collect["event_id"],
        "workbench:upload",
        "ok",
    )
    assert "sales.xlsx" not in audit_stream.getvalue()


def test_a_required_purpose_refuses_an_upload_unread(
    tmp_path: Path, audit_stream: CapturedAuditStream, door: FakeDoor
) -> None:
    """An upload discloses to workbench, so a required ``disclose`` purpose refuses it with 428 first."""
    settings = make_settings(tmp_path, require_purpose=frozenset({"disclose"}))
    app = create_app(settings, audit_stream=audit_stream, workbench_transport=httpx.MockTransport(door))
    with TestClient(app) as client:
        r = _attach(client, _new(client))
        assert r.status_code == 428 and door.requests == []
        [denied] = [e for e in events(audit_stream) if e["outcome"] == "denied"]
        assert denied["operation"] == "chat.attachment"


# --- downloads ---------------------------------------------------------------


def test_a_download_is_the_owners_and_keeps_workbenchs_hardening(
    client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """An attachment and a result stream back with workbench's type and disposition and its sandbox headers."""
    cid = _new(client)
    assert _attach(client, cid).status_code == 201
    before = len(events(audit_stream))
    r = client.get(f"/api/chats/{cid}/files/{FID}", headers=as_user())
    assert (r.status_code, r.content) == (200, b"SENTINEL-BYTES")
    assert r.headers["content-type"] == XLSX and r.headers["content-length"] == "14"
    assert r.headers["content-disposition"] == "inline; filename*=UTF-8''sales.xlsx"
    assert r.headers["content-security-policy"].startswith("sandbox")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cross-origin-resource-policy"] == "same-origin"
    assert r.headers["cache-control"] == "no-store"
    [download] = _since(audit_stream, before)
    assert (download["action"], download["operation"], download["recipient"], download["target"]) == (
        "disclose",
        "file.download",
        "download",
        {"kind": "file", "ids": [FID], "count": 1, "container": cid},
    )
    store = _state(client).store
    path = f"{chat_folder(KEY, cid)}/out/sums.xlsx"
    door.files[path] = (b"PK-RESULT", XLSX)
    result_id = "2" * 32
    store.add_result(
        store.start_assistant_message(cid),
        kind="table",
        file_id=result_id,
        path=path,
        name="sums.xlsx",
        mime=XLSX,
        size=9,
        preview=None,
    )
    r = client.get(f"/api/chats/{cid}/files/{result_id}", headers=as_user())
    assert (r.status_code, r.content) == (200, b"PK-RESULT")
    assert door.last("/files/view").url.params["path"] == path


def test_another_chats_file_is_not_found(client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream) -> None:
    """A file is reached through its own chat only: another chat, another owner or an unknown id is 404, unasked."""
    cid, other = _new(client), _new(client)
    assert _attach(client, cid).status_code == 201
    before = len(events(audit_stream))
    for path, user in [
        (f"/api/chats/{other}/files/{FID}", "user-a"),
        (f"/api/chats/{cid}/files/{FID}", "user-b"),
        (f"/api/chats/{cid}/files/{'3' * 32}", "user-a"),
    ]:
        r = client.get(path, headers=as_user(user))
        assert (r.status_code, r.json()) == NOT_FOUND
    assert [request for request, _ in door.requests if request.url.path == "/files/view"] == []
    assert _since(audit_stream, before) == []


def test_an_expired_file_is_gone(
    client: TestClient, door: FakeDoor, clock: Clock, audit_stream: CapturedAuditStream
) -> None:
    """Past workbench's period the file is gone unasked; a live row workbench no longer has is gone too."""
    cid = _new(client)
    assert _attach(client, cid).status_code == 201
    clock.now += timedelta(days=8)
    before = len(events(audit_stream))
    r = client.get(f"/api/chats/{cid}/files/{FID}", headers=as_user())
    assert (r.status_code, r.json()) == GONE
    assert [request for request, _ in door.requests if request.url.path == "/files/view"] == []
    assert _since(audit_stream, before) == []

    clock.now -= timedelta(days=8)
    door.files.clear()
    r = client.get(f"/api/chats/{cid}/files/{FID}", headers=as_user())
    assert (r.status_code, r.json()) == GONE
    download, failed = _since(audit_stream, before)
    assert (failed["parent_event_id"], failed["outcome"], failed["criteria"]) == (
        download["event_id"],
        "error",
        {"reason": "not_found"},
    )


def test_a_download_workbench_cannot_serve_is_502(
    client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """Workbench unreachable: 502, and the download's error child."""
    cid = _new(client)
    assert _attach(client, cid).status_code == 201
    before = len(events(audit_stream))
    door.answers["/files/view"] = httpx.ConnectError("connection refused")
    r = client.get(f"/api/chats/{cid}/files/{FID}", headers=as_user())
    assert (r.status_code, r.json()) == (502, {"detail": "The file could not be fetched from workbench."})
    _, failed = _since(audit_stream, before)
    assert (failed["outcome"], failed["criteria"]) == ("error", {"reason": "unreachable"})


# --- pictures and PDFs: prepared after the upload, followed by long-poll, previewed ---


def test_an_image_is_prepared_after_it_is_attached(
    seeing: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """The upload answers processing at once; workbench prepares the file it stored, and the hand-over is recorded."""
    cid = _new(seeing)
    before = len(events(audit_stream))
    r = _attach(seeing, cid, PHOTO, TINY_JPEG)
    assert r.status_code == 201
    photo = r.json()
    assert (photo["kind"], photo["status"], photo["mime"]) == ("image", "processing", "image/jpeg")
    assert _settled(seeing, cid, photo["id"]) == "ready"
    [prepare] = _prepares(door)
    assert dict(prepare.url.params) == {"path": f"{chat_folder(KEY, cid)}/{PHOTO}"}
    assert prepare.headers["x-session-id"] == session_pseudonym(KEY, cid)
    collect, upload, handed, query = _since(audit_stream, before)
    assert [(e["action"], e["operation"], e.get("recipient")) for e in (collect, upload, handed)] == [
        ("collect", "chat.attachment", None),
        ("disclose", "chat.attachment", "workbench:upload"),
        ("disclose", "chat.attachment", "workbench:prepare"),
    ]
    target = {"kind": "attachment", "ids": [photo["id"]], "container": cid}
    assert (handed["parent_event_id"], handed["target"], handed["criteria"], handed["outcome"]) == (
        collect["event_id"],
        target,
        {"kind": "image"},
        "ok",
    )
    assert (query["action"], query["operation"], query["target"], query["criteria"]) == (
        "query",
        "chat.attachment",
        target,
        {"status": "ready"},
    )
    assert PHOTO not in audit_stream.getvalue()


def test_a_pdf_waits_and_a_word_file_is_ready_at_once(seeing: TestClient, door: FakeDoor) -> None:
    """A PDF is processing until it is prepared; a Word file is ready at once and prepared after, a sheet never."""
    cid = _new(seeing)
    store = _state(seeing).store
    scan = _attach(seeing, cid, SCAN, b"%PDF-1.7").json()
    assert (scan["kind"], scan["status"]) == ("document", "processing")
    assert _settled(seeing, cid, scan["id"]) == "ready"
    found = store.find_attachment("user-a", cid, scan["id"])
    assert found is not None and found.manifest is not None and found.manifest["total"] == 3
    door.upload_id = "5" * 32
    memo = _attach(seeing, cid, "memo.docx").json()
    assert (memo["kind"], memo["status"]) == ("document", "ready")

    def kept() -> bool:
        found = store.find_attachment("user-a", cid, memo["id"])
        return found is not None and found.manifest is not None

    _wait_until(kept)
    found = store.find_attachment("user-a", cid, memo["id"])
    assert found is not None and found.status == "ready" and found.manifest is not None
    assert (found.manifest["kind"], found.manifest["unit"], found.manifest["total"]) == ("office", "page", 3)
    door.upload_id = "6" * 32
    sheet = _attach(seeing, cid, "figures.xlsx").json()
    assert (sheet["kind"], sheet["status"]) == ("document", "ready")
    assert not _state(seeing).processor.running(sheet["id"])
    found = store.find_attachment("user-a", cid, sheet["id"])
    assert found is not None and found.status == "ready" and found.manifest is None
    assert len(_prepares(door)) == 2


def test_an_office_file_is_not_prepared_with_vision_off(client: TestClient, door: FakeDoor) -> None:
    """Without a model that sees, a deck is ready at once as in 0.3.0: no job is submitted, workbench is not asked."""
    cid = _new(client)
    deck = _attach(client, cid, "harbour-deck.pptx", b"PK").json()
    assert (deck["kind"], deck["status"]) == ("document", "ready")
    assert not _state(client).processor.running(deck["id"])
    time.sleep(0.2)
    assert _prepares(door) == []
    found = _state(client).store.find_attachment("user-a", cid, deck["id"])
    assert found is not None and found.manifest is None


async def test_a_status_request_for_a_ready_attachment_never_waits(tmp_path: Path) -> None:
    """A ready deck whose background job runs is answered at once, whatever wait was asked; a processing row waits."""
    settings = make_settings(tmp_path, vision=True)
    engine = make_engine(settings.db_path)
    init_db(engine)
    try:
        store = ChatStore(engine, file_days=settings.file_days, notice_days=settings.notice_days)
        cid = store.create_chat("user-a").id

        def add(name: str, status: str) -> str:
            return store.add_attachment(
                cid,
                file_id="7" * 32,
                path=f"{chat_folder(KEY, cid)}/{name}",
                name=name,
                size=2,
                mime="application/octet-stream",
                kind="document",
                status=status,
            ).id

        deck, scan = add("harbour-deck.pptx", "ready"), add("quay-scan.pdf", "processing")
        processor = create_autospec(AttachmentProcessor, instance=True)
        processor.running.return_value = True
        found = await _read_after_wait(store, processor, "user-a", cid, deck, wait=20)
        assert found is not None and found.status == "ready"
        processor.wait.assert_not_called()
        found = await _read_after_wait(store, processor, "user-a", cid, scan, wait=20)
        assert found is not None and found.status == "processing"
        processor.wait.assert_awaited_once_with(scan, 20)
    finally:
        engine.dispose()


def test_an_unreadable_image_fails_and_a_pdf_stays_ready(
    seeing: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """A picture workbench cannot read cannot be seen: failed. A PDF it cannot render still reads as text: ready."""
    door.answers["/files/prepare"] = _unreadable
    cid = _new(seeing)
    before = len(events(audit_stream))
    photo = _attach(seeing, cid, PHOTO, TINY_JPEG).json()
    door.upload_id = "5" * 32
    scan = _attach(seeing, cid, SCAN, b"%PDF-1.7").json()
    assert (_settled(seeing, cid, photo["id"]), _settled(seeing, cid, scan["id"])) == ("failed", "ready")
    found = _state(seeing).store.find_attachment("user-a", cid, scan["id"])
    assert found is not None and found.manifest is None
    failed = [e for e in _since(audit_stream, before) if e["outcome"] == "error"]
    assert sorted(e["target"]["ids"][0] for e in failed) == sorted([photo["id"], scan["id"]])
    for event in failed:
        assert (event["action"], event["operation"], event["recipient"], event["criteria"]) == (
            "disclose",
            "chat.attachment",
            "workbench:prepare",
            {"reason": "unreadable_file"},
        )


def test_a_busy_prepare_is_tried_once_more(seeing: TestClient, door: FakeDoor, monkeypatch: pytest.MonkeyPatch) -> None:
    """A busy workbench is asked once more: 503 then the manifest is ready; 503 twice is the token of the failure."""
    monkeypatch.setattr(processing, "PREPARE_RETRY_S", 0)
    busy = [httpx.Response(503, json={"detail": "busy, retry later"})]
    door.answers["/files/prepare"] = lambda folder: busy.pop() if busy else None
    cid = _new(seeing)
    photo = _attach(seeing, cid, PHOTO, TINY_JPEG).json()
    assert _settled(seeing, cid, photo["id"]) == "ready"
    assert len(_prepares(door)) == 2

    door.answers["/files/prepare"] = lambda folder: httpx.Response(503, json={"detail": "busy, retry later"})
    door.upload_id = "5" * 32
    other = _attach(seeing, cid, "harbour-night.jpg", TINY_JPEG).json()
    assert _settled(seeing, cid, other["id"]) == "failed"
    assert len(_prepares(door)) == 4


def test_a_prepare_that_timed_out_is_not_tried_again(
    seeing: TestClient, door: FakeDoor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prepare that waited its whole time is not sent again: one request, and the picture fails at once."""
    monkeypatch.setattr(processing, "PREPARE_RETRY_S", 0)
    door.answers["/files/prepare"] = httpx.ReadTimeout("slow")
    cid = _new(seeing)
    photo = _attach(seeing, cid, PHOTO, TINY_JPEG).json()
    assert _settled(seeing, cid, photo["id"]) == "failed"
    assert len(_prepares(door)) == 1
    found = _state(seeing).store.find_attachment("user-a", cid, photo["id"])
    assert found is not None and found.status == "failed"


def test_the_status_route_is_the_owners_and_the_chats(
    seeing: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """Another owner, another chat or an unknown id is not found, and nothing is recorded."""
    cid, other = _new(seeing), _new(seeing)
    photo = _attach(seeing, cid, PHOTO, TINY_JPEG).json()
    assert _settled(seeing, cid, photo["id"]) == "ready"
    before = len(events(audit_stream))
    for chat, aid, user in [
        (cid, photo["id"], "user-b"),
        (other, photo["id"], "user-a"),
        (cid, "a-made-up-id", "user-a"),
    ]:
        r = _status(seeing, chat, aid, user=user)
        assert (r.status_code, r.json()) == NO_ATTACHMENT
    assert _since(audit_stream, before) == []


def test_the_long_poll_returns_when_ready_or_after_its_wait(
    seeing: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """wait=0 answers at once; a longer wait returns as the job ends, or after its wait for a row no job settles."""
    door.delays["/files/prepare"] = 0.5
    cid = _new(seeing)
    photo = _attach(seeing, cid, PHOTO, TINY_JPEG).json()
    started = time.monotonic()
    r = _status(seeing, cid, photo["id"], wait=0)
    assert (r.status_code, r.json()["status"]) == (200, "processing")
    r = _status(seeing, cid, photo["id"], wait=20)
    assert (r.status_code, r.json()["status"]) == (200, "ready")
    assert time.monotonic() - started < 5
    before = len(events(audit_stream))
    assert _status(seeing, cid, photo["id"], wait=21).status_code == 422
    assert _since(audit_stream, before) == []

    stuck = _state(seeing).store.add_attachment(
        cid,
        file_id="6" * 32,
        path=f"{chat_folder(KEY, cid)}/harbour-night.jpg",
        name="harbour-night.jpg",
        size=64,
        mime="image/jpeg",
        kind="image",
        status="processing",
    )
    started = time.monotonic()
    r = _status(seeing, cid, stuck.id, wait=1)
    assert (r.status_code, r.json()["status"]) == (200, "processing")
    assert 1 <= time.monotonic() - started < 5


def test_a_preview_streams_the_rendition_with_the_hardening(
    seeing: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """The picture's render streams back as workbench's jpeg, with the view's hardening; recorded before it goes."""
    cid = _new(seeing)
    photo = _attach(seeing, cid, PHOTO, TINY_JPEG).json()
    assert _settled(seeing, cid, photo["id"]) == "ready"
    before = len(events(audit_stream))
    r = _preview(seeing, cid, photo["id"])
    assert (r.status_code, r.content, r.headers["content-type"]) == (200, TINY_JPEG, "image/jpeg")
    for name, value in FILE_HEADERS.items():
        assert r.headers[name] == value
    [view] = _views(door)
    assert dict(view.url.params) == {"path": f"{chat_folder(KEY, cid)}/{PHOTO}", "rendition": "1"}
    assert view.headers["x-session-id"] == session_pseudonym(KEY, cid)
    [preview] = _since(audit_stream, before)
    assert (preview["action"], preview["operation"], preview["recipient"], preview["outcome"]) == (
        "disclose",
        "file.preview",
        "preview",
        "ok",
    )
    assert (preview["target"], preview["criteria"]) == (
        {"kind": "file", "ids": [FID], "count": 1, "container": cid},
        {"page": 1},
    )


def test_a_preview_is_found_only_through_its_chat(
    seeing: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """Another owner or another chat is not found: workbench is not asked, nothing is recorded."""
    cid, other = _new(seeing), _new(seeing)
    photo = _attach(seeing, cid, PHOTO, TINY_JPEG).json()
    assert _settled(seeing, cid, photo["id"]) == "ready"
    before = len(events(audit_stream))
    for chat, aid, user in [
        (cid, photo["id"], "user-b"),
        (other, photo["id"], "user-a"),
        (cid, "a-made-up-id", "user-a"),
    ]:
        r = _preview(seeing, chat, aid, user=user)
        assert (r.status_code, r.json()) == NOT_FOUND
    assert _views(door) == [] and _since(audit_stream, before) == []


def test_a_preview_of_a_processing_failed_or_expired_attachment(
    seeing: TestClient, door: FakeDoor, clock: Clock, audit_stream: CapturedAuditStream
) -> None:
    """Still being prepared: 409. Failed: not found. Past its file's period: gone. None of them asks workbench."""
    cid = _new(seeing)
    pending = _state(seeing).store.add_attachment(
        cid,
        file_id="6" * 32,
        path=f"{chat_folder(KEY, cid)}/harbour-night.jpg",
        name="harbour-night.jpg",
        size=64,
        mime="image/jpeg",
        kind="image",
        status="processing",
    )
    door.answers["/files/prepare"] = _unreadable
    failed = _attach(seeing, cid, PHOTO, TINY_JPEG).json()
    assert _settled(seeing, cid, failed["id"]) == "failed"
    door.answers["/files/prepare"] = None
    door.upload_id = "5" * 32
    ready = _attach(seeing, cid, "harbour-day.jpg", TINY_JPEG).json()
    assert _settled(seeing, cid, ready["id"]) == "ready"
    before = len(events(audit_stream))
    r = _preview(seeing, cid, pending.id)
    assert (r.status_code, r.json()) == (
        409,
        {"detail": "The file is still being prepared.", "code": "attachment_processing"},
    )
    r = _preview(seeing, cid, failed["id"])
    assert (r.status_code, r.json()) == NOT_FOUND
    clock.now += timedelta(days=8)
    r = _preview(seeing, cid, ready["id"])
    assert (r.status_code, r.json()) == GONE
    assert _views(door) == [] and _since(audit_stream, before) == []


def test_a_pdf_page_past_its_total_is_404(seeing: TestClient, door: FakeDoor) -> None:
    """Pages 1 to 3 of a three-page PDF are shown; page 4 is not found unasked, and page 0 is refused."""
    cid = _new(seeing)
    scan = _attach(seeing, cid, SCAN, b"%PDF-1.7").json()
    assert _settled(seeing, cid, scan["id"]) == "ready"
    r = _preview(seeing, cid, scan["id"], page=3)
    assert (r.status_code, r.content) == (200, TINY_JPEG)
    assert door.last("/files/view").url.params["rendition"] == "3"
    r = _preview(seeing, cid, scan["id"], page=4)
    assert (r.status_code, r.json()) == NOT_FOUND
    assert len(_views(door)) == 1
    assert _preview(seeing, cid, scan["id"], page=0).status_code == 422


def test_a_preview_workbench_cannot_serve(
    seeing: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """A render workbench no longer has is gone (410); an unreachable workbench is 502; each has an error child."""
    cid = _new(seeing)
    photo = _attach(seeing, cid, PHOTO, TINY_JPEG).json()
    assert _settled(seeing, cid, photo["id"]) == "ready"
    before = len(events(audit_stream))
    door.answers["/files/view"] = lambda folder: httpx.Response(404, json={"detail": "not found"})
    r = _preview(seeing, cid, photo["id"])
    assert (r.status_code, r.json()) == GONE
    door.answers["/files/view"] = httpx.ConnectError("connection refused")
    r = _preview(seeing, cid, photo["id"])
    assert (r.status_code, r.json()) == (502, {"detail": "The file could not be fetched from workbench."})
    recorded = _since(audit_stream, before)
    assert [(e["operation"], e["outcome"], e.get("criteria")) for e in recorded] == [
        ("file.preview", "ok", {"page": 1}),
        ("file.preview", "error", {"reason": "not_found"}),
        ("file.preview", "ok", {"page": 1}),
        ("file.preview", "error", {"reason": "unreachable"}),
    ]
    for primary, child in zip(recorded[::2], recorded[1::2], strict=True):
        assert (child["parent_event_id"], child["recipient"]) == (primary["event_id"], "preview")


def test_a_chat_deleted_while_preparing_cancels_the_job(
    seeing: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream, loguru_caplog: pytest.LogCaptureFixture
) -> None:
    """The delete does not wait for workbench's prepare: it cancels the job, which writes nothing, and the folder goes.

    The prepare takes 5 s; the job ends within 3 s of the delete only because it was cancelled.
    """
    door.delays["/files/prepare"] = 5.0
    cid = _new(seeing)
    photo = _attach(seeing, cid, PHOTO, TINY_JPEG).json()
    processor = _state(seeing).processor
    _wait_until(lambda: bool(_prepares(door)))
    started = time.monotonic()
    r = seeing.delete(f"/api/chats/{cid}", headers=as_user())
    assert r.status_code == 204 and time.monotonic() - started < 3
    _wait_until(lambda: not processor.running(photo["id"]), timeout_s=3)
    assert door.last("/files/chat").headers["x-session-id"] == session_pseudonym(KEY, cid)
    assert [e for e in events(audit_stream) if e["outcome"] == "error"] == []
    assert [record for record in loguru_caplog.records if record.levelno >= logging.ERROR] == []
    assert _state(seeing).store.find_attachment("user-a", cid, photo["id"]) is None


@pytest.mark.parametrize(
    ("failure", "answer"),
    [
        ("workbench", (502, {"detail": "The chat's files could not be deleted; try again."})),
        ("audit", (500, {"detail": "Request failed."})),
    ],
)
def test_a_failed_delete_prepares_the_chats_pictures_again(
    seeing: TestClient,
    door: FakeDoor,
    audit_stream: CapturedAuditStream,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    answer: tuple[int, dict[str, str]],
) -> None:
    """The delete cancelled the job, then failed and kept the chat: its picture is prepared again, as at start.

    Without that, the picture would stay processing with no job, and every message carrying it would get 409.
    """
    door.delays["/files/prepare"] = 1.5
    cid = _new(seeing)
    photo = _attach(seeing, cid, PHOTO, TINY_JPEG).json()
    _wait_until(lambda: bool(_prepares(door)))
    if failure == "workbench":
        door.answers["/files/chat"] = lambda folder: httpx.Response(503, json={"detail": "busy, retry later"})
    else:
        unrecorded_child(monkeypatch, "chat.workbench")
    r = seeing.delete(f"/api/chats/{cid}", headers=as_user())
    assert (r.status_code, r.json()) == answer
    assert _settled(seeing, cid, photo["id"]) == "ready"
    assert len(_prepares(door)) == 2
    handed = [e for e in events(audit_stream) if (e["action"], e["operation"]) == ("disclose", "chat.attachment")]
    assert [(e["actor"], e.get("parent_event_id"), e["criteria"]) for e in handed[2:]] == [
        (SYSTEM, None, {"kind": "image", "requeued": True})
    ]


def test_processing_attachments_are_prepared_again_at_start(
    tmp_path: Path, audit_stream: CapturedAuditStream, door: FakeDoor
) -> None:
    """A picture whose job ended with the last process is prepared again at start, by the system, requeued."""
    settings = make_settings(tmp_path, vision=True)
    cid, [aid] = _stranded(settings, PHOTO)
    path = f"{chat_folder(KEY, cid)}/{PHOTO}"
    door.manifests[path] = stock_manifest(path)
    with TestClient(
        create_app(settings, audit_stream=audit_stream, workbench_transport=httpx.MockTransport(door))
    ) as c:
        assert _settled(c, cid, aid) == "ready"
        found = _state(c).store.find_attachment("user-a", cid, aid)
        assert found is not None and found.manifest is not None and found.manifest["total"] == 1
    assert dict(door.last("/files/prepare").url.params) == {"path": path}
    [primary] = [e for e in events(audit_stream) if (e["action"], e["operation"]) == ("disclose", "chat.attachment")]
    assert (primary["actor"], primary.get("parent_event_id"), primary["recipient"], primary["outcome"]) == (
        SYSTEM,
        None,
        "workbench:prepare",
        "ok",
    )
    assert (primary["target"], primary["criteria"]) == (
        {"kind": "attachment", "ids": [aid], "container": cid},
        {"kind": "image", "requeued": True},
    )


def test_without_workbench_stranded_rows_settle(tmp_path: Path, audit_stream: CapturedAuditStream) -> None:
    """With workbench off nothing will prepare them: the picture fails, the PDF reads as text, ready."""
    settings = make_settings(tmp_path, vision=True, workbench_api_key=None)
    cid, [photo, scan] = _stranded(settings, PHOTO, SCAN)
    with TestClient(create_app(settings, audit_stream=audit_stream)) as client:
        assert _state(client).processor is None
        assert [_status(client, cid, aid).json()["status"] for aid in (photo, scan)] == ["failed", "ready"]
