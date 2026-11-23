"""Shared fixtures and helpers for the Ghostwriter tests.

Every test builds its settings from an explicit mapping (:func:`make_settings`),
never from the process environment, so a developer's own ``.env`` or shell
exports cannot change what a test sees.
"""

import dataclasses
import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any, TextIO

import pytest
from _pytest.logging import LogCaptureFixture
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from loguru import logger
from typing_extensions import override

from ghostwriter.audit import Operation, audited
from ghostwriter.config import Settings, load_settings
from ghostwriter.main import create_app
from ghostwriter.utils.audit_event import AuditWriteError, verify_chain

#: The edge secret of the test gateway, as long as the backend requires.
SECRET = "s" * 32
#: Ghostwriter's workbench key in tests.
KEY = "k" * 32
#: Ghostwriter's service token at Nextext in tests, as long as the backend requires.
NEXTEXT_KEY = "n" * 32
#: Nextext's API base in tests.
NEXTEXT_BASE = "http://nextext.test/api/v1"


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Build the settings of one test: the edge secret, the workbench key, vision off and a throwaway database.

    Vision is off unless a test passes ``vision=True``, so every test that does not
    care about pictures keeps the behaviour of a model without them. Nextext is off
    too (no key in the environment); a test of video or audio passes
    ``nextext_api_base`` and ``nextext_api_key``.

    Args:
        tmp_path: The test's temporary directory, which holds the database.
        **overrides: Fields of :class:`~ghostwriter.config.Settings` to set.

    Returns:
        Settings: The defaults of :func:`~ghostwriter.config.load_settings`
        with ``SECRET``, ``KEY``, ``vision=False``, Nextext off, a ``db_path`` under
        ``tmp_path`` and the overrides.
    """
    settings = load_settings({"GHOSTWRITER_EDGE_SECRET": SECRET, "WORKBENCH_API_KEY": KEY})
    fields: dict[str, Any] = {"vision": False, "db_path": str(tmp_path / "ghostwriter.sqlite3"), **overrides}
    return dataclasses.replace(settings, **fields)


def as_user(name: str = "user-a") -> dict[str, str]:
    """Return the headers the edge gateway sends for a signed-in user.

    Args:
        name: The account, as Authelia names it.

    Returns:
        dict[str, str]: ``X-Auth-User`` and the matching ``X-Edge-Secret``.
    """
    return {"X-Auth-User": name, "X-Edge-Secret": SECRET}


def unrecorded_child(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    """Make every child event named ``operation`` fail to be written, as a broken stream would; others are written.

    Args:
        monkeypatch: The test's patcher, which undoes this at teardown.
        operation: The child's operation name, such as ``chat.workbench``.
    """
    original = Operation.child

    def child(self: Operation, action: str, name: str, **fields: Any) -> str:
        if name == operation:
            raise AuditWriteError("synthetic audit stream failure")
        return original(self, action, name, **fields)

    monkeypatch.setattr(Operation, "child", child)


class CapturedAuditStream(io.StringIO):
    """An in-memory audit stream for one test.

    Attributes:
        failing: While set, every write fails as a broken output would.
        writes_left: When set, the writes that still succeed; every later one fails.
    """

    failing: bool = False
    writes_left: int | None = None

    @override
    def write(self, s: str, /) -> int:
        """Keep the text, or fail while :attr:`failing` is set or no write is left.

        Args:
            s: The text to write.

        Returns:
            int: The number of characters written.

        Raises:
            OSError: While :attr:`failing` is set, or once :attr:`writes_left` is used up.
        """
        if self.writes_left is not None:
            if self.writes_left <= 0:
                raise OSError("synthetic audit stream failure")
            self.writes_left -= 1
        if self.failing:
            raise OSError("synthetic audit stream failure")
        return super().write(s)


def events(stream: CapturedAuditStream) -> list[dict[str, Any]]:
    """Return every event written to a stream so far.

    Args:
        stream: The test's audit stream.

    Returns:
        list[dict[str, Any]]: The parsed lines, in order.
    """
    return [json.loads(line) for line in stream.getvalue().splitlines()]


@pytest.fixture
def audit_stream() -> Iterator[CapturedAuditStream]:
    """Return the stream one test's audit events go to, and verify its chains afterwards.

    Yields:
        CapturedAuditStream: An empty in-memory stream.
    """
    stream = CapturedAuditStream()
    yield stream
    report = verify_chain(events(stream))
    assert report.ok, report.problems


def probe_app(settings: Settings, stream: TextIO) -> FastAPI:
    """Build the app with one audited route for the tests: ``POST /test``.

    The route opens ``collect`` / ``test.op``, which may also record
    ``ai_call``, records it and answers with the actor.

    Args:
        settings: The settings to serve with.
        stream: Where the app's audit chain goes.

    Returns:
        FastAPI: The app.
    """
    app = create_app(settings, audit_stream=stream)

    @app.post("/test")
    def probe(op: Annotated[Operation, Depends(audited("collect", "test.op", also=("ai_call",)))]) -> dict[str, Any]:
        """Record the operation.

        Args:
            op: The request's operation.

        Returns:
            dict[str, Any]: Who acted, and how the identity was established.
        """
        op.record()
        return {"user": op.context.actor.user, "via": op.context.actor.via}

    return app


@pytest.fixture
def client(tmp_path: Path, audit_stream: CapturedAuditStream) -> Iterator[TestClient]:
    """Serve :func:`probe_app` with the test settings, its chain open on ``audit_stream``.

    Args:
        tmp_path: The test's temporary directory.
        audit_stream: The stream the chain goes to.

    Yields:
        TestClient: The client, inside the app's lifespan.
    """
    with TestClient(probe_app(make_settings(tmp_path), audit_stream)) as test_client:
        yield test_client


@pytest.fixture
def loguru_caplog(caplog: LogCaptureFixture) -> Iterator[LogCaptureFixture]:
    """Bridge loguru WARNING records into ``caplog`` for the duration of a test.

    Loguru bypasses the stdlib ``logging`` module, so ``caplog`` sees none of
    its records by default. Adding ``caplog.handler`` as a loguru sink is the
    whole bridge; asserting on ``caplog.text`` then works as it would for a
    stdlib logger.

    Args:
        caplog: The standard pytest log-capture fixture.

    Yields:
        The same ``caplog`` fixture, now populated with loguru-sourced
        records at WARNING level and above.
    """
    handler_id = logger.add(caplog.handler, level="WARNING", format="{message}")
    caplog.set_level(logging.WARNING)
    try:
        yield caplog
    finally:
        logger.remove(handler_id)
