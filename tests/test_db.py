"""The SQLite file: its schema and version, its pragmas and its times."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Column, Connection, Engine, Integer, MetaData, Table, Text, inspect, select
from sqlalchemy.exc import IntegrityError, StatementError

from ghostwriter import db
from ghostwriter.db import SCHEMA_VERSION, UtcDateTime, init_db, make_engine, utcnow
from ghostwriter.models import Base

TABLES = {"chats", "messages", "tool_steps", "attachments", "results", "retention_periods", "file_marks"}


def _pragma(engine: Engine, name: str) -> object:
    """Read one pragma on a fresh connection.

    Args:
        engine: The database.
        name: The pragma.

    Returns:
        Its value.
    """
    with engine.connect() as conn:
        return conn.exec_driver_sql(f"PRAGMA {name}").scalar()


def test_a_fresh_database_gets_the_schema_and_its_version(tmp_path: Path) -> None:
    """An empty file gets every table and the current version; a second start changes nothing."""
    engine = make_engine(str(tmp_path / "a.sqlite3"))
    init_db(engine)
    assert set(inspect(engine).get_table_names()) == TABLES
    assert _pragma(engine, "user_version") == SCHEMA_VERSION
    init_db(engine)
    assert _pragma(engine, "user_version") == SCHEMA_VERSION


def test_the_lookups_by_message_and_chat_are_indexed(tmp_path: Path) -> None:
    """An answer's attachments and a chat's file marks are found by index."""
    engine = make_engine(str(tmp_path / "a.sqlite3"))
    init_db(engine)
    indexed = {
        table: [index["column_names"] for index in inspect(engine).get_indexes(table)]
        for table in ("attachments", "file_marks")
    }
    assert ["message_id"] in indexed["attachments"] and ["chat_id"] in indexed["file_marks"]


def test_a_newer_database_stops_startup(tmp_path: Path) -> None:
    """A file from a later release is never opened by an older one."""
    engine = make_engine(str(tmp_path / "a.sqlite3"))
    with engine.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="newer"):
        init_db(engine)


def test_a_database_with_tables_but_no_version_stops_startup(tmp_path: Path) -> None:
    """A file holding tables that Ghostwriter did not version is not Ghostwriter's."""
    engine = make_engine(str(tmp_path / "a.sqlite3"))
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE other (id INTEGER)")
    with pytest.raises(RuntimeError, match="no schema version"):
        init_db(engine)


def test_migrations_run_in_order_one_transaction_each(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each migration commits with its version; one that fails leaves no trace."""
    engine = make_engine(str(tmp_path / "a.sqlite3"))
    init_db(engine)
    ran: list[int] = []
    current = SCHEMA_VERSION

    def to_next(conn: Connection) -> None:
        conn.exec_driver_sql("CREATE TABLE extra_next (id INTEGER)")
        ran.append(current + 1)

    def to_after(conn: Connection) -> None:
        conn.exec_driver_sql("CREATE TABLE extra_after (id INTEGER)")
        ran.append(current + 2)
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(db, "SCHEMA_VERSION", current + 2)
    monkeypatch.setattr(db, "MIGRATIONS", {current: to_next, current + 1: to_after})
    with pytest.raises(RuntimeError, match="synthetic"):
        init_db(engine)
    assert ran == [current + 1, current + 2]
    assert _pragma(engine, "user_version") == current + 1
    tables = set(inspect(engine).get_table_names())
    assert "extra_next" in tables and "extra_after" not in tables


#: Messages as versions 1 and 2 wrote them.
_V2_MESSAGES = """CREATE TABLE messages (id VARCHAR(36) NOT NULL PRIMARY KEY,
  chat_id VARCHAR(36) NOT NULL REFERENCES chats (id) ON DELETE CASCADE, position INTEGER NOT NULL,
  role VARCHAR(16) NOT NULL, content TEXT NOT NULL, created_at DATETIME NOT NULL, UNIQUE (chat_id, position),
  CONSTRAINT ck_messages_role CHECK (role IN ('user', 'assistant')))"""
#: Version 1's attachments, as the first release wrote them.
_V1_ATTACHMENTS = """CREATE TABLE attachments (id VARCHAR(36) NOT NULL PRIMARY KEY,
  chat_id VARCHAR(36) NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
  message_id VARCHAR(36), workbench_file_id VARCHAR(32) NOT NULL, path VARCHAR(1024) NOT NULL,
  name VARCHAR(255) NOT NULL, size INTEGER NOT NULL, mime VARCHAR(127) NOT NULL, created_at DATETIME NOT NULL)"""
#: Version 2's attachments, as 0.6 wrote them.
_V2_ATTACHMENTS = """CREATE TABLE attachments (id VARCHAR(36) NOT NULL PRIMARY KEY,
  chat_id VARCHAR(36) NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
  message_id VARCHAR(36) REFERENCES messages (id) ON DELETE SET NULL, workbench_file_id VARCHAR(32) NOT NULL,
  path VARCHAR(1024) NOT NULL, name VARCHAR(255) NOT NULL, size INTEGER NOT NULL, mime VARCHAR(127) NOT NULL,
  kind VARCHAR(8) DEFAULT 'document' NOT NULL, status VARCHAR(10) DEFAULT 'ready' NOT NULL, render TEXT,
  failure VARCHAR(40), created_at DATETIME NOT NULL,
  CONSTRAINT ck_attachments_kind CHECK (kind IN ('document', 'image', 'media')),
  CONSTRAINT ck_attachments_status CHECK (status IN ('processing', 'ready', 'failed')))"""
_ADD_CHAT = (
    "INSERT INTO chats (id, owner, title, created_at, updated_at)"
    " VALUES ('chat-1', 'user-a', NULL, '2026-11-16 00:00:00.000000', '2026-11-16 00:00:00.000000')"
)
_ADD_MESSAGE = (
    "INSERT INTO messages (id, chat_id, position, role, content, created_at)"
    " VALUES ('msg-{n}', 'chat-1', {n}, '{role}', 'Wie tief ist das Becken?', '2026-11-16 00:00:00.000000')"
)
_ADD_ATTACHMENT = (
    "INSERT INTO attachments (id, chat_id, workbench_file_id, path, name, size, mime, created_at{extra})"
    " VALUES ('att-{n}', 'chat-1', '{f}', '/workspace/s/quay-scan.pdf', 'quay-scan.pdf', 14,"
    " 'application/pdf', '2026-11-16 00:00:00.000000'{values})"
)


def test_a_version_1_file_gets_kinds_and_statuses(tmp_path: Path) -> None:
    """A file written before pictures has its attachments read as ready documents, and checks what comes after."""
    engine = make_engine(str(tmp_path / "a.sqlite3"))
    with engine.begin() as conn:
        Base.metadata.tables["chats"].create(conn)
        conn.exec_driver_sql(_V2_MESSAGES)
        conn.exec_driver_sql(_V1_ATTACHMENTS)
        conn.exec_driver_sql(_ADD_CHAT)
        conn.exec_driver_sql(_ADD_ATTACHMENT.format(n=1, f="f" * 32, extra="", values=""))
        conn.exec_driver_sql("PRAGMA user_version = 1")
    init_db(engine)
    assert _pragma(engine, "user_version") == SCHEMA_VERSION
    with engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT kind, status, render, failure FROM attachments").one()
    assert tuple(row) == ("document", "ready", None, None)
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.exec_driver_sql(_ADD_ATTACHMENT.format(n=2, f="e" * 32, extra=", kind", values=", 'video'"))
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.exec_driver_sql(_ADD_ATTACHMENT.format(n=3, f="d" * 32, extra=", status", values=", 'lost'"))
    assert ["status"] in [index["column_names"] for index in inspect(engine).get_indexes("attachments")]
    assert "ix_attachments_status" in {index["name"] for index in inspect(engine).get_indexes("attachments")}
    init_db(engine)
    assert _pragma(engine, "user_version") == SCHEMA_VERSION


def _version_2_file(path: Path) -> Engine:
    """Write a file as 0.6 left it: a chat, a question, an answer and a ready attachment, at version 2.

    Args:
        path: The file.

    Returns:
        Its engine.
    """
    engine = make_engine(str(path))
    with engine.begin() as conn:
        Base.metadata.tables["chats"].create(conn)
        conn.exec_driver_sql(_V2_MESSAGES)
        conn.exec_driver_sql(_V2_ATTACHMENTS)
        conn.exec_driver_sql(_ADD_CHAT)
        conn.exec_driver_sql(_ADD_MESSAGE.format(n=0, role="user"))
        conn.exec_driver_sql(_ADD_MESSAGE.format(n=1, role="assistant"))
        conn.exec_driver_sql(_ADD_ATTACHMENT.format(n=1, f="f" * 32, extra=", status", values=", 'ready'"))
        conn.exec_driver_sql("PRAGMA user_version = 2")
    return engine


def test_a_version_2_file_gets_notices_and_measures(tmp_path: Path) -> None:
    """A file 0.6 wrote reaches version 3 with every new column empty, and checks what comes after."""
    engine = _version_2_file(tmp_path / "a.sqlite3")
    init_db(engine)
    assert _pragma(engine, "user_version") == SCHEMA_VERSION == 3
    with engine.connect() as conn:
        messages = conn.exec_driver_sql(
            "SELECT role, notices, context_estimate, notices_expire_at FROM messages ORDER BY position"
        ).all()
        attachment = conn.exec_driver_sql("SELECT status, measure, measure_state FROM attachments").one()
    assert [tuple(row) for row in messages] == [("user", None, None, None), ("assistant", None, None, None)]
    assert tuple(attachment) == ("ready", None, None)
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.exec_driver_sql(_ADD_ATTACHMENT.format(n=2, f="e" * 32, extra=", measure_state", values=", 'lost'"))
    assert "ix_messages_notices_expire_at" in {index["name"] for index in inspect(engine).get_indexes("messages")}
    init_db(engine)
    assert _pragma(engine, "user_version") == SCHEMA_VERSION


def _columns(engine: Engine, table: str) -> set[tuple[str, str, bool]]:
    """Read a table's columns: each one's name, type and whether it takes ``NULL``."""
    return {(c["name"], str(c["type"]), c["nullable"]) for c in inspect(engine).get_columns(table)}


def test_a_fresh_file_has_the_same_columns_and_checks_as_a_migrated_one(tmp_path: Path) -> None:
    """The model's schema and the migrations agree on what a message and an attachment hold and refuse."""
    engine = make_engine(str(tmp_path / "a.sqlite3"))
    init_db(engine)
    migrated = _version_2_file(tmp_path / "b.sqlite3")
    init_db(migrated)
    for table in ("messages", "attachments"):
        assert _columns(engine, table) == _columns(migrated, table)
    columns = {column["name"] for column in inspect(engine).get_columns("attachments")}
    assert {"kind", "status", "render", "failure", "measure", "measure_state"} <= columns
    columns = {column["name"] for column in inspect(engine).get_columns("messages")}
    assert {"notices", "context_estimate", "notices_expire_at"} <= columns
    assert "ix_messages_notices_expire_at" in {index["name"] for index in inspect(engine).get_indexes("messages")}
    with engine.begin() as conn:
        conn.exec_driver_sql(_ADD_CHAT)
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.exec_driver_sql(_ADD_ATTACHMENT.format(n=1, f="f" * 32, extra=", kind", values=", 'video'"))
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.exec_driver_sql(_ADD_ATTACHMENT.format(n=2, f="e" * 32, extra=", status", values=", 'lost'"))
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.exec_driver_sql(_ADD_ATTACHMENT.format(n=3, f="d" * 32, extra=", measure_state", values=", 'lost'"))
    with engine.begin() as conn:
        conn.exec_driver_sql(_ADD_ATTACHMENT.format(n=4, f="c" * 32, extra=", measure_state", values=", 'pending'"))
    migrated.dispose()


def test_every_connection_has_the_pragmas(tmp_path: Path) -> None:
    """Foreign keys, WAL, a busy wait and overwritten deletes hold on each pooled connection."""
    engine = make_engine(str(tmp_path / "a.sqlite3"))
    with engine.connect() as first, engine.connect() as second:
        for conn in (first, second):
            values = {
                name: conn.exec_driver_sql(f"PRAGMA {name}").scalar()
                for name in ("foreign_keys", "journal_mode", "secure_delete", "busy_timeout")
            }
            assert values == {"foreign_keys": 1, "journal_mode": "wal", "secure_delete": 1, "busy_timeout": 5000}


def test_a_failed_statement_names_no_bound_value(tmp_path: Path) -> None:
    """An error's text, which the server log prints, never carries what was bound."""
    table = Table("t", MetaData(), Column("id", Integer, primary_key=True), Column("text", Text))
    engine = make_engine(str(tmp_path / "a.sqlite3"))
    table.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert().values(id=1, text="first"))
    with engine.begin() as conn, pytest.raises(IntegrityError) as refused:
        conn.execute(table.insert().values(id=1, text="SENTINEL-Umsatz pro Monat"))
    assert "SENTINEL" not in str(refused.value)


def test_times_come_back_aware_in_utc(tmp_path: Path) -> None:
    """A time is stored as UTC and read back aware; a naive one is refused."""
    assert utcnow().tzinfo is UTC
    table = Table("t", MetaData(), Column("id", Integer, primary_key=True), Column("at", UtcDateTime()))
    engine = make_engine(str(tmp_path / "a.sqlite3"))
    table.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert().values(id=1, at=datetime(2026, 11, 16, 10, 0, tzinfo=timezone(timedelta(hours=1)))))
        stored = conn.execute(select(table.c.at)).scalar_one()
    assert stored == datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
    assert stored.tzinfo is UTC
    with engine.begin() as conn, pytest.raises(StatementError) as refused:
        conn.execute(table.insert().values(id=2, at=datetime(2026, 11, 16, 10, 0)))
    assert isinstance(refused.value.orig, ValueError)
