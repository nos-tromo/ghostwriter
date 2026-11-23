"""The reconcile: Ghostwriter's Nextext jobs no clip owns are deleted at start, after a lost create, and daily.

A store, a :class:`~ghostwriter.workbench.WorkbenchClient` over :class:`~tests.fakes.FakeDoor` (workbench 0.9.0), a
:class:`~ghostwriter.nextext.NextextClient` over :class:`~tests.fakes.FakeNextext` and an emitter on
:class:`~tests.conftest.CapturedAuditStream`; ``POLL_S`` is 0.01 s. A job made by the fake carries a ``created_at`` of
the day before, so it is old to every grace; the jobs listed here carry times around now. Every name, id and value
here is invented.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ghostwriter import __version__, nextext, processing
from ghostwriter.audit import Operation, open_emitter
from ghostwriter.config import Settings
from ghostwriter.db import init_db, make_engine
from ghostwriter.main import create_app
from ghostwriter.models import Attachment
from ghostwriter.nextext import NextextClient, NextextHealth
from ghostwriter.processing import NEXTEXT_OPERATION, RECONCILE_EXTRA_S, AttachmentProcessor, PrepareJob
from ghostwriter.store import ChatStore
from ghostwriter.utils.audit_event import AuditEmitter, AuditWriteError
from ghostwriter.workbench import WorkbenchClient, chat_folder
from tests.conftest import KEY, NEXTEXT_BASE, NEXTEXT_KEY, CapturedAuditStream, as_user, events, make_settings
from tests.fakes import JOB_CREATED_AT, JOB_ID, FakeDoor, FakeNextext

CLIP = b"\x00\x00\x00\x18ftypmp42" + bytes(200)
#: The id FakeNextext gives its second and third job.
SECOND_JOB = f"{JOB_ID[:-4]}0001"
THIRD_JOB = f"{JOB_ID[:-4]}0002"
#: Jobs a process that ended left at Nextext.
ORPHAN = "0a1b2c3d4e5f40718293a4b5c6d7e8f9"
OLD_ORPHAN = "1b2c3d4e5f60718293a4b5c6d7e8f90a"
#: A job Nextext made a moment ago: another clip's, whose create has not answered yet.
YOUNG = "2c3d4e5f60718293a4b5c6d7e8f90a1b"
SYSTEM = {"user": None, "via": "system"}
Handler = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]


@dataclass
class Env:
    """One test's store, workbench, Nextext and chain, with one chat.

    Attributes:
        settings: The test settings, Nextext on.
        engine: The database.
        store: The chats.
        door: workbench 0.9.0.
        fake: Nextext.
        health: Nextext's health over :attr:`fake`, healthy.
        emitter: The audit chain.
        chat_id: The chat the clips go to.
        made: Every processor the test built, closed at teardown.
    """

    settings: Settings
    engine: Engine
    store: ChatStore
    door: FakeDoor
    fake: FakeNextext
    health: NextextHealth
    emitter: AuditEmitter
    chat_id: str
    made: list[AttachmentProcessor] = field(default_factory=list)

    def client(self, handler: Handler | None = None) -> NextextClient:
        """Return a Nextext client over the fake, or over a handler in front of it.

        Args:
            handler: What answers instead of the fake; ``None`` for the fake.

        Returns:
            The client.
        """
        return NextextClient(self.settings, transport=httpx.MockTransport(handler or self.fake))

    def make(
        self, *, client: NextextClient | None | bool = True, health: NextextHealth | None = None
    ) -> AttachmentProcessor:
        """Build a processor over this env, its start reconcile not started, and close it at teardown.

        Args:
            client: The Nextext client; ``True`` for one over the fake, ``None`` for none.
            health: Its health; :attr:`health` when ``None``.

        Returns:
            The processor.
        """
        reached = self.client() if client is True else client if isinstance(client, NextextClient) else None
        processor = AttachmentProcessor(
            self.store,
            WorkbenchClient(self.settings, transport=httpx.MockTransport(self.door)),
            self.emitter,
            nextext=reached,
            health=(health or self.health) if reached is not None else None,
        )
        self.made.append(processor)
        return processor

    def clip(self, name: str = "harbour-tour.mp4") -> PrepareJob:
        """Store a ``processing`` clip whose file the door holds, and return its job, as requeued at start.

        Args:
            name: The file's name.

        Returns:
            The job, with no request's operation.
        """
        path = f"{chat_folder(KEY, self.chat_id)}/{name}"
        self.door.files[path] = (CLIP, "video/mp4")
        out = self.store.add_attachment(
            self.chat_id,
            file_id=uuid4().hex,
            path=path,
            name=name,
            size=len(CLIP),
            mime="video/mp4",
            kind="media",
            status="processing",
        )
        return PrepareJob(out.id, self.chat_id, "user-a", path, "media", None)

    def listed(self, job_id: str, made: datetime, status: str = "running") -> None:
        """List a job at Nextext that no clip of this process made.

        Args:
            job_id: Its id.
            made: When Nextext made it.
            status: Its status.
        """
        self.fake.listed.append(self.fake.item(job_id, status, created_at=_iso(made)))


@pytest.fixture
async def env(tmp_path: Path, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Env]:
    """Build the store, the chain on ``audit_stream``, workbench and Nextext behind their fakes."""
    monkeypatch.setattr(processing, "POLL_S", 0.01)
    settings = make_settings(tmp_path, vision=True, nextext_api_base=NEXTEXT_BASE, nextext_api_key=NEXTEXT_KEY)
    engine = make_engine(settings.db_path)
    init_db(engine)
    store = ChatStore(engine, file_days=settings.file_days, notice_days=settings.notice_days)
    emitter = open_emitter(__version__, audit_stream)
    emitter.start()
    fake = FakeNextext()
    health = NextextHealth(NextextClient(settings, transport=httpx.MockTransport(fake)))
    health.healthy = True
    built = Env(
        settings, engine, store, FakeDoor(server_version="0.9.0"), fake, health, emitter, store.create_chat("user-a").id
    )
    try:
        yield built
    finally:
        for processor in built.made:
            await processor.aclose()
        emitter.close()
        engine.dispose()


def _status(env: Env, job: PrepareJob) -> tuple[str, str | None]:
    """Return the attachment's status and failure token, as stored."""
    with Session(env.engine) as s:
        status, failure = s.execute(
            select(Attachment.status, Attachment.failure).where(Attachment.id == job.attachment_id)
        ).one()
        return status, failure


def _reconciles(stream: CapturedAuditStream, before: int = 0) -> list[dict[str, Any]]:
    """Return the reconciles' events since ``before``: the system's ``chat.nextext`` by count, primaries and children.

    A clip's own ``chat.nextext`` child names its job by id, and a requeued clip's is the system's too.
    """
    return [
        e
        for e in events(stream)[before:]
        if e["operation"] == NEXTEXT_OPERATION and e["actor"] == SYSTEM and "ids" not in e["target"]
    ]


def _iso(moment: datetime) -> str:
    """Return a time as Nextext writes it."""
    return moment.isoformat().replace("+00:00", "Z")


def _row(event: dict[str, Any]) -> tuple[Any, ...]:
    """Return an event as its action, target, criteria and outcome."""
    return (event["action"], event["target"], event.get("criteria"), event["outcome"])


async def _until(condition: Callable[[], bool]) -> None:
    """Wait until the condition holds, at most 5 s."""
    with anyio.fail_after(5):
        while not condition():
            await anyio.sleep(0.01)


async def _settle(processor: AttachmentProcessor, job: PrepareJob) -> None:
    """Wait until the clip's job has ended, at most 5 s."""
    await _until(lambda: not processor.running(job.attachment_id))


# --- at start -----------------------------------------------------------------------


async def test_the_start_reconcile_deletes_every_job_listed(env: Env, audit_stream: CapturedAuditStream) -> None:
    """At start no clip can own a job, so all are deleted, one even ahead of Ghostwriter's clock; recorded by count."""
    now = datetime.now(UTC)
    env.listed(ORPHAN, now - timedelta(hours=3))
    env.listed(OLD_ORPHAN, now - timedelta(hours=2), "completed")
    env.listed(YOUNG, now + timedelta(seconds=30))
    processor = env.make()
    processor.start_media()
    await _until(lambda: len(env.fake.deleted) == 3)
    assert env.fake.events == ["list", f"delete:{ORPHAN}", f"delete:{OLD_ORPHAN}", f"delete:{YOUNG}"]
    [event] = _reconciles(audit_stream)
    assert [e for e in events(audit_stream) if e["operation"] == NEXTEXT_OPERATION] == [event]
    assert (event["actor"], event.get("parent_event_id"), event.get("recipient")) == (SYSTEM, None, None)
    assert _row(event) == ("delete", {"kind": "job", "count": 3}, {"listed": 3, "at_start": True}, "ok")
    assert event["operation"] == NEXTEXT_OPERATION == "chat.nextext"


async def test_no_clip_creates_a_job_before_the_start_reconcile(env: Env, audit_stream: CapturedAuditStream) -> None:
    """Review Focus 5: a clip left processing waits for the start reconcile; the orphan goes, its new job never does."""
    env.listed(ORPHAN, datetime.now(UTC) - timedelta(minutes=5))
    job = env.clip()
    processor = env.make()
    assert processor.requeue() == 1
    processor.start_media()
    await _settle(processor, job)
    assert _status(env, job) == ("ready", None)
    assert env.fake.events[:3] == ["list", f"delete:{ORPHAN}", "create"]
    assert env.fake.events.count(f"delete:{JOB_ID}") == 1 and env.fake.deleted == [ORPHAN, JOB_ID]
    [reconciled] = _reconciles(audit_stream)
    assert _row(reconciled) == ("delete", {"kind": "job", "count": 1}, {"listed": 1, "at_start": True}, "ok")
    [handed] = [e for e in events(audit_stream) if e.get("recipient") == "nextext:transcribe"]
    [own] = [e for e in events(audit_stream) if e["operation"] == NEXTEXT_OPERATION and e != reconciled]
    assert (own["target"]["ids"], own["criteria"]) == ([JOB_ID], {"deleted": True})
    assert own["parent_event_id"] == handed["event_id"]
    assert events(audit_stream).index(reconciled) < events(audit_stream).index(handed)


def test_a_restart_deletes_the_orphan_before_the_requeued_clip_makes_its_job(
    tmp_path: Path, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Focus 5, through the lifespan: the probe passes, the orphan goes, then the requeued clip is ready."""
    monkeypatch.setattr(processing, "POLL_S", 0.01)
    settings = make_settings(tmp_path, vision=True, nextext_api_base=NEXTEXT_BASE, nextext_api_key=NEXTEXT_KEY)
    door, fake = FakeDoor(server_version="0.9.0"), FakeNextext()
    fake.listed.append(fake.item(ORPHAN, "running"))
    engine = make_engine(settings.db_path)
    init_db(engine)
    try:
        store = ChatStore(engine, file_days=settings.file_days, notice_days=settings.notice_days)
        cid = store.create_chat("user-a").id
        path = f"{chat_folder(KEY, cid)}/harbour-tour.mp4"
        door.files[path] = (CLIP, "video/mp4")
        aid = store.add_attachment(
            cid,
            file_id=uuid4().hex,
            path=path,
            name="harbour-tour.mp4",
            size=len(CLIP),
            mime="video/mp4",
            kind="media",
            status="processing",
        ).id
    finally:
        engine.dispose()
    app = create_app(
        settings,
        audit_stream=audit_stream,
        workbench_transport=httpx.MockTransport(door),
        nextext_transport=httpx.MockTransport(fake),
    )
    with TestClient(app) as client:
        r = client.get(f"/api/chats/{cid}/attachments/{aid}", params={"wait": 20}, headers=as_user())
        assert (r.status_code, r.json()["status"]) == (200, "ready")
    assert fake.events[:3] == ["list", f"delete:{ORPHAN}", "create"]
    assert fake.deleted == [ORPHAN, JOB_ID]
    [reconciled] = _reconciles(audit_stream)
    assert _row(reconciled) == ("delete", {"kind": "job", "count": 1}, {"listed": 1, "at_start": True}, "ok")


async def test_the_start_reconcile_waits_for_the_probe(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """While Nextext's health is not 200 nothing is listed; the list follows the first healthy probe."""
    monkeypatch.setattr(nextext, "PROBE_FIRST_S", 0.02)
    env.fake.health = 503
    env.listed(ORPHAN, datetime.now(UTC) - timedelta(hours=1))
    health = NextextHealth(env.client())
    processor = env.make(health=health)
    processor.start_media()
    probe = asyncio.create_task(health.run())
    try:
        await _until(lambda: sum(r.url.path.endswith("/health") for r in env.fake.requests) >= 3)
        assert "list" not in env.fake.events
        env.fake.health = 200
        await _until(lambda: env.fake.deleted == [ORPHAN])
    finally:
        probe.cancel()
        with suppress(asyncio.CancelledError):
            await probe
    routes = [(r.method, r.url.path.removeprefix("/api/v1")) for r in env.fake.requests]
    assert routes[-2:] == [("GET", "/jobs"), ("DELETE", f"/jobs/{ORPHAN}")]
    assert len(routes) >= 4 and set(routes[:-2]) == {("GET", "/health")}


async def test_a_reconcile_that_ends_after_the_probe_wait_lets_a_waiting_clip_through(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G15: the probe passes just inside START_WAIT_S and the list is slow; the clip waiting meanwhile still goes on."""
    monkeypatch.setattr(processing, "START_WAIT_S", 0.5)
    monkeypatch.setattr(processing, "RECONCILE_EXTRA_S", 0.5)
    monkeypatch.setattr(nextext, "PROBE_FIRST_S", 0.35)
    env.fake.health = 503
    env.listed(ORPHAN, datetime.now(UTC) - timedelta(hours=1))
    started = anyio.current_time()
    created: list[float] = []

    async def slow_list(request: httpx.Request) -> httpx.Response:
        if (request.method, request.url.path) == ("GET", "/api/v1/jobs") and "list" not in env.fake.events:
            await anyio.sleep(0.3)
        if request.method == "POST":
            created.append(anyio.current_time() - started)
        return await env.fake(request)

    client = env.client(slow_list)
    health = NextextHealth(client)
    job = env.clip()
    processor = env.make(client=client, health=health)
    assert processor.requeue() == 1
    processor.start_media()
    probe = asyncio.create_task(health.run())
    try:
        await _until(health.checked.is_set)
        env.fake.health = 200
        await _settle(processor, job)
    finally:
        probe.cancel()
        with suppress(asyncio.CancelledError):
            await probe
    assert _status(env, job) == ("ready", None)
    assert env.fake.events[:3] == ["list", f"delete:{ORPHAN}", "create"]
    assert created[0] > processing.START_WAIT_S


async def test_a_start_reconcile_past_its_time_opens_the_gate(
    env: Env, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deletes that hang past RECONCILE_EXTRA_S are cut off and recorded as an error; then the clips go on."""
    monkeypatch.setattr(processing, "RECONCILE_EXTRA_S", 0.2)
    env.listed(ORPHAN, datetime.now(UTC) - timedelta(hours=1))
    env.listed(OLD_ORPHAN, datetime.now(UTC) - timedelta(hours=2))

    async def hung_deletes(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and request.url.path.endswith((ORPHAN, OLD_ORPHAN)):
            await anyio.sleep(5)
        return await env.fake(request)

    job = env.clip()
    processor = env.make(client=env.client(hung_deletes))
    assert processor.requeue() == 1
    processor.start_media()
    await _settle(processor, job)
    assert _status(env, job) == ("ready", None)
    assert env.fake.deleted == [JOB_ID]
    primary, child = _reconciles(audit_stream)
    assert _row(primary) == ("delete", {"kind": "job", "count": 2}, {"listed": 2, "at_start": True}, "ok")
    assert _row(child) == ("delete", {"kind": "job", "count": 2}, None, "error")
    assert child["parent_event_id"] == primary["event_id"]


async def test_a_clip_waits_out_a_start_reconcile_at_both_its_limits(
    env: Env, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe passes at START_WAIT_S, the deletes take all of RECONCILE_EXTRA_S, the record is slow: clips go on.

    The clip that waited all along waits longer than all of that, so the gate's opening never races its own limit.
    """
    monkeypatch.setattr(processing, "START_WAIT_S", 0.2)
    monkeypatch.setattr(processing, "RECONCILE_EXTRA_S", 0.2)

    async def at_the_limit(self: NextextHealth, timeout_s: float) -> bool:
        await anyio.sleep(timeout_s)
        return True

    monkeypatch.setattr(NextextHealth, "wait_healthy", at_the_limit)
    original = Operation.child

    def slow_count(self: Operation, action: str, name: str, **fields: Any) -> str:
        target = fields.get("target")
        if name == NEXTEXT_OPERATION and target is not None and not target.ids:
            time.sleep(0.1)
        return original(self, action, name, **fields)

    monkeypatch.setattr(Operation, "child", slow_count)
    env.listed(ORPHAN, datetime.now(UTC) - timedelta(hours=1))

    async def hung_delete(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and request.url.path.endswith(ORPHAN):
            await anyio.sleep(5)
        return await env.fake(request)

    job = env.clip()
    processor = env.make(client=env.client(hung_delete))
    assert processor.requeue() == 1
    processor.start_media()
    await _settle(processor, job)
    assert _status(env, job) == ("ready", None)
    primary, child = _reconciles(audit_stream)
    assert (_row(primary)[3], _row(child)[3]) == ("ok", "error")


async def test_an_unrecorded_start_reconcile_deletes_nothing_and_opens_the_gate(
    env: Env, audit_stream: CapturedAuditStream, loguru_caplog: pytest.LogCaptureFixture
) -> None:
    """The record fails: nothing is deleted, the failure is logged, and a clip then makes its job."""
    env.listed(ORPHAN, datetime.now(UTC) - timedelta(hours=1))
    processor = env.make()
    audit_stream.failing = True
    processor.start_media()
    await _until(lambda: any("could not be recorded" in record.getMessage() for record in loguru_caplog.records))
    audit_stream.failing = False
    job = env.clip()
    processor.submit(job)
    await _settle(processor, job)
    assert _status(env, job) == ("ready", None)
    assert env.fake.deleted == [JOB_ID] and f"delete:{ORPHAN}" not in env.fake.events


# --- any reconcile ------------------------------------------------------------------


async def test_an_unrecorded_reconcile_deletes_nothing(env: Env, audit_stream: CapturedAuditStream) -> None:
    """Fail closed: the record cannot be written, so AuditWriteError is raised and no DELETE is sent."""
    env.listed(ORPHAN, datetime.now(UTC) - timedelta(hours=1))
    processor = env.make()
    audit_stream.failing = True
    with pytest.raises(AuditWriteError):
        await processor.reconcile(grace_s=None)
    audit_stream.failing = False
    assert env.fake.events == ["list"] and env.fake.deleted == []
    assert [e for e in events(audit_stream) if e["operation"] == NEXTEXT_OPERATION] == []


async def test_a_down_nextext_reconciles_nothing_and_clips_go_on(env: Env, audit_stream: CapturedAuditStream) -> None:
    """A list Nextext refuses deletes and records nothing; the gate opens, and the clip fails at its own poll."""
    env.fake.list_status = 503
    env.listed(ORPHAN, datetime.now(UTC) - timedelta(hours=1))
    assert await env.make(client=None).reconcile(grace_s=None) == 0
    processor = env.make()
    assert await processor.reconcile(grace_s=None) == 0
    job = env.clip()
    assert processor.requeue() == 1
    processor.start_media()
    await _settle(processor, job)
    assert _status(env, job) == ("failed", "nextext_unavailable")
    assert len(env.fake.uploads) == 1 and env.fake.deleted == [JOB_ID]
    assert _reconciles(audit_stream) == []


async def test_the_sweep_spares_owned_and_young_jobs(env: Env, audit_stream: CapturedAuditStream) -> None:
    """With the sweep's grace, a running clip's job and one made 10 s ago stay; one made 2 h ago is deleted."""
    grace_s = env.settings.nextext_timeout_s + RECONCILE_EXTRA_S
    assert grace_s == 960
    env.fake.statuses = ["running"]
    processor = env.make()
    processor.start_media()
    owner = env.clip()
    processor.submit(owner)
    await _until(lambda: env.fake.jobs.get(JOB_ID, 0) >= 1)
    assert processor.owned_jobs() == frozenset({JOB_ID})
    now = datetime.now(UTC)
    assert datetime.fromisoformat(JOB_CREATED_AT) < now - timedelta(seconds=grace_s)
    env.listed(YOUNG, now - timedelta(seconds=10))
    env.listed(ORPHAN, now - timedelta(hours=2))
    before = len(events(audit_stream))
    assert await processor.reconcile(grace_s=grace_s) == 1
    assert env.fake.deleted == [ORPHAN] and processor.owned_jobs() == frozenset({JOB_ID})
    [event] = _reconciles(audit_stream, before)
    assert _row(event) == ("delete", {"kind": "job", "count": 1}, {"listed": 3, "at_start": False}, "ok")


async def test_jobs_nextext_does_not_delete_are_recorded_as_an_error(
    env: Env, audit_stream: CapturedAuditStream
) -> None:
    """Nextext refuses the deletes: none is counted, and an error child names how many stayed."""
    env.fake.delete_status = 500
    env.listed(ORPHAN, datetime.now(UTC) - timedelta(hours=1))
    env.listed(OLD_ORPHAN, datetime.now(UTC) - timedelta(hours=2))
    assert await env.make().reconcile(grace_s=None) == 0
    primary, child = _reconciles(audit_stream)
    assert _row(primary) == ("delete", {"kind": "job", "count": 2}, {"listed": 2, "at_start": True}, "ok")
    assert _row(child) == ("delete", {"kind": "job", "count": 2}, None, "error")


async def test_reconciles_run_one_at_a_time(env: Env, audit_stream: CapturedAuditStream) -> None:
    """Two reconciles at once: the second waits for the first, lists anew, and neither counts the orphan twice."""
    env.listed(ORPHAN, datetime.now(UTC) - timedelta(hours=2))

    async def slow_first_list(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/jobs") and "list" not in env.fake.events:
            await anyio.sleep(0.1)
        return await env.fake(request)

    processor = env.make(client=env.client(slow_first_list))
    grace_s = env.settings.nextext_timeout_s + RECONCILE_EXTRA_S
    first = asyncio.create_task(processor.reconcile(grace_s=grace_s))
    second = asyncio.create_task(processor.reconcile(grace_s=grace_s))
    assert await asyncio.gather(first, second) == [1, 0]
    assert env.fake.events == ["list", f"delete:{ORPHAN}", "list"]
    [event] = _reconciles(audit_stream)
    assert _row(event) == ("delete", {"kind": "job", "count": 1}, {"listed": 1, "at_start": False}, "ok")


# --- after a create whose answer was lost ---------------------------------------------


def _losing(env: Env, lose: Callable[[], bool], *, hang: bool = False, late: timedelta | None = None) -> NextextClient:
    """Return a client whose creates reach Nextext, which makes the job, but whose answer is lost while ``lose()``.

    Args:
        env: The test's env.
        lose: Whether the next create's answer is lost.
        hang: Whether a lost answer never comes, rather than timing out.
        late: Stamp the lost job this long after the moment its answer is lost, as Nextext does once it has stored
            the upload; ``None`` leaves the fake's stamp, a day before.

    Returns:
        The client.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        response = await env.fake(request)
        if request.method == "POST" and lose():
            if late is not None:
                env.fake.created[response.json()["job_id"]] = _iso(datetime.now(UTC) + late)
            if hang:
                await anyio.sleep_forever()
            raise httpx.ReadTimeout("the answer did not come")
        return response

    return env.client(handler)


async def test_a_create_whose_answer_was_lost_is_reconciled_later(
    env: Env, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G16: the create timed out after Nextext made the job; the reconcile after the wait deletes it, sparing others."""
    monkeypatch.setattr(processing, "ORPHAN_GRACE_S", 0.3)
    monkeypatch.setattr(processing, "ORPHAN_RECONCILE_S", 0.6)
    env.fake.statuses = ["running"]
    lose = False
    processor = env.make(client=_losing(env, lambda: lose))
    processor.start_media()
    owner = env.clip("harbour-tour.mp4")
    processor.submit(owner)
    await _until(lambda: env.fake.jobs.get(JOB_ID, 0) >= 1)
    env.listed(YOUNG, datetime.now(UTC) + timedelta(seconds=60))
    lose = True
    lost = env.clip("quay-walk.mov")
    processor.submit(lost)
    await _settle(processor, lost)
    assert _status(env, lost) == ("failed", "nextext_unavailable")
    assert SECOND_JOB in env.fake.jobs and env.fake.deleted == []
    await _until(lambda: env.fake.deleted == [SECOND_JOB])
    assert processor.owned_jobs() == frozenset({JOB_ID}) and processor.running(owner.attachment_id)
    [event] = _reconciles(audit_stream)
    assert _row(event) == ("delete", {"kind": "job", "count": 1}, {"listed": 3, "at_start": False}, "ok")


def test_the_reconcile_after_a_lost_create_waits_past_its_grace() -> None:
    """The grace spares another clip's create in flight (at most CREATE_READ_S old) and skew; the wait is 60 s more."""
    assert processing.ORPHAN_GRACE_S == nextext.CREATE_READ_S + RECONCILE_EXTRA_S == 180
    assert processing.ORPHAN_RECONCILE_S == processing.ORPHAN_GRACE_S + RECONCILE_EXTRA_S == 240


async def test_a_lost_job_nextext_stamped_after_the_failure_is_deleted_by_the_first_run(
    env: Env, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nextext stamps the lost job 50 ms after the create gave up; the run after the wait is past its grace already."""
    monkeypatch.setattr(processing, "ORPHAN_GRACE_S", 0.3)
    monkeypatch.setattr(processing, "ORPHAN_RECONCILE_S", 0.6)
    processor = env.make(client=_losing(env, lambda: True, late=timedelta(milliseconds=50)))
    processor.start_media()
    job = env.clip()
    processor.submit(job)
    await _settle(processor, job)
    assert _status(env, job) == ("failed", "nextext_unavailable") and env.fake.deleted == []
    await _until(lambda: env.fake.deleted == [JOB_ID])
    [event] = _reconciles(audit_stream)
    assert _row(event) == ("delete", {"kind": "job", "count": 1}, {"listed": 1, "at_start": False}, "ok")


async def test_a_clip_cancelled_during_its_create_leaves_no_job(
    env: Env, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Taken back while Nextext's answer is on its way: the job it made is deleted by the reconcile after the wait."""
    monkeypatch.setattr(processing, "ORPHAN_RECONCILE_S", 0.2)
    processor = env.make(client=_losing(env, lambda: True, hang=True))
    processor.start_media()
    job = env.clip()
    processor.submit(job)
    await _until(lambda: JOB_ID in env.fake.jobs)
    assert processor.cancel(job.attachment_id)
    await _settle(processor, job)
    await _until(lambda: env.fake.deleted == [JOB_ID])
    [event] = _reconciles(audit_stream)
    assert _row(event) == ("delete", {"kind": "job", "count": 1}, {"listed": 1, "at_start": False}, "ok")
    assert [e for e in events(audit_stream) if e["operation"] == NEXTEXT_OPERATION] == [event]


async def test_one_reconcile_waits_for_lost_creates_at_a_time(
    env: Env, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three lost creates: one reconcile waits, and one more follows the last failure's wait; all three jobs go."""
    monkeypatch.setattr(processing, "ORPHAN_RECONCILE_S", 0.3)
    processor = env.make(client=_losing(env, lambda: True))
    processor.start_media()
    jobs = [env.clip(f"harbour-tour-{n}.mp4") for n in range(3)]
    for job in jobs:
        processor.submit(job)
    for job in jobs:
        await _settle(processor, job)
    assert [_status(env, job) for job in jobs] == [("failed", "nextext_unavailable")] * 3
    pending = processor._orphans
    assert pending is not None
    with anyio.fail_after(5):
        await pending.task
    assert sorted(env.fake.deleted) == sorted([JOB_ID, SECOND_JOB, THIRD_JOB])
    assert env.fake.events.count("list") == 3
    [event] = _reconciles(audit_stream)
    assert _row(event) == ("delete", {"kind": "job", "count": 3}, {"listed": 3, "at_start": False}, "ok")


async def test_a_waiting_reconcile_is_cancelled_at_shutdown(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reconcile still waiting when the processor closes never runs."""
    monkeypatch.setattr(processing, "ORPHAN_RECONCILE_S", 0.2)
    processor = env.make(client=_losing(env, lambda: True))
    processor.start_media()
    job = env.clip()
    processor.submit(job)
    await _settle(processor, job)
    lists = env.fake.events.count("list")
    pending = processor._orphans
    assert pending is not None and not pending.task.done()
    await processor.aclose()
    assert pending.task.done() and env.fake.events.count("list") == lists and env.fake.deleted == []
