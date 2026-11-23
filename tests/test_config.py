"""Settings read from the environment, and the health and metrics routes."""

from pathlib import Path

import pytest
from _pytest.logging import LogCaptureFixture
from fastapi.testclient import TestClient

from ghostwriter.config import load_settings
from ghostwriter.main import create_app
from tests.conftest import KEY, NEXTEXT_KEY, SECRET, make_settings

ENV = {"GHOSTWRITER_EDGE_SECRET": SECRET}
ROUTER_KEY = "router-key-0001"


def test_defaults_match_the_contract() -> None:
    """Unset variables take the defaults the shared contract names."""
    s = load_settings(ENV)
    assert (s.chat_days, s.notice_days, s.file_days, s.speech_input, s.workbench_timeout_s) == (90, 30, 7, True, 150.0)
    assert (s.workbench_api_base, s.openai_api_base) == ("http://workbench-api:8000", "http://vllm-router:4000/v1")
    assert (s.text_model, s.whisper_model) == ("Qwen/Qwen3.8-27B-FP8", "openai/whisper-large-v3")
    assert (s.workbench_enabled, s.require_purpose, s.db_path) == (
        False,
        frozenset(),
        "/var/lib/ghostwriter/ghostwriter.sqlite3",
    )
    assert (s.edge_secret, s.default_identity, s.ui_language) == (SECRET, None, "en")
    assert (s.vision, s.max_images, s.image_tokens) == (True, 8, None)
    assert (s.nextext_api_base, s.nextext_api_key, s.nextext_timeout_s, s.nextext_enabled) == ("", None, 900.0, False)


def test_an_unset_edge_secret_is_the_dev_fallback(loguru_caplog: LogCaptureFixture) -> None:
    """Without the secret, the dev identity counts and one warning names the secret."""
    s = load_settings({"GHOSTWRITER_DEFAULT_IDENTITY": "dev"})
    assert (s.edge_secret, s.default_identity) == (None, "dev")
    assert len([r for r in loguru_caplog.records if "GHOSTWRITER_EDGE_SECRET" in r.getMessage()]) == 1


def test_a_dev_identity_is_ignored_beside_a_secret() -> None:
    """With the secret set, the dev identity is ignored."""
    s = load_settings({**ENV, "GHOSTWRITER_DEFAULT_IDENTITY": "dev"})
    assert (s.edge_secret, s.default_identity) == (SECRET, None)


@pytest.mark.parametrize("name", ["GHOSTWRITER_EDGE_SECRET", "WORKBENCH_API_KEY"])
def test_a_short_secret_or_key_stops_startup(name: str) -> None:
    """A secret or key under 32 characters stops startup, named but never quoted."""
    short = "x" * 31
    with pytest.raises(ValueError, match=name) as raised:
        load_settings({**ENV, name: short})
    assert short not in str(raised.value)


def test_a_blank_workbench_key_switches_workbench_off() -> None:
    """A blank key counts as unset: workbench is off, and nothing stops."""
    s = load_settings({**ENV, "WORKBENCH_API_KEY": "   "})
    assert (s.workbench_api_key, s.workbench_enabled) == (None, False)


def test_a_workbench_key_switches_workbench_on() -> None:
    """A key and the base switch workbench on; a trailing slash leaves the bases."""
    s = load_settings(
        {
            **ENV,
            "WORKBENCH_API_KEY": KEY,
            "WORKBENCH_API_BASE": "http://workbench-api:8000/",
            "OPENAI_API_BASE": "http://vllm-router:4000/v1/",
        }
    )
    assert (s.workbench_api_key, s.workbench_enabled) == (KEY, True)
    assert (s.workbench_api_base, s.openai_api_base) == ("http://workbench-api:8000", "http://vllm-router:4000/v1")


def test_nextext_is_on_with_a_base_and_a_key() -> None:
    """The base and the key together switch clips on; a trailing slash leaves the base; a short key stops startup."""
    s = load_settings(
        {**ENV, "NEXTEXT_API_BASE": "http://nextext-backend:8000/api/v1/", "NEXTEXT_API_KEY": NEXTEXT_KEY}
    )
    assert (s.nextext_api_base, s.nextext_api_key, s.nextext_enabled) == (
        "http://nextext-backend:8000/api/v1",
        NEXTEXT_KEY,
        True,
    )
    assert NEXTEXT_KEY not in repr(s)
    only_base = load_settings({**ENV, "NEXTEXT_API_BASE": "http://nextext-backend:8000/api/v1"})
    assert (only_base.nextext_api_key, only_base.nextext_enabled) == (None, False)
    only_key = load_settings({**ENV, "NEXTEXT_API_KEY": NEXTEXT_KEY})
    assert (only_key.nextext_api_base, only_key.nextext_enabled) == ("", False)
    short = "x" * 31
    with pytest.raises(ValueError, match=r"NEXTEXT_API_KEY must be at least 32 characters long\.") as raised:
        load_settings({**ENV, "NEXTEXT_API_KEY": short})
    assert short not in str(raised.value)


def test_nextext_timeout_is_seconds_from_60_to_86400() -> None:
    """The clip budget is a finite number of seconds from 60 to 86400; anything else stops startup."""
    assert load_settings({**ENV, "NEXTEXT_TIMEOUT": "1800"}).nextext_timeout_s == 1800.0
    assert load_settings({**ENV, "NEXTEXT_TIMEOUT": "60"}).nextext_timeout_s == 60.0
    message = r"NEXTEXT_TIMEOUT must be a number of seconds from 60 to 86400\."
    for raw in ("59", "x", "inf", "nan", "86401"):
        with pytest.raises(ValueError, match=message):
            load_settings({**ENV, "NEXTEXT_TIMEOUT": raw})


@pytest.mark.parametrize(("raw", "days"), [("off", None), ("30", 30), (" 90 ", 90)])
def test_chat_retention_is_days_or_off(raw: str, days: int | None) -> None:
    """The chat period is a number of days, or ``off``."""
    assert load_settings({**ENV, "GHOSTWRITER_CHAT_RETENTION_DAYS": raw}).chat_days == days


def test_chat_retention_off_is_announced(loguru_caplog: LogCaptureFixture) -> None:
    """Switching chat retention off logs a warning."""
    load_settings({**ENV, "GHOSTWRITER_CHAT_RETENTION_DAYS": "off"})
    assert [r for r in loguru_caplog.records if "GHOSTWRITER_CHAT_RETENTION_DAYS" in r.getMessage()]


@pytest.mark.parametrize("raw", ["0", "-1", "soon", "3651"])
def test_chat_retention_refuses_anything_else(raw: str) -> None:
    """Anything but 1 to 3650 days or ``off`` stops startup."""
    with pytest.raises(ValueError, match="GHOSTWRITER_CHAT_RETENTION_DAYS"):
        load_settings({**ENV, "GHOSTWRITER_CHAT_RETENTION_DAYS": raw})


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("RETENTION_NOTICE_DAYS", "-1"),
        ("RETENTION_NOTICE_DAYS", "366"),
        ("WORKBENCH_RETENTION_DAYS", "0"),
        ("WORKBENCH_RETENTION_DAYS", "3651"),
        ("WORKBENCH_RETENTION_DAYS", "week"),
    ],
)
def test_the_other_periods_stay_in_range(name: str, raw: str) -> None:
    """The notice is 0 to 365 days, the file period 1 to 3650."""
    with pytest.raises(ValueError, match=name):
        load_settings({**ENV, name: raw})


def test_a_notice_of_zero_days_is_allowed() -> None:
    """A notice of 0 days applies a new period at once."""
    assert load_settings({**ENV, "RETENTION_NOTICE_DAYS": "0"}).notice_days == 0


def test_speech_input_is_on_or_off() -> None:
    """Speech input is ``on`` or ``off``, and nothing else."""
    assert load_settings({**ENV, "GHOSTWRITER_SPEECH_INPUT": "off"}).speech_input is False
    with pytest.raises(ValueError, match="GHOSTWRITER_SPEECH_INPUT"):
        load_settings({**ENV, "GHOSTWRITER_SPEECH_INPUT": "maybe"})


def test_vision_is_on_or_off() -> None:
    """Vision is ``on`` or ``off`` in any case, and nothing else."""
    assert load_settings({**ENV, "GHOSTWRITER_VISION": "off"}).vision is False
    assert load_settings({**ENV, "GHOSTWRITER_VISION": "ON"}).vision is True
    with pytest.raises(ValueError, match=r"GHOSTWRITER_VISION must be on or off\."):
        load_settings({**ENV, "GHOSTWRITER_VISION": "maybe"})


def test_max_images_is_a_whole_number_from_1_to_32() -> None:
    """The image cap is read as given from 1 to 32; anything else stops startup."""
    assert load_settings({**ENV, "GHOSTWRITER_MAX_IMAGES": "4"}).max_images == 4
    message = r"GHOSTWRITER_MAX_IMAGES must be a whole number from 1 to 32\."
    for raw in ("0", "33", "x"):
        with pytest.raises(ValueError, match=message):
            load_settings({**ENV, "GHOSTWRITER_MAX_IMAGES": raw})


def test_image_tokens_is_unset_or_a_fixed_cost() -> None:
    """Unset, the cost follows the pixels (``None``); a number from 1 to 10 000 is a fixed cost per image."""
    assert load_settings(ENV).image_tokens is None
    assert load_settings({**ENV, "GHOSTWRITER_IMAGE_CONTEXT_TOKENS": "280"}).image_tokens == 280
    message = r"GHOSTWRITER_IMAGE_CONTEXT_TOKENS must be a whole number of tokens from 1 to 10000, or unset\."
    for raw in ("0", "10001", "many"):
        with pytest.raises(ValueError, match=message):
            load_settings({**ENV, "GHOSTWRITER_IMAGE_CONTEXT_TOKENS": raw})


@pytest.mark.parametrize("raw", ["0", "-5", "nan", "inf", "soon"])
def test_the_timeout_must_be_a_positive_finite_number(raw: str) -> None:
    """The workbench timeout is a positive, finite number of seconds."""
    with pytest.raises(ValueError, match="WORKBENCH_TIMEOUT"):
        load_settings({**ENV, "WORKBENCH_TIMEOUT": raw})


def test_the_context_defaults_to_40k_and_reads_a_smaller_one() -> None:
    """The context is the router's 40k unless set; a smaller one down to 22 000 tokens is read as given."""
    assert load_settings(ENV).context_tokens == 40_000
    assert load_settings({**ENV, "GHOSTWRITER_CONTEXT_TOKENS": "24576"}).context_tokens == 24_576
    assert load_settings({**ENV, "GHOSTWRITER_CONTEXT_TOKENS": "22000"}).context_tokens == 22_000


@pytest.mark.parametrize("raw", ["21999", "8192", "-1", "40k", "nan"])
def test_a_context_too_small_for_a_reasoning_round_stops_startup(raw: str) -> None:
    """Below 22 000 tokens a reasoning round's answer leaves its prompt no room; the error never quotes the value."""
    with pytest.raises(ValueError, match="GHOSTWRITER_CONTEXT_TOKENS") as raised:
        load_settings({**ENV, "GHOSTWRITER_CONTEXT_TOKENS": raw})
    assert raw not in str(raised.value)


def test_an_unknown_required_action_stops_startup() -> None:
    """A misspelt action in AUDIT_REQUIRE_PURPOSE stops startup."""
    with pytest.raises(ValueError):
        load_settings({**ENV, "AUDIT_REQUIRE_PURPOSE": "ai_cal"})
    assert load_settings({**ENV, "AUDIT_REQUIRE_PURPOSE": "ai_call, disclose"}).require_purpose == frozenset(
        {"ai_call", "disclose"}
    )


@pytest.mark.parametrize(
    ("raw", "provider"),
    [
        (None, "vllm"),
        ("", "vllm"),
        ("   ", "vllm"),
        (" Ollama ", "ollama"),
        ("OpenAI", "openai"),
        ("lmstudio", "lmstudio"),
    ],
)
def test_the_inference_provider_defaults_to_vllm(raw: str | None, provider: str) -> None:
    """Unset or blank is the federation's vllm; any other value is kept, stripped and lower-cased."""
    env = ENV if raw is None else {**ENV, "INFERENCE_PROVIDER": raw}
    assert load_settings(env).inference_provider == provider


def test_an_unknown_ui_language_falls_back_to_english() -> None:
    """An unknown UI language falls back to English; German is kept."""
    assert load_settings({**ENV, "RESPONSE_LANGUAGE": "fr"}).ui_language == "en"
    assert load_settings({**ENV, "RESPONSE_LANGUAGE": " DE "}).ui_language == "de"


def test_secrets_stay_out_of_the_repr() -> None:
    """Neither the edge secret, the workbench key nor the router key shows in the repr."""
    s = load_settings({**ENV, "WORKBENCH_API_KEY": KEY, "OPENAI_API_KEY": ROUTER_KEY})
    assert s.openai_api_key == ROUTER_KEY
    assert all(value not in repr(s) for value in (SECRET, KEY, ROUTER_KEY))


def test_health_and_metrics_answer(tmp_path: Path) -> None:
    """The health route answers ok, and the metrics are exposed for Prometheus."""
    settings = make_settings(tmp_path)
    app = create_app(settings)
    client = TestClient(app)
    assert app.state.settings is settings
    assert client.get("/api/health").json() == {"status": "ok"}
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "http_requests_total" in metrics.text
