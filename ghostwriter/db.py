"""The SQLite file: its engine, its schema version and its times.

Every connection turns on foreign keys, WAL, a 5 s busy wait and
``secure_delete``, so a deleted row is overwritten in the file rather than left
in a free page. The schema is versioned in ``PRAGMA user_version``:
:func:`init_db` creates it in an empty file, migrates an older one a version at
a time and refuses a newer one. Times are stored as naive UTC and read back
aware (:class:`UtcDateTime`).
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import Connection, DateTime, Dialect, Engine, create_engine, event, inspect
from sqlalchemy.types import TypeDecorator
from typing_extensions import override

#: The schema this release writes.
SCHEMA_VERSION = 1
#: Version ``n`` to ``n + 1``, each run in a transaction of its own.
MIGRATIONS: dict[int, Callable[[Connection], None]] = {}

_PRAGMAS: Final = ("busy_timeout=5000", "foreign_keys=ON", "secure_delete=ON", "journal_mode=WAL")


def utcnow() -> datetime:
    """Return the current time, aware in UTC.

    Returns:
        Now.
    """
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A time stored as naive UTC and returned aware in UTC; a naive time is refused."""

    impl = DateTime
    cache_ok = True

    @override
    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Convert an aware time to naive UTC for storage.

        Args:
            value: The time.
            dialect: The database dialect.

        Returns:
            The naive UTC time, or ``None``.

        Raises:
            ValueError: If the time carries no timezone.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("A stored time must carry a timezone.")
        return value.astimezone(UTC).replace(tzinfo=None)

    @override
    def process_result_value(self, value: Any | None, dialect: Dialect) -> datetime | None:
        """Mark a stored time as UTC.

        Args:
            value: The naive UTC time read.
            dialect: The database dialect.

        Returns:
            The aware time, or ``None``.
        """
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def _set_pragmas(dbapi_connection: Any, connection_record: Any) -> None:
    """Configure a new connection.

    Args:
        dbapi_connection: The driver's connection.
        connection_record: The pool's record of it.
    """
    cursor = dbapi_connection.cursor()
    try:
        for pragma in _PRAGMAS:
            cursor.execute(f"PRAGMA {pragma}")
    finally:
        cursor.close()


def make_engine(path: str) -> Engine:
    """Return the engine of the SQLite file at ``path``.

    Args:
        path: The database file.

    Returns:
        The engine; each of its connections has the pragmas set.
    """
    engine = create_engine(f"sqlite:///{path}")
    event.listen(engine, "connect", _set_pragmas)
    return engine


def _user_version(conn: Connection) -> int:
    """Read the schema version.

    Args:
        conn: An open connection.

    Returns:
        The version; 0 for a file no release has written.
    """
    return int(conn.exec_driver_sql("PRAGMA user_version").scalar_one())


def init_db(engine: Engine) -> None:
    """Bring the file to :data:`SCHEMA_VERSION`, one transaction per step.

    An empty file gets the schema; an older one runs its migrations in order,
    each committed with its version, so a failed one leaves the version before.

    Args:
        engine: The database.

    Raises:
        RuntimeError: If the file is from a newer release, or holds tables but no version.
    """
    from ghostwriter.models import Base

    while True:
        with engine.begin() as conn:
            # pysqlite begins a transaction only before DML; DDL and the version need it too.
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            version = _user_version(conn)
            if version == SCHEMA_VERSION:
                return
            if version > SCHEMA_VERSION:
                raise RuntimeError("database is from a newer Ghostwriter")
            if version == 0:
                if inspect(conn).get_table_names():
                    raise RuntimeError("database has tables but no schema version")
                Base.metadata.create_all(conn)
                target = SCHEMA_VERSION
            else:
                MIGRATIONS[version](conn)
                target = version + 1
            conn.exec_driver_sql(f"PRAGMA user_version = {target}")
