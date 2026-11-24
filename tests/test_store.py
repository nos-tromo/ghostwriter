"""The chat store: ownership, the chat period, contents that expire with their files, and deletion."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.orm import InstrumentedAttribute, Session

from ghostwriter.db import init_db, make_engine
from ghostwriter.main import create_app
from ghostwriter.models import Attachment, Chat, FileMark, Message, Result, ToolStep
from ghostwriter.schemas import AttachmentOut, LengthOut, MeasureState, NoticeOut, ResultOut, StepOut, TablePreviewOut
from ghostwriter.store import (
    STORED_MANIFEST_KEYS,
    AttachmentNotPending,
    ChatNotFound,
    ChatStore,
    FileRef,
    TooManyPending,
)
from tests.conftest import CapturedAuditStream, make_settings

T0 = datetime(2026, 11, 16, tzinfo=UTC)
DAY = timedelta(days=1)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PREVIEW = {"columns": ["Monat", "Summe"], "rows": [["2026-01", "42"]], "row_count": 1, "truncated": False}


class Clock:
    """A clock set by hand.

    Attributes:
        now: The time it reads.
    """

    def __init__(self) -> None:
        """Start at ``T0``."""
        self.now = T0

    def __call__(self) -> datetime:
        """Read the clock.

        Returns:
            The time it is set to.
        """
        return self.now


@pytest.fixture
def clock() -> Clock:
    """Return the store's clock, at ``T0``."""
    return Clock()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    """Yield a fresh database with the schema."""
    engine = make_engine(str(tmp_path / "a.sqlite3"))
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def store(engine: Engine, clock: Clock) -> ChatStore:
    """Return a store with a 7-day file period and a 30-day notice."""
    return ChatStore(engine, file_days=7, notice_days=30, now=clock)


def _attach(store: ChatStore, chat_id: str) -> AttachmentOut:
    """Add a pending attachment to a chat."""
    file_id = uuid4().hex
    return store.add_attachment(
        chat_id, file_id=file_id, path=f"/workspace/s/{file_id}.xlsx", name="sales.xlsx", size=14, mime=XLSX
    )


def _picture(store: ChatStore, chat_id: str, *, kind: str = "image", status: str = "processing") -> AttachmentOut:
    """Add a pending picture, by default one still being prepared."""
    file_id = uuid4().hex
    return store.add_attachment(
        chat_id,
        file_id=file_id,
        path=f"/workspace/s/{file_id}.jpg",
        name="harbour-photo.jpg",
        size=900,
        mime="image/jpeg",
        kind=kind,
        status=status,
    )


def _raw_attachment(engine: Engine, attachment_id: str) -> tuple[str, str | None, str | None]:
    """Read an attachment's status, manifest column and failure as stored."""
    with Session(engine) as s:
        row = s.execute(
            select(Attachment.status, Attachment.render, Attachment.failure).where(Attachment.id == attachment_id)
        ).one()
        return row[0], row[1], row[2]


def _step(store: ChatStore, message_id: str, position: int = 0) -> StepOut:
    """Store a tool step of an answer."""
    return store.add_step(
        message_id,
        id=str(uuid4()),
        position=position,
        round=1,
        call_id=f"call-{position}",
        tool="read_document",
        arguments='{"path": "/workspace/s/sales.xlsx"}',
        result="Umsatz 42",
        status="ok",
    )


def _result(store: ChatStore, message_id: str) -> ResultOut:
    """Store a table result of an answer."""
    file_id = uuid4().hex
    return store.add_result(
        message_id,
        kind="table",
        file_id=file_id,
        path=f"/workspace/s/{file_id}.xlsx",
        name="sums.xlsx",
        mime=XLSX,
        size=2048,
        preview=PREVIEW,
    )


def _value(engine: Engine, column: InstrumentedAttribute[datetime], row_id: str) -> datetime:
    """Read one stored time by its row's id."""
    model = column.class_
    with Session(engine) as s:
        return s.execute(select(column).where(model.id == row_id)).scalar_one()


def _counts(engine: Engine) -> dict[str, int]:
    """Count the rows of every chat table."""
    with Session(engine) as s:
        return {
            model.__tablename__: s.execute(select(func.count()).select_from(model)).scalar_one()
            for model in (Chat, Message, ToolStep, Result, Attachment, FileMark)
        }


def test_a_chat_belongs_to_its_owner(store: ChatStore) -> None:
    """No lookup by another owner, or of an unknown id, finds a chat."""
    chat = store.create_chat("user-a")
    assert store.get_chat("user-a", chat.id) == chat
    assert (chat.title, chat.created_at, chat.updated_at, chat.expires_at) == (None, T0, T0, None)
    assert store.list_chats("user-b") == []
    for owner, chat_id in (("user-b", chat.id), ("user-a", str(uuid4()))):
        with pytest.raises(ChatNotFound):
            store.get_chat(owner, chat_id)
        with pytest.raises(ChatNotFound):
            store.chat_detail(owner, chat_id)
        with pytest.raises(ChatNotFound):
            store.rename_chat(owner, chat_id, "Q3 Zahlen")
        with pytest.raises(ChatNotFound):
            store.add_user_message(owner, chat_id, "Hallo", [])
        with pytest.raises(ChatNotFound):
            store.last_answer(owner, chat_id)
    assert store.get_chat("user-a", chat.id).title is None


def test_a_chat_expires_90_days_after_it_starts(store: ChatStore) -> None:
    """A chat started under a period in force is due that period after it started."""
    store.apply_periods(90)
    assert store.create_chat("user-a").expires_at == T0 + 90 * DAY


def test_a_new_period_waits_its_notice_for_older_chats(store: ChatStore, clock: Clock) -> None:
    """A chat already older than a new period is due when the notice ends."""
    clock.now = T0 - 100 * DAY
    chat = store.create_chat("user-a")
    clock.now = T0
    state = store.apply_periods(90)
    assert (state.days, state.set_at, state.grace_until) == (90, T0, T0 + 30 * DAY)
    assert store.get_chat("user-a", chat.id).expires_at == T0 + 30 * DAY


def test_a_shorter_period_gives_notice_only_to_what_it_makes_due_sooner(store: ChatStore, clock: Clock) -> None:
    """90 → 30: a chat the 90 days make due sooner than the notice keeps that date; a younger one waits the notice."""
    clock.now = T0 - 200 * DAY
    store.apply_periods(90)
    clock.now = T0 - 80 * DAY
    older = store.create_chat("user-a")
    clock.now = T0 - 10 * DAY
    younger = store.create_chat("user-a")
    clock.now = T0
    store.apply_periods(30)
    assert store.get_chat("user-a", older.id).expires_at == T0 + 10 * DAY
    assert store.get_chat("user-a", younger.id).expires_at == T0 + 30 * DAY


def test_off_clears_every_deadline(store: ChatStore) -> None:
    """Without a period no chat has a deadline, old or new."""
    store.apply_periods(90)
    chats = [("user-a", store.create_chat("user-a").id), ("user-b", store.create_chat("user-b").id)]
    assert store.apply_periods(None).days is None
    assert [store.get_chat(owner, chat_id).expires_at for owner, chat_id in chats] == [None, None]
    assert store.create_chat("user-a").expires_at is None


def test_tool_contents_expire_with_the_files(store: ChatStore, engine: Engine, clock: Clock) -> None:
    """A step or a preview expires with the chat's earliest file, or after its own period without one."""
    store.apply_periods(90)
    alone = store.create_chat("user-a")
    answer = store.start_assistant_message(alone.id)
    first_step = _step(store, answer)
    first_result = _result(store, answer)
    assert _value(engine, ToolStep.content_expires_at, first_step.id) == T0 + 7 * DAY
    assert _value(engine, Result.preview_expires_at, first_result.id) == T0 + 7 * DAY

    chat = store.create_chat("user-a")
    _attach(store, chat.id)
    clock.now = T0 + 6 * DAY
    later = store.start_assistant_message(chat.id)
    late_step = _step(store, later)
    late_result = _result(store, later)
    after_a_result = _step(store, answer, position=1)
    for column, row_id in (
        (ToolStep.content_expires_at, late_step.id),
        (Result.preview_expires_at, late_result.id),
        (ToolStep.content_expires_at, after_a_result.id),
    ):
        assert _value(engine, column, row_id) == T0 + 7 * DAY

    clock.now = T0 - 10 * DAY
    old = store.create_chat("user-a")
    emptied = _step(store, store.start_assistant_message(old.id))
    with Session(engine) as s, s.begin():
        s.execute(update(ToolStep).where(ToolStep.id == emptied.id).values(arguments=None, result=None))

    clock.now = T0 + 6 * DAY
    ChatStore(engine, file_days=3, notice_days=30, now=clock).apply_periods(90)
    for column, row_id in (
        (ToolStep.content_expires_at, first_step.id),
        (Result.preview_expires_at, first_result.id),
        (ToolStep.content_expires_at, late_step.id),
        (Result.preview_expires_at, late_result.id),
        (ToolStep.content_expires_at, after_a_result.id),
    ):
        assert _value(engine, column, row_id) == T0 + 3 * DAY
    assert _value(engine, ToolStep.content_expires_at, emptied.id) == T0 - 3 * DAY


def test_content_made_after_a_week_lives_its_own_period(store: ChatStore, engine: Engine, clock: Clock) -> None:
    """A file on day 0, then a step and a table on day 10: the day-0 file is gone, so they live until day 17."""
    chat = store.create_chat("user-a")
    _attach(store, chat.id)
    clock.now = T0 + 10 * DAY
    store.add_user_message("user-a", chat.id, "Und pro Quartal?", [])
    answer = store.start_assistant_message(chat.id)
    step = _step(store, answer)
    result = _result(store, answer)
    store.finish_assistant_message(answer, "Summe: 42")
    assert (step.expired, step.detail, step.result) == (False, "sales.xlsx", "Umsatz 42")
    assert (result.preview, result.expired) == (TablePreviewOut(**PREVIEW), False)
    assert _value(engine, ToolStep.content_expires_at, step.id) == T0 + 17 * DAY
    assert _value(engine, Result.preview_expires_at, result.id) == T0 + 17 * DAY

    clock.now = T0 + 12 * DAY
    ChatStore(engine, file_days=7, notice_days=30, now=clock).apply_periods(90)
    assert _value(engine, ToolStep.content_expires_at, step.id) == T0 + 17 * DAY
    assert _value(engine, Result.preview_expires_at, result.id) == T0 + 17 * DAY
    reply = store.chat_detail("user-a", chat.id).messages[1]
    assert (reply.steps[0].expired, reply.results[0].preview) == (False, TablePreviewOut(**PREVIEW))
    turns, expired = store.past_turns(chat.id, before_position=2)
    assert expired is False
    assert [len(calls) for calls in turns[0].rounds] == [1]


def test_content_is_capped_by_the_earliest_file_that_still_exists(
    store: ChatStore, engine: Engine, clock: Clock
) -> None:
    """Files on day 0 and day 5, then a step on day 6: the day-0 file is still there, so the step goes on day 7."""
    chat = store.create_chat("user-a")
    _attach(store, chat.id)
    clock.now = T0 + 5 * DAY
    _attach(store, chat.id)
    clock.now = T0 + 6 * DAY
    step = _step(store, store.start_assistant_message(chat.id))
    assert _value(engine, ToolStep.content_expires_at, step.id) == T0 + 7 * DAY


def test_a_chat_has_used_workbench_once_it_holds_a_file_mark(store: ChatStore) -> None:
    """Plain conversation leaves no mark; a hand-over, an attachment or a step does, and it goes only with the chat."""
    plain, attached, called, handed = (store.create_chat("user-a") for _ in range(4))
    store.add_user_message("user-a", plain.id, "Hallo", [])
    store.finish_assistant_message(store.start_assistant_message(plain.id), "Hallo!")
    _attach(store, attached.id)
    dropped = store.start_assistant_message(called.id)
    _step(store, dropped)
    store.drop_message(dropped)
    store.mark_files(handed.id)
    chats = [plain.id, attached.id, called.id, handed.id]
    assert store.workbench_chats(chats) == {attached.id, called.id, handed.id}
    store.delete_chat_rows(attached.id)
    assert store.workbench_chats(chats) == {called.id, handed.id}
    with pytest.raises(ChatNotFound):
        store.mark_files(attached.id)


def test_a_step_without_any_file_row_still_caps_later_steps(store: ChatStore, engine: Engine, clock: Clock) -> None:
    """A chat's first workbench call may write files no row names; content after it expires with them."""
    chat = store.create_chat("user-a")
    _step(store, store.start_assistant_message(chat.id))
    clock.now = T0 + 5 * DAY
    later = _step(store, store.start_assistant_message(chat.id))
    assert _value(engine, ToolStep.content_expires_at, later.id) == T0 + 7 * DAY


@pytest.mark.parametrize("named_by", ["attachment", "result"])
def test_dropped_rows_never_move_the_cap_later(store: ChatStore, engine: Engine, clock: Clock, named_by: str) -> None:
    """The chat's first file is remembered when the row that named it is gone (a regenerate, a lost row)."""
    chat = store.create_chat("user-a")
    if named_by == "attachment":
        _attach(store, chat.id)
        with Session(engine) as s, s.begin():
            s.execute(delete(Attachment).where(Attachment.chat_id == chat.id))
    else:
        dropped = store.start_assistant_message(chat.id)
        _result(store, dropped)
        store.drop_message(dropped)
    assert store.chat_counts(chat.id) == {"messages": 0, "steps": 0, "results": 0, "attachments": 0}
    clock.now = T0 + 5 * DAY
    answer = store.start_assistant_message(chat.id)
    step = _step(store, answer)
    result = _result(store, answer)
    assert _value(engine, ToolStep.content_expires_at, step.id) == T0 + 7 * DAY
    assert _value(engine, Result.preview_expires_at, result.id) == T0 + 7 * DAY
    ChatStore(engine, file_days=7, notice_days=30, now=clock).apply_periods(90)
    assert _value(engine, ToolStep.content_expires_at, step.id) == T0 + 7 * DAY
    assert _value(engine, Result.preview_expires_at, result.id) == T0 + 7 * DAY


def test_a_result_lives_as_long_as_its_file_and_its_preview_with_the_earliest(store: ChatStore, clock: Clock) -> None:
    """A result written on day 6 over a file of day 0: its preview goes on day 7, the result on day 13."""
    chat = store.create_chat("user-a")
    _attach(store, chat.id)
    clock.now = T0 + 6 * DAY
    _result(store, store.start_assistant_message(chat.id))

    def card() -> ResultOut:
        return store.chat_detail("user-a", chat.id).messages[0].results[0]

    clock.now = T0 + 7 * DAY - timedelta(seconds=1)
    assert (card().preview, card().expired) == (TablePreviewOut(**PREVIEW), False)
    clock.now = T0 + 7 * DAY
    assert (card().preview, card().expired) == (None, False)
    clock.now = T0 + 13 * DAY - timedelta(seconds=1)
    assert card().expired is False
    clock.now = T0 + 13 * DAY
    assert (card().preview, card().expired) == (None, True)


def test_cards_reload_in_the_order_the_tools_wrote_them(store: ChatStore) -> None:
    """Results written in the same moment come back in their order, not by their ids."""
    chat = store.create_chat("user-a")
    answer = store.start_assistant_message(chat.id)
    names = [f"teil-{n}.csv" for n in range(8)]
    for name in names:
        store.add_result(
            answer,
            kind="file",
            file_id=uuid4().hex,
            path=f"/workspace/s/{name}",
            name=name,
            mime="text/csv",
            size=10,
            preview=None,
        )
    assert [r.name for r in store.chat_detail("user-a", chat.id).messages[0].results] == names


def test_a_user_message_keeps_the_id_it_was_given(store: ChatStore) -> None:
    """The id recorded before the insert is the stored message's; without one, a uuid4 is minted."""
    chat = store.create_chat("user-a")
    given = str(uuid4())
    assert store.add_user_message("user-a", chat.id, "Summe der Spalte B", [], id=given).id == given
    minted = store.add_user_message("user-a", chat.id, "Und pro Quartal?", []).id
    assert UUID(minted).version == 4 and minted != given
    assert [m.id for m in store.chat_detail("user-a", chat.id).messages] == [given, minted]


def test_an_attachment_keeps_the_id_it_was_given(store: ChatStore) -> None:
    """The id recorded before the hand-over is the stored attachment's; without one, a uuid4 is minted."""
    chat = store.create_chat("user-a")
    given = str(uuid4())
    path, name = "/workspace/s/sales.xlsx", "sales.xlsx"
    assert (
        store.add_attachment(chat.id, file_id=uuid4().hex, path=path, name=name, size=14, mime=XLSX, id=given).id
        == given
    )
    minted = store.add_attachment(chat.id, file_id=uuid4().hex, path=path, name=name, size=14, mime=XLSX).id
    assert UUID(minted).version == 4 and minted != given
    assert {a.id for a in store.chat_detail("user-a", chat.id).pending_attachments} == {given, minted}


def test_the_pending_cap_is_counted_inside_the_write(store: ChatStore) -> None:
    """At the cap an attachment is refused by the transaction that would store it; a sent one frees its place."""
    chat = store.create_chat("user-a")
    first = _attach(store, chat.id)
    _attach(store, chat.id)
    path, name = "/workspace/s/sales.xlsx", "sales.xlsx"
    with pytest.raises(TooManyPending):
        store.add_attachment(chat.id, file_id=uuid4().hex, path=path, name=name, size=14, mime=XLSX, max_pending=2)
    assert store.pending_count(chat.id) == 2
    store.add_user_message("user-a", chat.id, "Hallo", [first.id])
    store.add_attachment(chat.id, file_id=uuid4().hex, path=path, name=name, size=14, mime=XLSX, max_pending=2)
    assert store.pending_count(chat.id) == 2


def test_expired_pending_attachments_do_not_count_against_the_cap(store: ChatStore, clock: Clock) -> None:
    """Ten pending files whose 7 days are over no longer wait: an eleventh is taken."""
    chat = store.create_chat("user-a")
    for _ in range(10):
        _attach(store, chat.id)
    path, name = "/workspace/s/sales.xlsx", "sales.xlsx"
    clock.now = T0 + 7 * DAY - timedelta(seconds=1)
    with pytest.raises(TooManyPending):
        store.add_attachment(chat.id, file_id=uuid4().hex, path=path, name=name, size=14, mime=XLSX, max_pending=10)
    clock.now = T0 + 7 * DAY
    assert store.pending_count(chat.id) == 0
    store.add_attachment(chat.id, file_id=uuid4().hex, path=path, name=name, size=14, mime=XLSX, max_pending=10)
    assert store.pending_count(chat.id) == 1


def test_a_longer_file_period_brings_back_nothing_expired(store: ChatStore, engine: Engine, clock: Clock) -> None:
    """Content already read as expired stays expired; content still live gets the longer period."""
    chat = store.create_chat("user-a")
    expired = _step(store, store.start_assistant_message(chat.id))
    clock.now = T0 + 8 * DAY
    other = store.create_chat("user-a")
    live = _step(store, store.start_assistant_message(other.id))
    ChatStore(engine, file_days=30, notice_days=30, now=clock).apply_periods(90)
    assert _value(engine, ToolStep.content_expires_at, expired.id) == T0 + 7 * DAY
    assert _value(engine, ToolStep.content_expires_at, live.id) == T0 + 38 * DAY


def test_expired_content_reads_as_expired_before_any_sweep(store: ChatStore, clock: Clock) -> None:
    """Past its time, a step loses its arguments, result and detail, a result its preview; names stay."""
    chat = store.create_chat("user-a")
    attachment = _attach(store, chat.id)
    store.add_user_message("user-a", chat.id, "Summe der Spalte B", [attachment.id])
    answer = store.start_assistant_message(chat.id)
    _step(store, answer)
    _result(store, answer)
    store.finish_assistant_message(answer, "Summe: 42")

    clock.now = T0 + 7 * DAY - timedelta(seconds=1)
    question, reply = store.chat_detail("user-a", chat.id).messages
    assert (question.attachments[0].expired, reply.steps[0].expired, reply.results[0].expired) == (False, False, False)
    assert (reply.steps[0].arguments, reply.steps[0].result) == ('{"path": "/workspace/s/sales.xlsx"}', "Umsatz 42")
    assert reply.results[0].preview == TablePreviewOut(**PREVIEW)

    clock.now = T0 + 7 * DAY
    question, reply = store.chat_detail("user-a", chat.id).messages
    step, result = reply.steps[0], reply.results[0]
    assert (step.tool, step.status, step.arguments, step.result, step.detail, step.expired) == (
        "read_document",
        "ok",
        None,
        None,
        None,
        True,
    )
    assert (result.kind, result.name, result.preview, result.expired) == ("table", "sums.xlsx", None, True)
    assert question.attachments[0].expired is True
    assert (question.content, reply.content) == ("Summe der Spalte B", "Summe: 42")


def test_the_first_message_names_the_chat(store: ChatStore) -> None:
    """The first line of the first message, cut to 60 characters, names an untitled chat."""
    chat = store.create_chat("user-a")
    store.add_user_message("user-a", chat.id, "Summe der Spalte B pro Monat\nals Excel", [])
    assert store.get_chat("user-a", chat.id).title == "Summe der Spalte B pro Monat"
    store.add_user_message("user-a", chat.id, "Und pro Quartal?", [])
    assert store.get_chat("user-a", chat.id).title == "Summe der Spalte B pro Monat"

    long = store.create_chat("user-a")
    store.add_user_message("user-a", long.id, "x" * 80 + "\nRest", [])
    assert store.get_chat("user-a", long.id).title == "x" * 59 + "…"
    exact = store.create_chat("user-a")
    store.add_user_message("user-a", exact.id, "y" * 60, [])
    assert store.get_chat("user-a", exact.id).title == "y" * 60

    files_only = store.create_chat("user-a")
    store.add_user_message("user-a", files_only.id, "", [_attach(store, files_only.id).id])
    assert store.get_chat("user-a", files_only.id).title is None


def test_a_message_binds_only_this_chats_pending_attachments(store: ChatStore) -> None:
    """Another chat's attachment, an unknown one or one bound already is refused, and nothing is stored."""
    chat = store.create_chat("user-a")
    other = store.create_chat("user-a")
    mine = _attach(store, chat.id)
    theirs = _attach(store, other.id)
    for ids in ([theirs.id], ["unknown"], [mine.id, theirs.id]):
        with pytest.raises(AttachmentNotPending):
            store.add_user_message("user-a", chat.id, "Hallo", ids)
    assert store.chat_counts(chat.id) == {"messages": 0, "steps": 0, "results": 0, "attachments": 1}
    assert store.pending_count(chat.id) == 1

    message = store.add_user_message("user-a", chat.id, "Hallo", [mine.id, mine.id])
    assert [a.id for a in message.attachments] == [mine.id]
    assert (store.pending_count(chat.id), store.pending_count(other.id)) == (0, 1)
    assert [a.id for a in store.chat_detail("user-a", other.id).pending_attachments] == [theirs.id]
    assert store.chat_detail("user-a", chat.id).pending_attachments == []
    with pytest.raises(AttachmentNotPending):
        store.add_user_message("user-a", chat.id, "Nochmal", [mine.id])


def test_only_an_unsent_attachment_of_the_owners_chat_is_taken_back(store: ChatStore) -> None:
    """Another owner, another chat and a sent attachment keep their rows; the owner's unsent one goes, once."""
    chat = store.create_chat("user-a")
    other = store.create_chat("user-a")
    mine, kept, sent = _attach(store, chat.id), _attach(store, chat.id), _attach(store, chat.id)
    store.add_user_message("user-a", chat.id, "Hallo", [sent.id])
    assert store.find_pending_attachment("user-a", chat.id, mine.id) is not None
    assert store.find_pending_attachment("user-a", chat.id, sent.id) is None
    assert not store.remove_pending_attachment("user-b", chat.id, mine.id)
    assert not store.remove_pending_attachment("user-a", other.id, mine.id)
    assert not store.remove_pending_attachment("user-a", chat.id, sent.id)
    assert store.remove_pending_attachment("user-a", chat.id, mine.id)
    assert not store.remove_pending_attachment("user-a", chat.id, mine.id)
    assert [a.id for a in store.chat_detail("user-a", chat.id).pending_attachments] == [kept.id]
    assert store.find_attachment("user-a", chat.id, sent.id) is not None


def test_deleting_a_chat_takes_every_row_with_it(store: ChatStore, engine: Engine) -> None:
    """Messages, steps, results and attachments, bound or pending, go with their chat; other chats stay."""

    def fill(chat_id: str) -> None:
        bound = _attach(store, chat_id)
        store.add_user_message("user-a", chat_id, "Summe der Spalte B", [bound.id])
        _attach(store, chat_id)
        answer = store.start_assistant_message(chat_id)
        _step(store, answer)
        _result(store, answer)

    chat = store.create_chat("user-a")
    other = store.create_chat("user-a")
    fill(chat.id)
    fill(other.id)
    assert store.chat_counts(chat.id) == {"messages": 2, "steps": 1, "results": 1, "attachments": 2}
    store.delete_chat_rows(chat.id)
    assert _counts(engine) == {
        "chats": 1,
        "messages": 2,
        "tool_steps": 1,
        "results": 1,
        "attachments": 2,
        "file_marks": 4,
    }
    assert store.chat_counts(chat.id) == {"messages": 0, "steps": 0, "results": 0, "attachments": 0}
    with pytest.raises(ChatNotFound):
        store.get_chat("user-a", chat.id)


def test_a_file_is_found_only_through_its_own_chat(store: ChatStore, clock: Clock) -> None:
    """Attachments and results are found by owner, chat and file id together."""
    chat = store.create_chat("user-a")
    attachment = _attach(store, chat.id)
    clock.now = T0 + DAY
    result = _result(store, store.start_assistant_message(chat.id))
    assert store.find_file("user-a", chat.id, attachment.file_id) == FileRef(
        path=f"/workspace/s/{attachment.file_id}.xlsx", created_at=T0
    )
    assert store.find_file("user-a", chat.id, result.file_id) == FileRef(
        path=f"/workspace/s/{result.file_id}.xlsx", created_at=T0 + DAY
    )
    other = store.create_chat("user-a")
    assert store.find_file("user-a", other.id, attachment.file_id) is None
    assert store.find_file("user-b", chat.id, attachment.file_id) is None
    assert store.find_file("user-a", chat.id, uuid4().hex) is None


def test_a_file_expires_by_the_rule_its_card_reads(store: ChatStore, clock: Clock) -> None:
    """A file is gone once workbench's period for it has passed, as its attachment or result reads ``expired``."""
    chat = store.create_chat("user-a")
    attachment = _attach(store, chat.id)
    ref = store.find_file("user-a", chat.id, attachment.file_id)
    assert ref is not None
    clock.now = T0 + 7 * DAY - timedelta(microseconds=1)
    assert not store.file_expired(ref)
    clock.now = T0 + 7 * DAY
    assert store.file_expired(ref)
    assert store.chat_detail("user-a", chat.id).pending_attachments[0].expired is True


#: What workbench's preparation reports of a three-page scan: more than the chat keeps.
MANIFEST: dict[str, Any] = {
    "path": "/workspace/s/quay-scan.pdf",
    "kind": "pdf",
    "total": 3,
    "pages_without_text": [2, 3],
    "renditions": [
        {"n": 1, "width": 800, "height": 1000, "mime": "image/jpeg", "bytes": 4096, "path": "/workspace/s/p1.jpg"},
        {"n": 2, "width": 800, "height": 1000, "mime": "image/jpeg", "bytes": 5120, "hint": "scan"},
    ],
    "frames": [],
    "unit": "page",
    "hint": "two pages carry no text",
    "duration_s": None,
    "transcript": False,
}


def test_an_attachment_carries_its_kind_and_status(store: ChatStore) -> None:
    """A picture still being prepared says so, and a file added without either is a ready document."""
    chat = store.create_chat("user-a")
    picture = _picture(store, chat.id)
    document = _attach(store, chat.id)
    assert (picture.kind, picture.status) == ("image", "processing")
    assert (document.kind, document.status) == ("document", "ready")
    pending = store.chat_detail("user-a", chat.id).pending_attachments
    assert {a.id: (a.kind, a.status) for a in pending} == {
        picture.id: ("image", "processing"),
        document.id: ("document", "ready"),
    }
    sent = store.add_user_message("user-a", chat.id, "Was zeigt das Foto?", [picture.id]).attachments
    assert [(a.kind, a.status) for a in sent] == [("image", "processing")]
    message = store.chat_detail("user-a", chat.id).messages[0]
    assert [(a.kind, a.status) for a in message.attachments] == [("image", "processing")]


def test_an_attachment_is_served_through_its_owners_chat(store: ChatStore) -> None:
    """The served attachment is the one the detail serves, for the owner's chat only."""
    chat = store.create_chat("user-a")
    picture = _picture(store, chat.id)
    assert store.attachment_out("user-a", chat.id, picture.id) == picture
    assert store.attachment_out("user-b", chat.id, picture.id) is None
    assert store.attachment_out("user-a", store.create_chat("user-a").id, picture.id) is None
    assert store.attachment_out("user-a", chat.id, str(uuid4())) is None


def test_a_settled_manifest_keeps_metadata_only(store: ChatStore, engine: Engine) -> None:
    """Of what workbench reports, only the counts and sizes are kept: never a path, a hint or a byte count."""
    chat = store.create_chat("user-a")
    picture = _picture(store, chat.id, kind="document")
    assert store.settle_attachment(picture.id, status="ready", manifest=MANIFEST, failure=None) is True
    ref = store.find_attachment("user-a", chat.id, picture.id)
    assert ref is not None and ref.manifest is not None and ref.status == "ready"
    assert list(ref.manifest) == list(STORED_MANIFEST_KEYS)
    assert ref.manifest["renditions"] == [
        {"n": 1, "width": 800, "height": 1000, "mime": "image/jpeg"},
        {"n": 2, "width": 800, "height": 1000, "mime": "image/jpeg"},
    ]
    assert (ref.manifest["total"], ref.manifest["pages_without_text"], ref.manifest["kind"]) == (3, [2, 3], "pdf")
    _, render, failure = _raw_attachment(engine, picture.id)
    assert failure is None and render is not None
    assert all(word not in render for word in ("/workspace", "hint", "bytes", "scan"))


def test_a_settled_manifest_keeps_its_unit(store: ChatStore) -> None:
    """A deck's manifest keeps ``unit``; one stored by 0.3.0 has none, and still reads."""
    chat = store.create_chat("user-a")
    deck = _picture(store, chat.id, kind="document")
    manifest = {
        "kind": "office",
        "total": 3,
        "pages_without_text": [2],
        "renditions": [{"n": 1, "width": 1131, "height": 1600, "mime": "image/jpeg", "bytes": 4096}],
        "frames": None,
        "unit": "slide",
        "hint": "a note",
    }
    assert store.settle_attachment(deck.id, status="ready", manifest=manifest, failure=None) is True
    ref = store.find_attachment("user-a", chat.id, deck.id)
    assert ref is not None and ref.manifest is not None
    assert (ref.manifest["kind"], ref.manifest["unit"], ref.manifest["total"]) == ("office", "slide", 3)
    older = _picture(store, chat.id, kind="document")
    stored_by_0_3_0: dict[str, Any] = {
        "kind": "pdf",
        "total": 3,
        "pages_without_text": [2],
        "renditions": [],
        "frames": None,
    }
    assert store.settle_attachment(older.id, status="ready", manifest=stored_by_0_3_0, failure=None) is True
    ref = store.find_attachment("user-a", chat.id, older.id)
    assert ref is not None and ref.manifest is not None
    assert ref.manifest == stored_by_0_3_0 and "unit" not in ref.manifest


def test_a_settled_media_manifest_keeps_its_facts(store: ChatStore, engine: Engine) -> None:
    """A clip's manifest keeps its unit, its duration and whether it has a transcript; a 0.8 one has neither key."""
    chat = store.create_chat("user-a")
    clip = _picture(store, chat.id, kind="media")
    manifest: dict[str, Any] = {
        "path": "/workspace/s/harbour-tour.mp4",
        "kind": "media",
        "unit": "frame",
        "total": 3,
        "pages_without_text": [],
        "renditions": [
            {
                "n": n,
                "width": 1280,
                "height": 720,
                "mime": "image/jpeg",
                "bytes": 48213,
                "path": f"/workspace/s/f{n}.jpg",
            }
            for n in (1, 2, 3)
        ],
        "frames": None,
        "hint": "frames come from view_file",
        "duration_s": 134.0,
        "transcript": True,
    }
    assert store.settle_attachment(clip.id, status="ready", manifest=manifest, failure=None) is True
    ref = store.find_attachment("user-a", chat.id, clip.id)
    assert ref is not None and ref.manifest is not None and ref.status == "ready"
    assert list(ref.manifest) == list(STORED_MANIFEST_KEYS)
    assert (ref.manifest["kind"], ref.manifest["unit"], ref.manifest["total"]) == ("media", "frame", 3)
    assert (ref.manifest["duration_s"], ref.manifest["transcript"]) == (134.0, True)
    _, render, failure = _raw_attachment(engine, clip.id)
    assert failure is None and render is not None
    assert all(word not in render for word in ("/workspace", "hint", "bytes", "tour"))
    silent = _picture(store, chat.id, kind="media")
    quiet = {**manifest, "duration_s": None, "transcript": False}
    assert store.settle_attachment(silent.id, status="ready", manifest=quiet, failure=None) is True
    ref = store.find_attachment("user-a", chat.id, silent.id)
    assert ref is not None and ref.manifest is not None
    assert (ref.manifest["duration_s"], ref.manifest["transcript"]) == (None, False)
    older = _picture(store, chat.id, kind="document")
    stored_by_0_8: dict[str, Any] = {
        "kind": "pdf",
        "total": 3,
        "pages_without_text": [],
        "renditions": [],
        "frames": None,
        "unit": "page",
    }
    assert store.settle_attachment(older.id, status="ready", manifest=stored_by_0_8, failure=None) is True
    ref = store.find_attachment("user-a", chat.id, older.id)
    assert ref is not None and ref.manifest == stored_by_0_8


def test_a_manifest_without_some_keys_keeps_those_it_has(store: ChatStore) -> None:
    """A picture has no pages: only what its manifest reports is kept."""
    chat = store.create_chat("user-a")
    picture = _picture(store, chat.id)
    manifest = {"kind": "image", "total": 1, "renditions": [{"n": 1, "width": 64, "height": 48, "mime": "image/png"}]}
    assert store.settle_attachment(picture.id, status="ready", manifest=manifest, failure=None) is True
    ref = store.find_attachment("user-a", chat.id, picture.id)
    assert ref is not None and ref.manifest == manifest


def test_a_failed_attachment_keeps_its_token_and_no_manifest(store: ChatStore, engine: Engine) -> None:
    """A picture that could not be prepared is ``failed``, with the reason as a token, and has no manifest."""
    chat = store.create_chat("user-a")
    picture = _picture(store, chat.id)
    assert store.settle_attachment(picture.id, status="failed", manifest=None, failure="unreadable") is True
    assert _raw_attachment(engine, picture.id) == ("failed", None, "unreadable")
    ref = store.find_attachment("user-a", chat.id, picture.id)
    assert ref is not None and (ref.status, ref.manifest) == ("failed", None)


@pytest.mark.parametrize("render", ["not json", "[1, 2]", '"pdf"'], ids=["not_json", "list", "string"])
def test_a_manifest_that_is_no_json_object_reads_as_none(store: ChatStore, engine: Engine, render: str) -> None:
    """A stored manifest that is not a JSON object is read as none, by the processor's lookup as by every other read."""
    chat = store.create_chat("user-a")
    picture = _picture(store, chat.id)
    assert store.settle_attachment(picture.id, status="ready", manifest={"total": 1}, failure=None) is True
    with Session(engine) as s, s.begin():
        s.execute(update(Attachment).where(Attachment.id == picture.id).values(render=render))
    ref = store.find_attachment("user-a", chat.id, picture.id)
    assert ref is not None and (ref.status, ref.manifest) == ("ready", None)


def test_settling_is_once_and_only_from_processing(store: ChatStore, engine: Engine) -> None:
    """Only a row still being prepared settles; a later settle, a ready row and a gone row change nothing."""
    chat = store.create_chat("user-a")
    picture = _picture(store, chat.id)
    assert store.settle_attachment(picture.id, status="ready", manifest={"total": 1}, failure=None) is True
    assert store.settle_attachment(picture.id, status="failed", manifest=None, failure="late") is False
    assert _raw_attachment(engine, picture.id)[0] == "ready"
    document = _attach(store, chat.id)
    assert store.settle_attachment(document.id, status="failed", manifest=None, failure="late") is False
    assert _raw_attachment(engine, document.id) == ("ready", None, None)
    assert store.settle_attachment(str(uuid4()), status="ready", manifest=None, failure=None) is False


def test_keep_manifest_fills_only_a_ready_row_without_one(store: ChatStore, engine: Engine) -> None:
    """A background prepare's manifest goes to a ``ready`` row that has none, once; nothing else changes."""
    chat = store.create_chat("user-a")
    deck = _picture(store, chat.id, kind="document", status="ready")
    manifest = {"kind": "office", "unit": "slide", "total": 3, "pages_without_text": [2], "path": "/workspace/s/x"}
    assert store.keep_manifest(deck.id, manifest) is True
    ref = store.find_attachment("user-a", chat.id, deck.id)
    assert ref is not None and ref.status == "ready" and ref.manifest is not None
    assert (ref.manifest["kind"], ref.manifest["unit"], ref.manifest["total"]) == ("office", "slide", 3)
    assert "path" not in ref.manifest
    kept = _raw_attachment(engine, deck.id)
    assert store.keep_manifest(deck.id, {"kind": "office", "unit": "page", "total": 9}) is False
    assert _raw_attachment(engine, deck.id) == kept

    processing = _picture(store, chat.id, kind="document")
    failed = _picture(store, chat.id)
    assert store.settle_attachment(failed.id, status="failed", manifest=None, failure="unreadable") is True
    assert store.keep_manifest(processing.id, manifest) is False
    assert store.keep_manifest(failed.id, manifest) is False
    assert _raw_attachment(engine, processing.id) == ("processing", None, None)
    assert _raw_attachment(engine, failed.id) == ("failed", None, "unreadable")

    gone = _picture(store, chat.id, kind="document", status="ready")
    store.delete_chat_rows(chat.id)
    assert store.keep_manifest(gone.id, manifest) is False
    assert store.keep_manifest(str(uuid4()), manifest) is False


def test_an_attachment_is_found_only_through_its_owners_chat(store: ChatStore) -> None:
    """Owner, chat and attachment id together: another owner's or another chat's lookup finds nothing."""
    chat = store.create_chat("user-a")
    picture = _picture(store, chat.id)
    ref = store.find_attachment("user-a", chat.id, picture.id)
    assert ref is not None
    assert (ref.id, ref.chat_id, ref.owner, ref.file_id) == (picture.id, chat.id, "user-a", picture.file_id)
    assert (ref.path, ref.name, ref.kind, ref.status) == (
        f"/workspace/s/{picture.file_id}.jpg",
        "harbour-photo.jpg",
        "image",
        "processing",
    )
    assert (ref.manifest, ref.created_at, ref.expired) == (None, T0, False)
    assert store.find_attachment("user-b", chat.id, picture.id) is None
    assert store.find_attachment("user-a", store.create_chat("user-a").id, picture.id) is None
    assert store.find_attachment("user-a", chat.id, str(uuid4())) is None


def test_pending_statuses_name_each_waiting_attachment(store: ChatStore) -> None:
    """The waiting attachments of a chat, by id, with their status; a sent one and another chat's are not named."""
    chat = store.create_chat("user-a")
    preparing = _picture(store, chat.id)
    ready = _attach(store, chat.id)
    sent = _picture(store, chat.id, status="ready")
    store.add_user_message("user-a", chat.id, "Hallo", [sent.id])
    _picture(store, store.create_chat("user-a").id)
    assert store.pending_statuses(chat.id) == {preparing.id: "processing", ready.id: "ready"}
    assert store.pending_statuses(store.create_chat("user-a").id) == {}


def test_processing_attachments_are_listed_across_chats_with_their_owner(store: ChatStore, clock: Clock) -> None:
    """Every picture still being prepared, oldest first, each with the owner of its chat."""
    first, second = store.create_chat("user-a"), store.create_chat("user-b")
    late = _picture(store, second.id)
    clock.now = T0 - DAY
    early = _picture(store, first.id)
    _attach(store, first.id)
    settled = _picture(store, second.id)
    store.settle_attachment(settled.id, status="failed", manifest=None, failure="unreadable")
    refs = store.processing_attachments()
    assert [(r.id, r.chat_id, r.owner, r.kind, r.status) for r in refs] == [
        (early.id, first.id, "user-a", "image", "processing"),
        (late.id, second.id, "user-b", "image", "processing"),
    ]


def test_stranded_processing_rows_settle(store: ChatStore, engine: Engine) -> None:
    """A restart finds rows nothing will settle: a picture or a clip fails with the token, a document is ready."""
    chat = store.create_chat("user-a")
    picture = _picture(store, chat.id)
    pdf = _picture(store, chat.id, kind="document")
    clip = _picture(store, chat.id, kind="media")
    ready = _attach(store, chat.id)
    failed = _picture(store, chat.id)
    store.settle_attachment(failed.id, status="failed", manifest=None, failure="unreadable")
    assert store.settle_processing("interrupted") == 3
    assert _raw_attachment(engine, picture.id) == ("failed", None, "interrupted")
    assert _raw_attachment(engine, pdf.id) == ("ready", None, None)
    assert _raw_attachment(engine, clip.id) == ("failed", None, "interrupted")
    assert _raw_attachment(engine, ready.id) == ("ready", None, None)
    assert _raw_attachment(engine, failed.id) == ("failed", None, "unreadable")
    assert store.processing_attachments() == []
    assert store.settle_processing("interrupted") == 0


def test_a_manifest_expires_with_its_file(store: ChatStore, engine: Engine, clock: Clock) -> None:
    """Past its file's period a manifest is not served, is due for emptying, and is emptied; the row stays."""
    chat = store.create_chat("user-a")
    scan = _picture(store, chat.id, kind="document")
    store.settle_attachment(scan.id, status="ready", manifest=MANIFEST, failure=None)
    _attach(store, chat.id)
    clock.now = T0 + 7 * DAY - timedelta(microseconds=1)
    live = store.find_attachment("user-a", chat.id, scan.id)
    assert live is not None and live.manifest is not None and not live.expired
    assert store.due_contents(clock.now, 10) == ([], [], [])
    clock.now = T0 + 7 * DAY
    gone = store.find_attachment("user-a", chat.id, scan.id)
    assert gone is not None and gone.manifest is None and gone.expired
    assert store.due_contents(clock.now, 10) == ([], [], [scan.id])
    store.empty_contents([], [], [scan.id])
    assert _raw_attachment(engine, scan.id) == ("ready", None, None)
    assert store.due_contents(clock.now, 10) == ([], [], [])


def test_emptying_a_manifest_leaves_steps_and_results_alone(store: ChatStore, engine: Engine) -> None:
    """The three kinds of content are emptied by their own ids."""
    chat = store.create_chat("user-a")
    answer = store.start_assistant_message(chat.id)
    step, result = _step(store, answer), _result(store, answer)
    scan = _picture(store, chat.id, kind="document")
    store.settle_attachment(scan.id, status="ready", manifest=MANIFEST, failure=None)
    store.empty_contents([], [], [scan.id])
    assert _raw_attachment(engine, scan.id)[1] is None
    with Session(engine) as s:
        assert s.execute(select(ToolStep.result).where(ToolStep.id == step.id)).scalar_one() == "Umsatz 42"
        assert s.execute(select(Result.preview).where(Result.id == result.id)).scalar_one() is not None


MEASURE = {"total": 412, "unit": "page", "chars": 90_000, "capped": False}


def _document(
    store: ChatStore, chat_id: str, *, status: str = "ready", measure_state: MeasureState | None = "pending"
) -> str:
    """Add a pending document, its measure in the state given."""
    file_id = uuid4().hex
    return store.add_attachment(
        chat_id,
        file_id=file_id,
        path=f"/workspace/s/{file_id}.pdf",
        name="long-report.pdf",
        size=4096,
        mime="application/pdf",
        status=status,
        measure_state=measure_state,
    ).id


def _measure_row(engine: Engine, attachment_id: str) -> tuple[str | None, str | None]:
    """Read an attachment's measure state and measure as stored."""
    with Session(engine) as s:
        row = s.execute(
            select(Attachment.measure_state, Attachment.measure).where(Attachment.id == attachment_id)
        ).one()
        return row[0], row[1]


def test_a_measure_settles_once(store: ChatStore, engine: Engine) -> None:
    """Only a pending measure settles: a measure is kept as its four counts, ``None`` is ``none``; nothing twice."""
    chat = store.create_chat("user-a")
    report, ledger = _document(store, chat.id), _document(store, chat.id)
    assert store.settle_measure(report, {**MEASURE, "path": "/workspace/s/x.pdf", "kind": "pdf"}) is True
    assert store.settle_measure(report, None) is False
    assert store.settle_measure(ledger, None) is True
    assert store.settle_measure(ledger, MEASURE) is False
    assert store.settle_measure(_document(store, chat.id, measure_state=None), MEASURE) is False
    assert store.settle_measure("0" * 32, MEASURE) is False
    state, measure = _measure_row(engine, report)
    assert state == "measured" and measure is not None and json.loads(measure) == MEASURE
    assert _measure_row(engine, ledger) == ("none", None)
    out = store.attachment_out("user-a", chat.id, report)
    assert out is not None and out.length == LengthOut(state="measured", total=412, unit="page", too_long=False)
    out = store.attachment_out("user-a", chat.id, ledger)
    assert out is not None and out.length == LengthOut(state="none", total=None, unit=None, too_long=False)


def test_a_document_too_long_for_the_prompt(engine: Engine, clock: Clock) -> None:
    """Too long: more characters than the store's prompt budget in tokens, or a count workbench capped."""
    store = ChatStore(engine, file_days=7, notice_days=30, prompt_budget=1_000, now=clock)
    chat = store.create_chat("user-a")
    cases = {
        (3_000, False): False,
        (3_001, False): True,
        (10, True): True,
    }
    for (chars, capped), too_long in cases.items():
        attachment = _document(store, chat.id)
        store.settle_measure(attachment, {**MEASURE, "chars": chars, "capped": capped})
        out = store.attachment_out("user-a", chat.id, attachment)
        assert out is not None and out.length is not None and out.length.too_long is too_long


def test_a_picture_and_a_row_before_measuring_have_no_length(store: ChatStore) -> None:
    """``NULL`` is never measured: a picture, a clip, or a row from before schema 3; its length is ``None``."""
    chat = store.create_chat("user-a")
    picture = _picture(store, chat.id)
    old = _document(store, chat.id, measure_state=None)
    assert picture.length is None
    out = store.attachment_out("user-a", chat.id, old)
    assert out is not None and out.length is None
    found = store.find_attachment("user-a", chat.id, old)
    assert found is not None and found.measure_state is None


def test_length_goes_with_the_file(store: ChatStore, engine: Engine, clock: Clock) -> None:
    """Past the file's period a length is not served, and the sweep empties the measure with the manifest."""
    chat = store.create_chat("user-a")
    scan = _document(store, chat.id, status="processing")
    store.settle_attachment(scan, status="ready", manifest=MANIFEST, failure=None)
    store.settle_measure(scan, MEASURE)
    notes = _document(store, chat.id)
    store.settle_measure(notes, MEASURE)
    clock.now = T0 + 7 * DAY - timedelta(microseconds=1)
    out = store.attachment_out("user-a", chat.id, scan)
    assert out is not None and out.length is not None and out.length.state == "measured"
    assert store.due_contents(clock.now, 10) == ([], [], [])
    clock.now = T0 + 7 * DAY
    out = store.attachment_out("user-a", chat.id, scan)
    assert out is not None and out.expired and out.length is None
    _, _, due = store.due_contents(clock.now, 10)
    assert sorted(due) == sorted([scan, notes])
    store.empty_contents([], [], due)
    assert _raw_attachment(engine, scan)[1] is None
    assert [_measure_row(engine, attachment)[1] for attachment in (scan, notes)] == [None, None]
    assert store.due_contents(clock.now, 10) == ([], [], [])


def test_pending_measures_are_the_ready_rows_still_pending(store: ChatStore) -> None:
    """A processing row is the prepare's to requeue, a settled measure is no one's; a ready pending row is listed."""
    chat = store.create_chat("user-a")
    ready = _document(store, chat.id)
    _document(store, chat.id, status="processing")
    measured = _document(store, chat.id)
    store.settle_measure(measured, MEASURE)
    assert [(ref.id, ref.owner, ref.measure_state) for ref in store.pending_measures()] == [
        (ready, "user-a", "pending")
    ]


def test_settle_pending_measures_at_start_without_workbench(
    tmp_path: Path, engine: Engine, audit_stream: CapturedAuditStream
) -> None:
    """Without workbench nothing will measure them: every pending measure is ``none`` once the app has started."""
    store = ChatStore(engine, file_days=7, notice_days=30)
    chat = store.create_chat("user-a")
    ready, processing = _document(store, chat.id), _document(store, chat.id, status="processing")
    measured = _document(store, chat.id)
    store.settle_measure(measured, MEASURE)
    assert store.settle_pending_measures() == 2
    assert store.settle_pending_measures() == 0
    assert [_measure_row(engine, attachment)[0] for attachment in (ready, processing, measured)] == [
        "none",
        "none",
        "measured",
    ]
    settings = make_settings(tmp_path, workbench_api_key=None, db_path=str(tmp_path / "start.sqlite3"))
    started = make_engine(settings.db_path)
    init_db(started)
    try:
        earlier = ChatStore(started, file_days=7, notice_days=30)
        left = _document(earlier, earlier.create_chat("user-a").id)
    finally:
        started.dispose()
    with TestClient(create_app(settings, audit_stream=audit_stream)) as client:
        app_store = cast(FastAPI, client.app).state.store
        assert cast(FastAPI, client.app).state.processor is None
        [pending] = app_store.chat_detail("user-a", app_store.list_chats("user-a")[0].id).pending_attachments
        assert pending.id == left and pending.length == LengthOut(state="none", total=None, unit=None, too_long=False)


def test_an_attachment_for_a_chat_deleted_meanwhile_is_refused(store: ChatStore) -> None:
    """A chat deleted while its file was being handed over takes no attachment row."""
    chat = store.create_chat("user-a")
    store.delete_chat_rows(chat.id)
    with pytest.raises(ChatNotFound):
        _attach(store, chat.id)


def test_the_last_answer_can_be_replaced(store: ChatStore) -> None:
    """Only an answer that ends the chat is the last answer; dropping it takes its steps and results."""
    chat = store.create_chat("user-a")
    assert store.last_answer("user-a", chat.id) is None
    question = store.add_user_message("user-a", chat.id, "Summe der Spalte B", [])
    assert store.last_answer("user-a", chat.id) is None
    answer = store.start_assistant_message(chat.id)
    _step(store, answer)
    _result(store, answer)
    store.finish_assistant_message(answer, "Summe: 42")
    assert store.last_answer("user-a", chat.id) == answer
    assert store.question_of(chat.id, answer) == question.id
    assert [(m.id, m.role, m.content) for m in store.chat_detail("user-a", chat.id).messages] == [
        (question.id, "user", "Summe der Spalte B"),
        (answer, "assistant", "Summe: 42"),
    ]
    later = store.add_user_message("user-a", chat.id, "Und im Vorjahr?", [])
    later_answer = store.start_assistant_message(chat.id)
    assert store.question_of(chat.id, later_answer) == later.id and store.question_of(chat.id, answer) == question.id
    assert store.question_of(chat.id, str(uuid4())) is None
    store.drop_message(later_answer)
    store.drop_message(later.id)
    store.drop_message(answer)
    assert store.chat_counts(chat.id) == {"messages": 1, "steps": 0, "results": 0, "attachments": 0}
    assert store.last_answer("user-a", chat.id) is None
    again = store.start_assistant_message(chat.id)
    assert store.last_answer("user-a", chat.id) == again


def test_a_chats_pending_attachments_are_its_unsent_ones(store: ChatStore) -> None:
    """Pending are the chat's own attachments that no message has sent yet."""
    chat, other = store.create_chat("user-a"), store.create_chat("user-a")
    sent, waiting, theirs = _attach(store, chat.id), _attach(store, chat.id), _attach(store, other.id)
    store.add_user_message("user-a", chat.id, "Summe der Spalte B", [sent.id])
    assert store.pending_ids(chat.id) == {waiting.id}
    assert store.pending_ids(other.id) == {theirs.id}


def test_chats_list_by_their_latest_activity(store: ChatStore, clock: Clock) -> None:
    """A rename or a message moves a chat to the top of its owner's list."""
    first = store.create_chat("user-a")
    clock.now = T0 + timedelta(minutes=1)
    second = store.create_chat("user-a")
    store.create_chat("user-b")
    assert [c.id for c in store.list_chats("user-a")] == [second.id, first.id]
    clock.now = T0 + timedelta(minutes=2)
    renamed = store.rename_chat("user-a", first.id, "Q3 Zahlen")
    assert (renamed.title, renamed.updated_at) == ("Q3 Zahlen", clock.now)
    assert [c.id for c in store.list_chats("user-a")] == [first.id, second.id]
    clock.now = T0 + timedelta(minutes=3)
    store.add_user_message("user-a", second.id, "Hallo", [])
    assert store.get_chat("user-a", second.id).updated_at == clock.now
    assert [c.id for c in store.list_chats("user-a")] == [second.id, first.id]


# --- an answer's notices -----------------------------------------------------

#: workbench's id of the survey the notices are about.
SURVEY = "c" * 32
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PARTIAL = {"code": "document_partial", "file_id": SURVEY, "read": 9, "total": 500, "unit": "page"}


def _asked(store: ChatStore) -> tuple[str, str]:
    """Start a chat whose question sends the harbour survey, and open its answer.

    Args:
        store: The store.

    Returns:
        The chat's and the answer's ids.
    """
    chat = store.create_chat("user-a")
    survey = store.add_attachment(
        chat.id, file_id=SURVEY, path=f"/workspace/s/{SURVEY}.docx", name="harbour-survey.docx", size=4096, mime=DOCX
    )
    store.add_user_message("user-a", chat.id, "Fasse den Bericht zusammen.", [survey.id])
    return chat.id, store.start_assistant_message(chat.id)


def _raw_notices(engine: Engine, message_id: str) -> tuple[str | None, datetime | None]:
    """Read an answer's notices and their deadline as stored."""
    with Session(engine) as s:
        row = s.execute(select(Message.notices, Message.notices_expire_at).where(Message.id == message_id)).one()
        return row[0], row[1]


def test_an_answer_keeps_its_notices_and_estimate(store: ChatStore, engine: Engine) -> None:
    """The notices come back in order, a file's with its attachment's name, never stored; a question has none."""
    chat_id, answer = _asked(store)
    unread = {"code": "document_unread", "file_id": SURVEY}
    notices = [{"code": "answer_cut"}, unread]
    store.finish_assistant_message(answer, "Der Bericht", notices=notices, context_estimate=1234)
    question, reply = store.chat_detail("user-a", chat_id).messages
    assert reply.notices == [
        NoticeOut(code="answer_cut"),
        NoticeOut(code="document_unread", file_id=SURVEY, name="harbour-survey.docx"),
    ]
    assert reply.context_estimate == 1234
    assert (question.notices, question.context_estimate) == (None, None)
    stored, deadline = _raw_notices(engine, answer)
    assert stored is not None and json.loads(stored) == notices
    assert "harbour-survey" not in stored and deadline == T0 + 7 * DAY


def test_a_notice_is_stored_without_a_name_whoever_the_caller_is(store: ChatStore, engine: Engine) -> None:
    """A name a caller leaves on a notice is dropped before it is stored; the code and the file id stay."""
    chat_id, answer = _asked(store)
    named = {"code": "document_unread", "file_id": SURVEY, "name": "harbour-survey.docx"}
    store.finish_assistant_message(answer, "Der Bericht", notices=[named])
    stored, _ = _raw_notices(engine, answer)
    assert stored is not None and json.loads(stored) == [{"code": "document_unread", "file_id": SURVEY}]
    assert "harbour-survey" not in stored
    assert named["name"] == "harbour-survey.docx"
    reply = store.chat_detail("user-a", chat_id).messages[1]
    assert reply.notices == [NoticeOut(code="document_unread", file_id=SURVEY, name="harbour-survey.docx")]


def test_only_a_notice_about_a_file_sets_a_deadline(store: ChatStore, engine: Engine) -> None:
    """Codes without details set no deadline; no notices are an empty list; a code not known here is left out."""
    chat_id, answer = _asked(store)
    store.finish_assistant_message(answer, "Der Bericht", notices=[{"code": "images_dropped"}, {"code": "answer_cut"}])
    assert _raw_notices(engine, answer)[1] is None
    later = store.start_assistant_message(chat_id)
    store.finish_assistant_message(later, "Noch einmal")
    assert _raw_notices(engine, later) == ("[]", None)
    assert store.chat_detail("user-a", chat_id).messages[2].notices == []
    with Session(engine) as s, s.begin():
        stored = json.dumps([{"code": "later_code"}, ["answer_cut"], {"code": "answer_cut"}])
        s.execute(update(Message).where(Message.id == later).values(notices=stored))
    assert store.chat_detail("user-a", chat_id).messages[2].notices == [NoticeOut(code="answer_cut")]


def test_notice_details_follow_the_content_deadline(store: ChatStore, engine: Engine, clock: Clock) -> None:
    """The survey sent on day 0 and answered on day 1: its notice's details are served until day 7, then its code alone.

    Before any sweep the details are still stored; past the deadline they are only not served.
    """
    chat = store.create_chat("user-a")
    survey = store.add_attachment(
        chat.id, file_id=SURVEY, path=f"/workspace/s/{SURVEY}.docx", name="harbour-survey.docx", size=4096, mime=DOCX
    )
    store.add_user_message("user-a", chat.id, "Fasse den Bericht zusammen.", [survey.id])
    clock.now = T0 + DAY
    answer = store.start_assistant_message(chat.id)
    store.finish_assistant_message(answer, "Der Bericht", notices=[PARTIAL])
    assert _raw_notices(engine, answer)[1] == T0 + 7 * DAY

    def served() -> list[NoticeOut] | None:
        return store.chat_detail("user-a", chat.id).messages[1].notices

    clock.now = T0 + 7 * DAY - timedelta(seconds=1)
    assert served() == [
        NoticeOut(code="document_partial", file_id=SURVEY, name="harbour-survey.docx", read=9, total=500, unit="page")
    ]
    clock.now = T0 + 7 * DAY
    assert [notice.model_dump() for notice in served() or []] == [
        {"code": "document_partial", "file_id": None, "name": None, "read": None, "total": None, "unit": None}
    ]
    stored = _raw_notices(engine, answer)[0]
    assert stored is not None and json.loads(stored) == [PARTIAL]


def test_a_continued_write_keeps_the_deadline(store: ChatStore, engine: Engine, clock: Clock) -> None:
    """A later write of the answer (Continue) stores its own notices and estimate, and keeps the first one's deadline.

    The later write is made under a 3-day file period, which would set another deadline.
    """
    chat_id, answer = _asked(store)
    store.finish_assistant_message(answer, "Der Bericht", notices=[PARTIAL, {"code": "answer_cut"}])
    assert _raw_notices(engine, answer)[1] == T0 + 7 * DAY
    clock.now = T0 + DAY
    later = ChatStore(engine, file_days=3, notice_days=30, now=clock)
    later.finish_assistant_message(answer, "Der Bericht, weiter.", notices=[PARTIAL], context_estimate=2048)
    stored, deadline = _raw_notices(engine, answer)
    assert deadline == T0 + 7 * DAY
    assert stored is not None and json.loads(stored) == [PARTIAL]
    reply = store.chat_detail("user-a", chat_id).messages[1]
    assert (reply.content, reply.context_estimate) == ("Der Bericht, weiter.", 2048)


def test_apply_periods_moves_notice_deadlines(store: ChatStore, engine: Engine, clock: Clock) -> None:
    """A shorter file period moves a deadline still ahead earlier; one already passed stays, as a step's does."""
    clock.now = T0 - 10 * DAY
    store.apply_periods(90)
    _, passed = _asked(store)
    store.finish_assistant_message(passed, "Alt", notices=[PARTIAL])
    clock.now = T0
    _, live = _asked(store)
    store.finish_assistant_message(live, "Neu", notices=[PARTIAL])
    _, plain = _asked(store)
    store.finish_assistant_message(plain, "Ohne Datei", notices=[{"code": "answer_cut"}])
    clock.now = T0 + DAY
    ChatStore(engine, file_days=3, notice_days=30, now=clock).apply_periods(90)
    assert _raw_notices(engine, passed)[1] == T0 - 3 * DAY
    assert _raw_notices(engine, live)[1] == T0 + 3 * DAY
    assert _raw_notices(engine, plain)[1] is None


def test_an_answer_from_before_schema_3_has_no_notices(store: ChatStore, engine: Engine) -> None:
    """An answer 0.6 stored has ``NULL`` notices and estimate: it is served with neither, as before."""
    chat_id, answer = _asked(store)
    store.finish_assistant_message(answer, "Der Bericht")
    with Session(engine) as s, s.begin():
        s.execute(update(Message).where(Message.id == answer).values(notices=None, context_estimate=None))
    question, reply = store.chat_detail("user-a", chat_id).messages
    assert (reply.content, reply.notices, reply.context_estimate) == ("Der Bericht", None, None)
    assert (question.notices, question.context_estimate) == (None, None)


def test_the_app_opens_the_store_and_applies_the_period(client: TestClient) -> None:
    """At startup the schema is in place and the chat period is in force."""
    store = cast(FastAPI, client.app).state.store
    assert isinstance(store, ChatStore)
    chat = store.create_chat("user-a")
    assert chat.expires_at is not None and chat.expires_at - chat.created_at == 90 * DAY
