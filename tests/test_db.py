"""The SQLite file: its schema and version, its pragmas and its times."""

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Column, Connection, Engine, Integer, MetaData, Table, Text, inspect, select
from sqlalchemy.exc import IntegrityError, StatementError

from ghostwriter import db
from ghostwriter.db import UtcDateTime, init_db, make_engine, utcnow

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
    """An empty file gets every table and version 1; a second start changes nothing."""
    engine = make_engine(str(tmp_path / "a.sqlite3"))
    init_db(engine)
    assert set(inspect(engine).get_table_names()) == TABLES
    assert _pragma(engine, "user_version") == 1
    init_db(engine)
    assert _pragma(engine, "user_version") == 1


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
        conn.exec_driver_sql("PRAGMA user_version = 2")
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

    def to_2(conn: Connection) -> None:
        conn.exec_driver_sql("CREATE TABLE extra_2 (id INTEGER)")
        ran.append(2)

    def to_3(conn: Connection) -> None:
        conn.exec_driver_sql("CREATE TABLE extra_3 (id INTEGER)")
        ran.append(3)
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(db, "MIGRATIONS", {1: to_2, 2: to_3})
    with pytest.raises(RuntimeError, match="synthetic"):
        init_db(engine)
    assert ran == [2, 3]
    assert _pragma(engine, "user_version") == 2
    tables = set(inspect(engine).get_table_names())
    assert "extra_2" in tables and "extra_3" not in tables


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
