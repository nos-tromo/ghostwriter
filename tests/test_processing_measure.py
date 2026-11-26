"""The measure of a document: after its prepare and out of its slots, one at a time, ``pending`` then ``measured``.

A store, a :class:`~ghostwriter.workbench.WorkbenchClient` over :class:`~tests.fakes.FakeDoor` (workbench 0.10.0) and
an emitter on :class:`~tests.conftest.CapturedAuditStream`. Every name, id and value here is invented.
"""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import anyio
import httpx
import pytest
from loguru import logger
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ghostwriter import __version__, processing
from ghostwriter.api.files import _read_after_wait
from ghostwriter.audit import Operation, RequestContext, open_emitter
from ghostwriter.config import Settings
from ghostwriter.db import init_db, make_engine
from ghostwriter.models import Attachment
from ghostwriter.processing import (
    ATTACHMENT,
    MEASURE_RECIPIENT,
    MEASURE_SLOTS,
    PREPARE_SLOTS,
    AttachmentProcessor,
    PrepareJob,
)
from ghostwriter.schemas import AttachmentOut, LengthOut
from ghostwriter.store import ChatStore
from ghostwriter.utils.audit_event import Actor, AuditEmitter, Target
from ghostwriter.workbench import WorkbenchClient, chat_folder
from tests.conftest import KEY, CapturedAuditStream, events, make_settings, unrecorded_child
from tests.fakes import TINY_JPEG, FakeDoor

#: Two lines of text, 31 characters.
NOTES = b"high tide 06:12\nlow tide 12:30\n"
SYSTEM = {"user": None, "via": "system"}


@dataclass
class Env:
    """One test's store, workbench 0.10.0, chain and processor, with one chat.

    Attributes:
        settings: The test settings.
        engine: The database.
        store: The chats.
        door: workbench.
        emitter: The audit chain.
        processor: The processor over all of them.
        chat_id: The chat the attachments go to.
    """

    settings: Settings
    engine: Engine
    store: ChatStore
    door: FakeDoor
    emitter: AuditEmitter
    processor: AttachmentProcessor
    chat_id: str


@pytest.fixture
async def env(tmp_path: Path, audit_stream: CapturedAuditStream) -> AsyncIterator[Env]:
    """Build the store, the chain on ``audit_stream``, workbench 0.10.0 behind a door and the processor."""
    settings = make_settings(tmp_path, vision=True)
    engine = make_engine(settings.db_path)
    init_db(engine)
    store = ChatStore(engine, file_days=settings.file_days, notice_days=settings.notice_days)
    emitter = open_emitter(__version__, audit_stream)
    emitter.start()
    door = FakeDoor(server_version="0.10.0")
    processor = AttachmentProcessor(store, WorkbenchClient(settings, transport=httpx.MockTransport(door)), emitter)
    try:
        yield Env(settings, engine, store, door, emitter, processor, store.create_chat("user-a").id)
    finally:
        for gate in door.gates.values():
            gate.set()
        await processor.aclose()
        emitter.close()
        engine.dispose()


def _upload_op(env: Env, attachment_id: str) -> Operation:
    """Return an upload's operation for an attachment, its primary written, as the upload route leaves it."""
    op = Operation(env.emitter, RequestContext(Actor("user-a", "edge"), None, "req-0001"), "collect", ATTACHMENT)
    op.container = env.chat_id
    op.record(target=Target(kind="attachment", ids=(attachment_id,)), criteria={"mime": "text/plain"})
    return op


def _document(
    env: Env,
    name: str = "tide-notes.txt",
    data: bytes = NOTES,
    *,
    status: Literal["ready", "processing"] = "ready",
    background: bool = False,
    requeued: bool = False,
) -> PrepareJob:
    """Store a document, its measure ``pending``, whose file the door holds, and return its job.

    Args:
        env: The test's env.
        name: The file's name.
        data: Its bytes.
        status: ``ready`` for a file nothing prepares first, ``processing`` for a PDF that is prepared first.
        background: Whether it is an Office file, ready, prepared in the background before its measure.
        requeued: Whether the job is one requeued at start, with no operation.

    Returns:
        The job: it prepares a ``processing`` row or a background one, and measures.
    """
    path = f"{chat_folder(KEY, env.chat_id)}/{name}"
    env.door.files[path] = (data, "application/octet-stream")
    out = env.store.add_attachment(
        env.chat_id,
        file_id=uuid4().hex,
        path=path,
        name=name,
        size=len(data),
        mime="application/octet-stream",
        kind="document",
        status=status,
        measure_state="pending",
    )
    op = None if requeued else _upload_op(env, out.id)
    prepare = status == "processing" or background
    return PrepareJob(
        out.id, env.chat_id, "user-a", path, "document", op, background=background, prepare=prepare, measure=True
    )


def _picture(env: Env, name: str) -> PrepareJob:
    """Store a ``processing`` picture whose file the door holds, and return its job; it has no measure."""
    path = f"{chat_folder(KEY, env.chat_id)}/{name}"
    env.door.files[path] = (TINY_JPEG, "image/jpeg")
    out = env.store.add_attachment(
        env.chat_id,
        file_id=uuid4().hex,
        path=path,
        name=name,
        size=len(TINY_JPEG),
        mime="image/jpeg",
        kind="image",
        status="processing",
    )
    return PrepareJob(out.id, env.chat_id, "user-a", path, "image", None)


def _row(env: Env, job: PrepareJob) -> tuple[str, str | None, dict[str, Any] | None, bool]:
    """Return the attachment's status, measure state and measure as stored, and whether it has a manifest."""
    with Session(env.engine) as s:
        status, state, measure, render = s.execute(
            select(Attachment.status, Attachment.measure_state, Attachment.measure, Attachment.render).where(
                Attachment.id == job.attachment_id
            )
        ).one()
        return status, state, None if measure is None else json.loads(measure), render is not None


def _requests(door: FakeDoor, path: str) -> list[httpx.Request]:
    return [request for request, _ in door.requests if request.url.path == path]


def _routes(door: FakeDoor) -> list[str]:
    """Return the file routes asked, in order, discovery and uploads left out."""
    asked = ("/files/prepare", "/files/measure")
    return [request.url.path for request, _ in door.requests if request.url.path in asked]


def _length(env: Env, job: PrepareJob) -> LengthOut | None:
    out = env.store.attachment_out("user-a", env.chat_id, job.attachment_id)
    assert out is not None
    return out.length


async def _until(condition: Callable[[], bool]) -> None:
    with anyio.fail_after(5):
        while not condition():
            await anyio.sleep(0.01)


async def _ended(processor: AttachmentProcessor, *jobs: PrepareJob) -> None:
    await _until(lambda: not any(processor.running(job.attachment_id) for job in jobs))


def _measures(audit_stream: CapturedAuditStream) -> list[dict[str, Any]]:
    return [e for e in events(audit_stream) if e.get("recipient") == MEASURE_RECIPIENT]


@dataclass
class _Settles:
    """What ``settle_measure`` was asked, and what it answered."""

    calls: list[tuple[str, Mapping[str, Any] | None, bool]]


def _spied(env: Env, monkeypatch: pytest.MonkeyPatch) -> _Settles:
    """Record every ``settle_measure`` of the store."""
    seen = _Settles([])
    settle = env.store.settle_measure

    def spy(attachment_id: str, measure: Mapping[str, Any] | None) -> bool:
        settled = settle(attachment_id, measure)
        seen.calls.append((attachment_id, measure, settled))
        return settled

    monkeypatch.setattr(env.store, "settle_measure", spy)
    return seen


def _lines(level: str = "DEBUG") -> tuple[list[tuple[str, str]], int]:
    """Start keeping the log's lines from ``level`` up, as (level, message); returns them and the sink to remove."""
    lines: list[tuple[str, str]] = []
    sink = logger.add(lambda message: lines.append((message.record["level"].name, str(message))), level=level)
    return lines, sink


async def test_a_text_file_is_measured_in_the_background(env: Env, audit_stream: CapturedAuditStream) -> None:
    """A text file is ready and pending before its job runs, then measured with workbench's numbers.

    Its hand-over to the measure is the upload's child, written before the request.
    """
    job = _document(env)
    assert (job.prepare, job.measure) == (False, True)
    assert _row(env, job) == ("ready", "pending", None, False)
    assert _length(env, job) == LengthOut(state="pending", total=None, unit=None, too_long=False)
    recorded: list[list[dict[str, Any]]] = []

    def seen(folder: str) -> None:
        recorded.append(_measures(audit_stream))

    env.door.answers["/files/measure"] = seen
    env.processor.submit(job)
    await _ended(env.processor, job)
    assert _row(env, job) == ("ready", "measured", {"total": 2, "unit": "line", "chars": 31, "capped": False}, False)
    assert _length(env, job) == LengthOut(state="measured", total=2, unit="line", too_long=False)
    assert _routes(env.door) == ["/files/measure"]
    [[handed]] = recorded
    [collect] = [e for e in events(audit_stream) if e["action"] == "collect"]
    assert (handed["action"], handed["operation"], handed["criteria"], handed["outcome"]) == (
        "disclose",
        ATTACHMENT,
        {"kind": "document"},
        "ok",
    )
    assert (handed["parent_event_id"], handed["target"]) == (
        collect["event_id"],
        {"kind": "attachment", "ids": [job.attachment_id], "container": env.chat_id},
    )
    assert _measures(audit_stream) == [handed]


async def test_a_pdf_is_measured_after_its_prepare_in_the_same_job(env: Env) -> None:
    """One job prepares the PDF, which is ready with its manifest before the measure answers, then measures it."""
    job = _document(env, "long-report.pdf", b"%PDF-1.7", status="processing")
    measured = {"kind": "pdf", "unit": "page", "total": 3, "chars": 5400, "capped": False}
    env.door.measures[job.path] = {"path": job.path, **measured}
    env.door.gates["/files/measure"] = asyncio.Event()
    env.processor.submit(job)
    await _until(lambda: bool(_requests(env.door, "/files/measure")))
    assert env.processor.running(job.attachment_id)
    assert _row(env, job) == ("ready", "pending", None, True)
    env.door.gates["/files/measure"].set()
    await _ended(env.processor, job)
    assert _routes(env.door) == ["/files/prepare", "/files/measure"]
    assert _row(env, job) == ("ready", "measured", {"total": 3, "unit": "page", "chars": 5400, "capped": False}, True)


async def test_a_wait_on_a_processing_row_ends_with_its_prepare(env: Env) -> None:
    """Send never waits for a measure: a wait on a processing PDF ends once it is prepared, not once it is measured.

    A wait once it is prepared lasts until the measure has ended.
    """
    job = _document(env, "long-report.pdf", b"%PDF-1.7", status="processing")
    env.door.gates["/files/measure"] = asyncio.Event()
    env.processor.submit(job)
    with anyio.fail_after(5):
        await env.processor.wait(job.attachment_id, 30)
    assert _row(env, job)[:2] == ("ready", "pending")
    await _until(lambda: bool(_requests(env.door, "/files/measure")))
    with anyio.fail_after(5):
        await env.processor.wait(job.attachment_id, 0.2)
    assert env.processor.running(job.attachment_id) and _row(env, job)[1] == "pending"
    env.door.gates["/files/measure"].set()
    with anyio.fail_after(5):
        await env.processor.wait(job.attachment_id, 30)
    assert _row(env, job)[1] == "measured"


async def test_a_poll_that_read_processing_never_waits_for_the_measure(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A status poll that read a PDF ``processing`` answers once it is prepared, though its measure is held.

    The poll's first read is held until the job has left its prepare and sent its measure, so its wait starts with the
    row ``ready`` and its measure ``pending``: the window between a poll's read and its wait, where the job's stage has
    moved on. The poll answers the row ready at once; it is not held for the measure, and Send is not held with it.
    """
    job = _document(env, "long-report.pdf", b"%PDF-1.7", status="processing")
    env.door.gates["/files/prepare"] = asyncio.Event()
    env.door.gates["/files/measure"] = asyncio.Event()
    read, released = threading.Event(), threading.Event()
    attachment_out = env.store.attachment_out

    def held(owner: str, chat_id: str, attachment_id: str) -> AttachmentOut | None:
        found = attachment_out(owner, chat_id, attachment_id)
        if not read.is_set():
            read.set()
            released.wait(5)
        return found

    monkeypatch.setattr(env.store, "attachment_out", held)
    env.processor.submit(job)
    poll = asyncio.create_task(
        _read_after_wait(env.store, env.processor, "user-a", env.chat_id, job.attachment_id, wait=30)
    )
    await _until(read.is_set)
    assert _row(env, job)[0] == "processing"
    env.door.gates["/files/prepare"].set()
    await _until(lambda: bool(_requests(env.door, "/files/measure")))
    released.set()
    with anyio.fail_after(5):
        found = await poll
    assert found is not None and (found.status, found.length) == (
        "ready",
        LengthOut(state="pending", total=None, unit=None, too_long=False),
    )
    assert env.processor.running(job.attachment_id)


async def test_an_office_file_is_measured_after_its_background_prepare(env: Env) -> None:
    """A deck's job takes the background slot for its prepare and leaves it before it measures: no deadlock."""
    job = _document(env, "harbour-survey.docx", b"PK", background=True)
    with anyio.fail_after(5):
        env.processor.submit(job)
        while env.processor.running(job.attachment_id):
            await anyio.sleep(0.01)
    assert _routes(env.door) == ["/files/prepare", "/files/measure"]
    status, state, measure, manifest = _row(env, job)
    assert (status, state, manifest) == ("ready", "measured", True)
    assert measure is not None and measure["unit"] == "line"
    found = env.store.find_attachment("user-a", env.chat_id, job.attachment_id)
    assert found is not None and found.manifest is not None and found.manifest["kind"] == "office"


async def test_a_measure_holds_no_prepare_slot(env: Env) -> None:
    """While a text file's measure is held, both pictures prepare at once and a deck's background prepare goes next.

    Both pictures are ready while the measure is still held.
    """
    env.door.gates["/files/measure"] = asyncio.Event()
    env.door.gates["/files/prepare"] = asyncio.Event()
    notes = _document(env)
    env.processor.submit(notes)
    await _until(lambda: len(_requests(env.door, "/files/measure")) == 1)
    pictures = [_picture(env, "harbour-photo.jpg"), _picture(env, "quay-photo.jpg")]
    deck = _document(env, "harbour-deck.pptx", b"PK", background=True)
    for job in (*pictures, deck):
        env.processor.submit(job)
    await _until(lambda: len(_requests(env.door, "/files/prepare")) == PREPARE_SLOTS)
    await anyio.sleep(0.2)
    prepared = [request.url.params["path"] for request in _requests(env.door, "/files/prepare")]
    assert sorted(prepared) == sorted(job.path for job in pictures)
    env.door.gates["/files/prepare"].set()
    await _until(lambda: deck.path in [r.url.params["path"] for r in _requests(env.door, "/files/prepare")])
    await _ended(env.processor, *pictures)
    assert [_row(env, job)[0] for job in pictures] == ["ready", "ready"]
    assert len(_requests(env.door, "/files/measure")) == 1 and _row(env, notes)[1] == "pending"
    assert env.processor.running(notes.attachment_id)
    env.door.gates["/files/measure"].set()
    await _ended(env.processor, notes, deck)
    assert [_row(env, job)[1] for job in (notes, deck)] == ["measured", "measured"]


async def test_measures_run_one_at_a_time(env: Env) -> None:
    """With the first of two documents' measures held, the second's is not sent; once it is released, it is."""
    env.door.gates["/files/measure"] = asyncio.Event()
    first, second = _document(env), _document(env, "quay-ledger.csv", b"berth,ship\n1,Finkenwerder\n")
    env.processor.submit(first)
    await _until(lambda: len(_requests(env.door, "/files/measure")) == 1)
    env.processor.submit(second)
    await anyio.sleep(0.2)
    assert MEASURE_SLOTS == 1
    assert [json.loads(r.content)["path"] for r in _requests(env.door, "/files/measure")] == [first.path]
    assert env.processor.running(second.attachment_id)
    env.door.gates["/files/measure"].set()
    await _ended(env.processor, first, second)
    assert [json.loads(r.content)["path"] for r in _requests(env.door, "/files/measure")] == [first.path, second.path]


@pytest.mark.parametrize(
    ("answers", "token"),
    [
        ([httpx.Response(422, json={"detail": "unreadable_file: damaged"})], "unreadable_file"),
        ([httpx.Response(503, json={"detail": "busy"}), httpx.Response(503, json={"detail": "busy"})], "http:503"),
    ],
    ids=["unreadable", "busy_twice"],
)
async def test_a_failed_measure_is_none(
    env: Env,
    audit_stream: CapturedAuditStream,
    monkeypatch: pytest.MonkeyPatch,
    answers: list[httpx.Response],
    token: str,
) -> None:
    """A measure workbench refuses, or that is busy twice, is ``none``: an ``error`` child with its reason, no error."""
    monkeypatch.setattr(processing, "PREPARE_RETRY_S", 0)
    left = list(answers)
    env.door.answers["/files/measure"] = lambda folder: left.pop(0)
    job = _document(env)
    lines, sink = _lines("ERROR")
    try:
        env.processor.submit(job)
        await _ended(env.processor, job)
    finally:
        logger.remove(sink)
    assert lines == []
    assert len(_requests(env.door, "/files/measure")) == len(answers) and left == []
    assert _row(env, job) == ("ready", "none", None, False)
    assert _length(env, job) == LengthOut(state="none", total=None, unit=None, too_long=False)
    handed, failed = _measures(audit_stream)
    assert (handed["outcome"], failed["outcome"]) == ("ok", "error")
    assert (failed["action"], failed["operation"], failed["criteria"], failed["parent_event_id"]) == (
        "disclose",
        ATTACHMENT,
        {"reason": token},
        handed["parent_event_id"],
    )


async def test_an_unrecorded_measure_sends_nothing(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: the measure's hand-over cannot be written, so nothing is sent, and the state is ``none``."""
    job = _document(env)
    unrecorded_child(monkeypatch, ATTACHMENT)
    env.processor.submit(job)
    await _ended(env.processor, job)
    assert _requests(env.door, "/files/measure") == []
    assert _row(env, job)[:2] == ("ready", "none")


async def test_a_measure_after_take_back_writes_nothing(
    env: Env, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Taken back mid-measure, the job is cancelled: no settle, no ``error`` child, no error log.

    A row deleted mid-measure without a cancel is not settled either: ``settle_measure`` answers ``False``, and one
    debug line says so.
    """
    settles = _spied(env, monkeypatch)
    env.door.delays["/files/measure"] = 2.0
    job = _document(env)
    lines, sink = _lines()
    try:
        env.processor.submit(job)
        await _until(lambda: len(_requests(env.door, "/files/measure")) == 1)
        before = len(events(audit_stream))
        assert env.processor.cancel(job.attachment_id)
        assert env.store.remove_pending_attachment("user-a", env.chat_id, job.attachment_id)
        await _ended(env.processor, job)
        assert settles.calls == [] and events(audit_stream)[before:] == []
        assert [line for level, line in lines if level in ("ERROR", "CRITICAL")] == []

        env.door.delays["/files/measure"] = 0.3
        gone = _document(env, "quay-ledger.csv", b"berth,ship\n")
        env.processor.submit(gone)
        await _until(lambda: len(_requests(env.door, "/files/measure")) == 2)
        assert env.store.remove_pending_attachment("user-a", env.chat_id, gone.attachment_id)
        await _ended(env.processor, gone)
    finally:
        logger.remove(sink)
    assert [(call[0], call[2]) for call in settles.calls] == [(gone.attachment_id, False)]
    about = [(level, line) for level, line in lines if gone.attachment_id in line]
    assert len(about) == 1 and about[0][0] == "DEBUG"
    assert [e for e in _measures(audit_stream) if e["outcome"] == "error"] == []
    assert [line for level, line in lines if level in ("ERROR", "CRITICAL")] == []


async def test_a_chat_deleted_mid_measure_writes_nothing(
    env: Env, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat's delete cancels its measure: nothing is settled and nothing more is recorded."""
    settles = _spied(env, monkeypatch)
    env.door.delays["/files/measure"] = 2.0
    job = _document(env)
    env.processor.submit(job)
    await _until(lambda: len(_requests(env.door, "/files/measure")) == 1)
    before = len(events(audit_stream))
    assert env.processor.cancel_chat(env.chat_id) == 1
    env.store.delete_chat_rows(env.chat_id)
    await _ended(env.processor, job)
    assert settles.calls == [] and events(audit_stream)[before:] == []


async def test_a_pending_measure_is_requeued_at_start(
    env: Env, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ready row left ``pending`` is measured again, without a prepare; a processing PDF is prepared and measured.

    Each is one job, and its hand-over is a primary of the system's, ``requeued``.
    """
    notes = _document(env, requeued=True)
    scan = _document(env, "long-report.pdf", b"%PDF-1.7", status="processing", requeued=True)
    jobs: list[PrepareJob] = []
    submit = env.processor.submit

    def kept(job: PrepareJob) -> None:
        jobs.append(job)
        submit(job)

    monkeypatch.setattr(env.processor, "submit", kept)
    assert env.processor.requeue() == 2
    assert sorted((job.attachment_id, job.op, job.prepare, job.measure) for job in jobs) == sorted(
        [(notes.attachment_id, None, False, True), (scan.attachment_id, None, True, True)]
    )
    await _ended(env.processor, notes, scan)
    assert [_row(env, job)[:2] for job in (notes, scan)] == [("ready", "measured")] * 2
    assert [json.loads(r.content)["path"] for r in _requests(env.door, "/files/measure")].count(notes.path) == 1
    [handed] = [e for e in _measures(audit_stream) if e["target"]["ids"] == [notes.attachment_id]]
    assert (handed["actor"], handed.get("parent_event_id"), handed["criteria"]) == (
        SYSTEM,
        None,
        {"kind": "document", "requeued": True},
    )


async def test_a_chat_whose_delete_failed_measures_again(env: Env) -> None:
    """After cancel_chat mid-measure, requeue_chat measures the chat's pending documents once more."""
    env.door.delays["/files/measure"] = 0.5
    job = _document(env)
    env.processor.submit(job)
    await _until(lambda: len(_requests(env.door, "/files/measure")) == 1)
    assert env.processor.cancel_chat(env.chat_id) == 1
    env.door.delays.clear()
    assert await env.processor.requeue_chat(env.chat_id) == 1
    assert await env.processor.requeue_chat(env.chat_id) == 0
    await _ended(env.processor, job)
    assert _row(env, job)[:2] == ("ready", "measured")
    assert len(_requests(env.door, "/files/measure")) == 2
