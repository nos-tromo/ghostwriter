"""The chats API: list, new, read, rename and delete, each the owner's alone, and the turn registry.

Every name, id and value here is invented.
"""

import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typing_extensions import override

from ghostwriter.main import create_app
from ghostwriter.turns import TurnRegistry
from ghostwriter.utils.audit_runtime import encode_purpose
from ghostwriter.workbench import session_pseudonym, user_pseudonym
from tests.conftest import KEY, CapturedAuditStream, as_user, events, make_settings
from tests.fakes import FakeDoor

NOT_FOUND = (404, {"detail": "Chat not found."})
NOT_DELETED = (502, {"detail": "The chat's files could not be deleted; try again."})
FAILED = (500, {"detail": "Request failed."})
NO_COUNTS = {"messages": 0, "steps": 0, "results": 0, "attachments": 0}
#: The methods of /api/chats/{chat_id}.
ROUTE_METHODS = ("get", "patch", "delete")


@pytest.fixture
def door() -> FakeDoor:
    """Return workbench's doors, answering as a healthy workbench does.

    Returns:
        The door.
    """
    return FakeDoor()


@pytest.fixture
def client(tmp_path: Path, audit_stream: CapturedAuditStream, door: FakeDoor) -> Iterator[TestClient]:
    """Serve the app with the test settings, workbench behind ``door`` and the chain on ``audit_stream``.

    Args:
        tmp_path: The test's temporary directory.
        audit_stream: The stream the chain goes to.
        door: workbench.

    Yields:
        The client, inside the app's lifespan.
    """
    app = create_app(make_settings(tmp_path), audit_stream=audit_stream, workbench_transport=httpx.MockTransport(door))
    with TestClient(app) as test_client:
        yield test_client


def _state(client: TestClient) -> Any:
    app = client.app
    assert isinstance(app, FastAPI)
    return app.state


def _body(method: str) -> dict[str, Any]:
    return {"json": {"title": "Q3 Zahlen"}} if method == "patch" else {}


def _new(client: TestClient, user: str = "user-a") -> str:
    response = client.post("/api/chats", headers=as_user(user))
    assert response.status_code == 201
    return str(response.json()["id"])


def _ids(client: TestClient, user: str = "user-a") -> list[str]:
    return [chat["id"] for chat in client.get("/api/chats", headers=as_user(user)).json()]


def _operations(stream: CapturedAuditStream, since: int = 0) -> list[tuple[str, str, str]]:
    return [(e["action"], e["operation"], e["outcome"]) for e in events(stream)[since:]]


# --- the routes --------------------------------------------------------------


def test_a_user_sees_only_their_own_chats(client: TestClient, door: FakeDoor) -> None:
    """Another owner's chat, or an unknown one, is not found, and nothing about it changes."""
    chat = client.post("/api/chats", headers=as_user("user-a")).json()
    assert client.get("/api/chats", headers=as_user("user-b")).json() == []
    for method in ROUTE_METHODS:
        r = getattr(client, method)(f"/api/chats/{chat['id']}", headers=as_user("user-b"), **_body(method))
        assert (r.status_code, r.json()) == NOT_FOUND
        r = getattr(client, method)(f"/api/chats/{uuid4()}", headers=as_user("user-a"), **_body(method))
        assert (r.status_code, r.json()) == NOT_FOUND
    assert client.get("/api/chats", headers=as_user("user-a")).json() == [chat]
    assert door.requests == []


@pytest.mark.parametrize(("method", "chat"), [(m, "") for m in ("get", "post")] + [(m, "/x") for m in ROUTE_METHODS])
def test_every_route_needs_an_identity(client: TestClient, method: str, chat: str) -> None:
    """Without a trusted X-Auth-User every route is refused."""
    assert getattr(client, method)(f"/api/chats{chat}", **_body(method)).status_code == 401


def test_new_rename_and_list(client: TestClient) -> None:
    """A new chat has no title; a rename sets the stripped title and moves the chat to the top of the list."""
    created = client.post("/api/chats", headers=as_user())
    assert created.status_code == 201
    first = created.json()
    assert first["title"] is None and set(first) == {"id", "title", "created_at", "updated_at", "expires_at"}
    second = _new(client)
    assert _ids(client) == [second, first["id"]]

    renamed = client.patch(f"/api/chats/{first['id']}", json={"title": "  Q3 Zahlen  "}, headers=as_user())
    assert renamed.status_code == 200 and renamed.json()["title"] == "Q3 Zahlen"
    assert _ids(client) == [first["id"], second]
    detail = client.get(f"/api/chats/{first['id']}", headers=as_user())
    assert detail.json() == {"chat": renamed.json(), "messages": [], "pending_attachments": []}


@pytest.mark.parametrize("title", ["", "   ", "x" * 201])
def test_a_title_has_1_to_200_characters(client: TestClient, title: str) -> None:
    """After stripping, a title shorter than 1 or longer than 200 characters is refused."""
    cid = _new(client)
    assert client.patch(f"/api/chats/{cid}", json={"title": title}, headers=as_user()).status_code == 422
    assert client.get(f"/api/chats/{cid}", headers=as_user()).json()["chat"]["title"] is None


@pytest.mark.parametrize("body", [{}, {"title": None}, {"title": 5}])
def test_a_rename_needs_a_title_string(client: TestClient, body: dict[str, Any]) -> None:
    """A rename without a string title is refused."""
    assert client.patch(f"/api/chats/{_new(client)}", json=body, headers=as_user()).status_code == 422


@pytest.mark.parametrize("title", ["x", " " + "x" * 200 + " "])
def test_a_title_of_1_to_200_characters_is_kept(client: TestClient, title: str) -> None:
    """The bounds themselves are accepted, counted after stripping."""
    renamed = client.patch(f"/api/chats/{_new(client)}", json={"title": title}, headers=as_user())
    assert (renamed.status_code, renamed.json()["title"]) == (200, title.strip())


def test_reading_a_chat_serves_its_messages_and_counts_them(
    client: TestClient, audit_stream: CapturedAuditStream
) -> None:
    """The owner reads the chat with its messages; the event counts what the chat holds."""
    cid = _new(client)
    _state(client).store.add_user_message("user-a", cid, "Summe der Spalte B", [])
    detail = client.get(f"/api/chats/{cid}", headers=as_user()).json()
    assert [(m["role"], m["content"]) for m in detail["messages"]] == [("user", "Summe der Spalte B")]
    event = events(audit_stream)[-1]
    assert (event["action"], event["operation"]) == ("query", "chat.read")
    assert event["target"] == {"kind": "chat", "ids": [cid]} and event["criteria"] == {**NO_COUNTS, "messages": 1}


def test_deleting_records_first_then_workbench_then_rows(
    client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """The event is written before workbench is asked, and the rows go only after workbench deleted the files."""
    cid = _new(client)
    store = _state(client).store
    store.add_user_message("user-a", cid, "Summe der Spalte B", [])
    seen: list[tuple[list[str], list[str]]] = []
    door.on_delete = lambda: seen.append(
        ([e["operation"] for e in events(audit_stream)], [chat.id for chat in store.list_chats("user-a")])
    )
    response = client.delete(f"/api/chats/{cid}", headers={**as_user(), "X-Request-ID": "req-0001"})
    assert (response.status_code, response.content) == (204, b"")
    assert len(seen) == 1 and seen[0][0][-1] == "chat.delete" and seen[0][1] == [cid]
    sent = door.last("/files/chat")
    assert sent.headers["x-session-id"] == session_pseudonym(KEY, cid)
    assert sent.headers["x-user-id"] == user_pseudonym(KEY, "user-a")
    assert sent.headers["x-audit-purpose"] == encode_purpose(None, "ghostwriter chat")
    assert sent.headers["x-request-id"] == "req-0001"

    primary, child = events(audit_stream)[-2:]
    assert (primary["action"], primary["operation"], primary["outcome"]) == ("delete", "chat.delete", "ok")
    assert primary["target"] == {"kind": "chat", "ids": [cid]} and primary["criteria"] == {**NO_COUNTS, "messages": 1}
    assert (child["operation"], child["criteria"]["files_deleted"], child["parent_event_id"]) == (
        "chat.workbench",
        3,
        primary["event_id"],
    )
    assert (child["action"], child["outcome"], child["request_id"]) == ("delete", "ok", "req-0001")
    assert _ids(client) == []
    r = client.get(f"/api/chats/{cid}", headers=as_user())
    assert (r.status_code, r.json()) == NOT_FOUND


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (httpx.Response(503, json={"detail": "busy, retry later"}), "http:503"),
        (httpx.ConnectError("refused"), "unreachable"),
    ],
    ids=["503", "unreachable"],
)
def test_a_failed_workbench_delete_keeps_the_chat(
    client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream, answer: object, reason: str
) -> None:
    """A failed workbench delete is a 502; the child says why, the chat stays, and the delete can be tried again."""
    cid = _new(client)
    door.answers["/files/chat"] = answer
    before = len(events(audit_stream))
    response = client.delete(f"/api/chats/{cid}", headers=as_user())
    assert (response.status_code, response.json()) == NOT_DELETED
    assert _operations(audit_stream, before) == [("delete", "chat.delete", "ok"), ("delete", "chat.workbench", "error")]
    assert events(audit_stream)[-1]["criteria"] == {"reason": reason}
    assert _ids(client) == [cid] and not _state(client).turns.running(cid)

    door.answers["/files/chat"] = {"deleted": 0}
    assert client.delete(f"/api/chats/{cid}", headers=as_user()).status_code == 204
    assert _ids(client) == []


def test_without_workbench_the_chat_is_deleted_alone(tmp_path: Path, audit_stream: CapturedAuditStream) -> None:
    """With no workbench key there is no client; the child records that workbench is not configured."""
    settings = make_settings(tmp_path, workbench_api_key=None)
    with TestClient(create_app(settings, audit_stream=audit_stream)) as client:
        assert _state(client).workbench is None
        cid = _new(client)
        assert client.delete(f"/api/chats/{cid}", headers=as_user()).status_code == 204
        assert _ids(client) == []
    child = next(e for e in events(audit_stream) if e["operation"] == "chat.workbench")
    assert (child["outcome"], child["criteria"]) == ("ok", {"workbench": "not_configured"})


def test_a_chat_with_a_running_answer_is_not_deleted(
    client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """While an answer runs the delete is refused unrecorded; another owner still gets not found."""
    cid = _new(client)
    turns = _state(client).turns
    claim = turns.begin(cid)
    assert claim is not None
    before = len(events(audit_stream))
    response = client.delete(f"/api/chats/{cid}", headers=as_user())
    assert (response.status_code, response.json()) == (409, {"detail": "Stop the answer before deleting the chat."})
    r = client.delete(f"/api/chats/{cid}", headers=as_user("user-b"))
    assert (r.status_code, r.json()) == NOT_FOUND
    assert _operations(audit_stream, before) == [] and door.requests == [] and _ids(client) == [cid]
    turns.end(cid, claim)
    assert client.delete(f"/api/chats/{cid}", headers=as_user()).status_code == 204


def test_no_answer_starts_while_the_chat_is_being_deleted(client: TestClient, door: FakeDoor) -> None:
    """The delete holds the chat's turn until it ends, so no answer writes into a chat being deleted."""
    cid = _new(client)
    turns = _state(client).turns
    seen: list[threading.Event | None] = []
    door.on_delete = lambda: seen.append(turns.begin(cid))
    assert client.delete(f"/api/chats/{cid}", headers=as_user()).status_code == 204
    assert seen == [None] and not turns.running(cid)


def test_a_stale_end_does_not_release_a_deletes_claim(client: TestClient, door: FakeDoor) -> None:
    """An end with an event other than the delete's own leaves the chat held until the delete ends."""
    cid = _new(client)
    turns = _state(client).turns
    held: list[bool] = []

    def stale_end() -> None:
        turns.end(cid, threading.Event())
        held.append(turns.running(cid))

    door.on_delete = stale_end
    assert client.delete(f"/api/chats/{cid}", headers=as_user()).status_code == 204
    assert held == [True] and not turns.running(cid)


class _LateClaim(TurnRegistry):
    """A turn registry that runs ``first`` once, just before the next claim, as if that delete won the race."""

    def __init__(self, first: Callable[[], None]) -> None:
        super().__init__()
        self._first: Callable[[], None] | None = first

    @override
    def begin(self, chat_id: str) -> threading.Event | None:
        first, self._first = self._first, None
        if first is not None:
            first()
        return super().begin(chat_id)


def test_a_delete_that_claims_the_turn_after_another_finds_the_chat_gone(
    client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """The second of two deletes, past its lookup before the first ended, records and sends nothing and answers 404."""
    cid = _new(client)
    state = _state(client)
    first: list[int] = []
    state.turns = _LateClaim(lambda: first.append(client.delete(f"/api/chats/{cid}", headers=as_user()).status_code))
    before = len(events(audit_stream))
    second = client.delete(f"/api/chats/{cid}", headers=as_user())
    assert first == [204]
    assert second.status_code == NOT_FOUND[0] and second.json() == NOT_FOUND[1]
    assert [op for op in _operations(audit_stream, before) if op[1] == "chat.delete"] == [
        ("delete", "chat.delete", "ok")
    ]
    assert [request.url.path for request, _ in door.requests] == ["/files/chat"]
    assert not state.turns.running(cid)


def test_events_name_chats_by_id_only(client: TestClient, audit_stream: CapturedAuditStream) -> None:
    """Each route writes its event; the title appears in none of them."""
    cid = _new(client)
    client.patch(f"/api/chats/{cid}", json={"title": "Q3 Zahlen"}, headers=as_user())
    client.get("/api/chats", headers=as_user())
    client.get(f"/api/chats/{cid}", headers=as_user())
    client.delete(f"/api/chats/{cid}", headers=as_user())
    assert "Q3 Zahlen" not in audit_stream.getvalue()
    chat = {"kind": "chat", "ids": [cid]}
    written = [(e["action"], e["operation"], e.get("target")) for e in events(audit_stream)[2:]]
    assert written == [
        ("collect", "chat.create", chat),
        ("modify", "chat.rename", chat),
        ("query", "chat.list", {"kind": "chat", "count": 1}),
        ("query", "chat.read", chat),
        ("delete", "chat.delete", chat),
        ("delete", "chat.workbench", None),
    ]
    assert {e["actor"]["user"] for e in events(audit_stream)[2:]} == {"user-a"}


def test_an_action_whose_event_cannot_be_written_does_not_happen(
    client: TestClient, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """With the audit stream broken nothing is created, renamed, served or deleted."""
    cid = _new(client)
    audit_stream.failing = True
    for method, path in [(m, "/api/chats") for m in ("post", "get")] + [
        (m, f"/api/chats/{cid}") for m in ROUTE_METHODS
    ]:
        r = getattr(client, method)(path, headers=as_user(), **_body(method))
        assert (r.status_code, r.json()) == FAILED
    audit_stream.failing = False
    listed = client.get("/api/chats", headers=as_user()).json()
    assert [(chat["id"], chat["title"]) for chat in listed] == [(cid, None)]
    assert door.requests == [] and not _state(client).turns.running(cid)


# --- the turn registry -------------------------------------------------------


def test_one_turn_runs_per_chat_until_it_ends() -> None:
    """A second begin is refused while a turn runs; stop signals it; only its own end frees the chat."""
    turns = TurnRegistry()
    cancel = turns.begin("chat-1")
    assert isinstance(cancel, threading.Event) and not cancel.is_set()
    assert turns.begin("chat-1") is None and turns.running("chat-1") and not turns.running("chat-2")
    assert turns.begin("chat-2") is not None
    assert turns.stop("chat-1") is True and cancel.is_set() and turns.running("chat-1")
    turns.end("chat-1", threading.Event())
    assert turns.running("chat-1")
    turns.end("chat-1", cancel)
    assert not turns.running("chat-1") and turns.stop("chat-1") is False
    again = turns.begin("chat-1")
    assert again is not None and not again.is_set()
    turns.end("chat-1", cancel)
    assert turns.running("chat-1")
    turns.end("chat-unknown", again)


def test_of_many_threads_one_begins_the_turn() -> None:
    """Concurrent begins on one chat give exactly one turn."""
    turns = TurnRegistry()
    start = threading.Barrier(16)
    got: list[threading.Event | None] = []

    def race() -> None:
        start.wait()
        got.append(turns.begin("chat-1"))

    threads = [threading.Thread(target=race) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(event is not None for event in got) == 1
