"""Messages: the answer as an SSE stream, one turn per chat, stop and regenerate, and the turn's audit.

Every name, id and value here is invented.
"""

import base64
import json
import threading
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import anyio
import httpx
import httpx2
import openai
import pytest
from anyio import CapacityLimiter
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger
from sqlalchemy import update
from sqlalchemy.orm import Session
from typing_extensions import override

from ghostwriter.agent.limits import TURN_THREADS
from ghostwriter.agent.loop import AgentLoop, TokenEvent, Turn, TurnAudit, TurnEvent, TurnOutcome
from ghostwriter.agent.notices import NoticeEvent
from ghostwriter.api.messages import TurnStream, _frame
from ghostwriter.db import make_engine, utcnow
from ghostwriter.inference import ModelClient
from ghostwriter.main import create_app
from ghostwriter.models import FileMark, Message
from ghostwriter.schemas import AttachmentOut, ChatOut
from ghostwriter.sse import sse_frame
from ghostwriter.turns import TurnRegistry
from ghostwriter.utils.audit_event import sha256_text
from ghostwriter.utils.audit_runtime import encode_purpose
from ghostwriter.workbench import CallFor, chat_folder
from tests.conftest import KEY, CapturedAuditStream, as_user, events, make_settings
from tests.fakes import (
    CONTRACT_TOOLS,
    READ_DOCUMENT_TEXT,
    TINY_JPEG,
    UPLOAD_ID,
    XLSX,
    Chunk,
    FakeDoor,
    FakeOpenAI,
    Round,
    call_chunk,
    image_block,
    reasoning_chunk,
    stock_manifest,
    text_chunk,
    usage_chunk,
    view_result,
)

ANSWER: Round = [text_chunk("Summe: 42"), usage_chunk(950, 5, "served-model")]
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
RUNNING = (409, {"detail": "An answer is still being written in this chat.", "code": "busy"})
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


def _all_frames(response: httpx2.Response) -> list[Frame]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return list(_read(iter(response.text.split("\n"))))


def _frames(response: httpx2.Response) -> list[Frame]:
    """Return the frames after ``start``, which every stream sends first."""
    frames = _all_frames(response)
    assert frames[0].event == "start", frames
    return frames[1:]


def _read_after_start(lines: Iterator[str]) -> Iterator[Frame]:
    """Read a stream's frames as they come, past its ``start``."""
    frames = _read(lines)
    assert next(frames).event == "start"
    return frames


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


def test_final_carries_the_estimate(client: TestClient, fake_openai: FakeOpenAI) -> None:
    """The final frame names the estimate of the next prompt: the number the answer was stored with."""
    cid = _new(client)
    fake_openai.script = [_read_call(cid), ANSWER]
    final = _frames(_send(client, cid))[-1].data
    estimate = final["context_estimate"]
    assert isinstance(estimate, int) and not isinstance(estimate, bool) and estimate > 0
    first = _detail(client, cid)["messages"][-1]
    assert (first["id"], first["context_estimate"]) == (final["message_id"], estimate)
    fake_openai.script = [ANSWER]
    later = _frames(_send(client, cid, content="Und noch eine Frage?"))[-1].data
    assert later["context_estimate"] > estimate
    assert later["context_estimate"] == _detail(client, cid)["messages"][-1]["context_estimate"]
    fake_openai.script = [ANSWER]
    regenerated = _frames(client.post(f"/api/chats/{cid}/regenerate", headers=as_user()))[-1].data
    assert regenerated["context_estimate"] == _detail(client, cid)["messages"][-1]["context_estimate"]
    assert regenerated["context_estimate"] == later["context_estimate"]


def test_the_final_frame_carries_what_the_loop_estimated() -> None:
    """The frame holds the turn's own outcome, ``null`` where the turn says none."""
    for estimate, expected in ((1234, 1234), (None, None)):
        turns = TurnRegistry()
        claim = turns.begin("chat-1")
        assert claim is not None
        frames = list(_turn_stream(turns, claim, _Agent(TokenEvent("Hallo"), estimate=estimate)).frames())
        final = json.loads(frames[-1].split("data: ", 1)[1])
        assert final["context_estimate"] == expected and "context_estimate" in final


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
    assert set(final) == {"message_id", "user_message_id", "chat", "stopped", "context_estimate"}
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
        frames = _read_after_start(response.iter_lines())
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
        frames = _read_after_start(response.iter_lines())
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
        first = next(_read_after_start(response.iter_lines()))
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
        frames = _read_after_start(response.iter_lines())
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
    assert (r.status_code, r.json()) == (409, {"detail": "There is no answer to regenerate.", "code": "no_answer"})
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


def test_a_message_waits_for_its_attachments(
    client: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """An attachment still being prepared holds the message back unrecorded and unstored; a failed one is taken."""
    cid = _new(client)
    store = _state(client).store
    photo = store.add_attachment(
        cid,
        file_id=UPLOAD_ID,
        path=f"{chat_folder(KEY, cid)}/harbour-photo.jpg",
        name="harbour-photo.jpg",
        size=64,
        mime="image/jpeg",
        kind="image",
        status="processing",
    )
    clip = store.add_attachment(
        cid,
        file_id="3" * 32,
        path=f"{chat_folder(KEY, cid)}/harbour-tour.mp4",
        name="harbour-tour.mp4",
        size=208,
        mime="video/mp4",
        kind="media",
        status="processing",
    )
    sheet = _attach(client, cid, file_id="2" * 32)
    before = len(events(audit_stream))
    for waiting in ([sheet.id, photo.id, clip.id], [sheet.id, clip.id]):
        r = _send(client, cid, attachment_ids=waiting)
        assert (r.status_code, r.json()) == (
            409,
            {"detail": "Wait until the attached files are ready.", "code": "attachment_processing"},
        )
    assert events(audit_stream)[before:] == [] and fake_openai.calls == []
    assert not _state(client).turns.running(cid)
    assert _detail(client, cid)["messages"] == []

    assert store.settle_attachment(photo.id, status="failed", manifest=None, failure="unreadable_file")
    assert store.settle_attachment(clip.id, status="failed", manifest=None, failure="nextext_failed")
    fake_openai.script = [ANSWER]
    assert _send(client, cid, attachment_ids=[sheet.id, photo.id, clip.id]).status_code == 200
    [question, _] = _detail(client, cid)["messages"]
    assert sorted(a["id"] for a in question["attachments"]) == sorted([sheet.id, photo.id, clip.id])


def _prompt_users(call: dict[str, Any]) -> list[str]:
    """Return the text of each user message a model call carried, in order."""
    texts: list[str] = []
    for message in call["messages"]:
        if message["role"] != "user":
            continue
        content = message["content"]
        texts.append(
            content
            if isinstance(content, str)
            else "\n".join(part["text"] for part in content if part.get("type") == "text")
        )
    return texts


def test_a_message_sends_while_its_deck_is_prepared(client: TestClient, fake_openai: FakeOpenAI) -> None:
    """A deck is ready at once: its message streams, its line has no facts until its manifest is kept, then slides."""
    cid = _new(client)
    store = _state(client).store
    deck = store.add_attachment(
        cid,
        file_id=UPLOAD_ID,
        path=f"{chat_folder(KEY, cid)}/harbour-deck.pptx",
        name="harbour-deck.pptx",
        size=48_213,
        mime=PPTX,
        kind="document",
        status="ready",
    )
    assert deck.status == "ready"
    line = f"[Attached: harbour-deck.pptx ({chat_folder(KEY, cid)}/harbour-deck.pptx, 48213 bytes)]"
    fake_openai.script = [ANSWER, ANSWER]
    response = _send(client, cid, content="Worum geht es?", attachment_ids=[deck.id])
    assert response.status_code == 200
    assert _frames(response)[-1].event == "final"
    assert _prompt_users(fake_openai.calls[0]) == [f"Worum geht es?\n{line}"]

    assert store.keep_manifest(deck.id, stock_manifest("harbour-deck.pptx"))
    assert _send(client, cid, content="Und die Folien?").status_code == 200
    facts = f"{line[:-2]}; PowerPoint, 3 slides; slide 2 has no text)]"
    assert _prompt_users(fake_openai.calls[1]) == [f"Worum geht es?\n{facts}", "Und die Folien?"]


class _HeldDoor(FakeDoor):
    """A door whose prepare of a deck waits until ``release`` is set, as workbench's does while it converts.

    Attributes:
        reached: Set once a prepare of a deck is in flight, held.
        release: Set by the test to let the held prepare go on.
    """

    def __init__(self) -> None:
        """Answer as a healthy workbench does, but hold the prepare of a ``.pptx``."""
        super().__init__()
        self.reached = threading.Event()
        self.release = threading.Event()

    @override
    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Hold a deck's prepare open, then answer every request as :class:`FakeDoor` does.

        Args:
            request: The request the client sent.

        Returns:
            The answer.
        """
        if request.url.path == "/files/prepare" and request.url.params.get("path", "").endswith(".pptx"):
            self.reached.set()
            while not self.release.is_set():
                await anyio.sleep(0.01)
        return await super().__call__(request)


@pytest.fixture
def held_door() -> Iterator[_HeldDoor]:
    """Return a door that holds a deck's prepare, and let it go when the test ends, so no shutdown waits for it."""
    held = _HeldDoor()
    yield held
    held.release.set()


def test_a_deck_attached_through_the_route_is_seen_once_its_prepare_ends(
    tmp_path: Path, audit_stream: CapturedAuditStream, held_door: _HeldDoor, fake_openai: FakeOpenAI
) -> None:
    """A message sends while the deck's prepare is held open, with no facts; once it ends, the next names the slides."""
    with TestClient(_app(tmp_path, audit_stream, held_door, fake_openai, vision=True)) as seeing_held:
        cid = _new(seeing_held)
        files = {"file": ("harbour-deck.pptx", b"PK", "application/octet-stream")}
        uploaded = seeing_held.post(f"/api/chats/{cid}/attachments", files=files, headers=as_user())
        assert uploaded.status_code == 201, uploaded.text
        deck = uploaded.json()
        assert (deck["kind"], deck["status"]) == ("document", "ready")
        _wait_until(held_door.reached.is_set)
        processor = _state(seeing_held).processor
        assert processor.running(deck["id"])
        path = f"{chat_folder(KEY, cid)}/harbour-deck.pptx"
        line = f"[Attached: harbour-deck.pptx ({path}, 2 bytes)]"

        fake_openai.script = [ANSWER, ANSWER]
        first = _send(seeing_held, cid, content="Worum geht es?", attachment_ids=[deck["id"]])
        assert first.status_code == 200
        assert _frames(first)[-1].event == "final"
        assert _prompt_users(fake_openai.calls[0]) == [f"Worum geht es?\n{line}"]
        assert processor.running(deck["id"])
        found = _state(seeing_held).store.find_attachment("user-a", cid, deck["id"])
        assert found is not None and found.manifest is None

        held_door.release.set()
        _wait_until(lambda: not processor.running(deck["id"]))
        found = _state(seeing_held).store.find_attachment("user-a", cid, deck["id"])
        assert found is not None and found.status == "ready" and found.manifest is not None
        second = _send(seeing_held, cid, content="Und die Folien?")
        assert second.status_code == 200
        assert _frames(second)[-1].event == "final"
        facts = f"[Attached: harbour-deck.pptx ({path}, 2 bytes; PowerPoint, 3 slides; slide 2 has no text)]"
        assert _prompt_users(fake_openai.calls[1]) == [f"Worum geht es?\n{facts}", "Und die Folien?"]
        prepares = [r for r, _ in held_door.requests if r.url.path == "/files/prepare"]
        assert len(prepares) == 1


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
        "images": 0,
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

    def __init__(self, *scripted: TurnEvent, estimate: int | None = None) -> None:
        self.scripted = scripted
        self.estimate = estimate
        self.ran: list[Turn] = []

    def run(self, turn: Turn, audit: TurnAudit, *, outcome: TurnOutcome | None = None) -> Iterator[TurnEvent]:
        self.ran.append(turn)
        if outcome is not None:
            outcome.context_estimate = self.estimate
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
            frames = list(_read_after_start(r.iter_lines()))
        assert [f.event for f in frames] == ["final"] and frames[0].data["stopped"] is True
        assert frames[0].data["message_id"] == _detail(client, cid)["messages"][-1]["id"]
        assert frames[0].data["context_estimate"] is None
    finally:
        _give_back(client, limiter, borrowers)
    time.sleep(0.2)
    assert fake_openai.calls == []
    fake_openai.script = [ANSWER]
    assert _send(client, cid, content="Noch da?").status_code == 200


def test_the_first_frame_names_the_rows_the_turn_stored(client: TestClient, fake_openai: FakeOpenAI) -> None:
    """``start`` comes first, naming the stored question and answer as ``final`` does.

    A regenerate names no question.
    """
    cid = _new(client)
    fake_openai.script = [ANSWER]
    frames = _all_frames(_send(client, cid))
    assert [f.event for f in frames] == ["start", "token", "final"]
    start, final = frames[0].data, frames[-1].data
    assert start == {"message_id": final["message_id"], "user_message_id": final["user_message_id"]}
    assert [m["id"] for m in _detail(client, cid)["messages"]] == [start["user_message_id"], start["message_id"]]
    fake_openai.script = [ANSWER]
    frames = _all_frames(client.post(f"/api/chats/{cid}/regenerate", headers=as_user()))
    assert frames[0] == Frame("start", {"message_id": frames[-1].data["message_id"], "user_message_id": None})


def test_a_quiet_turn_keeps_its_stream_open_with_comments(
    client: TestClient, fake_openai: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While nothing is written, a comment line goes out every interval; it is no frame."""
    monkeypatch.setattr("ghostwriter.sse.STREAM_DISCONNECT_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr("ghostwriter.sse.KEEPALIVE_INTERVAL_S", 0.05)
    cid = _new(client)

    def slow() -> Chunk:
        time.sleep(0.4)
        return text_chunk("Summe: 42")

    fake_openai.script = [[slow, usage_chunk(950, 5, "served-model")]]
    response = _send(client, cid)
    lines = response.text.split("\n")
    assert lines.count(": keep-alive") >= 2
    assert all(line in ("", ": keep-alive") or line.startswith(("event: ", "data: ")) for line in lines)
    assert [f.event for f in _frames(response)] == ["token", "final"]


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


def test_a_fresh_table_keeps_its_preview_in_a_chat_older_than_the_file_period(
    client: TestClient, fake_openai: FakeOpenAI
) -> None:
    """The chat's first file was 10 days ago and is gone: a table written today has its preview, live."""
    cid = _new(client)
    engine = make_engine(_state(client).settings.db_path)
    with Session(engine) as s, s.begin():
        s.add(FileMark(chat_id=cid, created_at=utcnow() - timedelta(days=10)))
    engine.dispose()
    fake_openai.script = [_read_call(cid), ANSWER]
    [result] = [f.data for f in _frames(_send(client, cid)) if f.event == "result"]
    assert (result["kind"], result["expired"]) == ("table", False)
    assert result["preview"] is not None


# --- images: a refused image round, which calls ask for images, where data URLs never go ---

PHOTO = "harbour-photo.jpg"


@pytest.fixture
def seeing(
    tmp_path: Path, audit_stream: CapturedAuditStream, door: FakeDoor, fake_openai: FakeOpenAI
) -> Iterator[TestClient]:
    """Serve the app with pictures offered: vision on, and a workbench that lists ``view_file``."""
    with TestClient(_app(tmp_path, audit_stream, door, fake_openai, vision=True)) as test_client:
        yield test_client


def _refused() -> openai.BadRequestError:
    """Return the router's refusal of a request with images."""
    request = httpx2.Request("POST", "http://router.test/v1/chat/completions")
    return openai.BadRequestError("image refused", response=httpx2.Response(400, request=request), body=None)


def _attach_photo(client: TestClient, cid: str) -> str:
    """Attach the harbour photo through the route, wait until it is prepared, and return its id."""
    files = {"file": (PHOTO, TINY_JPEG, "application/octet-stream")}
    r = client.post(f"/api/chats/{cid}/attachments", files=files, headers=as_user())
    assert r.status_code == 201, r.text
    attachment_id = str(r.json()["id"])
    r = client.get(f"/api/chats/{cid}/attachments/{attachment_id}", params={"wait": 20}, headers=as_user())
    assert (r.status_code, r.json()["status"]) == (200, "ready")
    return attachment_id


def _tool_calls(door: FakeDoor) -> list[httpx.Request]:
    return [request for request, body in door.requests if request.url.path == "/mcp" and body["method"] == "tools/call"]


def _view_page(folder: str) -> dict[str, Any]:
    """Return workbench's answer to a view of the scan's first page."""
    shown = [{"n": 1, "label": "quay-scan.pdf, page 1", "width": 1131, "height": 1600}]
    return view_result(folder, shown, [image_block(TINY_JPEG)])


def test_a_refused_image_round_streams_a_notice(
    seeing: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """A model that refuses the picture gets the round again without it; the stream says so before any text.

    Each try is its own ``chat.answer`` record, naming how many images it sent.
    """
    cid = _new(seeing)
    photo = _attach_photo(seeing, cid)
    fake_openai.script = [_refused(), ANSWER]
    frames = _frames(_send(seeing, cid, "Was zeigt das Bild?", attachment_ids=[photo]))
    assert [frame.event for frame in frames] == ["notice", "token", "final"]
    assert frames[0].data == {"code": "images_dropped"}
    answers = [e for e in events(audit_stream) if e["operation"] == "chat.answer"]
    assert [(e["outcome"], e["criteria"]["round"], e["criteria"]["images"]) for e in answers] == [
        ("error", 1, 1),
        ("ok", 1, 0),
    ]


def test_notices_are_stored_with_their_answer(
    seeing: TestClient, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """The notice the stream told is the answer's when the chat is read again; the question has none."""
    cid = _new(seeing)
    photo = _attach_photo(seeing, cid)
    fake_openai.script = [_refused(), ANSWER]
    frames = _frames(_send(seeing, cid, "Was zeigt das Bild?", attachment_ids=[photo]))
    assert [frame.event for frame in frames] == ["notice", "token", "final"]
    r = seeing.get(f"/api/chats/{cid}", headers=as_user())
    assert r.status_code == 200, r.text
    question, answer = r.json()["messages"]
    assert (question["notices"], question["context_estimate"]) == (None, None)
    assert answer["notices"] == [
        {"code": "images_dropped", "file_id": None, "name": None, "read": None, "total": None, "unit": None}
    ]


def test_an_answer_from_before_notices_is_served_as_before(client: TestClient, fake_openai: FakeOpenAI) -> None:
    """An answer 0.6 stored has ``NULL`` notices and estimate: the chat serves both as ``null``, the text unchanged."""
    cid = _new(client)
    fake_openai.script = [ANSWER]
    _frames(_send(client, cid))
    engine = make_engine(_state(client).settings.db_path)
    with Session(engine) as s, s.begin():
        s.execute(update(Message).where(Message.chat_id == cid).values(notices=None, context_estimate=None))
    engine.dispose()
    r = client.get(f"/api/chats/{cid}", headers=as_user())
    assert r.status_code == 200, r.text
    question, answer = r.json()["messages"]
    assert [(m["notices"], m["context_estimate"]) for m in (question, answer)] == [(None, None), (None, None)]
    assert answer["content"] and answer["role"] == "assistant"


def test_a_regenerated_answer_reports_again(client: TestClient, door: FakeDoor, fake_openai: FakeOpenAI) -> None:
    """The first answer never opened the notes, and says so; its regenerate read them whole, and says nothing."""
    cid = _new(client)
    path = f"{chat_folder(KEY, cid)}/tide-notes.txt"
    notes = _state(client).store.add_attachment(
        cid, file_id="4" * 32, path=path, name="tide-notes.txt", size=31, mime="text/plain"
    )
    fake_openai.script = [ANSWER]
    frames = _frames(_send(client, cid, "Was steht in den Notizen?", attachment_ids=[notes.id]))
    unread = {"code": "document_unread", "file_id": "4" * 32, "name": "tide-notes.txt"}
    assert [(f.event, f.data) for f in frames[-2:-1]] == [("notice", unread)]
    assert _detail(client, cid)["messages"][1]["notices"] == [{**unread, "read": None, "total": None, "unit": None}]

    read = {
        "path": path,
        "kind": "text",
        "parts": [{"label": "lines 1-2", "text": "high tide 06:12\nlow tide 12:30", "units": [1, 2], "cut": False}],
        "total_parts": 2,
        "pages_without_text": list[int](),
        "shown": list[Any](),
        "truncated": False,
        "next_part": None,
        "hint": None,
    }
    door.answers["tools/call"] = {
        "resultType": "complete",
        "isError": False,
        "content": [{"type": "text", "text": json.dumps(read, separators=(",", ":"))}],
        "structuredContent": read,
    }
    arguments = json.dumps({"path": path})
    fake_openai.script = [[call_chunk(0, "call_0", "read_document", arguments)], ANSWER]
    frames = _frames(client.post(f"/api/chats/{cid}/regenerate", headers=as_user()))
    assert "notice" not in [f.event for f in frames]
    assert _detail(client, cid)["messages"][1]["notices"] == []


def _notice_data(event: NoticeEvent) -> dict[str, Any]:
    """Return the data of a notice's frame."""
    [frame] = _read(iter(_frame(event).split("\n")))
    assert frame.event == "notice"
    return frame.data


def test_a_notice_frame_carries_its_details() -> None:
    """A live notice carries its code and every detail it has, the name too; what is stored never holds the name."""
    partial = NoticeEvent("document_partial", file_id="f" * 32, name="long-report.pdf", read=9, total=500, unit="page")
    assert _notice_data(partial) == {
        "code": "document_partial",
        "file_id": "f" * 32,
        "name": "long-report.pdf",
        "read": 9,
        "total": 500,
        "unit": "page",
    }
    assert _notice_data(NoticeEvent("answer_cut")) == {"code": "answer_cut"}
    assert partial.stored() == {
        "code": "document_partial",
        "file_id": "f" * 32,
        "read": 9,
        "total": 500,
        "unit": "page",
    }
    unread = NoticeEvent("document_unread", file_id="e" * 32, name="harbour-survey.docx")
    assert unread.stored() == {"code": "document_unread", "file_id": "e" * 32}
    assert NoticeEvent.from_stored(partial.stored()) == NoticeEvent(
        "document_partial", file_id="f" * 32, read=9, total=500, unit="page"
    )
    assert NoticeEvent.from_stored({"code": "images_dropped"}) == NoticeEvent("images_dropped")
    assert NoticeEvent.from_stored({"code": "later_code"}) is None


@pytest.mark.parametrize(
    "value",
    [{}, {"code": 7}, {"code": "later_code", "file_id": "f" * 32}, ["code", "answer_cut"]],
    ids=["no-code", "code-not-text", "code-not-known", "not-a-mapping"],
)
def test_a_stored_notice_without_a_known_code_is_none(value: Any) -> None:
    """A stored value without a code this version knows reads as no notice at all, rather than raise."""
    assert NoticeEvent.from_stored(value) is None


@pytest.mark.parametrize(
    "details",
    [
        {"file_id": 7},
        {"read": "9"},
        {"read": True},
        {"total": -1},
        {"unit": "chapter"},
        {"unit": ["page"]},
    ],
    ids=["file-id-not-text", "read-not-a-number", "read-a-bool", "total-negative", "unit-not-known", "unit-not-text"],
)
def test_a_known_code_with_malformed_details_reads_as_the_code_alone(details: dict[str, Any]) -> None:
    """A known code keeps what it says, without details that are not this version's: no wrong number is served."""
    stored = {"code": "document_partial", "file_id": "f" * 32, "read": 9, "total": 500, "unit": "page", **details}
    assert NoticeEvent.from_stored(stored) == NoticeEvent("document_partial")


def test_a_stored_notice_never_reads_a_name_back() -> None:
    """A name is never stored; one found in a stored value is not served as the notice's."""
    stored = {"code": "document_unread", "file_id": "e" * 32, "name": "harbour-survey.docx"}
    assert NoticeEvent.from_stored(stored) == NoticeEvent("document_unread", file_id="e" * 32)


def _listings(door: FakeDoor) -> list[httpx.Request]:
    return [request for request, body in door.requests if request.url.path == "/mcp" and body["method"] == "tools/list"]


def _read_document_told(call: dict[str, Any]) -> str:
    return next(t["function"]["description"] for t in call["tools"] if t["function"]["name"] == "read_document")


def test_a_turn_asks_workbench_for_images_only_while_the_model_sees(
    client: TestClient, door: FakeDoor, fake_openai: FakeOpenAI
) -> None:
    """With vision off, neither the turn's discovery nor its calls carry ``X-Accept-Images``.

    The model is offered workbench's listing for a call without images: no view_file, 0.6's read_document text.
    """
    cid = _new(client)
    fake_openai.script = [_read_call(cid), ANSWER]
    _frames(_send(client, cid))
    [call] = _tool_calls(door)
    assert "x-accept-images" not in call.headers
    listings = _listings(door)
    assert listings and all("x-accept-images" not in request.headers for request in listings)
    offered = fake_openai.calls[0]
    assert "view_file" not in [tool["function"]["name"] for tool in offered["tools"]]
    assert _read_document_told(offered) == READ_DOCUMENT_TEXT


def test_a_seeing_turn_is_offered_the_listing_for_images(
    seeing: TestClient, door: FakeDoor, fake_openai: FakeOpenAI
) -> None:
    """With pictures offered, the turn's discovery asks for images, and the model is told read_document's image text."""
    cid = _new(seeing)
    fake_openai.script = [ANSWER]
    _frames(_send(seeing, cid))
    listings = _listings(door)
    assert listings and all(request.headers["x-accept-images"] == "1" for request in listings)
    offered = fake_openai.calls[0]
    assert "view_file" in [tool["function"]["name"] for tool in offered["tools"]]
    assert _read_document_told(offered) == CONTRACT_TOOLS["read_document"][0]


def test_no_data_url_reaches_the_log_the_store_or_the_audit(
    seeing: TestClient, door: FakeDoor, fake_openai: FakeOpenAI, audit_stream: CapturedAuditStream
) -> None:
    """An attached picture and a tool's page go to the model as data URLs, and nowhere else.

    Not into a log line at DEBUG, the audit chain, or the database file and its write-ahead log. The turn's
    call asks workbench for images.
    """
    lines: list[str] = []
    sink = logger.add(lines.append, level="DEBUG", format="{message} {extra}")
    try:
        cid = _new(seeing)
        photo = _attach_photo(seeing, cid)
        door.answers["tools/call"] = _view_page
        view = json.dumps({"path": f"{chat_folder(KEY, cid)}/quay-scan.pdf"})
        fake_openai.script = [
            [call_chunk(0, "call_0", "view_file", view), usage_chunk(900, 20, "served-model")],
            ANSWER,
        ]
        frames = _frames(_send(seeing, cid, "Was zeigt das Bild?", attachment_ids=[photo]))
    finally:
        logger.remove(sink)
    assert [frame.event for frame in frames] == ["step", "step", "token", "final"]
    [call] = _tool_calls(door)
    assert call.headers["x-accept-images"] == "1"
    encoded = base64.b64encode(TINY_JPEG).decode("ascii")
    url = f"data:image/jpeg;base64,{encoded}"
    sent = [json.dumps(call["messages"]) for call in fake_openai.calls]
    assert [text.count(url) for text in sent] == [1, 2]
    db = Path(_state(seeing).settings.db_path)
    wal = db.with_name(db.name + "-wal")
    assert db.exists() and wal.exists()
    stored = (db.read_bytes() + wal.read_bytes()).decode("latin-1")
    for where in ("\n".join(lines), audit_stream.getvalue(), stored):
        assert "data:image" not in where and encoded not in where
