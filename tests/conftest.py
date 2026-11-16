"""Shared fixtures and helpers for the Ghostwriter tests.

Every test builds its settings from an explicit mapping (:func:`make_settings`),
never from the process environment, so a developer's own ``.env`` or shell
exports cannot change what a test sees.
"""

import dataclasses
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _pytest.logging import LogCaptureFixture
from loguru import logger

from ghostwriter.config import Settings, load_settings

#: The edge secret of the test gateway, as long as the backend requires.
SECRET = "s" * 32
#: Ghostwriter's workbench key in tests.
KEY = "k" * 32


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Build the settings of one test: the edge secret, the workbench key and a throwaway database.

    Args:
        tmp_path: The test's temporary directory, which holds the database.
        **overrides: Fields of :class:`~ghostwriter.config.Settings` to set.

    Returns:
        Settings: The defaults of :func:`~ghostwriter.config.load_settings`
        with ``SECRET``, ``KEY``, a ``db_path`` under ``tmp_path`` and the
        overrides.
    """
    settings = load_settings({"GHOSTWRITER_EDGE_SECRET": SECRET, "WORKBENCH_API_KEY": KEY})
    return dataclasses.replace(settings, db_path=str(tmp_path / "ghostwriter.sqlite3"), **overrides)


def as_user(name: str = "user-a") -> dict[str, str]:
    """Return the headers the edge gateway sends for a signed-in user.

    Args:
        name: The account, as Authelia names it.

    Returns:
        dict[str, str]: ``X-Auth-User`` and the matching ``X-Edge-Secret``.
    """
    return {"X-Auth-User": name, "X-Edge-Secret": SECRET}


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
