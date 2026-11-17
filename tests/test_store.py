"""The chat store: ownership, the chat period, contents that expire with their files, and deletion."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.orm import InstrumentedAttribute, Session

from ghostwriter.db import init_db, make_engine
from ghostwriter.models import Attachment, Chat, Message, Result, ToolStep
from ghostwriter.schemas import AttachmentOut, ResultOut, StepOut, TablePreviewOut
from ghostwriter.store import AttachmentNotPending, ChatNotFound, ChatStore, FileRef, TooManyPending

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
            for model in (Chat, Message, ToolStep, Result, Attachment)
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
    assert _counts(engine) == {"chats": 1, "messages": 2, "tool_steps": 1, "results": 1, "attachments": 2}
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


def test_the_app_opens_the_store_and_applies_the_period(client: TestClient) -> None:
    """At startup the schema is in place and the chat period is in force."""
    store = cast(FastAPI, client.app).state.store
    assert isinstance(store, ChatStore)
    chat = store.create_chat("user-a")
    assert chat.expires_at is not None and chat.expires_at - chat.created_at == 90 * DAY
