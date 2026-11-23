"""The processor's media step: a clip goes to Nextext for one job, its artifacts to workbench, the job deleted at once.

A store, a :class:`~ghostwriter.workbench.WorkbenchClient` over :class:`~tests.fakes.FakeDoor` (workbench 0.9.0), a
:class:`~ghostwriter.nextext.NextextClient` over :class:`~tests.fakes.FakeNextext` and an emitter on
:class:`~tests.conftest.CapturedAuditStream`; ``POLL_S`` and ``PREPARE_RETRY_S`` are 0.01 s. A processor whose gate
opens at once has run its start reconcile, which found nothing to delete, before a test begins, and the fake's
requests start over then (``tests/test_reconcile.py`` covers the reconcile). Every name, id and value here is invented.
"""

import logging
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import httpx
import pytest
from loguru import logger
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from typing_extensions import override

from ghostwriter import __version__, nextext, processing
from ghostwriter.audit import Operation, RequestContext, open_emitter
from ghostwriter.config import Settings
from ghostwriter.db import init_db, make_engine
from ghostwriter.models import Attachment
from ghostwriter.nextext import ARTIFACT_CAPS, JOB_OPTIONS, JSONL, KEYFRAMES, NextextClient, NextextHealth, NextextJob
from ghostwriter.processing import (
    ATTACHMENT,
    MEDIA_SLOTS,
    NEXTEXT_OPERATION,
    NEXTEXT_RECIPIENT,
    POLL_TOLERATED,
    PREPARE_SLOTS,
    AttachmentProcessor,
    PrepareJob,
)
from ghostwriter.store import ChatStore
from ghostwriter.upload import MEDIA_TYPES
from ghostwriter.utils.audit_event import Actor, AuditEmitter, AuditWriteError, Target
from ghostwriter.workbench import WorkbenchClient, chat_folder
from tests.conftest import KEY, NEXTEXT_BASE, NEXTEXT_KEY, CapturedAuditStream, events, make_settings, unrecorded_child
from tests.fakes import FAILURE_TEXT, JOB_ID, TINY_JPEG, FakeDoor, FakeNextext, parse_job_upload

CLIP = b"\x00\x00\x00\x18ftypmp42" + bytes(200)
MEMO = b"\x00\x00\x00\x18ftypM4A " + bytes(120)
#: The id FakeNextext gives its second job.
SECOND_JOB = f"{JOB_ID[:-4]}0001"
#: A transcript line the log must never hold.
SPOKEN = "Die Fähre nach Finkenwerder legt um neun Uhr ab"
SYSTEM = {"user": None, "via": "system"}


@dataclass
class Env:
    """One test's store, workbench, Nextext, chain and processor, with one chat.

    Attributes:
        settings: The test settings, Nextext on.
        engine: The database.
        store: The chats.
        door: workbench 0.9.0.
        fake: Nextext.
        nextext: The client over ``fake``.
        health: Nextext's health, healthy.
        emitter: The audit chain.
        chat_id: The chat the clips go to.
        processor: The processor over all of them, its start reconcile run and its gate open.
        made: Every processor the test built, closed at teardown.
    """

    settings: Settings
    engine: Engine
    store: ChatStore
    door: FakeDoor
    fake: FakeNextext
    nextext: NextextClient
    health: NextextHealth
    emitter: AuditEmitter
    chat_id: str
    processor: AttachmentProcessor = field(init=False)
    made: list[AttachmentProcessor] = field(default_factory=list)

    async def make(
        self,
        *,
        client: NextextClient | None | bool = True,
        timeout_s: float = 30.0,
        start: bool = True,
        workbench: WorkbenchClient | None = None,
    ) -> AttachmentProcessor:
        """Build a processor over this env, and close it at teardown.

        Args:
            client: The Nextext client; ``True`` for :attr:`nextext`, ``None`` for none (and no health).
            timeout_s: The clip budget.
            start: Whether its start reconcile runs at once (:meth:`~AttachmentProcessor.start_media`); with a client,
                its list is awaited and then the fake's requests and events start over.
            workbench: The workbench client; one over :attr:`door` when ``None``.

        Returns:
            The processor.
        """
        reached = self.nextext if client is True else client if isinstance(client, NextextClient) else None
        processor = AttachmentProcessor(
            self.store,
            workbench or WorkbenchClient(self.settings, transport=httpx.MockTransport(self.door)),
            self.emitter,
            nextext=reached,
            health=self.health if reached is not None else None,
            nextext_timeout_s=timeout_s,
        )
        self.made.append(processor)
        if start:
            processor.start_media()
            if reached is not None:
                await _until(lambda: "list" in self.fake.events)
                self.fake.events.clear()
                self.fake.requests.clear()
        return processor


@pytest.fixture
async def env(tmp_path: Path, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    """Build the store, the chain on ``audit_stream``, workbench and Nextext behind their fakes, and the processor."""
    monkeypatch.setattr(processing, "POLL_S", 0.01)
    monkeypatch.setattr(processing, "PREPARE_RETRY_S", 0.01)
    settings = make_settings(tmp_path, vision=True, nextext_api_base=NEXTEXT_BASE, nextext_api_key=NEXTEXT_KEY)
    engine = make_engine(settings.db_path)
    init_db(engine)
    store = ChatStore(engine, file_days=settings.file_days, notice_days=settings.notice_days)
    emitter = open_emitter(__version__, audit_stream)
    emitter.start()
    fake = FakeNextext()
    client = NextextClient(settings, transport=httpx.MockTransport(fake))
    health = NextextHealth(client)
    health.healthy = True
    built = Env(
        settings,
        engine,
        store,
        FakeDoor(server_version="0.9.0"),
        fake,
        client,
        health,
        emitter,
        store.create_chat("user-a").id,
    )
    built.processor = await built.make()
    try:
        yield built
    finally:
        for processor in built.made:
            await processor.aclose()
        emitter.close()
        engine.dispose()


@pytest.fixture
def log_lines() -> Iterator[list[str]]:
    """Capture every loguru line, DEBUG included.

    Yields:
        The lines written while the test runs.
    """
    lines: list[str] = []
    handler_id = logger.add(lambda message: lines.append(str(message)), level="DEBUG", format="{level} {message}")
    try:
        yield lines
    finally:
        logger.remove(handler_id)


def _clip(env: Env, name: str = "harbour-tour.mp4", *, requeued: bool = False) -> PrepareJob:
    """Store a ``processing`` clip whose file the door holds, and return its job.

    Args:
        env: The test's env.
        name: The file's name; ``.m4a`` is audio.
        requeued: Whether the job is one requeued at start: no operation and no size.

    Returns:
        The job; unless requeued, with an upload's operation, its primary written, and the clip's size.
    """
    path = f"{chat_folder(KEY, env.chat_id)}/{name}"
    mime = MEDIA_TYPES[f".{name.rpartition('.')[2].lower()}"]
    data = MEMO if mime.startswith("audio/") else CLIP
    env.door.files[path] = (data, mime)
    out = env.store.add_attachment(
        env.chat_id,
        file_id=uuid4().hex,
        path=path,
        name=name,
        size=len(data),
        mime=mime,
        kind="media",
        status="processing",
    )
    if requeued:
        return PrepareJob(out.id, env.chat_id, "user-a", path, "media", None)
    op = Operation(env.emitter, RequestContext(Actor("user-a", "edge"), None, "req-0001"), "collect", ATTACHMENT)
    op.container = env.chat_id
    op.record(target=Target(kind="attachment", ids=(out.id,)), criteria={"mime": mime})
    return PrepareJob(out.id, env.chat_id, "user-a", path, "media", op, size=len(data))


def _picture(env: Env, name: str = "harbour-photo.jpg") -> PrepareJob:
    """Store a ``processing`` picture whose file the door holds, and return its job, requeued as at start."""
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


def _status(env: Env, job: PrepareJob) -> tuple[str, str | None]:
    """Return the attachment's status and failure token, as stored."""
    with Session(env.engine) as s:
        status, failure = s.execute(
            select(Attachment.status, Attachment.failure).where(Attachment.id == job.attachment_id)
        ).one()
        return status, failure


def _prepares(door: FakeDoor) -> list[httpx.Request]:
    return [request for request, _ in door.requests if request.url.path == "/files/prepare"]


def _rows(stream: CapturedAuditStream, before: int) -> list[tuple[Any, ...]]:
    """Return each event since ``before`` as its action, operation, recipient, criteria and outcome."""
    return [
        (e["action"], e["operation"], e.get("recipient"), e.get("criteria"), e["outcome"])
        for e in events(stream)[before:]
    ]


async def _run(processor: AttachmentProcessor, *jobs: PrepareJob) -> None:
    """Submit the jobs and wait until each has ended."""
    for job in jobs:
        processor.submit(job)
    for job in jobs:
        await processor.wait(job.attachment_id, 5)
        assert not processor.running(job.attachment_id)


async def _until(condition: Callable[[], bool]) -> None:
    """Wait until the condition holds, at most 5 s."""
    with anyio.fail_after(5):
        while not condition():
            await anyio.sleep(0.01)


# --- the round trip ------------------------------------------------------------


async def test_a_clip_goes_to_nextext_and_its_artifacts_to_workbench(
    env: Env, audit_stream: CapturedAuditStream
) -> None:
    """One upload as media.mp4, both artifacts into workbench in order, the job deleted, then the prepare: ready."""
    job = _clip(env)
    before = len(events(audit_stream))
    await _run(env.processor, job)
    assert _status(env, job) == ("ready", None)
    found = env.store.find_attachment("user-a", env.chat_id, job.attachment_id)
    assert found is not None and found.manifest is not None
    assert (found.manifest["kind"], found.manifest["unit"], found.manifest["transcript"]) == ("media", "frame", True)
    [upload] = env.fake.uploads
    sent = await parse_job_upload(upload)
    assert (sent.filename, sent.content_type, sent.data, sent.options) == (
        "media.mp4",
        "video/mp4",
        env.door.files[job.path][0],
        JOB_OPTIONS,
    )
    assert env.door.derived == [
        (job.path, "nextext_jsonl", env.fake.artifacts[JSONL]),
        (job.path, "nextext_zip", env.fake.artifacts[KEYFRAMES]),
    ]
    assert [request.url.path for request, _ in env.door.requests] == [
        "/files/view",
        "/files/derived",
        "/files/derived",
        "/files/prepare",
    ]
    assert all(request.headers["x-session-id"] not in ("", "default") for request, _ in env.door.requests)
    assert (env.fake.events, env.fake.deleted) == (["create", "list", f"delete:{JOB_ID}"], [JOB_ID])
    for request in env.fake.requests:
        assert request.headers["authorization"] == f"Bearer {NEXTEXT_KEY}"
        assert request.headers["x-request-id"] == "req-0001" and request.headers["x-audit-purpose"]
    assert env.processor.owned_jobs() == frozenset()
    assert _rows(audit_stream, before) == [
        ("disclose", ATTACHMENT, NEXTEXT_RECIPIENT, {"kind": "media", "bytes": len(CLIP)}, "ok"),
        ("disclose", ATTACHMENT, "workbench:prepare", {"kind": "media"}, "ok"),
        ("delete", NEXTEXT_OPERATION, None, {"deleted": True}, "ok"),
    ]
    since = events(audit_stream)[before:]
    assert job.op is not None and {e["parent_event_id"] for e in since} == {job.op.event_id}
    assert since[2]["target"] == {"kind": "job", "ids": [JOB_ID], "container": env.chat_id}
    assert (NEXTEXT_RECIPIENT, NEXTEXT_OPERATION) == ("nextext:transcribe", "chat.nextext")


class _Arriving(httpx.AsyncBaseTransport):
    """Nextext's transport, reading a create's body chunk by chunk as it arrives; then FakeNextext answers."""

    def __init__(self, fake: FakeNextext, log: list[str]) -> None:
        """Log each chunk of the clip as it arrives.

        Args:
            fake: Nextext.
            log: Where ``nextext:<i>`` goes for the clip's chunk ``i``.
        """
        self._fake = fake
        self._log = log

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Read a create's body as it streams, then answer as the fake.

        Args:
            request: The request, its body not read yet.

        Returns:
            The fake's answer.
        """
        if request.method == "POST":
            stream = request.stream
            assert isinstance(stream, httpx.AsyncByteStream)
            body = bytearray()
            async for chunk in stream:
                if chunk.startswith(b"<chunk "):
                    self._log.append(f"nextext:{chunk[7:8].decode()}")
                body += chunk
            request = httpx.Request(request.method, request.url, headers=request.headers, content=bytes(body))
        return await httpx.MockTransport(self._fake).handle_async_request(request)


async def test_the_clip_passes_through_chunk_by_chunk(env: Env) -> None:
    """The view from workbench and the upload to Nextext interleave: no chunk waits for the next, none is read whole."""
    log: list[str] = []
    pieces = [f"<chunk {i}>".encode() + bytes(64) for i in range(1, 9)]

    def view(folder: str) -> httpx.Response:
        async def body() -> AsyncIterator[bytes]:
            for i, piece in enumerate(pieces, start=1):
                log.append(f"view:{i}")
                yield piece

        return httpx.Response(200, content=body(), headers={"Content-Type": "video/mp4"})

    env.door.answers["/files/view"] = view
    processor = await env.make(client=NextextClient(env.settings, transport=_Arriving(env.fake, log)))
    job = _clip(env)
    await _run(processor, job)
    assert log == [entry for i in range(1, 9) for entry in (f"view:{i}", f"nextext:{i}")]
    assert (await parse_job_upload(env.fake.uploads[0])).data == b"".join(pieces)
    assert _status(env, job) == ("ready", None)


# --- what Nextext answers --------------------------------------------------------


async def test_a_clip_without_speech_is_ready(env: Env, audit_stream: CapturedAuditStream) -> None:
    """Skipped for no speech, with no transcript: the frames alone go to workbench, and the clip is ready."""
    env.fake.skipped, env.fake.skip_reason_code = True, "vad_no_speech"
    env.fake.artifacts[JSONL] = 404
    job = _clip(env)
    await _run(env.processor, job)
    assert _status(env, job) == ("ready", None)
    assert env.door.derived == [(job.path, "nextext_zip", env.fake.artifacts[KEYFRAMES])]
    found = env.store.find_attachment("user-a", env.chat_id, job.attachment_id)
    assert found is not None and found.manifest is not None
    assert (found.manifest["transcript"], found.manifest["total"]) == (False, 3)
    assert env.fake.deleted == [JOB_ID]
    assert [e for e in events(audit_stream) if e["outcome"] == "error"] == []


async def test_audio_has_no_keyframes(env: Env) -> None:
    """Audio has no frames at Nextext: the transcript alone goes to workbench, sent as media.m4a."""
    env.fake.artifacts[KEYFRAMES] = 404
    job = _clip(env, "voice-memo.m4a")
    await _run(env.processor, job)
    assert _status(env, job) == ("ready", None)
    assert env.door.derived == [(job.path, "nextext_jsonl", env.fake.artifacts[JSONL])]
    sent = await parse_job_upload(env.fake.uploads[0])
    assert (sent.filename, sent.content_type, sent.data) == ("media.m4a", "audio/mp4", MEMO)
    found = env.store.find_attachment("user-a", env.chat_id, job.attachment_id)
    assert found is not None and found.manifest is not None
    assert (found.manifest["transcript"], found.manifest["total"]) == (True, 0)


@pytest.mark.parametrize(
    ("script", "token", "created"),
    [
        ({"statuses": ["running", "failed"], "error_code": "undecodable_media"}, "undecodable_media", True),
        ({"statuses": ["failed"], "error_code": "internal"}, "nextext_failed", True),
        ({"statuses": ["failed"], "error_code": "inference_unavailable"}, "nextext_unavailable", True),
        ({"statuses": ["interrupted"]}, "nextext_failed", True),
        ({"statuses": ["cancelled"]}, "nextext_failed", True),
        ({"create_status": 503}, "nextext_unavailable", False),
        ({"create_status": 401}, "nextext_unavailable", False),
        ({"create_status": 422}, "nextext_failed", False),
        ({"list_status": 502}, "nextext_unavailable", True),
        ({"artifacts": {JSONL: 500, KEYFRAMES: b"PK\x03\x04"}}, "nextext_unavailable", True),
    ],
    ids=[
        "undecodable",
        "internal",
        "inference-unavailable",
        "interrupted",
        "cancelled",
        "create-503",
        "create-401",
        "create-422",
        "list-502",
        "artifact-500",
    ],
)
async def test_nextext_failures_settle_the_clip_failed(
    env: Env, audit_stream: CapturedAuditStream, script: dict[str, Any], token: str, created: bool
) -> None:
    """Each failure at Nextext settles the clip failed by its token, with an error child; a job made is deleted."""
    for name, value in script.items():
        setattr(env.fake, name, value)
    job = _clip(env)
    before = len(events(audit_stream))
    await _run(env.processor, job)
    assert _status(env, job) == ("failed", token)
    [failed] = [row for row in _rows(audit_stream, before) if row[4] == "error"]
    assert failed == ("disclose", ATTACHMENT, NEXTEXT_RECIPIENT, {"reason": token}, "error")
    assert env.fake.deleted == ([JOB_ID] if created else [])
    deletes = [row for row in _rows(audit_stream, before) if row[1] == NEXTEXT_OPERATION]
    assert deletes == ([("delete", NEXTEXT_OPERATION, None, {"deleted": True}, "ok")] if created else [])
    assert _prepares(env.door) == [] and env.door.derived == []
    assert env.health.healthy is (token != "nextext_unavailable")
    assert env.processor.owned_jobs() == frozenset()


@pytest.mark.parametrize(
    ("job", "token"),
    [
        (("failed", "inference_unavailable"), "nextext_unavailable"),
        (("failed", "undecodable_media"), "undecodable_media"),
        (("failed", "internal"), "nextext_failed"),
        (("failed", "timeout"), "nextext_failed"),
        (("failed", None), "nextext_failed"),
        (("interrupted", "inference_unavailable"), "nextext_failed"),
        (("gone", None), "nextext_failed"),
        (("completed", None), None),
    ],
)
def test_a_job_ends_as_its_status_and_code_say(job: tuple[str, str | None], token: str | None) -> None:
    """Only a failed job names its cause: inference not reached, or a clip not decodable; any other end is a failure."""
    status, code = job
    ended = NextextJob(JOB_ID, status, code, False, None, datetime(2026, 11, 22, tzinfo=UTC))
    assert processing._ended_as(ended) == token


def _spy_on_failed(env: Env, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count the calls to the probe's ``failed``, which still runs; return the list each call appends to."""
    calls: list[str] = []
    real = env.health.failed

    def failed() -> None:
        calls.append("failed")
        real()

    monkeypatch.setattr(env.health, "failed", failed)
    return calls


async def test_a_job_that_failed_on_inference_turns_clips_off_until_the_probe_passes(
    env: Env, audit_stream: CapturedAuditStream, log_lines: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nextext's job failed inference_unavailable: the clip is nextext_unavailable, and the probe is told at once."""
    env.fake.statuses, env.fake.error_code = ["running", "failed"], "inference_unavailable"
    probed = _spy_on_failed(env, monkeypatch)
    job = _clip(env)
    before = len(events(audit_stream))
    await _run(env.processor, job)
    assert _status(env, job) == ("failed", "nextext_unavailable")
    assert probed == ["failed"] and env.health.healthy is False
    [failed] = [row for row in _rows(audit_stream, before) if row[4] == "error"]
    assert failed == ("disclose", ATTACHMENT, NEXTEXT_RECIPIENT, {"reason": "nextext_unavailable"}, "error")
    assert env.fake.deleted == [JOB_ID]
    assert any(
        "ended failed (inference_unavailable): nextext_unavailable" in line and job.attachment_id in line
        for line in log_lines
    )
    assert not [line for line in log_lines if FAILURE_TEXT in line or NEXTEXT_KEY in line]


async def test_a_failed_job_of_another_cause_leaves_the_probe_alone(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A job that failed internal or undecodable_media says nothing of Nextext's inference: the probe is not told."""
    probed = _spy_on_failed(env, monkeypatch)
    for code in ("internal", "undecodable_media"):
        env.fake.statuses, env.fake.error_code = ["failed"], code
        await _run(env.processor, _clip(env))
    assert probed == [] and env.health.healthy is True


async def test_a_video_without_a_sound_track_is_ready_with_its_frames(
    env: Env, audit_stream: CapturedAuditStream
) -> None:
    """Nextext 1.22 skips it (no_audio_stream) with keyframes.zip alone: the zip goes to workbench, it is ready."""
    env.fake.skipped, env.fake.skip_reason_code = True, "no_audio_stream"
    env.fake.artifacts[JSONL] = 404
    job = _clip(env)
    await _run(env.processor, job)
    assert _status(env, job) == ("ready", None)
    assert env.door.derived == [(job.path, "nextext_zip", env.fake.artifacts[KEYFRAMES])]
    found = env.store.find_attachment("user-a", env.chat_id, job.attachment_id)
    assert found is not None and found.manifest is not None
    assert (found.manifest["transcript"], found.manifest["total"]) == (False, 3)
    assert env.fake.events == ["create", "list", f"delete:{JOB_ID}"] and env.fake.deleted == [JOB_ID]
    assert env.health.healthy is True
    assert [e for e in events(audit_stream) if e["outcome"] == "error"] == []


async def test_a_vanished_job_fails_at_the_next_poll(env: Env, audit_stream: CapturedAuditStream) -> None:
    """Nextext restarted: the job is gone from the list, so the clip fails now; the delete's 404 is a delete."""
    env.fake.statuses, env.fake.vanish_after = ["running"], 1
    job = _clip(env)
    before = len(events(audit_stream))
    await _run(env.processor, job)
    assert _status(env, job) == ("failed", "nextext_failed")
    assert env.fake.jobs[JOB_ID] <= 3
    assert env.fake.events[-1] == f"delete:{JOB_ID}" and env.fake.deleted == []
    deletes = [row for row in _rows(audit_stream, before) if row[1] == NEXTEXT_OPERATION]
    assert deletes == [("delete", NEXTEXT_OPERATION, None, {"deleted": True}, "ok")]
    assert env.health.healthy is True


@pytest.mark.parametrize(("vanish_after", "seen"), [(None, "completed"), (1, "gone")], ids=["made-none", "gone"])
async def test_a_completed_job_that_served_nothing_fails_the_clip(
    env: Env, audit_stream: CapturedAuditStream, log_lines: list[str], vanish_after: int | None, seen: str
) -> None:
    """Not skipped, yet neither artifact came: polled once more, still listed or gone, the clip fails nextext_failed."""
    env.fake.artifacts = {JSONL: 404, KEYFRAMES: 404}
    env.fake.vanish_after = vanish_after
    job = _clip(env)
    before = len(events(audit_stream))
    await _run(env.processor, job)
    assert _status(env, job) == ("failed", "nextext_failed")
    assert env.fake.events == ["create", "list", "list", f"delete:{JOB_ID}"]
    assert env.door.derived == [] and _prepares(env.door) == []
    rows = _rows(audit_stream, before)
    assert [row for row in rows if row[4] == "error"] == [
        ("disclose", ATTACHMENT, NEXTEXT_RECIPIENT, {"reason": "nextext_failed"}, "error")
    ]
    assert [row for row in rows if row[1] == NEXTEXT_OPERATION] == [
        ("delete", NEXTEXT_OPERATION, None, {"deleted": True}, "ok")
    ]
    assert any(f"({seen}) served no transcript" in line and job.attachment_id in line for line in log_lines)
    assert env.health.healthy is True


async def test_audio_without_speech_is_ready_with_neither_artifact(env: Env) -> None:
    """Skipped for no speech, audio has no transcript and no frames: no extra poll, and the clip is ready."""
    env.fake.skipped, env.fake.skip_reason_code = True, "vad_no_speech"
    env.fake.artifacts = {JSONL: 404, KEYFRAMES: 404}
    job = _clip(env, "voice-memo.m4a")
    await _run(env.processor, job)
    assert _status(env, job) == ("ready", None)
    assert env.fake.events == ["create", "list", f"delete:{JOB_ID}"] and env.door.derived == []


async def test_a_transcript_past_its_cap_counts_as_served(env: Env) -> None:
    """Audio whose transcript is over 8 MiB: it is left out but was served, so the clip is ready, not failed."""
    env.fake.declared[JSONL] = ARTIFACT_CAPS[JSONL] + 1
    env.fake.artifacts[KEYFRAMES] = 404
    job = _clip(env, "voice-memo.m4a")
    await _run(env.processor, job)
    assert _status(env, job) == ("ready", None)
    assert env.fake.events == ["create", "list", f"delete:{JOB_ID}"] and env.door.derived == []


def _flaky(env: Env, script: list[Exception | int | None]) -> NextextClient:
    """Return a client whose polls, once a job is made, answer each entry of ``script`` in turn, then as the fake.

    Args:
        env: The test's env.
        script: An exception to raise, a status to answer, or ``None`` to let the fake answer.

    Returns:
        The client.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        polled = (request.method, request.url.path) == ("GET", "/api/v1/jobs")
        if polled and "create" in env.fake.events and script:
            answer = script.pop(0)
            if isinstance(answer, Exception):
                raise answer
            if answer is not None:
                return httpx.Response(answer, json={"detail": "refused"})
        return await env.fake(request)

    return NextextClient(env.settings, transport=httpx.MockTransport(handler))


async def test_a_clip_rides_out_failed_polls_short_of_four_in_a_row(
    env: Env, audit_stream: CapturedAuditStream
) -> None:
    """Three failed polls in a row are ridden out, and a poll that passes starts the count over: the clip is ready."""
    assert POLL_TOLERATED == 3
    env.fake.statuses = ["running", "completed"]
    hiccups: list[Exception | int | None] = [httpx.ReadTimeout("slow"), 503, httpx.ConnectError("reset")]
    processor = await env.make(client=_flaky(env, [*hiccups, None, *hiccups, None]))
    job = _clip(env)
    before = len(events(audit_stream))
    await _run(processor, job)
    assert _status(env, job) == ("ready", None)
    assert env.fake.jobs[JOB_ID] == 2 and env.fake.deleted == [JOB_ID]
    assert env.health.healthy is True
    assert [row for row in _rows(audit_stream, before) if row[4] == "error"] == []


async def test_a_fourth_failed_poll_in_a_row_fails_the_clip(env: Env, audit_stream: CapturedAuditStream) -> None:
    """Four polls in a row fail: the clip fails nextext_unavailable, the probe is told, and the job is deleted."""
    env.fake.statuses = ["running"]
    processor = await env.make(client=_flaky(env, [503] * (POLL_TOLERATED + 1)))
    job = _clip(env)
    before = len(events(audit_stream))
    await _run(processor, job)
    assert _status(env, job) == ("failed", "nextext_unavailable")
    assert env.fake.jobs[JOB_ID] == 0
    assert env.fake.deleted == [JOB_ID] and env.health.healthy is False
    [failed] = [row for row in _rows(audit_stream, before) if row[4] == "error"]
    assert failed == ("disclose", ATTACHMENT, NEXTEXT_RECIPIENT, {"reason": "nextext_unavailable"}, "error")


@pytest.mark.parametrize(
    "script",
    [{"create_status": 401}, {"create_status": 403}, {"list_status": 401}],
    ids=["create-401", "create-403", "list-401"],
)
async def test_a_refused_key_logs_one_error_and_turns_clips_off(
    env: Env, loguru_caplog: pytest.LogCaptureFixture, script: dict[str, Any]
) -> None:
    """Nextext refuses the key for two clips: one error names NEXTEXT_API_KEY, never its value, and clips are off."""
    for name, value in script.items():
        setattr(env.fake, name, value)
    first, second = _clip(env), _clip(env, "quay-walk.mov")
    await _run(env.processor, first)
    await _run(env.processor, second)
    assert [_status(env, job) for job in (first, second)] == [("failed", "nextext_unavailable")] * 2
    assert (env.health.healthy, env.health.key_refused) == (False, True)
    errors = [record.getMessage() for record in loguru_caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1 and "NEXTEXT_API_KEY" in errors[0] and NEXTEXT_KEY not in errors[0]


async def test_a_delete_nextext_refuses_is_recorded_as_an_error(
    env: Env, audit_stream: CapturedAuditStream, loguru_caplog: pytest.LogCaptureFixture
) -> None:
    """Nextext refuses the delete: the clip is still ready, the delete is recorded ``error``, and a warning says so."""
    env.fake.delete_status = 500
    job = _clip(env)
    before = len(events(audit_stream))
    await _run(env.processor, job)
    assert _status(env, job) == ("ready", None)
    deletes = [row for row in _rows(audit_stream, before) if row[1] == NEXTEXT_OPERATION]
    assert deletes == [("delete", NEXTEXT_OPERATION, None, {"deleted": False}, "error")]
    assert env.processor.owned_jobs() == frozenset()
    assert any(JOB_ID in record.getMessage() for record in loguru_caplog.records)
    assert [record for record in loguru_caplog.records if record.levelno >= logging.ERROR] == []


async def test_the_budget_ends_a_slow_job(env: Env, audit_stream: CapturedAuditStream) -> None:
    """A job still running when the budget is spent fails the clip with timeout, and the job is deleted."""
    env.fake.statuses = ["running"]
    processor = await env.make(timeout_s=0.2)
    job = _clip(env)
    before = len(events(audit_stream))
    with anyio.fail_after(5):
        await _run(processor, job)
    assert _status(env, job) == ("failed", "timeout")
    assert env.fake.deleted == [JOB_ID] and env.fake.jobs[JOB_ID] > 1
    [failed] = [row for row in _rows(audit_stream, before) if row[4] == "error"]
    assert failed == ("disclose", ATTACHMENT, NEXTEXT_RECIPIENT, {"reason": "timeout"}, "error")


async def test_a_budget_spent_in_workbench_is_recorded_under_workbench(
    env: Env, audit_stream: CapturedAuditStream
) -> None:
    """The budget runs out while the transcript streams into workbench: the error child is workbench's hand-over's."""
    env.door.delays["/files/derived"] = 5.0
    processor = await env.make(timeout_s=0.3)
    job = _clip(env)
    before = len(events(audit_stream))
    with anyio.fail_after(5):
        await _run(processor, job)
    assert _status(env, job) == ("failed", "timeout")
    assert env.fake.deleted == [JOB_ID] and env.fake.served == [JSONL]
    [failed] = [row for row in _rows(audit_stream, before) if row[4] == "error"]
    assert failed == ("disclose", ATTACHMENT, "workbench:prepare", {"reason": "timeout"}, "error")


async def test_an_oversized_keyframes_zip_is_left_out(env: Env, log_lines: list[str]) -> None:
    """A zip that declares more than 32 MiB is not taken: the transcript alone goes on, and the clip is ready."""
    env.fake.declared[KEYFRAMES] = ARTIFACT_CAPS[KEYFRAMES] + 1
    job = _clip(env)
    await _run(env.processor, job)
    assert _status(env, job) == ("ready", None)
    assert env.door.derived == [(job.path, "nextext_jsonl", env.fake.artifacts[JSONL])]
    assert KEYFRAMES not in env.fake.served
    assert any("artifact_too_large" in line and job.attachment_id in line for line in log_lines)
    assert env.fake.deleted == [JOB_ID]


async def test_an_artifact_past_its_cap_midstream_is_left_out(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A zip that declares no length and passes the cap as it streams is left out; workbench keeps none of it."""
    monkeypatch.setitem(nextext.ARTIFACT_CAPS, KEYFRAMES, 8)
    env.fake.artifacts[KEYFRAMES] = b"PK\x03\x04" + bytes(20)
    env.fake.declared[KEYFRAMES] = None
    job = _clip(env)
    await _run(env.processor, job)
    assert _status(env, job) == ("ready", None)
    assert KEYFRAMES in env.fake.served
    assert env.door.derived == [(job.path, "nextext_jsonl", env.fake.artifacts[JSONL])]


@pytest.mark.parametrize(
    ("error", "token"), [(httpx.ReadTimeout("slow"), "timeout"), (httpx.ReadError("reset"), "unreachable")]
)
async def test_a_clip_workbench_fails_to_stream_is_not_blamed_on_nextext(
    env: Env, audit_stream: CapturedAuditStream, error: Exception, token: str
) -> None:
    """A read of the clip from workbench that fails midway is workbench's: Nextext stays healthy, nothing is made."""

    def broken(folder: str) -> httpx.Response:
        async def body() -> AsyncIterator[bytes]:
            yield CLIP[:16]
            raise error

        return httpx.Response(200, content=body(), headers={"Content-Type": "video/mp4"})

    env.door.answers["/files/view"] = broken
    job = _clip(env)
    before = len(events(audit_stream))
    await _run(env.processor, job)
    assert _status(env, job) == ("failed", token)
    assert env.health.healthy is True
    assert env.fake.uploads == [] and env.fake.deleted == []
    [failed] = [row for row in _rows(audit_stream, before) if row[4] == "error"]
    assert failed == ("disclose", ATTACHMENT, NEXTEXT_RECIPIENT, {"reason": token}, "error")


# --- workbench's answers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "token"),
    [
        ({"detail": "unreadable_file: the clip holds no stream that can be decoded"}, "unreadable_file"),
        (
            {"detail": "unreadable_file: the clip holds no stream that can be decoded", "remembered": True},
            "unreadable_file",
        ),
        ({"detail": "unsupported_format: .mkv"}, "unsupported_format"),
        ({"detail": "read_timeout: the clip was not read in time"}, "timeout"),
    ],
    ids=["unreadable", "remembered", "unsupported", "read-timeout"],
)
async def test_a_clips_prepare_refusal_settles_it_failed(
    env: Env, audit_stream: CapturedAuditStream, answer: dict[str, Any], token: str
) -> None:
    """A clip workbench refuses to prepare is failed, by the refusal's code; read_timeout reads as timeout."""
    env.door.answers["/files/prepare"] = lambda folder: httpx.Response(422, json=answer)
    job = _clip(env)
    before = len(events(audit_stream))
    await _run(env.processor, job)
    assert _status(env, job) == ("failed", token)
    assert len(_prepares(env.door)) == 1 and env.fake.deleted == [JOB_ID]
    [failed] = [row for row in _rows(audit_stream, before) if row[4] == "error"]
    assert failed == ("disclose", ATTACHMENT, "workbench:prepare", {"reason": token}, "error")


async def test_a_busy_prepare_of_a_clip_is_asked_once_more(env: Env) -> None:
    """A prepare that finds workbench busy is sent once more, as for any file, and the clip is ready."""
    asked: list[str] = []

    def busy_once(folder: str) -> httpx.Response | None:
        asked.append(folder)
        return httpx.Response(503, json={"detail": "busy, retry later"}) if len(asked) == 1 else None

    env.door.answers["/files/prepare"] = busy_once
    job = _clip(env)
    await _run(env.processor, job)
    assert _status(env, job) == ("ready", None) and len(_prepares(env.door)) == 2


@pytest.mark.parametrize(("busy", "status"), [(1, ("ready", None)), (2, ("failed", "http:503"))], ids=["once", "twice"])
async def test_a_busy_workbench_is_asked_once_more_for_an_artifact(
    env: Env, audit_stream: CapturedAuditStream, busy: int, status: tuple[str, str | None]
) -> None:
    """A derived file workbench takes no lock for is sent once more, fetched again; still busy, the clip fails."""
    answered: list[str] = []

    def busy_first(folder: str) -> httpx.Response | None:
        answered.append(folder)
        return httpx.Response(503, json={"detail": "busy, retry later"}) if len(answered) <= busy else None

    env.door.answers["/files/derived"] = busy_first
    job = _clip(env)
    before = len(events(audit_stream))
    await _run(env.processor, job)
    assert _status(env, job) == status
    assert env.fake.deleted == [JOB_ID]
    if busy == 1:
        assert env.fake.served == [JSONL, JSONL, KEYFRAMES]
        assert [role for _, role, _ in env.door.derived] == ["nextext_jsonl", "nextext_zip"]
        assert [row for row in _rows(audit_stream, before) if row[4] == "error"] == []
    else:
        assert env.fake.served == [JSONL, JSONL] and env.door.derived == [] and _prepares(env.door) == []
        [failed] = [row for row in _rows(audit_stream, before) if row[4] == "error"]
        assert failed == ("disclose", ATTACHMENT, "workbench:prepare", {"reason": "http:503"}, "error")


# --- cancels and records --------------------------------------------------------------


async def test_a_chat_deleted_mid_job_still_deletes_the_nextext_job(
    env: Env, audit_stream: CapturedAuditStream, loguru_caplog: pytest.LogCaptureFixture
) -> None:
    """The chat's delete cancels the clip at Nextext: the job is still deleted and that recorded, nothing else."""
    env.fake.statuses = ["running"]
    job = _clip(env)
    env.processor.submit(job)
    await _until(lambda: env.fake.jobs.get(JOB_ID, 0) >= 1)
    assert env.processor.owned_jobs() == frozenset({JOB_ID})
    before = len(events(audit_stream))
    assert env.processor.cancel_chat(env.chat_id) == 1
    await env.processor.wait(job.attachment_id, 5)
    assert not env.processor.running(job.attachment_id)
    assert _status(env, job) == ("processing", None)
    assert env.fake.deleted == [JOB_ID] and env.processor.owned_jobs() == frozenset()
    assert _rows(audit_stream, before) == [("delete", NEXTEXT_OPERATION, None, {"deleted": True}, "ok")]
    assert [record for record in loguru_caplog.records if record.levelno >= logging.ERROR] == []


async def test_a_clip_taken_back_mid_job_still_deletes_the_nextext_job(
    env: Env, audit_stream: CapturedAuditStream, loguru_caplog: pytest.LogCaptureFixture
) -> None:
    """Taken back while at Nextext, as the route does it: the job is deleted and recorded; nothing settles the row."""
    env.fake.statuses = ["running"]
    job = _clip(env)
    env.processor.submit(job)
    await _until(lambda: env.fake.jobs.get(JOB_ID, 0) >= 1)
    before = len(events(audit_stream))
    assert env.processor.cancel(job.attachment_id)
    assert env.store.remove_pending_attachment("user-a", env.chat_id, job.attachment_id)
    await env.processor.wait(job.attachment_id, 5)
    assert not env.processor.running(job.attachment_id)
    assert env.store.find_attachment("user-a", env.chat_id, job.attachment_id) is None
    assert env.fake.deleted == [JOB_ID] and env.processor.owned_jobs() == frozenset()
    assert _rows(audit_stream, before) == [("delete", NEXTEXT_OPERATION, None, {"deleted": True}, "ok")]
    assert [record for record in loguru_caplog.records if record.levelno >= logging.ERROR] == []


async def test_a_clip_kept_by_a_failed_take_back_runs_its_round_trip_again(
    env: Env, audit_stream: CapturedAuditStream
) -> None:
    """After a take-back whose delete failed, the chat is requeued: the clip goes to Nextext again, a new job."""
    env.fake.statuses = ["running"]
    job = _clip(env)
    env.processor.submit(job)
    await _until(lambda: env.fake.jobs.get(JOB_ID, 0) >= 1)
    assert env.processor.cancel(job.attachment_id)
    env.fake.statuses = ["completed"]
    before = len(events(audit_stream))
    assert await env.processor.requeue_chat(env.chat_id) == 1
    await env.processor.wait(job.attachment_id, 5)
    assert _status(env, job) == ("ready", None)
    assert len(env.fake.uploads) == 2 and env.fake.deleted == [JOB_ID, SECOND_JOB]
    rows = _rows(audit_stream, before)
    assert rows[0] == ("delete", NEXTEXT_OPERATION, None, {"deleted": True}, "ok")
    assert rows[1:] == [
        ("disclose", ATTACHMENT, NEXTEXT_RECIPIENT, {"kind": "media", "requeued": True}, "ok"),
        ("disclose", ATTACHMENT, "workbench:prepare", {"kind": "media"}, "ok"),
        ("delete", NEXTEXT_OPERATION, None, {"deleted": True}, "ok"),
    ]


async def test_an_unrecorded_hand_over_sends_nothing_to_nextext(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: the hand-over to Nextext cannot be written, so nothing is read or sent, and the clip fails."""
    job = _clip(env)
    unrecorded_child(monkeypatch, ATTACHMENT)
    await _run(env.processor, job)
    assert env.fake.requests == [] and env.door.requests == []
    assert _status(env, job) == ("failed", "audit")


async def test_an_unrecorded_hand_over_to_workbench_sends_no_artifact(
    env: Env, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artifacts' hand-over to workbench cannot be written: none goes, the job is still deleted, the clip fails."""
    original = Operation.child

    def child(self: Operation, action: str, name: str, **fields: Any) -> str:
        if fields.get("recipient") == "workbench:prepare":
            raise AuditWriteError("synthetic audit stream failure")
        return original(self, action, name, **fields)

    monkeypatch.setattr(Operation, "child", child)
    job = _clip(env)
    before = len(events(audit_stream))
    await _run(env.processor, job)
    assert _status(env, job) == ("failed", "audit")
    assert env.door.derived == [] and _prepares(env.door) == [] and env.fake.served == []
    assert env.fake.deleted == [JOB_ID]
    assert _rows(audit_stream, before) == [
        ("disclose", ATTACHMENT, NEXTEXT_RECIPIENT, {"kind": "media", "bytes": len(CLIP)}, "ok"),
        ("delete", NEXTEXT_OPERATION, None, {"deleted": True}, "ok"),
    ]


# --- slots and start ------------------------------------------------------------------------


async def test_a_picture_is_prepared_while_clips_transcribe(env: Env) -> None:
    """Two clips at Nextext hold no prepare slot: a picture attached meanwhile is ready while both still run."""
    env.fake.statuses = ["running"]
    clips = [_clip(env, "harbour-tour.mp4"), _clip(env, "quay-walk.mov")]
    for job in clips:
        env.processor.submit(job)
    await _until(lambda: len(env.fake.jobs) == 2)
    picture = _picture(env)
    with anyio.fail_after(5):
        await _run(env.processor, picture)
    assert (MEDIA_SLOTS, PREPARE_SLOTS) == (2, 2)
    assert _status(env, picture) == ("ready", None)
    assert [_status(env, job) for job in clips] == [("processing", None)] * 2
    assert all(env.processor.running(job.attachment_id) for job in clips)


async def test_at_most_two_clips_are_at_nextext_at_once(env: Env, audit_stream: CapturedAuditStream) -> None:
    """A third clip waits for a media slot, and takes the one a cancelled clip leaves.

    The first two make their jobs in either order, so the cancelled clip's job is read from its own delete's record.
    """
    env.fake.statuses = ["running"]
    first, second, third = (_clip(env, f"harbour-tour-{n}.mp4") for n in range(3))
    for job in (first, second, third):
        env.processor.submit(job)
    await _until(lambda: len(env.fake.jobs) == 2)
    await anyio.sleep(0.2)
    assert len(env.fake.jobs) == 2 and len(env.fake.uploads) == 2
    assert env.processor.cancel(first.attachment_id)
    await _until(lambda: len(env.fake.jobs) == 3)
    assert first.op is not None
    [own] = [
        e
        for e in events(audit_stream)
        if e["operation"] == NEXTEXT_OPERATION and e.get("parent_event_id") == first.op.event_id
    ]
    assert own["target"]["ids"] in ([JOB_ID], [SECOND_JOB])
    assert env.fake.deleted == own["target"]["ids"]


async def test_a_requeued_clip_runs_again(env: Env, audit_stream: CapturedAuditStream) -> None:
    """A clip left processing by the last process goes the whole way again; its hand-over is the system's."""
    job = _clip(env, requeued=True)
    before = len(events(audit_stream))
    assert env.processor.requeue() == 1
    await env.processor.wait(job.attachment_id, 5)
    assert _status(env, job) == ("ready", None)
    assert env.fake.deleted == [JOB_ID]
    assert _rows(audit_stream, before) == [
        ("disclose", ATTACHMENT, NEXTEXT_RECIPIENT, {"kind": "media", "requeued": True}, "ok"),
        ("disclose", ATTACHMENT, "workbench:prepare", {"kind": "media"}, "ok"),
        ("delete", NEXTEXT_OPERATION, None, {"deleted": True}, "ok"),
    ]
    primary, *children = events(audit_stream)[before:]
    assert (primary["actor"], primary.get("parent_event_id")) == (SYSTEM, None)
    assert primary["target"] == {"kind": "attachment", "ids": [job.attachment_id], "container": env.chat_id}
    assert {child["parent_event_id"] for child in children} == {primary["event_id"]}


async def test_without_nextext_a_clip_fails_as_unavailable(env: Env, audit_stream: CapturedAuditStream) -> None:
    """Without a Nextext client the clip fails as unavailable; nothing is read or sent, so no hand-over is recorded."""
    processor = await env.make(client=None)
    job = _clip(env)
    before = len(events(audit_stream))
    await _run(processor, job)
    assert _status(env, job) == ("failed", "nextext_unavailable")
    assert env.fake.requests == [] and env.door.requests == []
    assert _rows(audit_stream, before) == []


async def test_no_job_is_made_before_the_gate_opens(env: Env) -> None:
    """A clip waits for start_media before its create; once the gate opens, it goes on."""
    processor = await env.make(start=False)
    job = _clip(env)
    processor.submit(job)
    await anyio.sleep(0.2)
    assert env.fake.requests == [] and processor.running(job.attachment_id)
    processor.start_media()
    await processor.wait(job.attachment_id, 5)
    assert _status(env, job) == ("ready", None)


async def test_a_gate_that_never_opens_fails_the_clip(
    env: Env, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the gate's whole wait without the start reconcile, nothing goes to Nextext, nor is a hand-over recorded."""
    monkeypatch.setattr(processing, "START_WAIT_S", 0.05)
    monkeypatch.setattr(processing, "RECONCILE_EXTRA_S", 0.05)
    monkeypatch.setattr(processing, "_GATE_SLACK_S", 0.05)
    processor = await env.make(start=False)
    job = _clip(env)
    before = len(events(audit_stream))
    await _run(processor, job)
    assert _status(env, job) == ("failed", "nextext_unavailable")
    assert env.fake.requests == [] and env.door.requests == []
    assert _rows(audit_stream, before) == []


# --- logs ----------------------------------------------------------------------------------------


async def test_the_log_holds_no_names(env: Env, log_lines: list[str]) -> None:
    """A clip that goes through and one that fails: the log names ids and tokens, never a name, a line or a key."""
    env.fake.artifacts[JSONL] = f'{{"text": "{SPOKEN}"}}\n'.encode()
    ready = _clip(env)
    await _run(env.processor, ready)
    env.fake.statuses, env.fake.error_code = ["failed"], "internal"
    failed = _clip(env, "quay-walk.mov")
    await _run(env.processor, failed)
    assert (_status(env, ready), _status(env, failed)) == (("ready", None), ("failed", "nextext_failed"))
    text = "\n".join(log_lines)
    assert ready.attachment_id in text and failed.attachment_id in text
    for kept in ("harbour-tour", "quay-walk", SPOKEN, FAILURE_TEXT, NEXTEXT_KEY, KEY, ready.path, "media.mp4"):
        assert kept not in text
