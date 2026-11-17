"""The retention sweep and the dry run: due chats recorded, then deleted in workbench and here; contents emptied.

Every name, id and value here is invented.
"""

import asyncio
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from typing_extensions import override

from ghostwriter import __version__
from ghostwriter.audit import open_emitter
from ghostwriter.config import Settings
from ghostwriter.db import init_db, make_engine
from ghostwriter.main import create_app
from ghostwriter.models import Chat, Result, ToolStep
from ghostwriter.retention import (
    FIRST_SWEEP_DELAY_S,
    SWEEP_BATCH,
    SWEEP_INTERVAL_S,
    Sweeper,
    SweepReport,
    dry_run,
    main,
)
from ghostwriter.retention_rule import run_retention_loop
from ghostwriter.store import ChatStore
from ghostwriter.turns import TurnRegistry
from ghostwriter.utils.audit_event import AuditEmitter, AuditWriteError
from ghostwriter.utils.audit_runtime import encode_purpose
from ghostwriter.workbench import WorkbenchClient, chat_folder, session_pseudonym, user_pseudonym
from tests.conftest import KEY, CapturedAuditStream, events, make_settings
from tests.fakes import FakeDoor

DAY = timedelta(days=1)
SECOND = timedelta(seconds=1)
#: When the 90-day period took effect; its notice ended 30 days later.
T_SET = datetime(2026, 11, 16, tzinfo=UTC)
NOW = T_SET + 150 * DAY
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PREVIEW = {"columns": ["Monat", "Summe"], "rows": [["2026-01", "42"]], "row_count": 1, "truncated": False}
SYSTEM = {"user": None, "via": "system"}


class Clock:
    """The store's and the sweep's clock, set by hand.

    Attributes:
        now: The time it reads.
    """

    def __init__(self) -> None:
        """Start at ``T_SET``."""
        self.now = T_SET

    def __call__(self) -> datetime:
        """Read the clock.

        Returns:
            The time set.
        """
        return self.now


@dataclass
class Env:
    """One test's database, store, clock, turns, workbench and sweeper.

    Attributes:
        settings: The test settings.
        engine: The database.
        store: The chats.
        clock: The clock, at ``NOW`` between the helpers.
        turns: The turn registry.
        door: workbench.
        workbench: The client to ``door``.
        emitter: The audit chain.
        sweeper: The sweep over all of them.
    """

    settings: Settings
    engine: Engine
    store: ChatStore
    clock: Clock
    turns: TurnRegistry
    door: FakeDoor
    workbench: WorkbenchClient
    emitter: AuditEmitter
    sweeper: Sweeper

    def chat(self, started: datetime, owner: str = "user-a") -> str:
        """Start a chat at ``started``, then set the clock to ``NOW``.

        Args:
            started: When it starts.
            owner: Its owner.

        Returns:
            Its id.
        """
        self.clock.now = started
        chat_id = self.store.create_chat(owner).id
        self.clock.now = NOW
        return chat_id

    def has_rows(self, chat_id: str) -> bool:
        """Tell whether a chat's row is still there.

        Args:
            chat_id: The chat.

        Returns:
            Whether it is.
        """
        with Session(self.engine) as s:
            return s.get(Chat, chat_id) is not None

    def sweeper_with(self, turns: TurnRegistry) -> Sweeper:
        """Return a sweeper over this test's store and workbench, with other turns.

        Args:
            turns: The turn registry.

        Returns:
            The sweeper.
        """
        return Sweeper(
            store=self.store,
            workbench=self.workbench,
            emitter=self.emitter,
            settings=self.settings,
            turns=turns,
            now=self.clock,
        )


@pytest.fixture
def door() -> FakeDoor:
    """Return workbench's doors, answering as a healthy workbench does."""
    return FakeDoor()


@pytest.fixture
def env(tmp_path: Path, audit_stream: CapturedAuditStream, door: FakeDoor) -> Iterator[Env]:
    """Build the store, its chain on ``audit_stream`` and the sweeper; no period is recorded yet."""
    settings = make_settings(tmp_path)
    engine = make_engine(settings.db_path)
    init_db(engine)
    clock = Clock()
    store = ChatStore(engine, file_days=settings.file_days, notice_days=settings.notice_days, now=clock)
    emitter = open_emitter(__version__, audit_stream)
    emitter.start()
    turns = TurnRegistry()
    workbench = WorkbenchClient(settings, transport=httpx.MockTransport(door))
    sweeper = Sweeper(store=store, workbench=workbench, emitter=emitter, settings=settings, turns=turns, now=clock)
    yield Env(settings, engine, store, clock, turns, door, workbench, emitter, sweeper)
    emitter.close()
    engine.dispose()


@pytest.fixture
def settled(env: Env) -> Env:
    """Return ``env`` with the 90-day period in force since ``T_SET``, its notice long over, at ``NOW``."""
    env.store.apply_periods(90)
    env.clock.now = NOW
    return env


def _step(store: ChatStore, message_id: str, arguments: str = '{"path": "/workspace/s/sales.xlsx"}') -> str:
    return store.add_step(
        message_id,
        id=str(uuid4()),
        position=0,
        round=1,
        call_id="call-0",
        tool="read_document",
        arguments=arguments,
        result="Umsatz 42",
        status="ok",
    ).id


def _result(store: ChatStore, message_id: str) -> str:
    return store.add_result(
        message_id,
        kind="table",
        file_id="a" * 32,
        path="/workspace/s/out/sums.xlsx",
        name="sums.xlsx",
        mime=XLSX,
        size=2048,
        preview=PREVIEW,
    ).id


def _raw_step(engine: Engine, step_id: str) -> tuple[Any, ...]:
    with Session(engine) as s:
        row = s.execute(
            select(ToolStep.tool, ToolStep.status, ToolStep.arguments, ToolStep.result).where(ToolStep.id == step_id)
        ).one()
        return tuple(row)


def _raw_result(engine: Engine, result_id: str) -> tuple[Any, ...]:
    with Session(engine) as s:
        row = s.execute(
            select(Result.kind, Result.name, Result.mime, Result.size, Result.preview).where(Result.id == result_id)
        ).one()
        return tuple(row)


def _of(stream: CapturedAuditStream, operation: str) -> list[dict[str, Any]]:
    return [e for e in events(stream) if e["operation"] == operation]


def _deletes(door: FakeDoor) -> list[httpx.Request]:
    return [request for request, _ in door.requests if request.url.path == "/files/chat"]


def _contents(env: Env, made: datetime) -> tuple[str, str]:
    """Start a chat at ``made`` and give it a step and a table result made then.

    Args:
        env: The test's env.
        made: When the chat, its step and its result were made.

    Returns:
        The step's and the result's ids.
    """
    chat_id = env.chat(made)
    env.clock.now = made
    answer = env.store.start_assistant_message(chat_id)
    ids = _step(env.store, answer), _result(env.store, answer)
    env.clock.now = NOW
    return ids


# --- chats -------------------------------------------------------------------


async def test_due_chats_are_recorded_before_anything_is_deleted(
    settled: Env, audit_stream: CapturedAuditStream
) -> None:
    """The batch is recorded first, then workbench deletes the chat's folder, then the chat's rows go."""
    env = settled
    cid = env.chat(NOW - 95 * DAY)
    env.store.add_user_message("user-a", cid, "Summe der Spalte B", [])
    seen: list[bool] = []
    env.door.on_delete = lambda: seen.append(env.has_rows(cid) and "retention.chat" in audit_stream.getvalue())
    report = await env.sweeper.sweep()
    assert seen == [True] and not env.has_rows(cid)
    assert report == SweepReport(chats=1)
    [primary] = _of(audit_stream, "retention.chat")
    [child] = _of(audit_stream, "retention.chat.workbench")
    assert (primary["action"], primary["actor"], primary["outcome"]) == ("delete", SYSTEM, "ok")
    assert primary["target"] == {"kind": "chat", "ids": [cid], "count": 1}
    assert primary["criteria"] == {"period_days": 90, "cutoff": "2027-04-15T00:00:00Z"}
    assert (child["action"], child["parent_event_id"], child["outcome"]) == ("delete", primary["event_id"], "ok")
    assert child["criteria"] == {"files_deleted": 3, "failed": 0}
    assert "Summe der Spalte B" not in audit_stream.getvalue()


async def test_a_chat_is_kept_until_its_deadline(settled: Env) -> None:
    """A chat due one second from now stays; one due now goes."""
    env = settled
    kept = env.chat(NOW - 90 * DAY + SECOND)
    due = env.chat(NOW - 90 * DAY)
    assert (await env.sweeper.sweep()).chats == 1
    assert (env.has_rows(kept), env.has_rows(due)) == (True, False)
    env.clock.now = NOW + SECOND
    assert (await env.sweeper.sweep()).chats == 1 and not env.has_rows(kept)


async def test_the_notice_holds_for_chats_older_than_a_new_period(env: Env) -> None:
    """At the first start, chats already 200 days old wait the 30-day notice, then all go."""
    chats = [env.chat(NOW - 200 * DAY, owner=owner) for owner in ("user-a", "user-b")]
    env.store.apply_periods(90)
    env.clock.now = NOW + 30 * DAY - SECOND
    assert (await env.sweeper.sweep()).chats == 0 and all(env.has_rows(chat) for chat in chats)
    env.clock.now = NOW + 30 * DAY
    assert (await env.sweeper.sweep()).chats == 2 and not any(env.has_rows(chat) for chat in chats)


@pytest.mark.parametrize(
    ("answer", "reason"),
    [(httpx.Response(503, json={"detail": "busy, retry later"}), "503"), (httpx.ConnectError("refused"), "down")],
)
async def test_workbench_failures_do_not_keep_expired_chats(
    settled: Env, audit_stream: CapturedAuditStream, answer: object, reason: str
) -> None:
    """A chat workbench could not delete is due all the same: its rows go, and the child records the failure."""
    env = settled
    failing = env.chat(NOW - 95 * DAY)
    other = env.chat(NOW - 100 * DAY, owner="user-b")
    env.door.answers["/files/chat"] = lambda folder: answer if folder == chat_folder(KEY, failing) else {"deleted": 2}
    report = await env.sweeper.sweep()
    assert report == SweepReport(chats=2, workbench_failed=1)
    assert not env.has_rows(failing) and not env.has_rows(other)
    [child] = _of(audit_stream, "retention.chat.workbench")
    assert (child["criteria"], child["outcome"]) == ({"files_deleted": 2, "failed": 1}, "error")


@pytest.mark.parametrize("batch", [200, 1])
async def test_a_workbench_timeout_ends_the_sweeps_calls_to_it(
    settled: Env, audit_stream: CapturedAuditStream, monkeypatch: pytest.MonkeyPatch, batch: int
) -> None:
    """After one timeout no chat of the sweep is sent to workbench, also in later batches; the rows go all the same."""
    env = settled
    monkeypatch.setattr("ghostwriter.retention.SWEEP_BATCH", batch)
    first = env.chat(NOW - 100 * DAY)
    second = env.chat(NOW - 95 * DAY, owner="user-b")
    env.door.answers["/files/chat"] = lambda folder: (
        httpx.ReadTimeout("synthetic") if folder == chat_folder(KEY, first) else {"deleted": 2}
    )
    assert await env.sweeper.sweep() == SweepReport(chats=2, workbench_failed=2)
    assert [r.headers["x-session-id"] for r in _deletes(env.door)] == [session_pseudonym(KEY, first)]
    assert not env.has_rows(first) and not env.has_rows(second)
    assert not env.turns.running(first) and not env.turns.running(second)
    children = _of(audit_stream, "retention.chat.workbench")
    assert [(c["criteria"]["failed"], c["outcome"]) for c in children] == (
        [(2, "error")] if batch == 200 else [(1, "error"), (1, "error")]
    )
    assert {c["criteria"]["files_deleted"] for c in children} == {0}

    later = env.chat(NOW - 95 * DAY + DAY)
    env.clock.now = NOW + DAY
    assert await env.sweeper.sweep() == SweepReport(chats=1)
    assert _deletes(env.door)[-1].headers["x-session-id"] == session_pseudonym(KEY, later)


async def test_the_sweep_names_the_chat_and_the_system_reason_to_workbench(
    settled: Env, audit_stream: CapturedAuditStream
) -> None:
    """Workbench gets the chat's and the owner's pseudonyms, the system reason and the batch's request id."""
    env = settled
    cid = env.chat(NOW - 95 * DAY)
    await env.sweeper.sweep()
    [sent] = _deletes(env.door)
    assert sent.headers["x-session-id"] == session_pseudonym(KEY, cid)
    assert sent.headers["x-user-id"] == user_pseudonym(KEY, "user-a")
    assert sent.headers["x-audit-purpose"] == encode_purpose(None, "ghostwriter chat")
    assert re.fullmatch(r"[0-9a-f]{32}", sent.headers["x-request-id"])
    [primary] = _of(audit_stream, "retention.chat")
    assert sent.headers["x-request-id"] == primary["request_id"]
    assert cid not in str(sent.url)
    assert not any(cid in value or "user-a" in value for value in sent.headers.values())


async def test_without_workbench_due_chats_go_with_their_rows(settled: Env, audit_stream: CapturedAuditStream) -> None:
    """With workbench off there is no folder to delete; the child says so and the rows go."""
    env = settled
    cid = env.chat(NOW - 95 * DAY)
    sweeper = Sweeper(
        store=env.store, workbench=None, emitter=env.emitter, settings=env.settings, turns=env.turns, now=env.clock
    )
    assert await sweeper.sweep() == SweepReport(chats=1)
    assert not env.has_rows(cid) and env.door.requests == []
    [child] = _of(audit_stream, "retention.chat.workbench")
    assert (child["criteria"], child["outcome"]) == ({"workbench": "not_configured"}, "ok")


async def test_a_due_chat_with_a_running_turn_is_skipped(settled: Env, audit_stream: CapturedAuditStream) -> None:
    """A chat whose turn is held (an answer, or its owner's delete) is left to the next sweep, unrecorded."""
    env = settled
    busy = env.chat(NOW - 95 * DAY)
    free = env.chat(NOW - 96 * DAY)
    claim = env.turns.begin(busy)
    assert claim is not None
    assert (await env.sweeper.sweep()).chats == 1
    assert env.has_rows(busy) and not env.has_rows(free)
    assert busy not in audit_stream.getvalue() and env.turns.running(busy)
    assert [r.headers["x-session-id"] for r in _deletes(env.door)] == [session_pseudonym(KEY, free)]
    env.turns.end(busy, claim)
    assert (await env.sweeper.sweep()).chats == 1 and not env.has_rows(busy)
    assert not env.turns.running(busy)


class _DeletedBeforeTheClaim(TurnRegistry):
    """A turn registry that lets the owner's delete of one chat finish just before the sweep claims it."""

    def __init__(self, store: ChatStore, chat_id: str) -> None:
        super().__init__()
        self._store = store
        self._chat: str | None = chat_id

    @override
    def begin(self, chat_id: str) -> threading.Event | None:
        if chat_id == self._chat:
            self._chat = None
            self._store.delete_chat_rows(chat_id)
        return super().begin(chat_id)


async def test_a_chat_deleted_before_its_claim_is_not_recorded(settled: Env, audit_stream: CapturedAuditStream) -> None:
    """Holding the turns, the sweep reads again what is due: a chat its owner deleted meanwhile is not named."""
    env = settled
    gone = env.chat(NOW - 95 * DAY)
    other = env.chat(NOW - 96 * DAY)
    sweeper = env.sweeper_with(_DeletedBeforeTheClaim(env.store, gone))
    assert (await sweeper.sweep()).chats == 1
    [primary] = _of(audit_stream, "retention.chat")
    assert primary["target"] == {"kind": "chat", "ids": [other], "count": 1}
    assert gone not in audit_stream.getvalue()
    assert [r.headers["x-session-id"] for r in _deletes(env.door)] == [session_pseudonym(KEY, other)]


async def test_batches_hold_at_most_200_ids(settled: Env, audit_stream: CapturedAuditStream) -> None:
    """450 due chats: three batches of 200, 200 and 50, each recorded before it is deleted; 201 steps: 200 and 1."""
    env = settled
    env.clock.now = NOW - 95 * DAY
    chats = {env.store.create_chat(f"user-{n % 7}").id for n in range(450)}
    env.clock.now = NOW - 30 * DAY
    answer = env.store.start_assistant_message(env.store.create_chat("user-a").id)
    steps = {_step(env.store, answer) for _ in range(201)}
    env.clock.now = NOW
    assert SWEEP_BATCH == 200
    assert await env.sweeper.sweep() == SweepReport(chats=450, steps=201)
    primaries = _of(audit_stream, "retention.chat")
    assert [(len(e["target"]["ids"]), e["target"]["count"]) for e in primaries] == [(200, 200), (200, 200), (50, 50)]
    assert {i for e in primaries for i in e["target"]["ids"]} == chats
    children = _of(audit_stream, "retention.chat.workbench")
    assert [c["parent_event_id"] for c in children] == [p["event_id"] for p in primaries]
    assert [c["criteria"] for c in children] == [{"files_deleted": 600, "failed": 0}] * 2 + [
        {"files_deleted": 150, "failed": 0}
    ]
    emptied = _of(audit_stream, "retention.tool_content")
    assert [len(e["target"]["ids"]) for e in emptied] == [200, 1]
    assert {i for e in emptied for i in e["target"]["ids"]} == steps
    assert len(_deletes(env.door)) == 450


@pytest.mark.parametrize(("due_chat", "writes_left"), [(True, 0), (True, 1), (False, 0), (False, 1)])
async def test_a_failed_record_deletes_nothing(
    settled: Env, audit_stream: CapturedAuditStream, due_chat: bool, writes_left: int
) -> None:
    """A record that cannot be written raises before anything of its batch is deleted or emptied."""
    env = settled
    cid = env.chat(NOW - 95 * DAY) if due_chat else None
    if cid is not None:
        env.store.add_user_message("user-a", cid, "Summe der Spalte B", [])
    step, result = _contents(env, NOW - 8 * DAY)
    audit_stream.writes_left = writes_left
    with pytest.raises(AuditWriteError):
        await env.sweeper.sweep()
    audit_stream.writes_left = None
    if cid is not None:
        assert env.has_rows(cid) and env.store.chat_counts(cid)["messages"] == 1
        assert not env.turns.running(cid)
        assert len(_deletes(env.door)) == writes_left
    assert _raw_step(env.engine, step)[2:] == ('{"path": "/workspace/s/sales.xlsx"}', "Umsatz 42")
    assert _raw_result(env.engine, result)[4] is not None


# --- tool contents -----------------------------------------------------------


async def test_tool_contents_and_previews_are_emptied_after_the_file_period(
    settled: Env, audit_stream: CapturedAuditStream
) -> None:
    """Made 7 days and a second ago: arguments, result and preview go; the tool, status, name, size and type stay."""
    env = settled
    old_step, old_result = _contents(env, NOW - 7 * DAY - SECOND)
    live_step, live_result = _contents(env, NOW - DAY)
    assert await env.sweeper.sweep() == SweepReport(steps=1, previews=1)
    assert _raw_step(env.engine, old_step) == ("read_document", "ok", None, None)
    assert _raw_result(env.engine, old_result) == ("table", "sums.xlsx", XLSX, 2048, None)
    assert _raw_step(env.engine, live_step)[2:] == ('{"path": "/workspace/s/sales.xlsx"}', "Umsatz 42")
    assert _raw_result(env.engine, live_result)[4] is not None
    emptied = _of(audit_stream, "retention.tool_content")
    assert [(e["action"], e["actor"], e["target"], e["criteria"]) for e in emptied] == [
        ("delete", SYSTEM, {"kind": "tool_step", "ids": [old_step], "count": 1}, {"file_days": 7}),
        ("delete", SYSTEM, {"kind": "result", "ids": [old_result], "count": 1}, {"file_days": 7}),
    ]
    before = len(events(audit_stream))
    assert await env.sweeper.sweep() == SweepReport()
    assert len(events(audit_stream)) == before
    assert "Umsatz 42" not in audit_stream.getvalue() and "sums.xlsx" not in audit_stream.getvalue()


async def test_the_checkpoint_runs_outside_a_transaction_and_empties_the_log(settled: Env) -> None:
    """After a sweep the write-ahead log is empty, and neither file holds what was deleted or emptied."""
    env = settled
    cid = env.chat(NOW - 95 * DAY)
    env.store.add_user_message("user-a", cid, "SENTINEL-CHAT-TEXT", [])
    chat = env.chat(NOW - 10 * DAY)
    env.clock.now = NOW - 10 * DAY
    _step(env.store, env.store.start_assistant_message(chat), arguments='{"code": "SENTINEL-CODE"}')
    env.clock.now = NOW
    assert await env.sweeper.sweep() == SweepReport(chats=1, steps=1)
    db = Path(env.settings.db_path)
    wal = db.with_name(db.name + "-wal")
    assert wal.stat().st_size == 0
    for path in (db, wal):
        assert b"SENTINEL" not in path.read_bytes()
    env.store.checkpoint()


# --- the loop and the app ----------------------------------------------------


async def test_the_sweep_runs_daily_and_survives_a_failure() -> None:
    """The first sweep a minute after start, then one a day; a sweep that raises does not end the loop."""
    delays: list[float] = []
    runs: list[int] = []

    async def sleep(seconds: float) -> None:
        if len(delays) == 3:
            raise asyncio.CancelledError
        delays.append(seconds)

    async def sweep() -> None:
        runs.append(len(delays))
        if len(runs) == 2:
            raise RuntimeError("synthetic sweep failure")

    with pytest.raises(asyncio.CancelledError):
        await run_retention_loop(sweep, first_delay=FIRST_SWEEP_DELAY_S, interval=SWEEP_INTERVAL_S, sleep=sleep)
    assert delays == [60.0, 86400.0, 86400.0] and runs == [1, 2, 3]


def test_the_app_runs_the_sweep_and_stops_it_at_shutdown(
    tmp_path: Path, audit_stream: CapturedAuditStream, door: FakeDoor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lifespan starts the loop on the app's sweeper, which holds the app's turns, and cancels it at shutdown."""
    loops: list[tuple[Any, float, float]] = []
    stopped: list[bool] = []

    async def loop(sweep: Any, *, first_delay: float, interval: float) -> None:
        loops.append((sweep, first_delay, interval))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.append(True)
            raise

    monkeypatch.setattr("ghostwriter.main.run_retention_loop", loop)
    clock = Clock()
    app = create_app(
        make_settings(tmp_path), audit_stream=audit_stream, workbench_transport=httpx.MockTransport(door), now=clock
    )
    with TestClient(app) as client:
        assert isinstance(client.app, FastAPI)
        state = client.app.state
        [(sweep, first_delay, interval)] = loops
        assert (sweep, first_delay, interval) == (state.sweeper.sweep, 60.0, 86400.0)
        clock.now = NOW - 95 * DAY
        busy, free = state.store.create_chat("user-a").id, state.store.create_chat("user-b").id
        clock.now = NOW
        claim = state.turns.begin(busy)
        assert client.portal is not None
        assert client.portal.call(state.sweeper.sweep) == SweepReport(chats=1)
        assert [c.id for c in state.store.list_chats("user-a")] == [busy]
        assert state.store.list_chats("user-b") == [] and stopped == []
        state.turns.end(busy, claim)
    [primary] = _of(audit_stream, "retention.chat")
    assert primary["target"]["ids"] == [free] and stopped == [True]


# --- the dry run -------------------------------------------------------------


def _scenario(env: Env) -> None:
    """Two due chats, one with content; a kept chat with two due steps and a due preview; live content."""
    env.chat(NOW - 95 * DAY)
    _contents(env, NOW - 120 * DAY)
    kept = env.chat(NOW - 40 * DAY)
    env.store.rename_chat("user-a", kept, "Q3 Zahlen")
    env.clock.now = NOW - 30 * DAY
    _step(env.store, env.store.start_assistant_message(kept))
    env.clock.now = NOW - 8 * DAY
    later = env.store.start_assistant_message(kept)
    _step(env.store, later)
    _result(env.store, later)
    _contents(env, NOW - 2 * DAY)
    env.clock.now = NOW


def _files(env: Env) -> dict[str, bytes]:
    db = Path(env.settings.db_path)
    return {path.name: path.read_bytes() for path in (db, db.with_name(db.name + "-wal"))}


def test_the_dry_run_counts_and_changes_nothing(settled: Env, audit_stream: CapturedAuditStream) -> None:
    """Counts and ages only: no id, owner, title, content or file name; nothing is recorded, deleted or written."""
    env = settled
    _scenario(env)
    files, before = _files(env), len(events(audit_stream))
    out = dry_run(env.settings.db_path, chat_days=90, file_days=7, notice_days=30, now=NOW)
    assert out == (
        "chats due now: 2 (started 95 to 120 days ago; period 90 days, in force since 2026-11-16)\n"
        "tool contents due now: 2 steps, 1 preview (made 8 to 30 days ago; period 7 days)\n"
        "This run deletes nothing."
    )
    assert "chats due now: 2" in out
    with Session(env.engine) as s:
        chat_ids = list(s.scalars(select(Chat.id)))
    for secret in (*chat_ids, "user-a", "Q3 Zahlen", "Umsatz 42", "sums.xlsx"):
        assert secret not in out
    assert _files(env) == files and len(events(audit_stream)) == before


def test_the_dry_run_previews_another_period(settled: Env) -> None:
    """30 days: what the period in force makes due goes now; what only 30 days make due waits the notice."""
    env = settled
    _scenario(env)
    out = dry_run(env.settings.db_path, chat_days=30, file_days=7, notice_days=30, now=NOW)
    assert out.splitlines()[:2] == [
        "chats due now: 2 (started 95 to 120 days ago; period 30 days, previewed)",
        "chats held back by the notice: 1 (started 40 days ago; due by 2027-05-15)",
    ]
    out = dry_run(env.settings.db_path, chat_days=150, file_days=7, notice_days=30, now=NOW)
    assert out.splitlines()[0] == "chats due now: 0 (period 150 days, previewed)"
    out = dry_run(env.settings.db_path, chat_days=None, file_days=7, notice_days=30, now=NOW)
    assert out.splitlines()[:2] == [
        "chats due now: 0 (chat retention is off)",
        "tool contents due now: 3 steps, 2 previews (made 8 to 120 days ago; period 7 days)",
    ]


def test_the_dry_run_at_the_first_start_shows_the_notice(env: Env) -> None:
    """Before any period is recorded, old chats are held back by the notice a first start gives them."""
    env.chat(NOW - 200 * DAY)
    out = dry_run(env.settings.db_path, chat_days=90, file_days=7, notice_days=30, now=NOW)
    assert out.splitlines()[:2] == [
        "chats due now: 0 (period 90 days, previewed)",
        "chats held back by the notice: 1 (started 200 days ago; due by 2027-05-15)",
    ]


def test_the_command_is_a_dry_run(
    settled: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--dry-run`` reports on the configured period, ``--chat-days`` previews another; without it, nothing runs."""
    env = settled
    _scenario(env)
    chats = env.store.list_chats("user-a")
    monkeypatch.setattr("ghostwriter.retention.load_settings", lambda: env.settings)
    monkeypatch.setattr("ghostwriter.retention.utcnow", lambda: NOW)
    assert main(["--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines()[0].endswith("period 90 days, in force since 2026-11-16)")
    assert main(["--dry-run", "--chat-days", "30"]) == 0
    assert "period 30 days, previewed" in capsys.readouterr().out
    refused: list[list[str]] = [[], ["--dry-run", "--chat-days", "0"], ["--dry-run", "--chat-days", "x"]]
    for argv in refused:
        with pytest.raises(SystemExit) as exited:
            main(argv)
        assert exited.value.code == 2
    assert env.store.list_chats("user-a") == chats and len(chats) == 4


def test_the_module_runs_as_a_command() -> None:
    """``python -m ghostwriter.retention`` is the command; without ``--dry-run`` it refuses."""
    done = subprocess.run(
        [sys.executable, "-m", "ghostwriter.retention"], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 2 and "--dry-run" in done.stderr
