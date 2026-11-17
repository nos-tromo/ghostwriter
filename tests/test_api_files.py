"""Attachments stream into the chat's folder in workbench; downloads stream back, for the chat's owner only.

Every name, id and value here is invented.
"""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ghostwriter.db import utcnow
from ghostwriter.main import create_app
from ghostwriter.workbench import chat_folder, session_pseudonym
from tests.conftest import KEY, CapturedAuditStream, as_user, events, make_settings
from tests.fakes import UPLOAD_ID, XLSX, FakeDoor

MIB = 1024 * 1024
TOO_LARGE = {"detail": "The file is larger than 50 MB.", "code": "too_large"}
BOUNDARY = "gwtest"
NOT_FOUND = (404, {"detail": "File not found."})
GONE = (410, {"detail": "The file has expired."})
FID = UPLOAD_ID


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


def test_a_chat_deleted_during_the_upload_is_not_found(
    client: TestClient, door: FakeDoor, loguru_caplog: pytest.LogCaptureFixture
) -> None:
    """A chat deleted meanwhile: 404, no row; gone once workbench stored the file, its folder is deleted too."""
    cid = _new(client)
    door.answers["/files/upload"] = httpx.Response(404, json={"detail": "not found"})
    r = _attach(client, cid)
    assert (r.status_code, r.json()) == (404, {"detail": "Chat not found."})

    for answer in ({"deleted": 1}, httpx.ConnectError("connection refused")):
        other = _new(client)
        door.answers["/files/upload"] = lambda folder, chat=other: _state(client).store.delete_chat_rows(chat)
        door.answers["/files/chat"] = answer
        r = _attach(client, other)
        assert (r.status_code, r.json()) == (404, {"detail": "Chat not found."})
        assert door.uploads[-1].filename == "sales.xlsx"
        assert door.last("/files/chat").headers["x-session-id"] == session_pseudonym(KEY, other)
    assert "WorkbenchUnavailable" in loguru_caplog.text and other not in loguru_caplog.text


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
    assert (r.status_code, r.json()) == (409, {"detail": "Send the attached files before adding more."})
    assert len(door.uploads) == 10


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
