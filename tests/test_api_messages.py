"""Messages: the answer as an SSE stream, one turn per chat, stop and regenerate, and the turn's audit.

Every name, id and value here is invented.
"""

import json
import threading
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import httpx2
import openai
import pytest
from anyio import CapacityLimiter
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session
from typing_extensions import override

from ghostwriter.agent.limits import TURN_THREADS
from ghostwriter.agent.loop import AgentLoop, TokenEvent, Turn, TurnAudit, TurnEvent
from ghostwriter.api.messages import TurnStream
from ghostwriter.db import make_engine, utcnow
from ghostwriter.inference import ModelClient
from ghostwriter.main import create_app
from ghostwriter.models import Chat
from ghostwriter.schemas import AttachmentOut, ChatOut
from ghostwriter.sse import sse_frame
from ghostwriter.turns import TurnRegistry
from ghostwriter.utils.audit_event import sha256_text
from ghostwriter.utils.audit_runtime import encode_purpose
from ghostwriter.workbench import CallFor, chat_folder
from tests.conftest import KEY, CapturedAuditStream, as_user, events, make_settings
from tests.fakes import (
    UPLOAD_ID,
    XLSX,
    Chunk,
    FakeDoor,
    FakeOpenAI,
    Round,
    call_chunk,
    reasoning_chunk,
    text_chunk,
    usage_chunk,
)

ANSWER: Round = [text_chunk("Summe: 42"), usage_chunk(950, 5, "served-model")]
RUNNING = (409, {"detail": "An answer is still being written in this chat."})
NOT_FOUND = (404, {"detail": "Chat not found."})
THINKING_ON = {"chat_template_kwargs": {"enable_thinking": True}}
THINKING_OFF = {"chat_template_kwargs": {"enable_thinking": False}}
AUDIT_ERROR = {"code": "audit", "message": "The answer stopped: it could not be recorded."}


@dataclass(frozen=True)
class Frame:
    """One SSE frame.

    Attributes:
        event: Its event name.
        data: Its JSON data.
    """

    event: str
    data: dict[str, Any]


@pytest.fixture
def door() -> FakeDoor:
    """Return workbench's doors, answering as a healthy workbench does."""
    return FakeDoor()


@pytest.fixture
def fake_openai() -> FakeOpenAI:
    """Return the router, with no round scripted yet."""
    return FakeOpenAI()


def _app(
    tmp_path: Path, audit_stream: CapturedAuditStream, door: FakeDoor, fake_openai: FakeOpenAI, **overrides: Any
) -> FastAPI:
    settings = make_settings(tmp_path, text_model="chat-model", **overrides)
    model = ModelClient(settings, client=fake_openai.as_client())
    return create_app(settings, audit_stream=audit_stream, workbench_transport=httpx.MockTransport(door), model=model)


@pytest.fixture
def client(
    tmp_path: Path, audit_stream: CapturedAuditStream, door: FakeDoor, fake_openai: FakeOpenAI
) -> Iterator[TestClient]:
    """Serve the app with workbench behind ``door`` and the router behind ``fake_openai``."""
    with TestClient(_app(tmp_path, audit_stream, door, fake_openai)) as test_client:
        yield test_client


def _state(client: TestClient) -> Any:
    app = client.app
    assert isinstance(app, FastAPI)
    return app.state


def _new(client: TestClient, user: str = "user-a") -> str:
    response = client.post("/api/chats", headers=as_user(user))
    assert response.status_code == 201
    return str(response.json()["id"])


def _send(
    client: TestClient,
    cid: str,
    content: str = "Summe der Spalte B",
    headers: dict[str, str] | None = None,
    **body: Any,
) -> httpx2.Response:
    return client.post(f"/api/chats/{cid}/messages", json={"content": content, **body}, headers=headers or as_user())


def _read(lines: Iterator[str]) -> Iterator[Frame]:
    """Read SSE frames from a body's lines, as the SPA's reader does."""
    event: str | None = None
    data: dict[str, Any] = {}
    for line in lines:
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data = json.loads(line.removeprefix("data: "))
        elif line == "" and event is not None:
            yield Frame(event, data)
            event = None


def _frames(response: httpx2.Response) -> list[Frame]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return list(_read(iter(response.text.split("\n"))))


def _read_call(cid: str) -> list[Chunk]:
    """Return a round that reads the chat's sales.xlsx."""
    arguments = json.dumps({"path": f"{chat_folder(KEY, cid)}/sales.xlsx"})
    return [call_chunk(0, "call_0", "read_document", arguments), usage_chunk(900, 20, "served-model")]


def _attach(client: TestClient, cid: str, file_id: str = UPLOAD_ID) -> AttachmentOut:
    store = _state(client).store
    path = f"{chat_folder(KEY, cid)}/sales.xlsx"
    attachment: AttachmentOut = store.add_attachment(
        cid, file_id=file_id, path=path, name="sales.xlsx", size=48_213, mime=XLSX
    )
    return attachment


def _detail(client: TestClient, cid: str) -> dict[str, Any]:
    response = client.get(f"/api/chats/{cid}", headers=as_user())
    assert response.status_code == 200
    detail: dict[str, Any] = response.json()
    return detail


def _wait_until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "the condition never held"
        time.sleep(0.01)


def _gate(opened: threading.Event, then: Chunk | Exception) -> Callable[[], Chunk]:
    """Return a chunk that waits for ``opened``, as a router that stalls; then it answers or raises."""

    def late() -> Chunk:
        assert opened.wait(5)
        if isinstance(then, Exception):
            raise then
        return then

    return late


def _take_all(client: TestClient, limiter: CapacityLimiter) -> list[object]:
    portal = client.portal
    assert portal is not None
    borrowers = [object() for _ in range(TURN_THREADS)]
    for borrower in borrowers:
        portal.call(limiter.acquire_on_behalf_of, borrower)
    return borrowers


def _give_back(client: TestClient, limiter: CapacityLimiter, borrowers: list[object]) -> None:
    portal = client.portal
    assert portal is not None
    for borrower in borrowers:
        portal.call(limiter.release_on_behalf_of, borrower)
    borrowers.clear()


# --- the stream --------------------------------------------------------------


def test_a_message_streams_steps_results_tokens_and_a_final_frame(
    client: TestClient, fake_openai: FakeOpenAI, door: FakeDoor
) -> None:
    """A tool round, then the answer: the step's two frames, its card, the text, and the final frame last."""
    cid = _new(client)
    fake_openai.script = [_read_call(cid), ANSWER]
    response = _send(client, cid)
    frames = _frames(response)
    assert [f.event for f in frames] == ["step", "step", "result", "token", "final"]
    assert frames[0].data["id"] == frames[1].data["id"]
    assert [frames[0].data["status"], frames[1].data["status"]] == ["running", "ok"]
    assert (frames[2].data["kind"], frames[2].data["name"]) == ("table", "sums.xlsx")
    assert frames[3].data == {"text": "Summe: 42"}
    final = frames[-1].data
    assert final["stopped"] is False and final["chat"]["title"] == "Summe der Spalte B"
    assert (response.headers["cache-control"], response.headers["x-accel-buffering"]) == ("no-cache", "no")
    detail = _detail(client, cid)
    question, answer = detail["messages"]
    assert (final["user_message_id"], final["message_id"], final["chat"]) == (
        question["id"],
        answer["id"],
        detail["chat"],
    )
    assert answer["content"] == "Summe: 42"
    assert [s["id"] for s in answer["steps"]] == [frames[0].data["id"]]
    assert [r["id"] for r in answer["results"]] == [frames[2].data["id"]]
    assert "tools/call" in door.methods() and not _state(client).turns.running(cid)


def test_frames_have_the_documented_fields(client: TestClient, fake_openai: FakeOpenAI) -> None:
    """Each frame carries exactly its documented fields."""
    cid = _new(client)
    broken = openai.APIConnectionError(request=httpx2.Request("POST", "http://router.test/v1/chat/completions"))
    fake_openai.script = [_read_call(cid), broken]
    frames = _frames(_send(client, cid))
    assert [f.event for f in frames] == ["step", "step", "result", "error", "final"]
    step, _, result, error, final = (f.data for f in frames)
    assert set(step) == {"id", "tool", "status", "detail", "arguments", "result", "expired"}
    assert set(result) == {"id", "kind", "file_id", "name", "mime", "size", "preview", "expired"}
    assert error == {"code": "model", "message": "The model did not answer."}
    assert set(final) == {"message_id", "user_message_id", "chat", "stopped"}
    assert set(final["chat"]) == {"id", "title", "created_at", "updated_at", "expires_at"}
    fake_openai.script = [ANSWER]
    [token, _] = _frames(_send(client, cid, content="Noch einmal"))
    assert token == Frame("token", {"text": "Summe: 42"})


def test_a_frame_is_one_event_and_one_line_of_compact_json() -> None:
    """``sse_frame`` writes the event, then the data as compact JSON on one line, then a blank line."""
    assert (
        sse_frame("token", {"text": "Größe\nZeile"}) == 'event: token\ndata: {"text":"Gr\\u00f6\\u00dfe\\nZeile"}\n\n'
    )


def test_the_chat_is_free_once_the_final_frame_is_out(client: TestClient, fake_openai: FakeOpenAI) -> None:
    """A client that sends again as soon as it reads ``final`` finds the chat free."""
    cid = _new(client)
    fake_openai.script = [ANSWER, ANSWER]
    with client.stream("POST", f"/api/chats/{cid}/messages", json={"content": "Hallo"}, headers=as_user()) as response:
        frames = _read(response.iter_lines())
        assert next(frames).event == "token" and next(frames).event == "final"
        assert not _state(client).turns.running(cid)
        assert _send(client, cid, content="Und jetzt?").status_code == 200


def test_one_turn_per_chat_at_a_time(client: TestClient, fake_openai: FakeOpenAI) -> None:
    """While a turn holds a chat, a message and a regenerate are refused; another chat answers."""
    cid, other = _new(client), _new(client)
    turns = _state(client).turns
    claim = turns.begin(cid)
    assert claim is not None
    for path in ("messages", "regenerate"):
        r = client.post(f"/api/chats/{cid}/{path}", json={"content": "Hallo"}, headers=as_user())
        assert (r.status_code, r.json()) == RUNNING
    fake_openai.script = [ANSWER]
    assert _send(client, other).status_code == 200
    assert turns.running(cid) and not turns.running(other)
    turns.end(cid, claim)
    assert _detail(client, cid)["messages"] == []


def test_stop_ends_the_turn_and_keeps_the_text(client: TestClient, fake_openai: FakeOpenAI) -> None:
    """A stop while tokens arrive ends the round at the next chunk; the text shown is the text stored."""
    cid = _new(client)
    stopped = threading.Event()
    fake_openai.script = [
        [text_chunk("Eins"), text_chunk(" zwei"), _gate(stopped, text_chunk(" drei")), text_chunk("!")]
    ]
    tokens: list[str] = []
    with client.stream("POST", f"/api/chats/{cid}/messages", json={"content": "Zähl"}, headers=as_user()) as response:
        frames = _read(response.iter_lines())
        for frame in frames:
            assert frame.event == "token"
            tokens.append(frame.data["text"])
            if len(tokens) == 2:
                break
        assert client.post(f"/api/chats/{cid}/stop", headers=as_user()).status_code == 204
        stopped.set()
        rest = list(frames)
    assert [f.event for f in rest] == ["final"] and rest[0].data["stopped"] is True
    assert tokens == ["Eins", " zwei"]
    assert _detail(client, cid)["messages"][-1]["content"] == "Eins zwei"
    assert not _state(client).turns.running(cid) and fake_openai.streams[0].closed


def test_a_stop_mid_tool_call_keeps_what_was_done_and_frees_the_chat(
    client: TestClient, fake_openai: FakeOpenAI, door: FakeDoor
) -> None:
    """A tab closed during a call: the call finishes and is stored, no round follows, the chat answers again."""
    cid = _new(client)
    door.delays["tools/call"] = 0.5
    fake_openai.script = [_read_call(cid)]
    with client.stream("POST", f"/api/chats/{cid}/messages", json={"content": "Lies"}, headers=as_user()) as response:
        first = next(_read(response.iter_lines()))
        assert (first.event, first.data["status"]) == ("step", "running")
    turns = _state(client).turns
    _wait_until(lambda: not turns.running(cid))
    answer = _detail(client, cid)["messages"][-1]
    assert [(s["id"], s["status"]) for s in answer["steps"]] == [(first.data["id"], "ok")]
    assert len(fake_openai.calls) == 1 and door.methods().count("tools/call") == 1
    fake_openai.script = [ANSWER]
    assert _send(client, cid, content="Und jetzt?").status_code == 200


def test_a_round_that_fails_after_a_stop_ends_as_stopped(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """A stalled router that times out once the stop is set ends the turn as stopped, with no error frame."""
    cid = _new(client)
    stopped = threading.Event()
    timeout = openai.APITimeoutError(request=httpx2.Request("POST", "http://router.test/v1/chat/completions"))
    fake_openai.script = [[text_chunk("Eins."), _gate(stopped, timeout)]]
    with client.stream("POST", f"/api/chats/{cid}/messages", json={"content": "Zähl"}, headers=as_user()) as response:
        frames = _read(response.iter_lines())
        assert next(frames) == Frame("token", {"text": "Eins."})
        assert client.post(f"/api/chats/{cid}/stop", headers=as_user()).status_code == 204
        stopped.set()
        rest = list(frames)
    assert [f.event for f in rest] == ["final"] and rest[0].data["stopped"] is True
    assert _detail(client, cid)["messages"][-1]["content"] == "Eins."
    [answer] = [e for e in events(audit_stream) if e["operation"] == "chat.answer"]
    assert (answer["outcome"], answer["criteria"]["stopped"]) == ("error", True)


def test_stop_is_idempotent_owned_and_unrecorded(client: TestClient, audit_stream: CapturedAuditStream) -> None:
    """Stop answers 204 with or without a turn, 404 for another owner's chat, and records nothing."""
    cid = _new(client)
    before = len(events(audit_stream))
    for _ in range(2):
        assert client.post(f"/api/chats/{cid}/stop", headers=as_user()).status_code == 204
    r = client.post(f"/api/chats/{cid}/stop", headers=as_user("user-b"))
    assert (r.status_code, r.json()) == NOT_FOUND
    assert events(audit_stream)[before:] == []


def test_another_owners_chat_takes_no_message(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """A message or a regenerate to another owner's chat, or an unknown one, is not found and records nothing."""
    cid = _new(client, "user-b")
    before = len(events(audit_stream))
    for path in (f"/api/chats/{cid}/messages", f"/api/chats/{cid}/regenerate", "/api/chats/x/messages"):
        r = client.post(path, json={"content": "Hallo"}, headers=as_user())
        assert (r.status_code, r.json()) == NOT_FOUND
    assert events(audit_stream)[before:] == [] and fake_openai.calls == []
    assert not _state(client).turns.running(cid)


@pytest.mark.parametrize("path", ["messages", "regenerate", "stop"])
def test_every_message_route_needs_an_identity(client: TestClient, path: str) -> None:
    """Without a trusted X-Auth-User the routes are refused."""
    cid = _new(client)
    assert client.post(f"/api/chats/{cid}/{path}", json={"content": "Hallo"}).status_code == 401


# --- regenerate --------------------------------------------------------------


def test_regenerate_replaces_the_last_answer(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """The last answer goes with its steps and results; the question is answered again; the event names the old id."""
    cid = _new(client)
    fake_openai.script = [_read_call(cid), ANSWER]
    first = _frames(_send(client, cid))[-1].data
    store = _state(client).store
    assert store.chat_counts(cid) == {"messages": 2, "steps": 1, "results": 1, "attachments": 0}
    fake_openai.script = [[text_chunk("Summe: 43")]]
    frames = _frames(client.post(f"/api/chats/{cid}/regenerate", headers=as_user()))
    assert [f.event for f in frames] == ["token", "final"]
    final = frames[-1].data
    assert final["user_message_id"] is None and final["message_id"] != first["message_id"]
    messages = _detail(client, cid)["messages"]
    assert [(m["role"], m["id"]) for m in messages] == [
        ("user", first["user_message_id"]),
        ("assistant", final["message_id"]),
    ]
    assert messages[1]["content"] == "Summe: 43"
    assert store.chat_counts(cid) == {"messages": 2, "steps": 0, "results": 0, "attachments": 0}
    assert fake_openai.calls[-1]["messages"][1:] == [{"role": "user", "content": "Summe der Spalte B"}]
    [regenerated] = [e for e in events(audit_stream) if e["operation"] == "chat.regenerate"]
    assert (regenerated["action"], regenerated["target"]) == (
        "modify",
        {"kind": "message", "ids": [first["message_id"]], "container": cid},
    )
    children = [e for e in events(audit_stream) if e.get("parent_event_id") == regenerated["event_id"]]
    assert [(e["action"], e["operation"]) for e in children] == [("ai_call", "chat.answer")]


def test_regenerate_needs_an_answer(client: TestClient, audit_stream: CapturedAuditStream) -> None:
    """A chat without an answer has nothing to regenerate; the chat is left free."""
    cid = _new(client)
    before = len(events(audit_stream))
    r = client.post(f"/api/chats/{cid}/regenerate", json={}, headers=as_user())
    assert (r.status_code, r.json()) == (409, {"detail": "There is no answer to regenerate."})
    assert events(audit_stream)[before:] == [] and not _state(client).turns.running(cid)


# --- what a message may say ---------------------------------------------------


def test_a_message_names_only_its_chats_pending_attachments(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """Another chat's attachment, one already sent, or an unknown id is refused before anything is recorded."""
    cid, other = _new(client), _new(client)
    theirs = _attach(client, other)
    sent = _attach(client, cid, file_id="2" * 32)
    fake_openai.script = [ANSWER]
    assert _send(client, cid, attachment_ids=[sent.id]).status_code == 200
    before = len(events(audit_stream))
    for ids in ([theirs.id], [sent.id], ["a-made-up-id"]):
        r = _send(client, cid, content="Und die?", attachment_ids=ids)
        assert (r.status_code, r.json()) == (400, {"detail": "Unknown attachment."})
    assert events(audit_stream)[before:] == [] and len(_detail(client, cid)["messages"]) == 2
    assert [a["id"] for a in _detail(client, other)["pending_attachments"]] == [theirs.id]
    assert not _state(client).turns.running(cid)


@pytest.mark.parametrize(
    "body",
    [
        {"content": ""},
        {"content": " \n\t "},
        {"content": "x" * 20_001},
        {"content": "a", "attachment_ids": [f"x{n}" for n in range(11)]},
        {"content": 7},
    ],
)
def test_a_message_must_say_something_and_stay_small(
    client: TestClient, audit_stream: CapturedAuditStream, body: dict[str, Any]
) -> None:
    """Empty without an attachment, over 20 000 characters, or more than 10 distinct attachments is refused."""
    cid = _new(client)
    before = len(events(audit_stream))
    r = client.post(f"/api/chats/{cid}/messages", json=body, headers=as_user())
    assert r.status_code == 422
    assert events(audit_stream)[before:] == [] and not _state(client).turns.running(cid)


def test_a_message_of_20000_characters_is_taken_after_stripping(client: TestClient, fake_openai: FakeOpenAI) -> None:
    """The limit counts the stripped text."""
    cid = _new(client)
    fake_openai.script = [ANSWER]
    assert _send(client, cid, content=f"  {'x' * 20_000}\n").status_code == 200
    assert _detail(client, cid)["messages"][0]["content"] == "x" * 20_000


def test_duplicate_attachment_ids_count_once(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """Eleven times the same id is one attachment, bound once; a message may be empty beside it."""
    cid = _new(client)
    pending = _attach(client, cid)
    fake_openai.script = [ANSWER]
    assert _send(client, cid, content="", attachment_ids=[pending.id] * 11).status_code == 200
    [question, _] = _detail(client, cid)["messages"]
    assert [a["id"] for a in question["attachments"]] == [pending.id]
    [primary] = [e for e in events(audit_stream) if e["operation"] == "chat.message"]
    assert primary["criteria"] == {"attachments": 1, "chars": 0}


# --- the audit ----------------------------------------------------------------


def test_the_turn_is_recorded_with_its_children(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """The primary event, then each model round and each disclosure as its children; references only."""
    cid = _new(client)
    sales = _attach(client, cid)
    fake_openai.script = [_read_call(cid), ANSWER]
    final = _frames(_send(client, cid, attachment_ids=[sales.id]))[-1].data
    written = events(audit_stream)[-4:]
    ops = [(e["action"], e["operation"]) for e in written]
    assert ops == [
        ("collect", "chat.message"),
        ("ai_call", "chat.answer"),
        ("disclose", "chat.tool"),
        ("ai_call", "chat.answer"),
    ]
    primary, first, disclosed, second = written
    assert {e["parent_event_id"] for e in written[1:]} == {primary["event_id"]}
    assert {e["outcome"] for e in written} == {"ok"}
    assert primary["target"] == {"kind": "message", "ids": [final["user_message_id"]], "container": cid}
    assert primary["criteria"] == {"attachments": 1, "chars": 18}
    assert disclosed["recipient"] == "workbench:read_document"
    assert disclosed["target"] == {"kind": "message", "ids": [final["message_id"]], "container": cid}
    assert disclosed["criteria"] == {"tool": "read_document", "round": 1, "call_id": "call_0", "attempt": 1}
    assert first["recipient"] == "inference:chat" and "target" not in first
    assert first["ai"] == {
        "model_alias": "chat-model",
        "model_id": "served-model",
        "output_sha256": sha256_text(""),
        "input_tokens": 900,
        "output_tokens": 20,
    }
    assert first["criteria"] == {
        "round": 1,
        "tool_calls": 1,
        "finish_reason": None,
        "stopped": False,
        "reasoning": False,
    }
    assert second["ai"]["output_sha256"] == sha256_text("Summe: 42")
    assert (second["criteria"]["round"], second["criteria"]["tool_calls"]) == (2, 0)
    text = audit_stream.getvalue()
    for content in ("Summe der Spalte B", "sales.xlsx", "Summe: 42", chat_folder(KEY, cid)):
        assert content not in text


def test_a_turn_whose_event_cannot_be_written_does_not_happen(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """With the audit stream broken, a message stores nothing and a regenerate keeps the old answer."""
    cid = _new(client)
    fake_openai.script = [ANSWER]
    answer = _frames(_send(client, cid))[-1].data["message_id"]
    audit_stream.failing = True
    for path in ("messages", "regenerate"):
        r = client.post(f"/api/chats/{cid}/{path}", json={"content": "Und im Vorjahr?"}, headers=as_user())
        assert (r.status_code, r.json()) == (500, {"detail": "Request failed."})
    audit_stream.failing = False
    messages = _detail(client, cid)["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"] and messages[1]["id"] == answer
    assert len(fake_openai.calls) == 1 and not _state(client).turns.running(cid)


class _DeletedBeforeTheClaim(TurnRegistry):
    """A turn registry that runs ``first`` once, just before the next claim, as a delete that won the race."""

    def __init__(self, first: Callable[[], None]) -> None:
        super().__init__()
        self._first: Callable[[], None] | None = first

    @override
    def begin(self, chat_id: str) -> threading.Event | None:
        first, self._first = self._first, None
        if first is not None:
            first()
        return super().begin(chat_id)


def test_a_chat_deleted_before_the_claim_takes_no_message(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """A message past its lookup when a delete ended finds the chat gone once it holds the turn: 404, unrecorded."""
    cid = _new(client)
    state = _state(client)
    deleted: list[int] = []
    state.turns = _DeletedBeforeTheClaim(
        lambda: deleted.append(client.delete(f"/api/chats/{cid}", headers=as_user()).status_code)
    )
    r = _send(client, cid)
    assert deleted == [204] and (r.status_code, r.json()) == NOT_FOUND
    assert [e for e in events(audit_stream) if e["operation"] == "chat.message"] == []
    assert fake_openai.calls == [] and not state.turns.running(cid)


class _Agent:
    """The loop as a turn stream drives it: each run is recorded and yields the scripted events."""

    def __init__(self, *scripted: TurnEvent) -> None:
        self.scripted = scripted
        self.ran: list[Turn] = []

    def run(self, turn: Turn, audit: TurnAudit) -> Iterator[TurnEvent]:
        self.ran.append(turn)
        yield from self.scripted


def _turn_stream(turns: TurnRegistry, claim: threading.Event, agent: _Agent) -> TurnStream:
    turn = Turn("chat-1", "user-a", "q-1", "a-1", CallFor("user-a", "chat-1", {}), "req-0001", claim)
    now = utcnow()
    chat = ChatOut(id="chat-1", title=None, created_at=now, updated_at=now, expires_at=None)
    return TurnStream(
        agent=cast(AgentLoop, agent),
        turn=turn,
        audit=cast(TurnAudit, None),
        turns=turns,
        chat=chat,
        user_message_id="q-1",
    )


def test_a_turn_closed_before_its_thread_starts_never_runs() -> None:
    """A response that ended before the worker began ends the turn; the late worker runs nothing, frees nothing."""
    turns = TurnRegistry()
    claim = turns.begin("chat-1")
    assert claim is not None
    agent = _Agent()
    stream = _turn_stream(turns, claim, agent)
    stream.close()
    assert claim.is_set() and not turns.running("chat-1")
    assert turns.begin("chat-1") is not None
    assert list(stream.frames()) == [] and agent.ran == [] and turns.running("chat-1")


def test_the_turn_has_ended_when_its_final_frame_goes_out() -> None:
    """The worker frees the chat before it hands over ``final``, not after."""
    turns = TurnRegistry()
    claim = turns.begin("chat-1")
    assert claim is not None
    frames = _turn_stream(turns, claim, _Agent(TokenEvent("Hallo"))).frames()
    assert next(frames).startswith("event: token\n") and turns.running("chat-1")
    assert next(frames).startswith("event: final\n") and not turns.running("chat-1")


def test_a_worker_cut_off_mid_stream_still_ends_its_turn() -> None:
    """A frames generator closed before ``final`` frees the chat all the same."""
    turns = TurnRegistry()
    claim = turns.begin("chat-1")
    assert claim is not None
    frames = _turn_stream(turns, claim, _Agent(TokenEvent("Hallo"), TokenEvent(" Welt"))).frames()
    assert isinstance(frames, Generator) and next(frames).startswith("event: token\n")
    frames.close()
    assert not turns.running("chat-1")


def test_a_required_purpose_refuses_before_anything_is_stored(
    tmp_path: Path, audit_stream: CapturedAuditStream, door: FakeDoor, fake_openai: FakeOpenAI
) -> None:
    """A turn discloses to workbench, so a required ``disclose`` purpose refuses it with 428 until one is stated."""
    with TestClient(_app(tmp_path, audit_stream, door, fake_openai, require_purpose=frozenset({"disclose"}))) as client:
        cid = _new(client)
        for path in ("messages", "regenerate"):
            r = client.post(f"/api/chats/{cid}/{path}", json={"content": "Summe der Spalte B"}, headers=as_user())
            assert r.status_code == 428
        assert _detail(client, cid)["messages"] == [] and fake_openai.calls == [] and door.requests == []
        denied = [(e["operation"], e["outcome"]) for e in events(audit_stream) if e["outcome"] == "denied"]
        assert denied == [("chat.message", "denied"), ("chat.regenerate", "denied")]
        fake_openai.script = [ANSWER]
        stated = as_user() | {"X-Audit-Purpose": encode_purpose("case-0001", "review")}
        assert _send(client, cid, headers=stated).status_code == 200


def test_workbench_hears_the_purpose_or_ghostwriter_chat(
    client: TestClient, fake_openai: FakeOpenAI, door: FakeDoor
) -> None:
    """A stated purpose reaches workbench's tools/call; without one, the reason is ``ghostwriter chat``."""
    cid = _new(client)
    stated = encode_purpose("case-0001", "review")
    for headers, heard in [
        (as_user() | {"X-Audit-Purpose": stated}, stated),
        (as_user(), encode_purpose(None, "ghostwriter chat")),
    ]:
        fake_openai.script = [_read_call(cid), ANSWER]
        assert _send(client, cid, headers=headers).status_code == 200
        [*_, call] = [r for r, body in door.requests if isinstance(body, dict) and body["method"] == "tools/call"]
        assert call.headers["x-audit-purpose"] == heard


def test_an_audit_failure_mid_turn_ends_with_an_error_frame(
    client: TestClient, fake_openai: FakeOpenAI, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """The round's record cannot be written: the audit error frame, then final; workbench hears nothing."""
    cid = _new(client)
    fake_openai.script = [_read_call(cid), ANSWER]
    audit_stream.writes_left = 1
    frames = _frames(_send(client, cid))
    audit_stream.writes_left = None
    assert [f.event for f in frames] == ["error", "final"] and frames[0].data == AUDIT_ERROR
    assert "tools/call" not in door.methods() and len(fake_openai.calls) == 1
    assert not _state(client).turns.running(cid)


def test_a_disclosure_that_cannot_be_recorded_ends_its_pending_step(
    client: TestClient, fake_openai: FakeOpenAI, door: FakeDoor, audit_stream: CapturedAuditStream
) -> None:
    """The running step's disclosure fails: after the audit error frame, that step's own frame ends it as error."""
    cid = _new(client)
    fake_openai.script = [_read_call(cid), ANSWER]
    audit_stream.writes_left = 2
    frames = _frames(_send(client, cid))
    audit_stream.writes_left = None
    assert [f.event for f in frames] == ["step", "error", "step", "final"]
    running, error, ended, _ = (f.data for f in frames)
    assert running["status"] == "running" and ended == {**running, "status": "error"}
    assert error == AUDIT_ERROR and "tools/call" not in door.methods()
    assert _detail(client, cid)["messages"][-1]["steps"] == []


# --- reasoning, threads and previews (controller's addendum) --------------------


def test_a_reasoning_message_thinks_in_every_round_and_the_thinking_goes_nowhere(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """The switch reaches every round and the records; the thinking reaches no frame, store or event."""
    cid = _new(client)
    fake_openai.script = [
        [reasoning_chunk("Geheime Überlegung"), *_read_call(cid)],
        [text_chunk("Grübeln über B"), text_chunk("</think>"), text_chunk("Summe: 42")],
    ]
    response = _send(client, cid, reasoning=True)
    assert [f.data["text"] for f in _frames(response) if f.event == "token"] == ["Summe: 42"]
    assert [call["extra_body"] for call in fake_openai.calls] == [THINKING_ON, THINKING_ON]
    stored = client.get(f"/api/chats/{cid}", headers=as_user()).text
    for thinking in ("Geheime", "Grübeln"):
        assert thinking not in response.text and thinking not in stored and thinking not in audit_stream.getvalue()
    fake_openai.script = [[text_chunk("Summe: 42")]]
    assert client.post(f"/api/chats/{cid}/regenerate", json={"reasoning": True}, headers=as_user()).status_code == 200
    fake_openai.script = [[text_chunk("Summe: 42")]]
    assert client.post(f"/api/chats/{cid}/regenerate", headers=as_user()).status_code == 200
    assert [call["extra_body"] for call in fake_openai.calls[2:]] == [THINKING_ON, THINKING_OFF]
    answers = [e["criteria"]["reasoning"] for e in events(audit_stream) if e["operation"] == "chat.answer"]
    assert answers == [True, True, True, False]


def test_turns_wait_for_their_own_threads_and_leave_the_shared_pool_free(
    client: TestClient, fake_openai: FakeOpenAI
) -> None:
    """With every turn thread taken, a message waits and is never refused; health and the chats still answer."""
    cid = _new(client)
    limiter = _state(client).turn_threads
    assert isinstance(limiter, CapacityLimiter) and limiter.total_tokens == TURN_THREADS == 16
    borrowers = _take_all(client, limiter)
    try:
        fake_openai.script = [ANSWER]
        answered: list[httpx2.Response] = []
        waiting = threading.Thread(target=lambda: answered.append(_send(client, cid)))
        waiting.start()
        _wait_until(lambda: _state(client).turns.running(cid))
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/api/chats", headers=as_user()).status_code == 200
        time.sleep(0.2)
        assert waiting.is_alive() and fake_openai.calls == []
        _give_back(client, limiter, borrowers[:1])
        borrowers.pop(0)
        waiting.join(5)
        assert [f.event for f in _frames(answered[0])] == ["token", "final"]
    finally:
        _give_back(client, limiter, borrowers)


def test_a_turn_waiting_for_a_thread_frees_its_chat_when_the_tab_closes(
    client: TestClient, fake_openai: FakeOpenAI
) -> None:
    """A tab closed before the turn got a thread ends the turn at once; it never runs."""
    cid = _new(client)
    limiter = _state(client).turn_threads
    turns = _state(client).turns
    borrowers = _take_all(client, limiter)
    try:
        with client.stream("POST", f"/api/chats/{cid}/messages", json={"content": "Hallo"}, headers=as_user()) as r:
            assert r.status_code == 200 and turns.running(cid)
        _wait_until(lambda: not turns.running(cid))
    finally:
        _give_back(client, limiter, borrowers)
    time.sleep(0.2)
    assert fake_openai.calls == []
    fake_openai.script = [ANSWER]
    assert _send(client, cid, content="Noch da?").status_code == 200


def test_a_stop_ends_a_turn_still_waiting_for_a_thread(client: TestClient, fake_openai: FakeOpenAI) -> None:
    """With every turn thread taken, a stop gives ``final``, stopped, at once and frees the chat; no round runs."""
    cid = _new(client)
    limiter = _state(client).turn_threads
    turns = _state(client).turns
    borrowers = _take_all(client, limiter)
    try:
        with client.stream("POST", f"/api/chats/{cid}/messages", json={"content": "Hallo"}, headers=as_user()) as r:
            assert r.status_code == 200 and turns.running(cid)
            assert client.post(f"/api/chats/{cid}/stop", headers=as_user()).status_code == 204
            _wait_until(lambda: not turns.running(cid), timeout_s=2.0)
            frames = list(_read(r.iter_lines()))
        assert [f.event for f in frames] == ["final"] and frames[0].data["stopped"] is True
        assert frames[0].data["message_id"] == _detail(client, cid)["messages"][-1]["id"]
    finally:
        _give_back(client, limiter, borrowers)
    time.sleep(0.2)
    assert fake_openai.calls == []
    fake_openai.script = [ANSWER]
    assert _send(client, cid, content="Noch da?").status_code == 200


def test_a_regenerated_answer_moves_its_chat_to_the_top(client: TestClient, fake_openai: FakeOpenAI) -> None:
    """Regenerating is activity, as a new message is: the chat list orders by it."""
    first, second = _new(client), _new(client)
    for cid in (first, second):
        fake_openai.script = [ANSWER]
        assert _send(client, cid).status_code == 200
    listed = client.get("/api/chats", headers=as_user()).json()
    assert [c["id"] for c in listed] == [second, first]
    fake_openai.script = [[text_chunk("Summe: 43")]]
    final = _frames(client.post(f"/api/chats/{first}/regenerate", headers=as_user()))[-1].data
    listed = client.get("/api/chats", headers=as_user()).json()
    assert [c["id"] for c in listed] == [first, second]
    assert final["chat"] == listed[0]


def test_a_fresh_table_may_come_without_its_preview(client: TestClient, fake_openai: FakeOpenAI) -> None:
    """When the chat's earliest file is past the file period, a new table's card has no preview and is live."""
    cid = _new(client)
    engine = make_engine(_state(client).settings.db_path)
    with Session(engine) as s, s.begin():
        s.execute(update(Chat).where(Chat.id == cid).values(first_file_at=utcnow() - timedelta(days=8)))
    engine.dispose()
    fake_openai.script = [_read_call(cid), ANSWER]
    [result] = [f.data for f in _frames(_send(client, cid)) if f.event == "result"]
    assert (result["kind"], result["preview"], result["expired"]) == ("table", None, False)
