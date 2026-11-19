"""Ghostwriter's settings, read once from the environment at startup.

:func:`load_settings` is the one place the backend reads its environment. A
blank variable counts as unset. A value that cannot work stops startup with a
``ValueError`` that names the variable and never quotes its value.
"""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from loguru import logger

from ghostwriter.agent.limits import CONTEXT_TOKENS, MIN_CONTEXT_TOKENS
from ghostwriter.utils.audit_event import ACTIONS
from ghostwriter.utils.audit_runtime import parse_required_actions

#: The shortest edge secret or workbench key the backend accepts.
KEY_MIN_LENGTH: Final = 32
#: The SQLite file on the ``ghostwriter-data`` volume.
DB_PATH: Final = "/var/lib/ghostwriter/ghostwriter.sqlite3"

EDGE_SECRET_ENV: Final = "GHOSTWRITER_EDGE_SECRET"
DEFAULT_IDENTITY_ENV: Final = "GHOSTWRITER_DEFAULT_IDENTITY"
WORKBENCH_KEY_ENV: Final = "WORKBENCH_API_KEY"
WORKBENCH_TIMEOUT_ENV: Final = "WORKBENCH_TIMEOUT"
CHAT_DAYS_ENV: Final = "GHOSTWRITER_CHAT_RETENTION_DAYS"
SPEECH_INPUT_ENV: Final = "GHOSTWRITER_SPEECH_INPUT"
CONTEXT_TOKENS_ENV: Final = "GHOSTWRITER_CONTEXT_TOKENS"
REQUIRE_PURPOSE_ENV: Final = "AUDIT_REQUIRE_PURPOSE"

MAX_DAYS: Final = 3650
MAX_NOTICE_DAYS: Final = 365


@dataclass(frozen=True)
class Settings:
    """Everything the backend reads from its environment.

    Attributes:
        edge_secret: The secret the edge gateway sends in ``X-Edge-Secret``;
            ``None`` is the dev fallback, which trusts identity headers from any caller.
        default_identity: The dev account for requests without a trusted
            identity; always ``None`` while the edge secret is set.
        openai_api_base: The router's OpenAI-compatible base URL, without a trailing ``/``.
        openai_api_key: The router's key.
        inference_provider: The provider behind the endpoint, lower-case: ``vllm`` (the
            federation's router), ``ollama``, ``openai`` or another.
        text_model: The chat model.
        context_tokens: The chat model's context, in tokens; each round's prompt budget follows from it.
        whisper_model: The speech-to-text model.
        workbench_api_base: workbench's base URL, without a trailing ``/``.
        workbench_api_key: Ghostwriter's key at workbench; ``None`` switches
            tools and attachments off.
        workbench_timeout_s: The longest one tool call may take, in seconds.
        file_days: How long workbench keeps a chat's files, in days.
        chat_days: How long a chat is kept after it starts, in days; ``None`` keeps chats.
        notice_days: How long a new or shorter chat period waits before it deletes.
        speech_input: Whether dictation is offered.
        ui_language: The SPA's language.
        require_purpose: The audit actions that need a stated purpose.
        db_path: The SQLite file.
    """

    edge_secret: str | None = field(repr=False)
    default_identity: str | None
    openai_api_base: str
    openai_api_key: str = field(repr=False)
    inference_provider: str
    text_model: str
    context_tokens: int
    whisper_model: str
    workbench_api_base: str
    workbench_api_key: str | None = field(repr=False)
    workbench_timeout_s: float
    file_days: int
    chat_days: int | None
    notice_days: int
    speech_input: bool
    ui_language: Literal["en", "de"]
    require_purpose: frozenset[str]
    db_path: str = DB_PATH

    @property
    def workbench_enabled(self) -> bool:
        """Whether tools and attachments are on: workbench's key and base are both set."""
        return bool(self.workbench_api_key and self.workbench_api_base)


def _get(environ: Mapping[str, str], name: str) -> str | None:
    """Return a variable with surrounding whitespace removed, or ``None`` when unset or blank.

    Args:
        environ: The environment.
        name: The variable.

    Returns:
        The value, or ``None``.
    """
    value = environ.get(name, "").strip()
    return value or None


def _secret(environ: Mapping[str, str], name: str) -> str | None:
    """Return a secret, or ``None`` when unset or blank.

    Args:
        environ: The environment.
        name: The variable.

    Returns:
        The secret, or ``None``.

    Raises:
        ValueError: If the secret is shorter than :data:`KEY_MIN_LENGTH`.
    """
    value = _get(environ, name)
    if value is not None and len(value) < KEY_MIN_LENGTH:
        raise ValueError(f"{name} must be at least {KEY_MIN_LENGTH} characters long.")
    return value


def _base(environ: Mapping[str, str], name: str, default: str) -> str:
    """Return a base URL without its trailing ``/``.

    Args:
        environ: The environment.
        name: The variable.
        default: The value while it is unset.

    Returns:
        The base URL.
    """
    return (_get(environ, name) or default).rstrip("/")


def _days(environ: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    """Return a whole number of days within its range.

    Args:
        environ: The environment.
        name: The variable.
        default: The value while it is unset.
        low: The smallest value allowed.
        high: The largest value allowed.

    Returns:
        The number of days.

    Raises:
        ValueError: If the value is not a whole number from ``low`` to ``high``.
    """
    raw = _get(environ, name)
    if raw is None:
        return default
    message = f"{name} must be a whole number of days from {low} to {high}."
    try:
        days = int(raw)
    except ValueError:
        raise ValueError(message) from None
    if not low <= days <= high:
        raise ValueError(message)
    return days


def _chat_days(environ: Mapping[str, str]) -> int | None:
    """Return the chat period, or ``None`` for ``off``, which logs a warning.

    Args:
        environ: The environment.

    Returns:
        The days, or ``None``.
    """
    raw = _get(environ, CHAT_DAYS_ENV)
    if raw is not None and raw.lower() == "off":
        logger.warning(f"{CHAT_DAYS_ENV} is off: chats are kept until their owner deletes them. Development only.")
        return None
    return _days(environ, CHAT_DAYS_ENV, 90, 1, MAX_DAYS)


def _timeout(environ: Mapping[str, str]) -> float:
    """Return the workbench timeout in seconds.

    Args:
        environ: The environment.

    Returns:
        The timeout.

    Raises:
        ValueError: If it is not a positive, finite number.
    """
    raw = _get(environ, WORKBENCH_TIMEOUT_ENV)
    if raw is None:
        return 150.0
    message = f"{WORKBENCH_TIMEOUT_ENV} must be a positive number of seconds."
    try:
        seconds = float(raw)
    except ValueError:
        raise ValueError(message) from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(message)
    return seconds


def _context_tokens(environ: Mapping[str, str]) -> int:
    """Return the chat model's context in tokens.

    Args:
        environ: The environment.

    Returns:
        The context, :data:`~ghostwriter.agent.limits.CONTEXT_TOKENS` while unset.

    Raises:
        ValueError: If it is not a whole number of at least
            :data:`~ghostwriter.agent.limits.MIN_CONTEXT_TOKENS`.
    """
    raw = _get(environ, CONTEXT_TOKENS_ENV)
    if raw is None:
        return CONTEXT_TOKENS
    message = f"{CONTEXT_TOKENS_ENV} must be a whole number of tokens, at least {MIN_CONTEXT_TOKENS}."
    try:
        tokens = int(raw)
    except ValueError:
        raise ValueError(message) from None
    if tokens < MIN_CONTEXT_TOKENS:
        raise ValueError(message)
    return tokens


def _speech_input(environ: Mapping[str, str]) -> bool:
    """Return whether dictation is on.

    Args:
        environ: The environment.

    Returns:
        ``True`` for ``on`` (the default), ``False`` for ``off``.

    Raises:
        ValueError: For any other value.
    """
    raw = (_get(environ, SPEECH_INPUT_ENV) or "on").lower()
    if raw not in ("on", "off"):
        raise ValueError(f"{SPEECH_INPUT_ENV} must be on or off.")
    return raw == "on"


def _ui_language(environ: Mapping[str, str]) -> Literal["en", "de"]:
    """Return the SPA's language from ``RESPONSE_LANGUAGE``; an unknown value falls back to English.

    Args:
        environ: The environment.

    Returns:
        ``en`` or ``de``.
    """
    raw = _get(environ, "RESPONSE_LANGUAGE")
    if raw is None:
        return "en"
    language = raw.lower()
    if language == "de":
        return "de"
    if language != "en":
        logger.warning("Unknown RESPONSE_LANGUAGE; falling back to 'en'.")
    return "en"


def _identity(environ: Mapping[str, str]) -> tuple[str | None, str | None]:
    """Return the edge secret and the dev identity in effect, as translator decides them.

    Unset, the secret is the dev fallback, announced by one warning. The dev
    identity counts only without the secret; either case logs one warning,
    and neither names the account.

    Args:
        environ: The environment.

    Returns:
        The edge secret and the dev identity, each possibly ``None``.
    """
    edge_secret = _secret(environ, EDGE_SECRET_ENV)
    if edge_secret is None:
        logger.warning(
            f"{EDGE_SECRET_ENV} is not set: identity headers are trusted from any caller that can reach "
            f"the backend. Set {EDGE_SECRET_ENV} (see edge-plane docs/identity-contract.md)."
        )
    default_identity = _get(environ, DEFAULT_IDENTITY_ENV)
    if default_identity is None:
        return edge_secret, None
    if edge_secret is not None:
        logger.warning(
            f"{DEFAULT_IDENTITY_ENV} is ignored because {EDGE_SECRET_ENV} is set: "
            "requests without a trusted identity get 401."
        )
        return edge_secret, None
    logger.warning(
        f"{DEFAULT_IDENTITY_ENV} is set: requests without a trusted identity act as that account. "
        "Dev only; leave it unset in production."
    )
    return edge_secret, default_identity


def load_settings(environ: Mapping[str, str] = os.environ) -> Settings:
    """Read the settings from the environment.

    Args:
        environ: The environment; the process's by default.

    Returns:
        The settings.

    Raises:
        ValueError: For a value that cannot work, naming the variable and never its value.
    """
    edge_secret, default_identity = _identity(environ)
    return Settings(
        edge_secret=edge_secret,
        default_identity=default_identity,
        openai_api_base=_base(environ, "OPENAI_API_BASE", "http://vllm-router:4000/v1"),
        openai_api_key=_get(environ, "OPENAI_API_KEY") or "dummy",
        inference_provider=(_get(environ, "INFERENCE_PROVIDER") or "vllm").lower(),
        text_model=_get(environ, "TEXT_MODEL") or "Qwen/Qwen3.8-27B-FP8",
        context_tokens=_context_tokens(environ),
        whisper_model=_get(environ, "WHISPER_MODEL") or "openai/whisper-large-v3",
        workbench_api_base=_base(environ, "WORKBENCH_API_BASE", "http://workbench-api:8000"),
        workbench_api_key=_secret(environ, WORKBENCH_KEY_ENV),
        workbench_timeout_s=_timeout(environ),
        file_days=_days(environ, "WORKBENCH_RETENTION_DAYS", 7, 1, MAX_DAYS),
        chat_days=_chat_days(environ),
        notice_days=_days(environ, "RETENTION_NOTICE_DAYS", 30, 0, MAX_NOTICE_DAYS),
        speech_input=_speech_input(environ),
        ui_language=_ui_language(environ),
        require_purpose=parse_required_actions(_get(environ, REQUIRE_PURPOSE_ENV), known=ACTIONS - {"lifecycle"}),
    )
