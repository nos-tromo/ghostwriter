"""The attachment processor alone: one job per attachment, two at once, fail closed, cancelled by its chat.

A store, a :class:`~ghostwriter.workbench.WorkbenchClient` over :class:`~tests.fakes.FakeDoor` and an
emitter on :class:`~tests.conftest.CapturedAuditStream`. Every name, id and value here is invented.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import anyio
import httpx
import pytest
from loguru import logger
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ghostwriter import __version__
from ghostwriter.audit import open_emitter
from ghostwriter.config import Settings
from ghostwriter.db import init_db, make_engine
from ghostwriter.models import Attachment
from ghostwriter.processing import PREPARE_SLOTS, STRIPS_TIMEOUT_S, AttachmentProcessor, PrepareJob
from ghostwriter.store import ChatStore
from ghostwriter.utils.audit_event import AuditEmitter
from ghostwriter.workbench import PREPARE_TIMEOUT_S, WorkbenchClient, chat_folder
from tests.conftest import KEY, CapturedAuditStream, events, make_settings
from tests.fakes import TINY_JPEG, FakeDoor, stock_manifest


@dataclass
class Env:
    """One test's store, workbench, chain and processor, with one chat.

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
    """Build the store, the chain on ``audit_stream``, workbench behind a door and the processor."""
    settings = make_settings(tmp_path, vision=True)
    engine = make_engine(settings.db_path)
    init_db(engine)
    store = ChatStore(engine, file_days=settings.file_days, notice_days=settings.notice_days)
    emitter = open_emitter(__version__, audit_stream)
    emitter.start()
    door = FakeDoor()
    workbench = WorkbenchClient(settings, transport=httpx.MockTransport(door))
    processor = AttachmentProcessor(store, workbench, emitter)
    try:
        yield Env(settings, engine, store, door, emitter, processor, store.create_chat("user-a").id)
    finally:
        await processor.aclose()
        emitter.close()
        engine.dispose()


def _job(env: Env, name: str = "harbour-photo.jpg", kind: Literal["document", "image"] = "image") -> PrepareJob:
    """Store a ``processing`` attachment whose file the door holds, and return its job, requeued as at start.

    Args:
        env: The test's env.
        name: The file's name.
        kind: Its kind.

    Returns:
        The job, with no request's operation.
    """
    path = f"{chat_folder(KEY, env.chat_id)}/{name}"
    env.door.files[path] = (TINY_JPEG, "image/jpeg")
    out = env.store.add_attachment(
        env.chat_id,
        file_id="4" * 32,
        path=path,
        name=name,
        size=len(TINY_JPEG),
        mime="image/jpeg",
        kind=kind,
        status="processing",
    )
    return PrepareJob(out.id, env.chat_id, "user-a", path, kind, None)


def _prepares(door: FakeDoor) -> list[httpx.Request]:
    return [request for request, _ in door.requests if request.url.path == "/files/prepare"]


def _status(env: Env, job: PrepareJob) -> tuple[str, str | None]:
    """Return the attachment's status and failure token, as stored."""
    with Session(env.engine) as s:
        status, failure = s.execute(
            select(Attachment.status, Attachment.failure).where(Attachment.id == job.attachment_id)
        ).one()
        return status, failure


async def test_a_second_submit_of_one_attachment_is_a_no_op(env: Env, audit_stream: CapturedAuditStream) -> None:
    """While its job runs, submitting the attachment again starts nothing: one prepare, one hand-over."""
    job = _job(env)
    env.processor.submit(job)
    env.processor.submit(job)
    await env.processor.wait(job.attachment_id, 5)
    assert len(_prepares(env.door)) == 1
    assert _status(env, job) == ("ready", None)
    disclosed = [e for e in events(audit_stream) if e["operation"] == "chat.attachment"]
    assert [(e["action"], e["recipient"], e["criteria"]) for e in disclosed] == [
        ("disclose", "workbench:prepare", {"kind": "image", "requeued": True})
    ]


async def test_at_most_two_prepares_run_at_once(env: Env) -> None:
    """Three jobs over a slow workbench: never more than two requests in flight, and all three end ready."""
    in_flight = peak = 0

    async def counted(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        if request.url.path != "/files/prepare":
            return await env.door(request)
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            return await env.door(request)
        finally:
            in_flight -= 1

    env.door.delays["/files/prepare"] = 0.3
    processor = AttachmentProcessor(
        env.store, WorkbenchClient(env.settings, transport=httpx.MockTransport(counted)), env.emitter
    )
    jobs = [_job(env, f"harbour-photo-{n}.jpg") for n in range(3)]
    try:
        for job in jobs:
            processor.submit(job)
        for job in jobs:
            await processor.wait(job.attachment_id, 5)
    finally:
        await processor.aclose()
    assert (PREPARE_SLOTS, peak, len(_prepares(env.door))) == (2, 2, 3)
    assert [_status(env, job) for job in jobs] == [("ready", None)] * 3


async def test_an_unrecorded_hand_over_sends_nothing(env: Env, audit_stream: CapturedAuditStream) -> None:
    """Fail closed: the hand-over cannot be written, so workbench is not asked, and the picture fails with audit."""
    job = _job(env)
    audit_stream.failing = True
    env.processor.submit(job)
    await env.processor.wait(job.attachment_id, 5)
    audit_stream.failing = False
    assert _prepares(env.door) == []
    assert _status(env, job) == ("failed", "audit")


async def test_a_cancelled_job_settles_nothing(env: Env, audit_stream: CapturedAuditStream) -> None:
    """A job cancelled while workbench prepares leaves its row processing and records nothing more."""
    env.door.delays["/files/prepare"] = 2.0
    job = _job(env)
    env.processor.submit(job)
    with anyio.fail_after(5):
        while not _prepares(env.door):
            await anyio.sleep(0.01)
    before = len(events(audit_stream))
    assert env.processor.cancel_chat(env.chat_id) == 1
    await env.processor.wait(job.attachment_id, 5)
    assert not env.processor.running(job.attachment_id)
    assert _status(env, job) == ("processing", None)
    assert events(audit_stream)[before:] == []
    assert env.processor.cancel_chat(env.chat_id) == 0


async def test_a_manifest_this_client_cannot_read_fails_a_picture_and_leaves_a_document_ready(
    env: Env, audit_stream: CapturedAuditStream
) -> None:
    """A manifest of a kind this client does not know is a bad answer: the picture fails, the PDF reads as text."""
    photo, scan = _job(env), _job(env, "quay-scan.pdf", "document")
    for job in (photo, scan):
        env.door.manifests[job.path] = {**stock_manifest(job.path), "kind": "office"}
        env.processor.submit(job)
    for job in (photo, scan):
        await env.processor.wait(job.attachment_id, 5)
    assert (_status(env, photo), _status(env, scan)) == (("failed", "bad_answer"), ("ready", None))
    found = env.store.find_attachment("user-a", env.chat_id, scan.attachment_id)
    assert found is not None and found.manifest is None
    failed = [e for e in events(audit_stream) if e["outcome"] == "error"]
    assert [e["criteria"] for e in failed] == [{"reason": "bad_answer"}] * 2


async def test_a_manifest_that_cannot_be_kept_settles_as_a_failure(
    env: Env, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Storing the manifest raises: the attachment does not stay processing, it fails as a bad answer."""
    settle = env.store.settle_attachment

    def refuse_manifests(
        attachment_id: str,
        *,
        status: Literal["ready", "failed"],
        manifest: Mapping[str, Any] | None,
        failure: str | None,
    ) -> bool:
        if manifest is not None:
            raise TypeError("synthetic malformed manifest")
        return settle(attachment_id, status=status, manifest=manifest, failure=failure)

    monkeypatch.setattr(env.store, "settle_attachment", refuse_manifests)
    job = _job(env)
    env.processor.submit(job)
    await env.processor.wait(job.attachment_id, 5)
    assert _status(env, job) == ("failed", "bad_answer")
    [failed] = [e for e in events(audit_stream) if e["outcome"] == "error"]
    assert (failed["recipient"], failed["criteria"]) == ("workbench:prepare", {"reason": "bad_answer"})


async def test_an_unexpected_failure_still_settles_the_attachment(
    env: Env, monkeypatch: pytest.MonkeyPatch, loguru_caplog: pytest.LogCaptureFixture
) -> None:
    """A store that fails before the hand-over: nothing is sent, the picture fails, and the log names the type only."""

    def broken(chat_id: str) -> None:
        raise RuntimeError("synthetic store failure")

    monkeypatch.setattr(env.store, "mark_files", broken)
    job = _job(env)
    env.processor.submit(job)
    await env.processor.wait(job.attachment_id, 5)
    assert _prepares(env.door) == []
    assert _status(env, job) == ("failed", "error")
    assert "RuntimeError" in loguru_caplog.text and "harbour-photo" not in loguru_caplog.text


async def test_a_chat_whose_delete_failed_is_prepared_again(env: Env, audit_stream: CapturedAuditStream) -> None:
    """After cancel_chat, requeue_chat awaits the cancelled job, then starts one new job per processing attachment."""
    env.door.delays["/files/prepare"] = 0.5
    job = _job(env)
    env.processor.submit(job)
    with anyio.fail_after(5):
        while not _prepares(env.door):
            await anyio.sleep(0.01)
    assert env.processor.cancel_chat(env.chat_id) == 1
    assert await env.processor.requeue_chat(env.chat_id) == 1
    assert await env.processor.requeue_chat(env.chat_id) == 0
    await env.processor.wait(job.attachment_id, 5)
    assert _status(env, job) == ("ready", None)
    assert len(_prepares(env.door)) == 2
    handed = [e for e in events(audit_stream) if e["operation"] == "chat.attachment"]
    assert [e["criteria"] for e in handed] == [{"kind": "image", "requeued": True}] * 2


def _sized(env: Env, job: PrepareJob, width: int, height: int) -> None:
    """Have the door report the job's picture at ``width`` x ``height``."""
    manifest = stock_manifest(job.path)
    env.door.manifests[job.path] = {
        **manifest,
        "renditions": [{**manifest["renditions"][0], "width": width, "height": height}],
    }


@pytest.mark.parametrize(
    ("image_tokens", "width", "height", "strips"),
    [
        (280, 2048, 1536, [None, "2"]),
        (280, 1536, 2048, [None, "2"]),
        (280, 1024, 768, [None]),
        (None, 2048, 1536, [None]),
    ],
)
async def test_a_large_picture_at_a_fixed_cost_gets_its_strips_cut_before_it_is_ready(
    env: Env,
    audit_stream: CapturedAuditStream,
    image_tokens: int | None,
    width: int,
    height: int,
    strips: list[str | None],
) -> None:
    """With a fixed image cost, a picture whose long side is over 1024 is prepared once more with ``strips=2``."""
    job = _job(env)
    _sized(env, job, width, height)
    workbench = WorkbenchClient(env.settings, transport=httpx.MockTransport(env.door))
    processor = AttachmentProcessor(env.store, workbench, env.emitter, image_tokens=image_tokens)
    try:
        processor.submit(job)
        await processor.wait(job.attachment_id, 5)
    finally:
        await processor.aclose()
    assert [request.url.params.get("strips") for request in _prepares(env.door)] == strips
    waits = [request.extensions["timeout"]["read"] for request in _prepares(env.door)]
    assert waits == [PREPARE_TIMEOUT_S, STRIPS_TIMEOUT_S][: len(strips)] and STRIPS_TIMEOUT_S == 60.0
    assert _status(env, job) == ("ready", None)
    found = env.store.find_attachment("user-a", env.chat_id, job.attachment_id)
    assert found is not None and found.manifest is not None
    assert (found.manifest["renditions"][0]["width"], found.manifest["renditions"][0]["height"]) == (width, height)
    handed = [e for e in events(audit_stream) if e["operation"] == "chat.attachment"]
    assert [(e["action"], e["outcome"]) for e in handed] == [("disclose", "ok")]


async def test_a_failed_strips_prepare_leaves_the_picture_ready(env: Env, audit_stream: CapturedAuditStream) -> None:
    """The strips request answers 503: it is not tried again, the picture is ready, and the log names the token only."""
    job = _job(env)
    _sized(env, job, 2048, 1536)
    asked: list[str] = []

    def busy_on_strips(folder: str) -> httpx.Response | None:
        asked.append(folder)
        return None if len(asked) == 1 else httpx.Response(503, json={"detail": "busy"})

    env.door.answers["/files/prepare"] = busy_on_strips
    workbench = WorkbenchClient(env.settings, transport=httpx.MockTransport(env.door))
    processor = AttachmentProcessor(env.store, workbench, env.emitter, image_tokens=280)
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    try:
        processor.submit(job)
        await processor.wait(job.attachment_id, 5)
    finally:
        logger.remove(sink)
        await processor.aclose()
    assert [request.url.params.get("strips") for request in _prepares(env.door)] == [None, "2"]
    assert _status(env, job) == ("ready", None)
    assert any("http:503" in line and job.attachment_id in line for line in lines)
    assert not any("harbour-photo" in line or job.path in line for line in lines)
    assert [e for e in events(audit_stream) if e["outcome"] == "error"] == []


async def test_an_unexpected_strips_failure_is_a_warning(env: Env, loguru_caplog: pytest.LogCaptureFixture) -> None:
    """The strips request raises what the client does not map: a warning names its type, and the picture is ready."""
    job = _job(env)
    _sized(env, job, 2048, 1536)
    asked: list[str] = []

    def broken_on_strips(folder: str) -> Exception | None:
        asked.append(folder)
        return None if len(asked) == 1 else RuntimeError("synthetic transport failure")

    env.door.answers["/files/prepare"] = broken_on_strips
    workbench = WorkbenchClient(env.settings, transport=httpx.MockTransport(env.door))
    processor = AttachmentProcessor(env.store, workbench, env.emitter, image_tokens=280)
    try:
        processor.submit(job)
        await processor.wait(job.attachment_id, 5)
    finally:
        await processor.aclose()
    assert len(_prepares(env.door)) == 2 and _status(env, job) == ("ready", None)
    assert "RuntimeError" in loguru_caplog.text and job.attachment_id in loguru_caplog.text
    assert "synthetic transport failure" not in loguru_caplog.text and "harbour-photo" not in loguru_caplog.text
